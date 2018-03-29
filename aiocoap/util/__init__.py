# This file is part of the Python aiocoap library project.
#
# Copyright (c) 2012-2014 Maciej Wasilak <http://sixpinetrees.blogspot.com/>,
#               2013-2014 Christian Amsüss <c.amsuess@energyharvesting.at>
#
# aiocoap is free software, this file is published under the MIT license as
# described in the accompanying LICENSE file.

"""Tools not directly related with CoAP that are needed to provide the API"""

import urllib.parse

class ExtensibleEnumMeta(type):
    """Metaclass for ExtensibleIntEnum, see there for detailed explanations"""
    def __init__(self, name, bases, dict):
        self._value2member_map_ = {}
        for k, v in dict.items():
            if k.startswith('_'):
                continue
            if callable(v):
                continue
            if isinstance(v, property):
                continue
            instance = self(v)
            instance.name = k
            setattr(self, k, instance)
        type.__init__(self, name, bases, dict)

    def __call__(self, value):
        if isinstance(value, self):
            return value
        if value not in self._value2member_map_:
            self._value2member_map_[value] = super(ExtensibleEnumMeta, self).__call__(value)
        return self._value2member_map_[value]

class ExtensibleIntEnum(int, metaclass=ExtensibleEnumMeta):
    """Similar to Python's enum.IntEnum, this type can be used for named
    numbers which are not comprehensively known, like CoAP option numbers."""

    def __add__(self, delta):
        return type(self)(int(self) + delta)

    def __repr__(self):
        return '<%s %d%s>'%(type(self).__name__, self, ' "%s"'%self.name if hasattr(self, "name") else "")

    def __str__(self):
        return self.name if hasattr(self, "name") else int.__str__(self)

def hostportjoin(host, port=None):
    """Join a host and optionally port into a hostinfo-style host:port
    string"""
    if ':' in host:
        host = '[%s]'%host

    if port is None:
        hostinfo = host
    else:
        hostinfo = "%s:%d"%(host, port)
    return hostinfo

def hostportsplit(hostport):
    """Like urllib.parse.splitport, but return port as int, and as None if not
    given. Also, it allows giving IPv6 addresses like a netloc:

    >>> hostportsplit('foo')
    ('foo', None)
    >>> hostportsplit('foo:5683')
    ('foo', 5683)
    >>> hostportsplit('[::1%eth0]:56830')
    ('::1%eth0', 56830)
    """

    pseudoparsed = urllib.parse.SplitResult(None, hostport, None, None, None)
    return pseudoparsed.hostname, pseudoparsed.port

class Sentinel:
    """Class for sentinel that can only be compared for identity. No efforts
    are taken to make these singletons; it is up to the users to always refer
    to the same instance, which is typically defined on module level."""
    def __init__(self, label):
        self._label = label

    def __repr__(self):
        return '<%s>' % self._label
