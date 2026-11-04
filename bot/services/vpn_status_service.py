"""Read-only VPN observations; never treat peer presence as client routing proof."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_MAX_STATUS_BYTES = 256 * 1024
_PROBE_TIMEOUT = 2.0
_CACHE_SECONDS = 10.0
_HEADERS = {"cf-connecting-ip", "x-real-ip", "x-forwarded-for"}
_TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
_STATES = {
    "Running": "running",
    "Stopped": "stopped",
    "NeedsLogin": "login_required",
    "NeedsMachineAuth": "login_required",
    "Starting": "starting",
    "NoState": "starting",
}


class _InvalidStatus(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _address(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or any(character in value for character in ("%", "\r", "\n", "\x00"))
    ):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except (ValueError, OverflowError):
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _configured_public_ip(value: str, version: int) -> str:
    if not value:
        return ""
    address = _address(value)
    if address is None or address.version != version or not _is_public(address):
        raise ValueError(f"VPN expected IPv{version} must be a public IP address")
    return str(address)


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not getattr(address, "is_site_local", False)
    )


def _last_seen(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.year <= 1 or timestamp.tzinfo is None:
        return None
    try:
        return timestamp.astimezone(UTC).isoformat()
    except OverflowError:
        return None


class VpnStatusService:
    """Project a bounded local Tailscale status and the current request's IP.

    Only explicit trusted proxy networks can supply a configured forwarding
    header. No shell, API credentials, root privilege, or mutating commands are
    used. CLI observations are cached; request observations are never cached.
    """

    def __init__(
        self,
        enabled: bool = False,
        exit_node_name: str = "",
        expected_ipv4: str = "",
        expected_ipv6: str = "",
        device_ips: tuple[str, ...] = (),
        trusted_proxy_cidrs: tuple[str, ...] = (),
        client_ip_header: str = "",
        country_code: str = "",
    ):
        self.enabled = bool(enabled)
        self.exit_node_name = exit_node_name.strip()
        if len(self.exit_node_name) > 128:
            raise ValueError("VPN exit node name must contain at most 128 characters")
        self.expected_ipv4 = _configured_public_ip(expected_ipv4, 4)
        self.expected_ipv6 = _configured_public_ip(expected_ipv6, 6)
        if len(device_ips) > 32:
            raise ValueError("VPN device list must contain at most 32 IPs")
        devices = [_address(value) for value in device_ips]
        if any(address is None for address in devices):
            raise ValueError("VPN device IPs must be valid IP addresses")
        self.device_ips = tuple(dict.fromkeys(str(address) for address in devices))
        if len(trusted_proxy_cidrs) > 64:
            raise ValueError("VPN proxy list must contain at most 64 networks")
        self._trusted_proxies = tuple(
            ipaddress.ip_network(value, strict=True) for value in trusted_proxy_cidrs
        )
        if any(network.prefixlen == 0 for network in self._trusted_proxies):
            raise ValueError("Trusting every address as a proxy is not permitted")
        self.client_ip_header = client_ip_header.strip().lower()
        if self.client_ip_header and self.client_ip_header not in _HEADERS:
            raise ValueError("Unsupported VPN client IP header")
        self.country_code = country_code.strip().upper()
        if self.country_code and (
            len(self.country_code) != 2
            or not self.country_code.isascii()
            or not self.country_code.isalpha()
        ):
            raise ValueError("VPN country must be a two-letter country code")
        self._probe_lock = asyncio.Lock()
        self._cached_probe: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def snapshot(
        self,
        *,
        remote_ip: str | None,
        headers: Mapping,
    ) -> dict[str, Any]:
        checked_at = _now()
        if self.enabled:
            probe = await self._cached_status()
        else:
            probe = self._empty_status("disabled", checked_at)
        connection = self._connection(remote_ip, headers, checked_at)
        return {
            "enabled": self.enabled,
            "checked_at": checked_at,
            "exit_node": {
                "name": self.exit_node_name,
                "expected_ipv4": self.expected_ipv4 or None,
                "expected_ipv6": self.expected_ipv6 or None,
                "country_code": self.country_code or None,
                "country_source": "configured" if self.country_code else "unknown",
                "static_ip_verified": None,
            },
            **copy.deepcopy(probe),
            "connection": connection,
        }

    def _trusted(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(
            address.version == network.version and address in network
            for network in self._trusted_proxies
        )

    def _tailnet_device(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return str(address) in self.device_ips and any(
            address.version == network.version and address in network
            for network in _TAILNET_NETWORKS
        )

    def _observed_ip(self, remote_ip: str | None, headers: Mapping) -> tuple[str | None, str]:
        remote = _address(remote_ip)
        if remote is None:
            return None, "unknown"
        if not self._trusted(remote):
            if _is_public(remote) or self._tailnet_device(remote):
                return str(remote), "direct"
            return None, "unknown"
        if not self.client_ip_header:
            return None, "unknown"
        values = [
            value
            for key, value in headers.items()
            if isinstance(key, str) and key.lower() == self.client_ip_header
        ]
        if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 2048:
            return None, "unknown"
        raw = values[0]
        if self.client_ip_header == "x-forwarded-for":
            pieces = raw.split(",")
            if len(pieces) > 32:
                return None, "unknown"
            addresses = [_address(piece) for piece in pieces]
            if any(address is None for address in addresses):
                return None, "unknown"
            candidate = remote
            for address in reversed(addresses):
                if not self._trusted(candidate):
                    break
                candidate = address
            if self._trusted(candidate):
                return None, "unknown"
        else:
            candidate = _address(raw)
        if candidate is None or not (_is_public(candidate) or self._tailnet_device(candidate)):
            return None, "unknown"
        return str(candidate), "proxy"

    def _connection(self, remote_ip: str | None, headers: Mapping, checked_at: str) -> dict:
        observed, source = self._observed_ip(remote_ip, headers)
        address = _address(observed)
        family = address.version if address else None
        expected = self.expected_ipv4 if family == 4 else self.expected_ipv6
        state = "unknown"
        if address is not None and self._tailnet_device(address):
            state = "via_tailnet"
        elif observed is not None and expected:
            state = "via_vps" if observed == expected else "other_route"
        return {
            "state": state,
            "observed_ip": observed,
            "family": family,
            "source": source,
            "checked_at": checked_at,
            "unverified_families": (
                [4, 6]
                if state in {"unknown", "via_tailnet"}
                else [value for value in (4, 6) if value != family]
            ),
        }

    def _empty_devices(self) -> list[dict]:
        return [
            {
                "name": value,
                "tailscale_ips": [value],
                "online": None,
                "last_seen": None,
                "exit_node_in_use": None,
            }
            for value in self.device_ips
        ]

    def _empty_status(self, state: str, checked_at: str) -> dict:
        return {
            "server": {
                "state": state,
                "name": None,
                "matches_expected": None,
                "online": False if state in {"stopped", "login_required"} else None,
                "exit_node_available": None,
                "checked_at": checked_at,
            },
            "devices": self._empty_devices(),
        }

    async def _cached_status(self) -> dict:
        async with self._probe_lock:
            if self._cached_probe is not None and time.monotonic() - self._cached_at < _CACHE_SECONDS:
                return self._cached_probe
            checked_at = _now()
            try:
                payload = await asyncio.wait_for(self._command_status(), timeout=_PROBE_TIMEOUT)
            except _InvalidStatus:
                status = self._empty_status("unknown", checked_at)
            except (TimeoutError, OSError):
                status = self._empty_status("unavailable", checked_at)
            else:
                status = self._project_status(payload, checked_at)
            self._cached_probe = status
            self._cached_at = time.monotonic()
            return status

    async def _command_status(self) -> dict:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "tailscale",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output = bytearray()
            while True:
                chunk = await process.stdout.read(min(16_384, _MAX_STATUS_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _MAX_STATUS_BYTES:
                    raise _InvalidStatus("status output exceeds the permitted size")
            await process.wait()
            if process.returncode and not output:
                raise OSError("Tailscale status unavailable")
            try:
                payload = json.loads(output)
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise _InvalidStatus("invalid status response") from exc
            if not isinstance(payload, dict):
                raise _InvalidStatus("status response is not an object")
            return payload
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                reaper = asyncio.create_task(process.wait())
                cancelled = False
                while not reaper.done():
                    try:
                        await asyncio.shield(reaper)
                    except asyncio.CancelledError:
                        cancelled = True
                await reaper
                if cancelled:
                    raise asyncio.CancelledError

    def _project_status(self, payload: dict, checked_at: str) -> dict:
        backend_state = payload.get("BackendState")
        state = _STATES.get(backend_state, "unknown") if isinstance(backend_state, str) else "unknown"
        result = self._empty_status(state, checked_at)
        self_node = payload.get("Self")
        if isinstance(self_node, dict):
            hostname = self_node.get("HostName")
            hostname = hostname.strip() if isinstance(hostname, str) and len(hostname) <= 253 else ""
            dns_name = self_node.get("DNSName")
            dns_name = (
                dns_name.strip().rstrip(".")
                if isinstance(dns_name, str) and len(dns_name) <= 254
                else ""
            )
            names = {value.rstrip(".").casefold() for value in (hostname, dns_name) if value}
            if dns_name:
                names.add(dns_name.split(".")[0].casefold())
            result["server"]["name"] = (hostname or dns_name.split(".")[0])[:128] or None
            if self.exit_node_name and not names:
                result["server"]["state"] = "unknown"
                result["server"]["online"] = None
                return result
            if self.exit_node_name and names:
                matches = self.exit_node_name.rstrip(".").casefold() in names
                result["server"]["matches_expected"] = matches
                if not matches:
                    result["server"]["state"] = "different_node"
                    result["server"]["online"] = None
                    return result
        elif self.exit_node_name:
            result["server"]["state"] = "unknown"
            result["server"]["online"] = None
            return result
        if state != "running":
            return result
        if isinstance(self_node, dict):
            for source, destination in (("Online", "online"), ("ExitNodeOption", "exit_node_available")):
                value = self_node.get(source)
                result["server"][destination] = value if isinstance(value, bool) else None
        peers = payload.get("Peer")
        if not isinstance(peers, dict):
            return result
        selected = set(self.device_ips)
        matched: set[str] = set()
        devices = []
        for peer in peers.values():
            if not isinstance(peer, dict) or not isinstance(peer.get("TailscaleIPs"), list):
                continue
            addresses = {
                str(address)
                for value in peer["TailscaleIPs"]
                if (address := _address(value)) is not None
            }
            addresses &= selected - matched
            if not addresses:
                continue
            matched |= addresses
            name = peer.get("HostName")
            name = name.strip()[:128] if isinstance(name, str) else ""
            online = peer.get("Online")
            devices.append(
                {
                    "name": name or next(value for value in self.device_ips if value in addresses),
                    "tailscale_ips": [value for value in self.device_ips if value in addresses],
                    "online": online if isinstance(online, bool) else None,
                    "last_seen": _last_seen(peer.get("LastSeen")),
                    "exit_node_in_use": None,
                }
            )
        devices.extend(value for value in result["devices"] if value["tailscale_ips"][0] not in matched)
        result["devices"] = devices
        return result
