from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot.config import get_config
from bot.main import build_vpn_status_service

BASE_ENV = {"BOT_TOKEN": "123:test", "BOT_ID": "123", "ALLOWED_USER_ID": "42"}


class VpnConfigurationTests(unittest.TestCase):
    def test_panel_is_opt_in_and_configuration_parses_lists(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            self.assertFalse(get_config().vpn_panel_enabled)
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "VPN_PANEL_ENABLED": "true",
                "VPN_EXPECTED_IPV4": "89.22.228.86",
                "VPN_EXPECTED_IPV6": "2a0b:4140:5020::2",
                "VPN_DEVICE_IPS": "100.124.44.54, , fd7a:115c:a1e0::1",
                "VPN_TRUSTED_PROXY_CIDRS": "127.0.0.1/32, ::1/128",
                "VPN_CLIENT_IP_HEADER": "X-Forwarded-For",
                "VPN_COUNTRY_CODE": "fi",
            },
            clear=True,
        ):
            config = get_config()
        self.assertTrue(config.vpn_panel_enabled)
        self.assertEqual(("100.124.44.54", "fd7a:115c:a1e0::1"), config.vpn_device_ips)
        self.assertEqual(("127.0.0.1/32", "::1/128"), config.vpn_trusted_proxy_cidrs)
        self.assertEqual("FI", config.vpn_country_code)
        self.assertTrue(build_vpn_status_service(config).enabled)

    def test_invalid_optional_configuration_disables_only_the_panel(self):
        for key, value in (
            ("VPN_EXPECTED_IPV4", "not-an-address"),
            ("VPN_TRUSTED_PROXY_CIDRS", "0.0.0.0/0"),
            ("VPN_CLIENT_IP_HEADER", "Authorization"),
        ):
            with self.subTest(key=key):
                with patch.dict(
                    os.environ,
                    {**BASE_ENV, "VPN_PANEL_ENABLED": "true", key: value},
                    clear=True,
                ):
                    config = get_config()
                with self.assertLogs("bot.main", level="ERROR"):
                    service = build_vpn_status_service(config)
                self.assertFalse(service.enabled)
