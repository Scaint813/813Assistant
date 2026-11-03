#!/usr/bin/env python3
"""Guarded, local-on-VPS deployment of the owner-only VPN panel.

Only the default root-owned systemd/current-symlink/SQLite layout is supported.
The outer process uses the standard library. Dependency-dependent inspection
and signed localhost requests run in the newly prepared release's virtualenv.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CURRENT = Path("/opt/bot/813Assistant-current")
RELEASES = Path("/opt/bot/813Assistant-releases")
SERVICE = "813assistant.service"
UPDATES = {
    "VPN_PANEL_ENABLED": "true",
    "VPN_EXIT_NODE_NAME": "economic-peach",
    "VPN_EXPECTED_IPV4": "89.22.228.86",
    "VPN_EXPECTED_IPV6": "2a0b:4140:5020::2",
    "VPN_DEVICE_IPS": "100.124.44.54",
    "VPN_COUNTRY_CODE": "FI",
}
SAFE_ENV = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}
HOOKS = ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecCondition")
PROPERTIES = (
    "User", "Group", "DynamicUser", "WorkingDirectory", "FragmentPath", "Type", "ExecStart",
    "Environment", "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment", *HOOKS,
    "MainPID", "NRestarts", "ActiveState", "SubState",
)
REQUIRED_TABLES = {"schema_migrations", "user_profile", "user_runtime_state", "tasks", "reminders"}


class DeployError(RuntimeError):
    """Messages must never contain environment values or captured subprocess output."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(arguments, *, cwd=None, env=None, timeout=300):
    try:
        result = subprocess.run(
            [str(value) for value in arguments], cwd=cwd, env=env,
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeployError("Required local command failed; deployment stopped.") from exc
    if result.returncode:
        raise DeployError("Required local command failed; deployment stopped.")
    return result.stdout


def service_properties():
    output = run(["systemctl", "show", SERVICE, "--no-pager", *[f"--property={key}" for key in PROPERTIES]])
    if len(output) > 128 * 1024:
        raise DeployError("Unexpected service configuration size.")
    return dict(line.split("=", 1) for line in output.decode().splitlines() if "=" in line)


def guard_unit(properties, current=CURRENT):
    expected = str(current / ".venv/bin/python")
    if (
        properties.get("User", "") not in {"", "root"}
        or properties.get("Group", "") not in {"", "root"}
        or properties.get("DynamicUser", "no") != "no"
        or properties.get("WorkingDirectory") != str(current)
        or properties.get("FragmentPath") != "/etc/systemd/system/813assistant.service"
        or properties.get("Type") != "simple"
        or any(properties.get(key, "") for key in (*HOOKS, "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment"))
    ):
        raise DeployError("Unsupported service layout; unit was not changed.")
    command = properties.get("ExecStart", "")
    match = re.match(r"^\{\s*path=(.*?)\s*;\s*argv\[\]=(.*?)\s*;\s*ignore_errors=no\s*;", command)
    if (
        command.count("{") != 1 or command.count("}") != 1 or match is None
        or match[1] != expected or shlex.split(match[2]) != [expected, "-m", "bot.main"]
    ):
        raise DeployError("ExecStart must follow the current symlink and run bot.main.")
    for entry in shlex.split(properties.get("Environment", "")):
        key, separator, _value = entry.partition("=")
        if not separator or key not in SAFE_ENV:
            raise DeployError("Application settings overridden in the unit are unsupported.")
    if properties.get("ActiveState") != "active" or properties.get("SubState") != "running":
        raise DeployError("Existing assistant is not healthy; resolve that before deploying.")


def current_target(current=CURRENT):
    if not current.is_symlink():
        raise DeployError("Current path must already be a symlink; no changes made.")
    try:
        target = current.resolve(strict=True)
    except OSError as exc:
        raise DeployError("Current symlink target is unavailable.") from exc
    if not target.is_dir():
        raise DeployError("Current target is not a directory.")
    return target


def backup_sqlite(source: Path, destination: Path):
    if not source.is_file() or source.stat().st_size == 0:
        raise DeployError("Existing non-empty SQLite database is required.")
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
            tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES.issubset(tables):
                raise DeployError("Existing database is missing required tables.")
            if original.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise DeployError("Existing database failed integrity validation.")
            # Reserve the destination without overwriting an older backup and
            # without a world-readable interval before SQLite opens it.
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with sqlite3.connect(destination) as backup:
                original.backup(backup)
        with tempfile.TemporaryDirectory(prefix="813assistant-restore-") as directory:
            with (
                sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True) as backup,
                sqlite3.connect(Path(directory) / "restored.db") as restored,
            ):
                backup.backup(restored)
                if restored.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise DeployError("Backup restore verification failed.")
                restored_tables = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES.issubset(restored_tables):
                    raise DeployError("Backup restore is incomplete.")
    except sqlite3.Error as exc:
        raise DeployError("SQLite backup failed; deployment stopped.") from exc


def private_write(path: Path, content: bytes):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def guard_persistent_paths(*paths: Path):
    releases = RELEASES.resolve()
    if any(path.resolve().is_relative_to(releases) for path in paths):
        raise DeployError("Persistent state must live outside the release directory.")


def render_env(original: bytes):
    from dotenv.parser import parse_stream

    text = original.decode("utf-8")
    result = []
    found = set()
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise DeployError("Environment syntax is unsupported; no values were changed.")
        raw = binding.original.string
        if binding.key in UPDATES:
            found.add(binding.key)
            leading = re.match(r"(?:[ \t]*\r?\n)*", raw)[0]
            ending = "\r\n" if raw.endswith("\r\n") else "\n" if raw.endswith("\n") else ""
            result.append(f"{leading}{binding.key}={UPDATES[binding.key]}{ending}")
        else:
            result.append(raw)
    rendered = "".join(result)
    missing = [key for key in UPDATES if key not in found]
    if missing:
        ending = "\r\n" if "\r\n" in text else "\n"
        if rendered and not rendered.endswith("\n"):
            rendered += ending
        rendered += "".join(f"{key}={UPDATES[key]}{ending}" for key in missing)
    return rendered.encode("utf-8")


def effective_env_values(original: bytes):
    import dotenv

    # Interpolate each candidate from the same isolated environment, never from
    # values left behind by parsing the other candidate.
    for key in list(os.environ):
        if key not in SAFE_ENV:
            del os.environ[key]
    return dotenv.dotenv_values(stream=io.StringIO(original.decode("utf-8")))


def guard_env_values(original: bytes, rendered: bytes):
    before = effective_env_values(original)
    after = effective_env_values(rendered)
    if {key: value for key, value in before.items() if key not in UPDATES} != {
        key: value for key, value in after.items() if key not in UPDATES
    }:
        raise DeployError("An unrelated environment value would change; deployment stopped.")


def load_config(original: bytes):
    import dotenv

    # python-dotenv 1.1 has no reliable PYTHON_DOTENV_DISABLED switch.
    dotenv.load_dotenv = lambda *args, **kwargs: False
    values = effective_env_values(original)
    os.environ.update({key: value for key, value in values.items() if value is not None})
    from bot.config import get_config

    return get_config()


def inspect_config(config, previous: Path):
    from sqlalchemy.engine import make_url

    url = make_url(config.database_url)
    if (
        url.drivername != "sqlite+aiosqlite" or url.query
        or url.database not in {"./assistant.db", "assistant.db"}
        or config.backup_dir not in {"./backups", "backups"}
        or not config.mini_app_enabled or config.mini_app_host != "127.0.0.1"
        or config.mini_app_port != 8782 or not config.mini_app_public_url.startswith("https://")
    ):
        raise DeployError("Only the default SQLite/backups and enabled localhost Mini App layout is supported.")
    database = (previous / "assistant.db").resolve(strict=True)
    backups = (previous / "backups").resolve(strict=True)
    guard_persistent_paths(database, backups)
    if not database.is_file() or database.stat().st_size == 0 or not backups.is_dir():
        raise DeployError("Persistent database and backup directory must already exist.")
    from bot.database.migrations import _MIGRATIONS
    from bot.database.models import Base

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        for table in Base.metadata.sorted_tables:
            escaped = table.name.replace('"', '""')
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{escaped}")')}
            if not {column.name for column in table.columns}.issubset(columns):
                raise DeployError("Live schema needs a separate migration; VPN deployment stopped.")
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        if not {f"column:{table}.{column}" for table, column, _ in _MIGRATIONS}.issubset(applied):
            raise DeployError("Live migration records are incomplete; VPN deployment stopped.")
        if not connection.execute("SELECT 1 FROM user_profile WHERE user_id=?", (config.allowed_user_id,)).fetchone():
            raise DeployError("Owner profile must already exist before deployment.")
    return {"database": str(database), "backups": str(backups)}


def health_payload_valid(profile, vpn):
    return (
        isinstance(profile, dict) and profile.get("vpn_panel_available") is True
        and isinstance(vpn, dict) and vpn.get("enabled") is True
        and isinstance(vpn.get("server"), dict)
        and vpn["server"].get("state") == "running"
        and vpn["server"].get("matches_expected") is True
    )


def check_api(config):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": config.allowed_user_id}, separators=(",", ":"))}
    secret = hmac.new(b"WebAppData", config.bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, "\n".join(f"{key}={value}" for key, value in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    authorization = "tma " + urlencode(fields)
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(path):
        request = Request(f"http://127.0.0.1:8782{path}", headers={"Authorization": authorization})
        with opener.open(request, timeout=3) as response:
            raw = response.read(256 * 1024 + 1)
            if response.status != 200 or len(raw) > 256 * 1024:
                raise DeployError("Local API readiness failed.")
            return json.loads(raw)

    if get("/healthz") != {"status": "ok"} or not health_payload_valid(get("/api/v1/profile"), get("/api/v1/vpn")):
        raise DeployError("Owner VPN API readiness failed.")


def worker(mode, args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if mode == "tests":
        import unittest

        import dotenv

        dotenv.load_dotenv = lambda *args, **kwargs: False
        suite = unittest.defaultTestLoader.discover("tests")
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        if not result.wasSuccessful():
            raise DeployError("Release unit tests failed.")
        return {"tests": result.testsRun}
    env_file = Path(args[0])
    original = env_file.read_bytes()
    if mode == "prepare":
        config = load_config(original)
        metadata = inspect_config(config, Path(args[1]))
        rendered = render_env(original)
        guard_env_values(original, rendered)
        updated_config = load_config(rendered)
        if any(
            getattr(config, field.name) != getattr(updated_config, field.name)
            for field in dataclasses.fields(config)
            if not field.name.startswith("vpn_")
        ):
            raise DeployError("A non-VPN configuration value would change; deployment stopped.")
        if inspect_config(updated_config, Path(args[1])) != metadata:
            raise DeployError("Persistent storage configuration would change.")
        import asyncio

        from bot.main import build_vpn_status_service

        observation = asyncio.run(build_vpn_status_service(updated_config).snapshot(remote_ip=None, headers={}))
        if not health_payload_valid({"vpn_panel_available": True}, observation):
            raise DeployError("Local Tailscale must identify economic-peach and be running before deployment.")
        private_write(Path(args[2]), rendered)
        return metadata
    if mode == "health":
        check_api(load_config(original))
        return {"ok": True}
    raise DeployError("Unknown deployment worker operation.")


def isolated_env(properties=None):
    environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    if properties:
        environment.update(dict(entry.split("=", 1) for entry in shlex.split(properties.get("Environment", ""))))
    return environment


def run_worker(release, mode, *args, env):
    output = run([release / ".venv/bin/python", release / "deploy/deploy_vpn_panel.py", "--worker", mode, *args], cwd=release, env=env, timeout=300 if mode == "tests" else 20)
    try:
        return json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise DeployError("Deployment worker did not complete safely.") from exc


def replace_file(path, content, mode=0o600, uid=0, gid=0):
    descriptor, temporary = tempfile.mkstemp(prefix=".vpn-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), uid, gid)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def replace_link(current, target):
    descriptor, temporary = tempfile.mkstemp(prefix=".813assistant-next-", dir=current.parent)
    os.close(descriptor)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def ignore_termination():
    previous = {value: signal.getsignal(value) for value in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    try:
        for value in previous:
            signal.signal(value, signal.SIG_IGN)
        yield
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


def activate(current, previous, release, env_file, original_env, env_metadata, staged_env, restart, health):
    if current_target(current) != previous or env_file.read_bytes() != original_env:
        raise DeployError("Current release or environment changed during preparation; deployment stopped.")
    env_changed = False
    link_changed = False
    try:
        env_changed = True
        replace_file(env_file, staged_env, uid=env_metadata.st_uid, gid=env_metadata.st_gid)
        link_changed = True
        replace_link(current, release)
        restart()
        health()
    except BaseException:
        with ignore_termination():
            # Never restore the database: it may contain new writes.
            rollback_failures = []
            if env_changed:
                try:
                    replace_file(env_file, original_env, stat.S_IMODE(env_metadata.st_mode), env_metadata.st_uid, env_metadata.st_gid)
                except BaseException:
                    rollback_failures.append("environment")
            if link_changed:
                try:
                    replace_link(current, previous)
                except BaseException:
                    rollback_failures.append("release link")
            if env_changed or link_changed:
                try:
                    restart()
                except BaseException:
                    rollback_failures.append("service restart")
            if rollback_failures:
                raise DeployError(
                    "Automatic rollback incomplete (" + ", ".join(rollback_failures)
                    + "); use the saved environment and previous release for manual recovery."
                ) from None
        raise


def watch_health(release, env_file, environment, previous_pid):
    deadline = time.monotonic() + 30
    initial = service_properties()
    pid = initial.get("MainPID")
    restarts = initial.get("NRestarts")
    if not pid or pid in {"0", previous_pid}:
        raise DeployError("Assistant did not start a new process.")
    consecutive_ready = 0
    while time.monotonic() < deadline:
        state = service_properties()
        if state.get("ActiveState") != "active" or state.get("SubState") != "running" or state.get("MainPID") != pid or state.get("NRestarts") != restarts:
            raise DeployError("Assistant did not stay healthy; rollback required.")
        try:
            api_ready = run_worker(release, "health", env_file, env=environment).get("ok") is True
        except DeployError:
            api_ready = False
        consecutive_ready = consecutive_ready + 1 if api_ready else 0
        time.sleep(1)
    if consecutive_ready < 3:
        raise DeployError("Owner VPN API failed its readiness window.")


def deploy():
    if os.geteuid() != 0:
        raise DeployError("Run this helper as root on the VPS.")
    previous = current_target()
    properties = service_properties()
    guard_unit(properties)
    env_file = (previous / ".env").resolve(strict=True)
    guard_persistent_paths(env_file)
    original_env = env_file.read_bytes()
    env_metadata = env_file.stat()
    if not original_env or len(original_env) > 1024 * 1024 or env_metadata.st_uid != 0:
        raise DeployError("A root-owned non-empty persistent .env is required.")
    print("[1/5] Existing service and persistent environment checked.", flush=True)
    repository = Path(__file__).resolve().parents[1]
    if run(["git", "status", "--porcelain"], cwd=repository).strip() or run(["git", "ls-tree", "HEAD", "--", ".env"], cwd=repository).strip():
        raise DeployError("Use a clean Git clone without a tracked .env.")
    commit = run(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DeployError("Cannot identify the release commit.")
    if RELEASES.is_symlink() or RELEASES.resolve() != RELEASES:
        raise DeployError("Release directory must not be redirected.")
    RELEASES.mkdir(mode=0o700, parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=f"{commit[:12]}-", dir=RELEASES))
    archive = run(["git", "archive", "--format=tar", commit], cwd=repository)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(release, filter="data")
    for name in ("models.py", "migrations.py"):
        if (previous / "bot/database" / name).read_bytes() != (release / "bot/database" / name).read_bytes():
            raise DeployError("Database code differs; use a separately reviewed migration deployment.")
    environment = isolated_env(properties)
    print("[2/5] Preparing an isolated release and installing dependencies (may take several minutes).", flush=True)
    run([sys.executable, "-m", "venv", release / ".venv"], env=environment)
    run([release / ".venv/bin/python", "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", release / "requirements.txt"], cwd=release, env=environment, timeout=600)
    run_worker(release, "tests", env=environment)
    print("[3/5] Release tests passed; checking configuration and local Tailscale.", flush=True)
    staged = release / ".env.staged"
    metadata = run_worker(release, "prepare", env_file, previous, staged, env=environment)
    database, backups = Path(metadata["database"]), Path(metadata["backups"])
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_sqlite(database, backups / f"pre-vpn-panel-{stamp}-{commit[:12]}.sqlite3")
    env_backup = backups / f"pre-vpn-panel-{stamp}-{commit[:12]}.env"
    private_write(env_backup, original_env)
    print(f"[4/5] Backups verified in {backups}; previous release: {previous}", flush=True)
    for name, target in ((".env", env_file), ("assistant.db", database), ("backups", backups)):
        if os.path.lexists(release / name):
            raise DeployError("Release contains persistent-state files; activation refused.")
        (release / name).symlink_to(target)
    def restart():
        run(["systemctl", "restart", SERVICE])

    print("[5/5] Switching only 813assistant and checking the owner API for 30 seconds.", flush=True)
    try:
        activate(CURRENT, previous, release, env_file, original_env, env_metadata, staged.read_bytes(), restart,
                 lambda: watch_health(release, env_file, environment, properties.get("MainPID")))
    finally:
        staged.unlink(missing_ok=True)
    print(f"VPN panel deployed. Release: {release}; backups: {backups}")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        try:
            print(json.dumps(worker(sys.argv[2], sys.argv[3:])))
        except Exception:
            print("Deployment worker refused an unsupported or unhealthy configuration.", file=sys.stderr)
            raise SystemExit(1) from None
        return
    def interrupted(_number, _frame):
        raise KeyboardInterrupt
    for value in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(value, interrupted)
    try:
        with open("/run/lock/813assistant-vpn-panel.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeployError("Another VPN panel deployment is running.") from exc
            deploy()
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        print("Deployment stopped; backups preserved; no database was restored.", file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyboardInterrupt, OSError):
        print("Deployment stopped or rolled back. Existing backups were preserved; no database was restored.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
