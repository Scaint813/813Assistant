from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deploy import deploy_vpn_panel as deploy


class VpnDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def unit(self):
        executable = str(deploy.CURRENT / ".venv/bin/python")
        return {
            "WorkingDirectory": str(deploy.CURRENT),
            "FragmentPath": "/etc/systemd/system/813assistant.service",
            "Type": "simple",
            "ExecStart": f"{{ path={executable} ; argv[]={executable} -m bot.main ; ignore_errors=no ; }}",
            "Environment": "PYTHONUNBUFFERED=1",
            "ActiveState": "active",
            "SubState": "running",
        }

    def test_supported_unit_is_accepted(self):
        deploy.guard_unit(self.unit())

    def test_custom_units_are_refused(self):
        for key, value in (
            ("User", "assistant"), ("Environment", "BOT_TOKEN=not-for-output"),
            ("EnvironmentFiles", "/some/environment"), ("ExecStartPre", "custom-hook"),
            ("WorkingDirectory", "/another/path"), ("Type", "forking"),
            ("ExecStart", "{ path=/bin/sh ; argv[]=/bin/sh -c something ; ignore_errors=no ; }"),
            ("ActiveState", "failed"),
        ):
            with self.subTest(key=key), self.assertRaises(deploy.DeployError):
                deploy.guard_unit({**self.unit(), key: value})

    def test_current_must_be_an_existing_directory_symlink(self):
        current = self.root / "current"
        with self.assertRaises(deploy.DeployError):
            deploy.current_target(current)
        current.symlink_to(self.root / "missing")
        with self.assertRaises(deploy.DeployError):
            deploy.current_target(current)

    def test_render_preserves_unknown_and_multiline_settings(self):
        original = (
            b'# unchanged comment\r\nBOT_TOKEN="fake#token"\r\n'
            b'UNKNOWN="line one\nVPN_PANEL_ENABLED=not-a-key\nline three"\r\n'
            b'VPN_PANEL_ENABLED=false\r\nVPN_COUNTRY_CODE=SE\r\n'
            b'VPN_CLIENT_IP_HEADER=\r\n'
        )
        rendered = deploy.render_env(original)
        self.assertIn(b'UNKNOWN="line one\nVPN_PANEL_ENABLED=not-a-key\nline three"\r\n', rendered)
        self.assertIn(b'BOT_TOKEN="fake#token"\r\n', rendered)
        self.assertIn(b"VPN_PANEL_ENABLED=true\r\n", rendered)
        self.assertIn(b"VPN_COUNTRY_CODE=FI\r\n", rendered)
        self.assertIn(b"VPN_CLIENT_IP_HEADER=\r\n", rendered)
        with patch.dict(os.environ, {}, clear=True):
            deploy.guard_env_values(original, rendered)

    def test_render_rejects_malformed_environment(self):
        with self.assertRaises(deploy.DeployError):
            deploy.render_env(b'BOT_TOKEN="unterminated\n')

    def test_duplicate_vpn_keys_are_all_updated(self):
        rendered = deploy.render_env(b"VPN_PANEL_ENABLED=false\nVPN_PANEL_ENABLED=false\n")
        self.assertEqual(rendered.count(b"VPN_PANEL_ENABLED=true\n"), 2)

    def test_unknown_interpolated_values_must_not_change(self):
        original = b"VPN_COUNTRY_CODE=SE\nOTHER_SECRET=${VPN_COUNTRY_CODE}\n"
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(deploy.DeployError):
            deploy.guard_env_values(original, deploy.render_env(original))

    def test_each_env_parse_is_isolated(self):
        with patch.dict(os.environ, {"VPN_COUNTRY_CODE": "inherited"}, clear=True):
            first = deploy.effective_env_values(b"VPN_COUNTRY_CODE=SE\n")
            os.environ.update(first)
            second = deploy.effective_env_values(b"UNKNOWN=${VPN_COUNTRY_CODE}\n")
        self.assertEqual(second["UNKNOWN"], "")

    def test_private_files_are_exclusive_and_owner_only(self):
        destination = self.root / "backup.env"
        deploy.private_write(destination, b"private-test-value")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            deploy.private_write(destination, b"replacement")
        self.assertEqual(destination.read_bytes(), b"private-test-value")

    def test_persistent_state_outside_releases_is_allowed(self):
        releases = self.root / "releases"
        releases.mkdir()
        shared = self.root / "shared"
        shared.mkdir()
        with patch.object(deploy, "RELEASES", releases):
            deploy.guard_persistent_paths(shared / ".env", shared / "assistant.db")
            for path in (releases, releases / "old" / "assistant.db"):
                with self.assertRaises(deploy.DeployError):
                    deploy.guard_persistent_paths(path)

    def test_symlinked_state_inside_releases_is_refused(self):
        releases = self.root / "releases"
        releases.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(releases)
        with patch.object(deploy, "RELEASES", releases), self.assertRaises(deploy.DeployError):
            deploy.guard_persistent_paths(alias / "assistant.db")

    def test_database_backup_includes_committed_wal_records(self):
        source = self.root / "assistant.db"
        backup = self.root / "backup.sqlite3"
        with sqlite3.connect(source) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for table in deploy.REQUIRED_TABLES:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            connection.execute("INSERT INTO user_profile VALUES (42)")
            connection.commit()
            deploy.backup_sqlite(source, backup)
        with sqlite3.connect(backup) as connection:
            self.assertEqual(connection.execute("SELECT id FROM user_profile").fetchall(), [(42,)])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            deploy.backup_sqlite(source, backup)

    def test_missing_empty_and_unrelated_database_are_refused(self):
        source = self.root / "source.db"
        destination = self.root / "backup.db"
        for state in ("missing", "empty", "unrelated"):
            with self.subTest(state=state):
                if state == "empty":
                    source.touch()
                elif state == "unrelated":
                    with sqlite3.connect(source) as connection:
                        connection.execute("CREATE TABLE other (id INTEGER)")
                with self.assertRaises(deploy.DeployError):
                    deploy.backup_sqlite(source, destination)
                self.assertFalse(destination.exists())

    def activation(self):
        previous, release = self.root / "previous", self.root / "next"
        previous.mkdir()
        release.mkdir()
        current = self.root / "current"
        current.symlink_to(previous)
        env_file = self.root / "state.env"
        env_file.write_bytes(b"original")
        metadata = env_file.stat()
        return current, previous, release, env_file, metadata

    def test_successful_activation_replaces_environment_and_link(self):
        current, previous, release, env_file, metadata = self.activation()
        restart, health = Mock(), Mock()
        deploy.activate(current, previous, release, env_file, b"original", metadata, b"updated", restart, health)
        self.assertEqual(current.resolve(), release)
        self.assertEqual(env_file.read_bytes(), b"updated")
        restart.assert_called_once()
        health.assert_called_once()

    def test_failed_health_or_interrupt_restores_environment_and_link(self):
        for failure in (RuntimeError("unhealthy"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                with patch.object(self, "root", Path(directory)):
                    current, previous, release, env_file, metadata = self.activation()
                database = Path(directory) / "database-records"
                database.write_bytes(b"old-records")

                def unhealthy(database=database, failure=failure):
                    database.write_bytes(b"new-records")
                    raise failure

                restart = Mock()
                with self.assertRaises(type(failure)):
                    deploy.activate(current, previous, release, env_file, b"original", metadata, b"updated", restart, unhealthy)
                self.assertEqual(current.resolve(), previous)
                self.assertEqual(env_file.read_bytes(), b"original")
                self.assertEqual(database.read_bytes(), b"new-records")
                self.assertEqual(restart.call_count, 2)

    def test_restart_failure_also_rolls_back(self):
        current, previous, release, env_file, metadata = self.activation()
        restart = Mock(side_effect=[RuntimeError("restart failure"), None])
        with self.assertRaises(RuntimeError):
            deploy.activate(current, previous, release, env_file, b"original", metadata, b"updated", restart, Mock())
        self.assertEqual(current.resolve(), previous)
        self.assertEqual(env_file.read_bytes(), b"original")
        self.assertEqual(restart.call_count, 2)

    def test_rollback_attempts_link_and_restart_even_if_env_restore_fails(self):
        current, previous, release, env_file, metadata = self.activation()
        replace_file = deploy.replace_file
        calls = 0

        def fail_restore(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("write failed")
            return replace_file(*args, **kwargs)

        restart = Mock()
        with patch.object(deploy, "replace_file", side_effect=fail_restore), self.assertRaisesRegex(deploy.DeployError, "rollback incomplete"):
            deploy.activate(current, previous, release, env_file, b"original", metadata, b"updated", restart, Mock(side_effect=RuntimeError))
        self.assertEqual(current.resolve(), previous)
        self.assertEqual(restart.call_count, 2)

    def test_concurrent_env_changes_stop_before_activation(self):
        current, previous, release, env_file, metadata = self.activation()
        env_file.write_bytes(b"concurrent-change")
        restart = Mock()
        with self.assertRaises(deploy.DeployError):
            deploy.activate(current, previous, release, env_file, b"original", metadata, b"updated", restart, Mock())
        self.assertEqual(current.resolve(), previous)
        self.assertEqual(env_file.read_bytes(), b"concurrent-change")
        restart.assert_not_called()

    def readiness(self, results, states=None):
        stable = {"MainPID": "20", "NRestarts": "0", "ActiveState": "active", "SubState": "running"}
        ticks = [0, *range(1, len(results) + 1), 31]
        with (
            patch.object(deploy.time, "monotonic", side_effect=ticks),
            patch.object(deploy.time, "sleep"),
            patch.object(deploy, "service_properties", side_effect=states or [stable] * (len(results) + 1)),
            patch.object(deploy, "run_worker", side_effect=[{"ok": result} for result in results]),
        ):
            deploy.watch_health(self.root, self.root / ".env", {}, "10")

    def test_readiness_requires_three_consecutive_final_successes(self):
        self.readiness([False, True, True, True])
        for results in ([True], [True, True], [True, True, False, True]):
            with self.subTest(results=results), self.assertRaises(deploy.DeployError):
                self.readiness(results)

    def test_readiness_rejects_process_restart(self):
        stable = {"MainPID": "20", "NRestarts": "0", "ActiveState": "active", "SubState": "running"}
        with self.assertRaises(deploy.DeployError):
            self.readiness([True], [stable, {**stable, "MainPID": "21", "NRestarts": "1"}])

    def test_health_payload_does_not_infer_exit_route(self):
        self.assertTrue(deploy.health_payload_valid(
            {"vpn_panel_available": True},
            {"enabled": True, "server": {"state": "running", "matches_expected": True}},
        ))
        self.assertFalse(deploy.health_payload_valid(
            {"vpn_panel_available": True},
            {"enabled": True, "server": {"state": "running", "matches_expected": False}},
        ))

    def test_local_readiness_never_follows_redirects(self):
        self.assertIsNone(deploy.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid"))
