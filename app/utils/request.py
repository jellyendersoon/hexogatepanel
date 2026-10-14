import ipaddress
from functools import lru_cache

from fastapi import Request

from config import server_settings

# The panel normally listens on a unix socket behind a local reverse proxy
# (Caddy). Over a unix socket there is no peer address, so every request used
# to share the client IP "Unknown" and the login throttle locked out everyone
# once anyone failed ten logins. Forwarded headers are honoured only when the
# direct peer is such a trusted proxy; anyone else gets their own peer address.
_ALWAYS_TRUSTED = ("127.0.0.1", "::1")

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _trusted_networks(raw: str) -> tuple[bool, tuple[_IPNetwork, ...]]:
    """Parse forwarded_allow_ips into (trust_everyone, networks)."""
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if "*" in items:
        return True, ()
    networks = []
    for item in (*_ALWAYS_TRUSTED, *items):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue  # host names / unix literals cannot match an IP peer
    return False, tuple(networks)


def _trust_config() -> tuple[bool, tuple[_IPNetwork, ...]]:
    raw = server_settings.forwarded_allow_ips
    if not isinstance(raw, str):
        raw = ",".join(raw)
    return _trusted_networks(raw)


def _parse_ip(value: str) -> str | None:
    """Return the IP in a forwarded value ("ip", "ip:port", "[ipv6]:port"), or None."""
    value = value.strip().strip('"')
    if value.startswith("["):
        value = value[1 : value.find("]")] if "]" in value else ""
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _is_trusted(host: str | None) -> bool:
    """A peer is trusted when it is the unix socket (no host) or a configured proxy."""
    if not host:
        return True
    trust_all, networks = _trust_config()
    if trust_all:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def get_client_ip(request: Request) -> str:
    """Extract the client's IP address from the request.

    X-Forwarded-For (rightmost address that is not itself a trusted proxy) and
    then X-Real-IP are used only when the direct peer is trusted: the unix
    socket, loopback, or UVICORN_FORWARDED_ALLOW_IPS. Otherwise the peer
    address is returned, so a client cannot pick its own throttle key.
    """
    peer = request.client.host if request.client else None
    if _is_trusted(peer):
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        hops = [ip for ip in (_parse_ip(part) for part in forwarded.split(",")) if ip]
        for hop in reversed(hops):
            if not _is_trusted(hop):
                return hop
        if hops:
            # Every hop is a trusted proxy: the client is the first of them.
            return hops[0]
        real_ip = _parse_ip(request.headers.get("x-real-ip", ""))
        if real_ip:
            return real_ip
    return peer or "Unknown"
