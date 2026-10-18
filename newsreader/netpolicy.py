"""Which addresses the app may listen on (CLAUDE.md §4): loopback, private LAN, Tailscale.

One rule, used by the web server, the settings page and (as a verified copy) launcher.py.
``ipaddress`` alone is not enough: it reports 0.0.0.0 — "every interface", including a
public one — as *private*, so binding to it would have passed the v3 check.
"""
from __future__ import annotations

import ipaddress

TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def is_allowed_bind_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return False
    if address.version == 4 and address in _THIS_NETWORK:
        return False
    return address.is_loopback or address.is_private or (address.version == 4 and address in TAILSCALE)
