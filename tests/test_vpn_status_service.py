from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from bot.services.vpn_status_service import VpnStatusService

MODULE = "bot.services.vpn_status_service"
EXPECTED_V4 = "89.22.228.86"
EXPECTED_V6 = "2a0b:4140:5020::2"
MAC_IP = "100.124.44.54"
MAC_IPV6 = "fd7a:115c:a1e0::1234"


def running_payload():
    return {
        "BackendState": "Running",
        "Self": {"Online": True, "ExitNodeOption": True, "HostName": "economic-peach"},
        "Peer": {
            "selected-peer": {
                "HostName": "my-mac",
                "TailscaleIPs": [MAC_IP, MAC_IPV6],
                "Online": True,
                "LastSeen": "2026-11-03T10:30:00Z",
                "ExitNode": True,
                "PublicKey": "do-not-expose-key",
            },
            "unselected-peer": {
                "HostName": "do-not-expose-other-device",
                "TailscaleIPs": ["100.65.1.2"],
                "Online": True,
            },
        },
    }


class FakeProcess:
    def __init__(self, output: bytes | None = None, returncode: int = 0):
        self.stdout = asyncio.StreamReader()
        if output is not None:
            self.stdout.feed_data(output)
            self.stdout.feed_eof()
        self.returncode = None
        self.final_returncode = returncode
        self.killed = False
        self.wait_calls = 0

    async def wait(self):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class DuplicateHeaders(dict):
    def items(self):
        return [("CF-Connecting-IP", EXPECTED_V4), ("CF-Connecting-IP", "8.8.8.8")]


class VpnConfigurationTests(unittest.TestCase):
    def test_rejects_invalid_addresses_and_unbounded_proxy_trust(self):
        invalid = (
            {"expected_ipv4": "127.0.0.1"},
            {"expected_ipv4": "192.0.2.1"},
            {"expected_ipv4": "224.0.0.1"},
            {"expected_ipv4": "0.0.0.0"},
            {"expected_ipv4": EXPECTED_V6},
            {"expected_ipv6": "fe80::1%en0"},
            {"expected_ipv6": "fd7a::1"},
            {"expected_ipv6": "ff02::1"},
            {"expected_ipv6": "::"},
            {"expected_ipv6": EXPECTED_V4},
            {"device_ips": ("not-an-ip",)},
            {"trusted_proxy_cidrs": ("10.0.0.1/8",)},
            {"trusted_proxy_cidrs": ("0.0.0.0/0",)},
            {"trusted_proxy_cidrs": ("::/0",)},
            {"client_ip_header": "User-Agent"},
            {"country_code": "Finland"},
            {"exit_node_name": "x" * 129},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                VpnStatusService(**options)


class VpnRequestObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_mode_never_spawns_but_reports_current_request(self):
        service = VpnStatusService(expected_ipv4=EXPECTED_V4, country_code="fi")
        with patch(f"{MODULE}.asyncio.create_subprocess_exec") as spawn:
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        spawn.assert_not_called()
        self.assertEqual(result["server"]["state"], "disabled")
        self.assertEqual(result["connection"]["state"], "via_vps")
        self.assertEqual(result["exit_node"]["country_code"], "FI")
        self.assertEqual(result["exit_node"]["country_source"], "configured")
        self.assertIsNone(result["exit_node"]["static_ip_verified"])

    async def test_direct_client_cannot_spoof_exit_ip_with_forwarding_headers(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            client_ip_header="CF-Connecting-IP",
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        result = await service.snapshot(
            remote_ip="8.8.8.8", headers={"CF-Connecting-IP": EXPECTED_V4}
        )
        self.assertEqual(result["connection"]["state"], "other_route")
        self.assertEqual(result["connection"]["observed_ip"], "8.8.8.8")
        self.assertEqual(result["connection"]["source"], "direct")

    async def test_private_transport_is_not_trusted_by_default(self):
        service = VpnStatusService(expected_ipv4=EXPECTED_V4)
        for remote in (
            "127.0.0.1", "10.0.0.1", "100.64.1.2", "::1", "224.0.0.1", "ff02::1",
            "0.0.0.0", "::", None, "invalid",
        ):
            with self.subTest(remote=remote):
                result = await service.snapshot(
                    remote_ip=remote, headers={"CF-Connecting-IP": EXPECTED_V4}
                )
                self.assertIsNone(result["connection"]["observed_ip"])
                self.assertEqual(result["connection"]["state"], "unknown")
                self.assertEqual(result["connection"]["unverified_families"], [4, 6])

    async def test_configured_tailnet_client_is_observed_without_verifying_public_exit(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            expected_ipv6=EXPECTED_V6,
            device_ips=(MAC_IP, MAC_IPV6),
            trusted_proxy_cidrs=("127.0.0.1/32", "::1/128"),
            client_ip_header="X-Forwarded-For",
        )
        for address, family in ((MAC_IP, 4), (MAC_IPV6, 6)):
            for source in ("direct", "proxy"):
                with self.subTest(address=address, source=source):
                    result = await service.snapshot(
                        remote_ip=address if source == "direct" else "127.0.0.1",
                        headers={"X-Forwarded-For": f"1.1.1.1, {address}, ::1"},
                    )
                    connection = result["connection"]
                    self.assertEqual(connection["state"], "via_tailnet")
                    self.assertEqual(connection["observed_ip"], address)
                    self.assertEqual(connection["family"], family)
                    self.assertEqual(connection["source"], source)
                    self.assertEqual(connection["unverified_families"], [4, 6])
                    self.assertEqual(connection["checked_at"], result["checked_at"])

    async def test_tailnet_device_matching_normalizes_ipv4_mapped_and_ipv6_addresses(self):
        service = VpnStatusService(device_ips=(MAC_IP, "fd7a:115c:a1e0:0:0:0:0:1234"))
        for remote, observed, family in (
            (f"::ffff:{MAC_IP}", MAC_IP, 4),
            ("FD7A:115C:A1E0:0:0:0:0:1234", MAC_IPV6, 6),
        ):
            with self.subTest(remote=remote):
                result = await service.snapshot(remote_ip=remote, headers={})
                self.assertEqual(result["connection"]["state"], "via_tailnet")
                self.assertEqual(result["connection"]["observed_ip"], observed)
                self.assertEqual(result["connection"]["family"], family)
                self.assertEqual(result["connection"]["unverified_families"], [4, 6])

    async def test_tailnet_observation_requires_configured_device_and_tailscale_range(self):
        configured_private = (
            "10.0.0.4",
            "192.168.1.2",
            "127.0.0.2",
            "fd7a:115c:a1e1::1234",
            "fd00::1",
        )
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            expected_ipv6=EXPECTED_V6,
            device_ips=(MAC_IP, MAC_IPV6, *configured_private),
            trusted_proxy_cidrs=("127.0.0.1/32",),
            client_ip_header="X-Forwarded-For",
        )
        for address in ("100.64.1.2", "fd7a:115c:a1e0::5678", *configured_private):
            for remote, headers in (
                (address, {}),
                ("127.0.0.1", {"X-Forwarded-For": address}),
            ):
                with self.subTest(address=address, remote=remote):
                    result = await service.snapshot(remote_ip=remote, headers=headers)
                    connection = result["connection"]
                    self.assertEqual(connection["state"], "unknown")
                    self.assertIsNone(connection["observed_ip"])
                    self.assertIsNone(connection["family"])
                    self.assertEqual(connection["source"], "unknown")
                    self.assertEqual(connection["unverified_families"], [4, 6])

    async def test_tailnet_client_cannot_be_claimed_by_spoofed_forwarding_headers(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            device_ips=(MAC_IP, MAC_IPV6),
            trusted_proxy_cidrs=("127.0.0.1/32",),
            client_ip_header="X-Forwarded-For",
        )
        for claimed in (MAC_IP, MAC_IPV6):
            for remote, headers, observed, state in (
                ("8.8.8.8", {"X-Forwarded-For": claimed}, "8.8.8.8", "other_route"),
                ("192.168.1.2", {"X-Forwarded-For": claimed}, None, "unknown"),
                (
                    "127.0.0.1",
                    {"X-Forwarded-For": f"{claimed}, 8.8.8.8"},
                    "8.8.8.8",
                    "other_route",
                ),
                (
                    "127.0.0.1",
                    {"X-Forwarded-For": f"{claimed}, 192.168.1.2"},
                    None,
                    "unknown",
                ),
                ("127.0.0.1", {"X-Real-IP": claimed}, None, "unknown"),
            ):
                with self.subTest(claimed=claimed, remote=remote, headers=headers):
                    result = await service.snapshot(remote_ip=remote, headers=headers)
                    self.assertEqual(result["connection"]["state"], state)
                    self.assertEqual(result["connection"]["observed_ip"], observed)

    async def test_tailnet_devices_in_trusted_proxy_networks_still_require_valid_headers(self):
        service = VpnStatusService(
            device_ips=(MAC_IP, MAC_IPV6),
            trusted_proxy_cidrs=("100.64.0.0/10", "fd7a:115c:a1e0::/48", "127.0.0.1/32"),
            client_ip_header="X-Forwarded-For",
        )
        for address in (MAC_IP, MAC_IPV6):
            for remote, headers in (
                (address, {}),
                ("127.0.0.1", {"X-Forwarded-For": address}),
            ):
                with self.subTest(address=address, remote=remote):
                    result = await service.snapshot(remote_ip=remote, headers=headers)
                    self.assertEqual(result["connection"]["state"], "unknown")
                    self.assertIsNone(result["connection"]["observed_ip"])

    async def test_tailnet_client_does_not_bypass_forwarding_header_limits(self):
        service = VpnStatusService(
            device_ips=(MAC_IP,),
            trusted_proxy_cidrs=("127.0.0.1/32",),
            client_ip_header="X-Forwarded-For",
        )
        for headers in (
            {"X-Forwarded-For": MAC_IP, "x-forwarded-for": MAC_IP},
            {"X-Forwarded-For": ",".join([MAC_IP] * 33)},
            {"X-Forwarded-For": " " * 2048 + MAC_IP},
            {"X-Forwarded-For": f"{MAC_IP}\n"},
            {"X-Forwarded-For": f"invalid, {MAC_IP}"},
        ):
            with self.subTest(headers=headers):
                result = await service.snapshot(remote_ip="127.0.0.1", headers=headers)
                self.assertEqual(result["connection"]["state"], "unknown")
                self.assertIsNone(result["connection"]["observed_ip"])

    async def test_single_ip_headers_can_observe_only_configured_tailnet_clients(self):
        for header in ("CF-Connecting-IP", "X-Real-IP"):
            service = VpnStatusService(
                device_ips=(MAC_IP, MAC_IPV6),
                trusted_proxy_cidrs=("127.0.0.1/32",),
                client_ip_header=header,
            )
            for address, expected_state in (
                (MAC_IP, "via_tailnet"),
                (MAC_IPV6, "via_tailnet"),
                ("100.64.1.2", "unknown"),
            ):
                with self.subTest(header=header, address=address):
                    result = await service.snapshot(
                        remote_ip="127.0.0.1", headers={header: address}
                    )
                    self.assertEqual(result["connection"]["state"], expected_state)
                    self.assertEqual(result["connection"]["unverified_families"], [4, 6])
                    if expected_state == "via_tailnet":
                        self.assertEqual(result["connection"]["observed_ip"], address)
                        self.assertEqual(result["connection"]["source"], "proxy")

    async def test_configured_public_devices_retain_public_route_classification(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            expected_ipv6=EXPECTED_V6,
            device_ips=(
                EXPECTED_V4,
                EXPECTED_V6,
                "8.8.8.8",
                "2606:4700:4700::1111",
                "100.63.255.255",
                "100.128.0.1",
            ),
            trusted_proxy_cidrs=("127.0.0.1/32",),
            client_ip_header="X-Forwarded-For",
        )
        for address, family, state in (
            (EXPECTED_V4, 4, "via_vps"),
            (EXPECTED_V6, 6, "via_vps"),
            ("8.8.8.8", 4, "other_route"),
            ("2606:4700:4700::1111", 6, "other_route"),
            ("100.63.255.255", 4, "other_route"),
            ("100.128.0.1", 4, "other_route"),
        ):
            for source in ("direct", "proxy"):
                with self.subTest(address=address, source=source):
                    result = await service.snapshot(
                        remote_ip=address if source == "direct" else "127.0.0.1",
                        headers={"X-Forwarded-For": address},
                    )
                    connection = result["connection"]
                    self.assertEqual(connection["state"], state)
                    self.assertEqual(connection["observed_ip"], address)
                    self.assertEqual(connection["family"], family)
                    self.assertEqual(connection["source"], source)
                    self.assertEqual(connection["unverified_families"], [6 if family == 4 else 4])

    async def test_single_header_requires_trusted_transport_and_one_public_ip(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            trusted_proxy_cidrs=("10.0.0.0/8",),
            client_ip_header="CF-Connecting-IP",
        )
        result = await service.snapshot(
            remote_ip="10.0.0.3", headers={"cf-connecting-ip": EXPECTED_V4}
        )
        self.assertEqual(result["connection"]["state"], "via_vps")
        self.assertEqual(result["connection"]["source"], "proxy")
        headers = (
            {},
            {"X-Real-IP": EXPECTED_V4},
            {"CF-Connecting-IP": "10.0.0.1"},
            {"CF-Connecting-IP": "224.0.0.1"},
            {"CF-Connecting-IP": "ff02::1"},
            {"CF-Connecting-IP": "0.0.0.0"},
            {"CF-Connecting-IP": f"{EXPECTED_V4}, 8.8.8.8"},
            {"CF-Connecting-IP": f"{EXPECTED_V4}\n"},
            {"CF-Connecting-IP": EXPECTED_V4, "cf-connecting-ip": EXPECTED_V4},
            DuplicateHeaders(),
        )
        for values in headers:
            with self.subTest(headers=values):
                result = await service.snapshot(remote_ip="10.0.0.3", headers=values)
                self.assertEqual(result["connection"]["state"], "unknown")
                self.assertIsNone(result["connection"]["observed_ip"])

    async def test_xff_walks_right_to_left_and_ignores_spoofed_prefix(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            trusted_proxy_cidrs=("10.0.0.0/8", "203.0.113.0/24"),
            client_ip_header="X-Forwarded-For",
        )
        result = await service.snapshot(
            remote_ip="10.0.0.3",
            headers={"X-Forwarded-For": f"1.1.1.1, {EXPECTED_V4}, 203.0.113.12"},
        )
        self.assertEqual(result["connection"]["observed_ip"], EXPECTED_V4)
        self.assertEqual(result["connection"]["state"], "via_vps")
        result = await service.snapshot(
            remote_ip="10.0.0.3",
            headers={"X-Forwarded-For": f"{EXPECTED_V4}, 8.8.8.8"},
        )
        self.assertEqual(result["connection"]["observed_ip"], "8.8.8.8")
        self.assertEqual(result["connection"]["state"], "other_route")

    async def test_xff_rejects_duplicates_malformed_and_nonpublic_clients(self):
        service = VpnStatusService(
            expected_ipv4=EXPECTED_V4,
            trusted_proxy_cidrs=("10.0.0.0/8",),
            client_ip_header="X-Forwarded-For",
        )
        for values in (
            {"X-Forwarded-For": "bad, 8.8.8.8"},
            {"X-Forwarded-For": "192.168.1.2"},
            {"X-Forwarded-For": "10.0.0.4"},
            {"X-Forwarded-For": EXPECTED_V4, "x-forwarded-for": "8.8.8.8"},
            {"X-Forwarded-For": ",".join(["8.8.8.8"] * 33)},
        ):
            with self.subTest(headers=values):
                result = await service.snapshot(remote_ip="10.0.0.3", headers=values)
                self.assertEqual(result["connection"]["state"], "unknown")

    async def test_maps_ipv4_mapped_addresses_and_only_compares_current_family(self):
        service = VpnStatusService(expected_ipv4=EXPECTED_V4, expected_ipv6=EXPECTED_V6)
        result = await service.snapshot(remote_ip=f"::ffff:{EXPECTED_V4}", headers={})
        self.assertEqual(result["connection"]["observed_ip"], EXPECTED_V4)
        self.assertEqual(result["connection"]["family"], 4)
        self.assertEqual(result["connection"]["unverified_families"], [6])
        expanded = "2a0b:4140:5020:0:0:0:0:2"
        result = await service.snapshot(remote_ip=expanded, headers={})
        self.assertEqual(result["connection"]["state"], "via_vps")
        self.assertEqual(result["connection"]["observed_ip"], EXPECTED_V6)
        self.assertEqual(result["connection"]["unverified_families"], [4])
        without_ipv6 = VpnStatusService(expected_ipv4=EXPECTED_V4)
        result = await without_ipv6.snapshot(remote_ip=EXPECTED_V6, headers={})
        self.assertEqual(result["connection"]["state"], "unknown")
        self.assertEqual(result["connection"]["unverified_families"], [4, 6])


class VpnCommandObservationTests(unittest.IsolatedAsyncioTestCase):
    async def snapshot_for(self, payload, **options):
        process = FakeProcess(json.dumps(payload).encode())
        service = VpnStatusService(enabled=True, device_ips=(MAC_IP,), **options)
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process):
            return await service.snapshot(remote_ip=EXPECTED_V4, headers={})

    async def test_command_is_fixed_and_projection_only_contains_selected_devices(self):
        process = FakeProcess(json.dumps(running_payload()).encode())
        service = VpnStatusService(enabled=True, device_ips=(MAC_IP,))
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process) as spawn:
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        self.assertEqual(spawn.call_args.args, ("tailscale", "status", "--json"))
        self.assertNotIn("shell", spawn.call_args.kwargs)
        self.assertEqual(result["server"]["state"], "running")
        self.assertTrue(result["server"]["online"])
        self.assertTrue(result["server"]["exit_node_available"])
        self.assertEqual(len(result["devices"]), 1)
        device = result["devices"][0]
        self.assertEqual(device["name"], "my-mac")
        self.assertEqual(device["tailscale_ips"], [MAC_IP])
        self.assertTrue(device["online"])
        self.assertEqual(device["last_seen"], "2026-11-03T10:30:00+00:00")
        self.assertIsNone(device["exit_node_in_use"])
        self.assertNotIn("do-not-expose", json.dumps(result))
        self.assertNotIn("fd7a:115c:a1e0::1234", json.dumps(result))

    async def test_stopped_starting_login_and_unknown_are_not_misreported_as_online(self):
        for backend, state in (
            ("Stopped", "stopped"),
            ("NoState", "starting"),
            ("Starting", "starting"),
            ("NeedsLogin", "login_required"),
            ("NeedsMachineAuth", "login_required"),
            ("Unexpected", "unknown"),
            ([], "unknown"),
        ):
            with self.subTest(backend=backend):
                payload = running_payload()
                payload["BackendState"] = backend
                result = await self.snapshot_for(payload)
                self.assertEqual(result["server"]["state"], state)
                self.assertIsNone(result["server"]["exit_node_available"])
                self.assertIsNone(result["devices"][0]["online"])
                if state in {"starting", "unknown"}:
                    self.assertIsNone(result["server"]["online"])

    async def test_missing_devices_and_schema_fields_are_unknown_not_offline(self):
        result = await self.snapshot_for({"BackendState": "Running"})
        self.assertIsNone(result["server"]["online"])
        self.assertIsNone(result["server"]["exit_node_available"])
        self.assertIsNone(result["devices"][0]["online"])
        self.assertIsNone(result["devices"][0]["last_seen"])

    async def test_wrong_local_host_cannot_be_reported_as_healthy_configured_exit_node(self):
        result = await self.snapshot_for(running_payload(), exit_node_name="other-node")
        self.assertEqual(result["server"]["state"], "different_node")
        self.assertEqual(result["server"]["name"], "economic-peach")
        self.assertFalse(result["server"]["matches_expected"])
        self.assertIsNone(result["server"]["online"])
        self.assertIsNone(result["server"]["exit_node_available"])
        self.assertIsNone(result["devices"][0]["online"])
        payload = running_payload()
        payload["Self"]["HostName"] = "x" * 128 + "-other"
        result = await self.snapshot_for(payload, exit_node_name="x" * 128)
        self.assertEqual(result["server"]["state"], "different_node")

    async def test_local_host_identity_supports_case_dns_label_and_missing_name(self):
        result = await self.snapshot_for(running_payload(), exit_node_name="ECONOMIC-PEACH.")
        self.assertTrue(result["server"]["matches_expected"])
        self.assertEqual(result["server"]["state"], "running")
        payload = running_payload()
        payload["Self"].pop("HostName")
        payload["Self"]["DNSName"] = "economic-peach.example.ts.net."
        result = await self.snapshot_for(payload, exit_node_name="economic-peach")
        self.assertTrue(result["server"]["matches_expected"])
        self.assertEqual(result["server"]["name"], "economic-peach")
        payload["Self"].pop("DNSName")
        result = await self.snapshot_for(payload, exit_node_name="economic-peach")
        self.assertIsNone(result["server"]["matches_expected"])
        self.assertIsNone(result["server"]["name"])
        self.assertEqual(result["server"]["state"], "unknown")
        self.assertIsNone(result["server"]["online"])
        self.assertIsNone(result["server"]["exit_node_available"])
        self.assertIsNone(result["devices"][0]["online"])

    async def test_configured_vps_requires_local_identity_before_promoting_peer_health(self):
        payload = running_payload()
        payload["Self"] = {"Online": True, "ExitNodeOption": True}
        result = await self.snapshot_for(payload, exit_node_name="economic-peach")
        self.assertEqual(result["server"]["state"], "unknown")
        self.assertIsNone(result["server"]["name"])
        self.assertIsNone(result["server"]["matches_expected"])
        self.assertIsNone(result["server"]["online"])
        self.assertIsNone(result["server"]["exit_node_available"])
        self.assertIsNone(result["devices"][0]["online"])
        without_target = await self.snapshot_for(payload)
        self.assertEqual(without_target["server"]["state"], "running")
        self.assertTrue(without_target["server"]["online"])
        self.assertTrue(without_target["devices"][0]["online"])
        payload.pop("Self")
        missing_self = await self.snapshot_for(payload, exit_node_name="economic-peach")
        self.assertEqual(missing_self["server"]["state"], "unknown")
        self.assertIsNone(missing_self["server"]["online"])
        self.assertIsNone(missing_self["devices"][0]["online"])

    async def test_binary_unavailable_returns_unknown_observations_without_exposing_error(self):
        service = VpnStatusService(enabled=True)
        with patch(
            f"{MODULE}.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("sensitive path that must not be exposed"),
        ):
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        self.assertEqual(result["server"]["state"], "unavailable")
        self.assertIsNone(result["server"]["online"])
        self.assertNotIn("sensitive", json.dumps(result))

    async def test_bad_and_oversized_output_is_bounded_and_not_treated_as_stopped(self):
        for output in (b"not-json", b"[]", b'{"BackendState":', b"x" * (256 * 1024 + 1)):
            with self.subTest(size=len(output)):
                process = FakeProcess(output)
                service = VpnStatusService(enabled=True)
                with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process):
                    result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
                self.assertEqual(result["server"]["state"], "unknown")
                self.assertIsNone(result["server"]["online"])
                if len(output) > 256 * 1024:
                    self.assertTrue(process.killed)
                    self.assertGreaterEqual(process.wait_calls, 1)

    async def test_timeout_kills_and_reaps_process(self):
        process = FakeProcess()
        service = VpnStatusService(enabled=True)
        with (
            patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process),
            patch(f"{MODULE}._PROBE_TIMEOUT", 0.02),
        ):
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        self.assertEqual(result["server"]["state"], "unavailable")
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)

    async def test_request_cancellation_kills_and_reaps_process(self):
        process = FakeProcess()
        service = VpnStatusService(enabled=True)
        started = asyncio.Event()

        async def spawn_process(*args, **kwargs):
            started.set()
            return process

        with patch(f"{MODULE}.asyncio.create_subprocess_exec", side_effect=spawn_process):
            request = asyncio.create_task(service.snapshot(remote_ip=EXPECTED_V4, headers={}))
            await started.wait()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertIsNone(service._cached_probe)

    async def test_probe_cache_is_shared_but_connection_observations_stay_fresh(self):
        service = VpnStatusService(enabled=True, expected_ipv4=EXPECTED_V4, device_ips=(MAC_IP,))
        process = FakeProcess(json.dumps(running_payload()).encode())
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process) as spawn:
            first, second = await asyncio.gather(
                service.snapshot(remote_ip=EXPECTED_V4, headers={}),
                service.snapshot(remote_ip="8.8.8.8", headers={}),
            )
            self.assertEqual(spawn.await_count, 1)
            self.assertEqual(first["connection"]["state"], "via_vps")
            self.assertEqual(second["connection"]["state"], "other_route")
            first["devices"][0]["name"] = "mutated response"
            third = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
            self.assertEqual(third["devices"][0]["name"], "my-mac")
        service._cached_at -= 11
        next_process = FakeProcess(json.dumps({"BackendState": "Stopped"}).encode())
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=next_process) as spawn:
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        self.assertEqual(spawn.await_count, 1)
        self.assertEqual(result["server"]["state"], "stopped")

    async def test_nonzero_empty_command_response_is_unavailable(self):
        process = FakeProcess(b"", returncode=1)
        service = VpnStatusService(enabled=True)
        with patch(f"{MODULE}.asyncio.create_subprocess_exec", return_value=process):
            result = await service.snapshot(remote_ip=EXPECTED_V4, headers={})
        self.assertEqual(result["server"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
