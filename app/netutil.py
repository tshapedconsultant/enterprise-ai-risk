"""IP / CIDR helpers for trusted-proxy checks. No app.config import."""

from __future__ import annotations

import ipaddress
from typing import Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted_proxy_networks(raw: str) -> tuple[Network, ...]:
    """Parse comma-separated IPs or CIDRs. Empty string → no trusted proxies."""
    networks: list[Network] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            if "/" in item:
                networks.append(ipaddress.ip_network(item, strict=False))
            else:
                addr = ipaddress.ip_address(item)
                networks.append(ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}"))
        except ValueError as exc:
            raise ValueError(f"TRUSTED_PROXIES entry is not a valid IP or CIDR: {item}") from exc
    return tuple(networks)


def ip_in_networks(ip: str, networks: Sequence[Network]) -> bool:
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)
