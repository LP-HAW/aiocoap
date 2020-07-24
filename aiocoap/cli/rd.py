# This file is part of the Python aiocoap library project.
#
# Copyright (c) 2012-2014 Maciej Wasilak <http://sixpinetrees.blogspot.com/>,
#               2013-2014 Christian Amsüss <c.amsuess@energyharvesting.at>
#
# aiocoap is free software, this file is published under the MIT license as
# described in the accompanying LICENSE file.

"""A plain CoAP resource directory according to
draft-ietf-core-resource-directory-25

Known Caveats:

    * It is very permissive. Not only is no security implemented.

    * This may and will make exotic choices about discoverable paths whereever
      it can (see StandaloneResourceDirectory documentation)

    * Split-horizon is not implemented correctly

    * Unless enforced by security (ie. not so far), endpoint and sector names
      (ep, d) are not checked for their lengths or other validity.

    * Simple registrations don't cache .well-known/core contents
"""

import sys
import logging
import asyncio
import argparse
from urllib.parse import urljoin
import itertools

import aiocoap
from aiocoap.resource import Site, Resource, ObservableResource, PathCapable, WKCResource, link_format_to_message
from aiocoap.util.cli import AsyncCLIDaemon
from aiocoap import error
from aiocoap.cli.common import add_server_arguments, server_context_from_arguments
from aiocoap.numbers import media_types_rev

from aiocoap.util.linkformat import Link, LinkFormat, parse

import link_header

def query_split(msg):
    result = {}
    for q in msg.opt.uri_query:
        if '=' not in q:
            k = q
            # matching the representation in link_header
            v = None
        else:
            k, v = q.split('=', 1)
        result.setdefault(k, []).append(v)
    return result

def pop_single_arg(query, name):
    """Out of query which is the output of query_split, pick the single value
    at the key name, raise a suitable BadRequest on error, or return None if
    nothing is there. The value is removed from the query dictionary."""

    if name not in query:
        return None
    if len(query[name]) > 1:
        raise BadRequest("Multiple values for %r" % name)
    return query.pop(name)[0]

class CommonRD:
    # "Key" here always means an (ep, d) tuple.

    entity_prefix = ("reg",)

    def __init__(self):
        super().__init__()

        self._by_key = {} # key -> Registration
        self._by_path = {} # path -> Registration

        self._updated_state_cb = []

    class Registration:
        # FIXME: split this into soft and hard grace period (where the former
        # may be 0). the node stays discoverable for the soft grace period, but
        # the registration stays alive for a (possibly much longer, at least
        # +lt) hard grace period, in which any action on the reg resource
        # reactivates it -- preventing premature reuse of the resource URI
        grace_period = 15

        @property
        def href(self):
            return '/' + '/'.join(self.path)

        def __init__(self, static_registration_parameters, path, network_remote, delete_cb, update_cb, registration_parameters):
            # note that this can not modify d and ep any more, since they are
            # already part of the key and possibly the path
            self.path = path
            self.links = LinkFormat([])

            self._delete_cb = delete_cb
            self._update_cb = update_cb

            self.registration_parameters = static_registration_parameters
            self.lt = 90000
            self.base_is_explicit = False

            self.update_params(network_remote, registration_parameters, is_initial=True)

        def update_params(self, network_remote, registration_parameters, is_initial=False):
            """Set the registration_parameters from the parsed query arguments,
            update any effects of them, and and trigger any observation
            observation updates if requried (the typical ones don't because
            their registration_parameters are {} and all it does is restart the
            lifetime counter)"""

            if any(k in ('ep', 'd') for k in registration_parameters.keys()):
                # The ep and d of initial registrations are already popped out
                raise error.BadRequest("Parameters 'd' and 'ep' can not be updated")

            # Not in use class "R" or otherwise conflict with common parameters
            if any(k in ('page', 'count', 'rt', 'href', 'anchor') for k in
                    registration_parameters.keys()):
                raise error.BadRequest("Unsuitable parameter for registration")

            if (is_initial or not self.base_is_explicit) and 'base' not in \
                    registration_parameters:
                # check early for validity to avoid side effects of requests
                # answered with 4.xx
                try:
                    network_base = network_remote.uri
                except error.AnonymousHost:
                    raise error.BadRequest("explicit base required")

            if is_initial:
                # technically might be a re-registration, but we can't catch that at this point
                actual_change = True
            else:
                actual_change = False

            # Don't act while still checking
            set_lt = None
            set_base = None

            if 'lt' in registration_parameters:
                try:
                    set_lt = int(pop_single_arg(registration_parameters, 'lt'))
                except ValueError:
                    raise error.BadRequest("lt must be numeric")

            if 'base' in registration_parameters:
                set_base = pop_single_arg(registration_parameters, 'base')

            if set_lt is not None and self.lt != set_lt:
                actual_change = True
                self.lt = set_lt
            if set_base is not None and (is_initial or self.base != set_base):
                actual_change = True
                self.base = set_base
                self.base_is_explicit = True

            if not self.base_is_explicit and (is_initial or self.base != network_base):
                self.base = network_base
                actual_change = True

            if any(v != self.registration_parameters.get(k) for (k, v) in registration_parameters.items()):
                self.registration_parameters.update(registration_parameters)
                actual_change = True

            if is_initial:
                self._set_timeout()
            else:
                self.refresh_timeout()

            if actual_change:
                self._update_cb()

        def delete(self):
            self.timeout.cancel()
            self._update_cb()
            self._delete_cb()

        def _set_timeout(self):
            delay = self.lt + self.grace_period
            # workaround for python issue20493

            async def longwait(delay, callback):
                await asyncio.sleep(delay)
                callback()
            self.timeout = asyncio.Task(longwait(delay, self.delete))

        def refresh_timeout(self):
            self.timeout.cancel()
            self._set_timeout()

        def get_host_link(self):
            attr_pairs = []
            for (k, values) in self.registration_parameters.items():
                for v in values:
                    attr_pairs.append([k, v])
            return Link(href=self.href, attr_pairs=attr_pairs, base=self.base, rt="coire.rd-ep")

        def get_based_links(self):
            """Produce a LinkFormat object that represents all statements in
            the registration, resolved to the registration's base (and thus
            suitable for serving from the lookup interface).

            This implements Limited Link Format as described in Appendix C
            of draft-ietf-core-resource-directory-25."""
            result = []
            for l in self.links.links:
                if 'anchor' in l:
                    absanchor = urljoin(self.base, l.anchor)
                    data = [(k, v) for (k, v) in l.attr_pairs if k != 'anchor'] + [['anchor', absanchor]]
                else:
                    data = l.attr_pairs + [['anchor', self.base]]
                href = urljoin(self.base, l.href)
                result.append(Link(href, data))
            return LinkFormat(result)

    async def shutdown(self):
        pass

    def register_change_callback(self, callback):
        """Ask RD to invoke the callback whenever any of the RD state
        changed"""
        # This has no unregister equivalent as it's only called by the lookup
        # resources that are expected to be live for the remainder of the
        # program, like the Registry is.
        self._updated_state_cb.append(callback)

    def _updated_state(self):
        for cb in self._updated_state_cb:
            cb()

    def _new_pathtail(self):
        for i in itertools.count(1):
            # In the spirit of making legal but unconvential choices (see
            # StandaloneResourceDirectory documentation): Whoever strips or
            # ignores trailing slashes shall have a hard time keeping
            # registrations alive.
            path = (str(i), '')
            if path not in self._by_path:
                return path

    def initialize_endpoint(self, network_remote, registration_parameters):
        # copying around for later use in static, but not checking again
        # because reading them from the original will already have screamed by
        # the time this is used
        ep_and_d = {k: v for (k, v) in registration_parameters.items() if k in ('ep', 'd')}

        ep = pop_single_arg(registration_parameters, 'ep')
        if ep is None:
            raise error.BadRequest("ep argument missing")
        d = pop_single_arg(registration_parameters, 'd')

        key = (ep, d)

        try:
            oldreg = self._by_key[key]
        except KeyError:
            path = self._new_pathtail()
        else:
            path = oldreg.path[len(self.entity_prefix):]
            oldreg.delete()

        # this was the brutal way towards idempotency (delete and re-create).
        # if any actions based on that are implemented here, they have yet to
        # decide wheter they'll treat idempotent recreations like deletions or
        # just ignore them unless something otherwise unchangeable (ep, d)
        # changes.

        def delete():
            del self._by_path[path]
            del self._by_key[key]

        reg = self.Registration(ep_and_d, self.entity_prefix + path, network_remote, delete,
                self._updated_state, registration_parameters)

        self._by_key[key] = reg
        self._by_path[path] = reg

        return reg

    def get_endpoints(self):
        return self._by_key.values()

def link_format_from_message(message):
    try:
        if message.opt.content_format == media_types_rev['application/link-format']:
            return parse(message.payload.decode('utf8'))
        elif message.opt.content_format == media_types_rev['application/link-format+json']:
            return LinkFormat.from_json_string(message.payload.decode('utf8'))
        elif message.opt.content_format == media_types_rev['application/link-format+cbor']:
            return LinkFormat.from_cbor_bytes(message.payload)
        else:
            raise error.UnsupportedMediaType()
    except (UnicodeDecodeError, link_header.ParseException):
        raise error.BadRequest()

class ThingWithCommonRD:
    def __init__(self, common_rd):
        super().__init__()
        self.common_rd = common_rd

        if isinstance(self, ObservableResource):
            self.common_rd.register_change_callback(self.updated_state)

class DirectoryResource(ThingWithCommonRD, Resource):
    ct = link_format_to_message.supported_ct
    rt = "core.rd"

    async def render_post(self, request):
        links = link_format_from_message(request)

        registration_parameters = query_split(request)

        regresource = self.common_rd.initialize_endpoint(request.remote, registration_parameters)
        regresource.links = links

        return aiocoap.Message(code=aiocoap.CREATED, location_path=regresource.path)

class RegistrationResource(Resource):
    """The resource object wrapping a registration is just a very thin and
    ephemeral object; all those methods could just as well be added to
    Registration with `s/self.reg/self/g`, making RegistrationResource(reg) =
    reg (or handleded in a single RegistrationDispatchSite), but this is kept
    here for better separation of model and interface."""

    def __init__(self, registration):
        self.reg = registration

    async def render_get(self, request):
        return link_format_from_message(request, self.reg.links)

    def _update_params(self, msg):
        query = query_split(msg)
        self.reg.update_params(msg.remote, query)

    async def render_post(self, request):
        self._update_params(request)

        if request.opt.content_format is not None or request.payload:
            raise error.BadRequest("Registration update with body not specified")

        return aiocoap.Message(code=aiocoap.CHANGED)

    async def render_put(self, request):
        # this is not mentioned in the current spec, but seems to make sense
        links = link_format_from_message(request)

        self._update_params(request)
        self.reg.links = links

        return aiocoap.Message(code=aiocoap.CHANGED)

    async def render_delete(self, request):
        self.reg.delete()

        return aiocoap.Message(code=aiocoap.DELETED)

class RegistrationDispatchSite(ThingWithCommonRD, Resource, PathCapable):
    async def render(self, request):
        try:
            entity = self.common_rd._by_path[request.opt.uri_path]
        except KeyError:
            raise error.NotFound

        entity = RegistrationResource(entity)

        return await entity.render(request.copy(uri_path=()))

def _paginate(candidates, query):
    page = pop_single_arg(query, 'page')
    count = pop_single_arg(query, 'count')

    try:
        candidates = list(candidates)
        if page is not None:
            candidates = candidates[int(page) * int(count):]
        if count is not None:
            candidates = candidates[:int(count)]
    except (KeyError, ValueError):
        raise error.BadRequest("page requires count, and both must be ints")

    return candidates

def _link_matches(link, key, condition):
    return any(k == key and condition(v) for (k, v) in link.attr_pairs)

class EndpointLookupInterface(ThingWithCommonRD, ObservableResource):
    ct = link_format_to_message.supported_ct
    rt = "core.rd-lookup-ep"

    async def render_get(self, request):
        query = query_split(request)

        candidates = self.common_rd.get_endpoints()

        for search_key, search_values in query.items():
            if search_key in ('page', 'count'):
                continue # filtered last

            for search_value in search_values:
                if search_value is not None and search_value.endswith('*'):
                    matches = lambda x, start=search_value[:-1]: x.startswith(start)
                else:
                    matches = lambda x: x == search_value

                if search_key in ('if', 'rt'):
                    matches = lambda x, original_matches=matches: any(original_matches(v) for v in x.split())

                if search_key == 'href':
                    candidates = (c for c in candidates if
                            matches(c.href) or
                            any(matches(r.href) for r in c.get_based_links().links)
                            )
                    continue

                candidates = (c for c in candidates if
                        (search_key in c.registration_parameters and any(matches(x) for x in c.registration_parameters[search_key])) or
                        any(_link_matches(r, search_key, matches) for r in c.get_based_links().links)
                        )

        candidates = _paginate(candidates, query)

        result = [c.get_host_link() for c in candidates]

        return link_format_to_message(request, LinkFormat(result))

class ResourceLookupInterface(ThingWithCommonRD, ObservableResource):
    ct = link_format_to_message.supported_ct
    rt = "core.rd-lookup-res"

    async def render_get(self, request):
        print("Serving request from", request.remote)
        if not isinstance(request.remote, aiocoap.transports.oscore.OSCOREAddress):
            # FIXME better filtering...
            import cbor2
            return aiocoap.Message(
                    code=aiocoap.UNAUTHORIZED,
                    content_format=aiocoap.numbers.media_types_rev['application/ace+cbor'],
                    payload=cbor2.dumps({
                        1: "coap://localhost/token", # AS
                        5: "rs1", # audience
                        9: "lookup", # scope
                        })
                    )
        query = query_split(request)

        eps = self.common_rd.get_endpoints()
        candidates = ((e, c) for e in eps for c in e.get_based_links().links)

        for search_key, search_values in query.items():
            if search_key in ('page', 'count'):
                continue # filtered last

            for search_value in search_values:
                if search_value is not None and search_value.endswith('*'):
                    matches = lambda x, start=search_value[:-1]: x.startswith(start)
                else:
                    matches = lambda x: x == search_value

                if search_key in ('if', 'rt'):
                    matches = lambda x, original_matches=matches: any(original_matches(v) for v in x.split())

                if search_key == 'href':
                    candidates = ((e, c) for (e, c) in candidates if
                            matches(c.href) or
                            matches(e.href) # FIXME: They SHOULD give this as relative as we do, but don't have to
                            )
                    continue

                candidates = ((e, c) for (e, c) in candidates if
                        _link_matches(c, search_key, matches) or
                        (search_key in e.registration_parameters and any(matches(x) for x in e.registration_parameters[search_key]))
                        )

        # strip endpoint
        candidates = (c for (e, c) in candidates)

        candidates = _paginate(candidates, query)

        return link_format_to_message(request, LinkFormat(candidates))

class SimpleRegistrationWKC(WKCResource):
    def __init__(self, listgenerator, common_rd):
        super().__init__(listgenerator)
        self.common_rd = common_rd

    async def render_post(self, request):
        query = query_split(request)

        if 'base' in query:
            raise error.BadRequest("base is not allowed in simple registrations")

        await self.process_request(
                key=key,
                network_remote=request.remote,
                registration_parameters=query,
            )

        return aiocoap.Message(code=aiocoap.CHANGED)

    async def process_request(self, network_remote, registration_parameters):
        base = network_remote.uri

        fetch_address = (base + '/.well-known/core')

        # not trying to catch anything here -- the errors are most likely well renderable into the final response
        response = await self.context.request(aiocoap.Message(code=aiocoap.GET, uri=fetch_address)).response_raising
        links = link_format_from_message(response)

        registration = self.common_rd.initialize_endpoint(network_remote, registration_parameters)
        registration.links = links

class StandaloneResourceDirectory(Site):
    """A site that contains all function sets of the CoAP Resource Directoru

    To prevent or show ossification of example paths in the specification, all
    function set paths are configurable and default to values that are
    different from the specification (but still recognizable)."""

    rd_path = ("resourcedirectory", "")
    ep_lookup_path = ("endpoint-lookup", "")
    res_lookup_path = ("resource-lookup", "")

    def __init__(self):
        super().__init__()

        common_rd = CommonRD()

        self._simple_wkc = SimpleRegistrationWKC(self.get_resources_as_linkheader, common_rd=common_rd)
        self.add_resource([".well-known", "core"], self._simple_wkc)

        self.add_resource(self.rd_path, DirectoryResource(common_rd=common_rd))
        self.add_resource(self.ep_lookup_path, EndpointLookupInterface(common_rd=common_rd))
        self.add_resource(self.res_lookup_path, ResourceLookupInterface(common_rd=common_rd))

        self.add_resource(common_rd.entity_prefix, RegistrationDispatchSite(common_rd=common_rd))

        self.common_rd = common_rd

    async def shutdown(self):
        await self.common_rd.shutdown()

    # the need to pass this around crudely demonstrates that the setup of sites
    # and contexts direly needs improvement, and thread locals are giggling
    # about my stubbornness
    def set_context(self, new_context):
        self._simple_wkc.context = new_context

def build_parser():
    p = argparse.ArgumentParser(description=__doc__)

    add_server_arguments(p)

    return p

class Main(AsyncCLIDaemon):
    async def start(self, args=None):
        parser = build_parser()
        options = parser.parse_args(args if args is not None else sys.argv[1:])

        self.site = StandaloneResourceDirectory()

        self.context = await server_context_from_arguments(self.site, options)
        self.site.set_context(self.context)

    async def shutdown(self):
        await self.site.shutdown()
        await self.context.shutdown()

# import logging
# logging.basicConfig(level=logging.DEBUG)
# 
sync_main = Main.sync_main

if __name__ == "__main__":
    sync_main()
