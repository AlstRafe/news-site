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


def lan_address() -> str | None:
    """This PC's private LAN IPv4 address right now (the one Windows would route out of), or None."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent: this only asks the routing table
            address = ipaddress.ip_address(probe.getsockname()[0])
    except (OSError, ValueError):
        return None
    if address.is_private and not address.is_loopback and address not in TAILSCALE:
        return str(address)
    return None


def tailscale_address() -> str | None:
    """This PC's Tailscale IPv4 address right now, or None (Tailscale absent or not connected).

    Asked at every server start rather than once by the launcher: Tailscale may come up after
    the PC's logon task has already started the reader."""
    import json
    import os
    import shutil
    import socket
    import subprocess

    def ours(value: object) -> str | None:
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            return None
        return str(address) if address.version == 4 and address in TAILSCALE else None

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [shutil.which("tailscale.exe"), shutil.which("tailscale"),
                  os.path.join(program_files, "Tailscale", "tailscale.exe")]
    executable = next((item for item in candidates if item and os.path.isfile(item)), None)
    if executable:
        extra = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        try:
            output = subprocess.run([executable, "ip", "-4"], capture_output=True, text=True,
                                    timeout=4, check=True, **extra).stdout
            found = next((ours(line.strip()) for line in output.splitlines() if ours(line.strip())), None)
            if found:
                return found
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found = ours(result[4][0])
            if found:
                return found
    except OSError:
        pass
    return None
