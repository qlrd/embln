"""Integration tests for the emblnd / embln-cli binaries.

Each test spawns the binary as a subprocess (no in-process import) so it
exercises the real argparse → main → exit chain.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def _run(module: str, *argv, env_overrides=None, expect_exit=0):
    """Run `python -m <module> <argv>` from the project root with PYTHONPATH set."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT)
    env.pop("EMBLN_LOGLEVEL", None)  # tests opt in explicitly via --log-level / env
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-m", module, *argv],
        cwd=str(PROJECT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expect_exit, (
        f"unexpected exit {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return result


def _cli(*argv, **kwargs):
    return _run("src.bin.embln_cli", *argv, **kwargs)


def _emblnd(*argv, **kwargs):
    return _run("src.bin.emblnd", *argv, **kwargs)


# ---- embln-cli ----


def test_cli_wallet_create(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
    )
    assert "alias=alice" in r.stdout
    assert "fingerprint=" in r.stdout
    assert "(mnemonic is persisted to %s; never displayed)" % db in r.stdout
    assert db.exists()


def test_cli_wallet_address_returns_bech32(tmp_path):
    db = tmp_path / "embln.db"
    _cli("wallet", "create", "--alias", "alice", "--password", "test", "--db", str(db))
    r = _cli(
        "wallet", "address", "--alias", "alice", "--password", "test", "--db", str(db)
    )
    addr = r.stdout.strip()
    assert addr.startswith("bcrt1"), "expected regtest bech32, got: %r" % addr


def test_cli_wallet_info_lists_addresses(tmp_path):
    db = tmp_path / "embln.db"
    _cli("wallet", "create", "--alias", "alice", "--password", "test", "--db", str(db))
    _cli("wallet", "address", "--alias", "alice", "--password", "test", "--db", str(db))
    _cli("wallet", "address", "--alias", "alice", "--password", "test", "--db", str(db))
    r = _cli(
        "wallet", "info", "--alias", "alice", "--password", "test", "--db", str(db)
    )
    assert "addresses=2" in r.stdout
    assert r.stdout.count("addr[") == 2


def test_cli_node_info_shows_defaults(tmp_path):
    db = tmp_path / "embln.db"
    _cli("wallet", "create", "--alias", "alice", "--password", "test", "--db", str(db))
    r = _cli("node", "info", "--alias", "alice", "--password", "test", "--db", str(db))
    assert "alias=alice" in r.stdout
    assert "color_rgb=000000" in r.stdout
    assert "funding_satoshis=100000000" in r.stdout
    assert "minimum_depth=3" in r.stdout


def test_cli_missing_password_exits_nonzero(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "wallet",
        "create",
        "--alias",
        "alice",
        "--db",
        str(db),
        expect_exit=1,
    )
    assert "password required" in r.stderr


def test_cli_log_level_debug_emits_db_records(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "--log-level",
        "debug",
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
    )
    # Db DEBUG records appear on stderr (basicConfig default stream)
    assert "save_wallet alias=alice" in r.stderr


def test_cli_log_level_error_suppresses_info(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "--log-level",
        "error",
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
    )
    assert "INFO" not in r.stderr
    assert "DEBUG" not in r.stderr


def test_cli_env_loglevel_works(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
        env_overrides={"EMBLN_LOGLEVEL": "debug"},
    )
    assert "save_wallet alias=alice" in r.stderr


def test_cli_log_level_arg_overrides_env(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "--log-level",
        "error",
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
        env_overrides={"EMBLN_LOGLEVEL": "debug"},
    )
    # --log-level error wins; no DEBUG records
    assert "save_wallet" not in r.stderr


def test_cli_log_level_unknown_value_errors(tmp_path):
    db = tmp_path / "embln.db"
    r = _cli(
        "--log-level",
        "trace",  # not a valid choice
        "wallet",
        "create",
        "--alias",
        "alice",
        "--password",
        "test",
        "--db",
        str(db),
        expect_exit=2,  # argparse choice violation
    )
    assert "invalid choice" in r.stderr


# ---- emblnd ----


def _wait_for_listen(proc, timeout=10):
    """Poll the subprocess's stderr for 'listen=127.0.0.1:<PORT>' and return the port."""
    deadline = time.time() + timeout
    captured = []
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        captured.append(line)
        if "listen=127.0.0.1:" in line:
            port = int(line.split("listen=127.0.0.1:")[1].strip())
            return port, "".join(captured)
    raise TimeoutError(
        "emblnd did not log listen= within %ss; output:\n%s"
        % (timeout, "".join(captured))
    )


def test_emblnd_starts_listens_then_stops_cleanly(tmp_path):
    db = tmp_path / "embln.db"
    # pre-create the wallet so emblnd just loads it
    _cli("wallet", "create", "--alias", "alice", "--password", "test", "--db", str(db))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT)
    env["EMBLN_PASSWORD"] = "test"
    # cannot use `with` here: we need progressive stderr reads + signal-based
    # teardown, which the context manager would short-circuit.
    proc = subprocess.Popen(  # pylint: disable=consider-using-with  # pylint: disable=consider-using-with
        [sys.executable, "-m", "src.bin.emblnd", "--alias", "alice", "--db", str(db)],
        cwd=str(PROJECT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        port, _ = _wait_for_listen(proc)
        # confirm the port is actually accepting connections
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, "emblnd exited with %s" % proc.returncode


def test_emblnd_explicit_port(tmp_path):
    """`--port` makes the listener deterministic (useful for repro/testing)."""
    db = tmp_path / "embln.db"
    _cli("wallet", "create", "--alias", "alice", "--password", "test", "--db", str(db))

    # find a free port to ask for
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        wanted = s.getsockname()[1]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT)
    env["EMBLN_PASSWORD"] = "test"
    # cannot use `with` here: we need progressive stderr reads + signal-based
    # teardown, which the context manager would short-circuit.
    proc = subprocess.Popen(  # pylint: disable=consider-using-with  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-m",
            "src.bin.emblnd",
            "--alias",
            "alice",
            "--db",
            str(db),
            "--port",
            str(wanted),
        ],
        cwd=str(PROJECT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        port, _ = _wait_for_listen(proc)
        assert port == wanted
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    assert proc.returncode == 0


def test_emblnd_missing_password_exits(tmp_path):
    db = tmp_path / "embln.db"
    r = _emblnd("--alias", "alice", "--db", str(db), expect_exit=1)
    assert "password required" in r.stderr
