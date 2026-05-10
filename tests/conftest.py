import atexit
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from src import BaseDaemon, BitcoindDaemon, EmblnNode, WalletManager
from src.bin._common import (
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)

# tests/conftest.py is one level deep — parents[1] is the project root.
PROJECT = Path(__file__).resolve().parents[1]

_WALLET_PASSWORDS = {"alice": "alicepwd", "bob": "bobpwd"}

# Two MPSC channels — stream queue (colored stderr) + file queue (postmortem
# log file) — installed once for the whole test session. The file lives in
# the user's tmp dir so it's auto-cleaned and doesn't pollute the repo.
_log_file = os.path.join(tempfile.gettempdir(), "embln-tests.log")
_listeners = setup_mpsc_logging(level=resolve_log_level(), log_file=_log_file)
atexit.register(teardown_mpsc_logging, _listeners)

# Backstop for any leaked daemon (bitcoind subprocess OR embln listener):
# the BaseDaemon weakref registry tracks every live instance, so this single
# atexit hook reaps both kinds even if a test crashes hard before its
# fixture teardown runs. SO_REUSEADDR + random ports keep us mostly
# self-healing, but this catches the rest.
atexit.register(BaseDaemon.stop_all_running)


@pytest.fixture(autouse=True)
def _blank_line_before_test(request):
    """Print a blank line before each test runs so the first log record in
    the test doesn't get visually concatenated with the previous test's
    capture, then log a self-narrating header (`<file>::<test_name>`) on
    the `test` logger so a stream of mixed daemon/test records still tells
    you which test you're reading."""
    print()
    logging.getLogger("test").info("─── %s ───", request.node.nodeid)


@pytest.fixture(scope="session", autouse=True)
def _assert_rlimit_unchanged():
    """Regression guard: BitcoindDaemon's `ulimit ... && exec` must not leak back to
    the parent. The bitcoind subprocess raises its own RLIMIT_NOFILE; Python's must
    survive untouched across the full session."""
    import resource

    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    yield
    after = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert before == after, (
        f"RLIMIT_NOFILE changed during test session: {before} -> {after}"
    )


def _free_port() -> int:
    """Pick an unused ephemeral port. Tiny race window between close and bitcoind bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- bitcoind manager ----


class NodeManager:
    """Owns the bitcoind daemons spawned during one test and stops them all on teardown."""

    def __init__(self, datadir_root: str):
        self._datadir_root = datadir_root
        self._nodes: dict[str, BitcoindDaemon] = {}

    def add_and_start(self, name: str) -> BitcoindDaemon:
        if name in self._nodes:
            raise ValueError(f"node '{name}' already added")
        daemon = BitcoindDaemon(
            name=name,
            datadir=os.path.join(self._datadir_root, name),
            rpcport=_free_port(),
            port=_free_port(),
        )
        self._nodes[name] = (
            daemon  # track before start so teardown runs even on failure
        )
        daemon.start()
        return daemon

    def stop_all(self):
        for d in self._nodes.values():
            d.stop()


# ---- logging fixtures ----


@pytest.fixture
def make_logger():
    def _make(name: str) -> logging.Logger:
        return logging.getLogger(name)

    return _make


@pytest.fixture
def logger(request) -> logging.Logger:
    """Logger named after the running test (e.g. 'test_alice_opens_bob_accepts')."""
    return logging.getLogger(request.node.name)


# ---- bitcoind fixtures ----


@pytest.fixture
def node_manager(request):
    """Per-test NodeManager. Skips when EMBLN_BACKEND isn't pointing at a bitcoind."""
    if not os.getenv("EMBLN_BACKEND"):
        pytest.skip("EMBLN_BACKEND not set")
    datadir_root = os.path.join(os.environ["EMBLN_BACKEND"], "data", request.node.name)
    mgr = NodeManager(datadir_root)
    yield mgr
    mgr.stop_all()


@pytest.fixture
def bitcoind_node(node_manager) -> BitcoindDaemon:
    return node_manager.add_and_start("alice")


@pytest.fixture
def bitcoind_pair(node_manager) -> tuple[BitcoindDaemon, BitcoindDaemon]:
    a = node_manager.add_and_start("alice")
    b = node_manager.add_and_start("bob")
    return a, b


# ---- wallet / in-process EmblnNode fixtures ----


@pytest.fixture
def db_path(tmp_path) -> str:
    """Per-test SQLite file path (auto-cleaned by tmp_path)."""
    return str(tmp_path / "embln.db")


@pytest.fixture
def make_wallet(db_path):
    """Cached per-test factory: same name → same WalletManager instance."""
    cache: dict[str, WalletManager] = {}

    def _make(name: str) -> WalletManager:
        if name not in cache:
            cache[name] = WalletManager(
                alias=name, password=_WALLET_PASSWORDS[name], db_path=db_path
            )
        return cache[name]

    yield _make
    for w in cache.values():
        w.close()


@pytest.fixture
def make_ln_node(make_wallet, request):
    """Returns an in-process EmblnNode bound to the named wallet.
      - with_daemon=True: attach a backing bitcoind via node_manager (auto-stop).
      - with_server=True: start the LN listener via EmblndDaemon (auto-stop on teardown).
    Same name → same instance for the lifetime of the test."""
    cache: dict[str, EmblnNode] = {}
    started: list[EmblnNode] = []

    def _make(
        name: str, with_daemon: bool = False, with_server: bool = False
    ) -> EmblnNode:
        if name not in cache:
            bitcoind = None
            if with_daemon:
                node_manager = request.getfixturevalue("node_manager")
                bitcoind = node_manager.add_and_start(name)
            cache[name] = EmblnNode(make_wallet(name), bitcoind=bitcoind)
        node = cache[name]
        if with_server and node._emblnd is None:
            node.start()
            started.append(node)
        return node

    yield _make
    for n in started:
        n.stop()
        time.sleep(2)


# ---- subprocess CLI / daemon integration fixtures ----
#
# These exercise the real binaries (`python -m src.bin.emblnd`,
# `python -m src.bin.embln_cli`) so tests cover the full argparse → main
# → exit chain, not just the in-process API.
#
#   make_runner(module, *argv)     — generic one-shot subprocess runner.
#                                    Use directly for binaries without a
#                                    dedicated fixture, or to test failure
#                                    exits (e.g. emblnd --missing-password).
#   make_embln_cli(*argv)          — one-shot embln-cli invocation.
#                                    Returns subprocess.CompletedProcess.
#   make_emblnd(alias, ...)        — long-running emblnd spawn with full
#                                    readiness ceremony: wallet pre-create,
#                                    Popen, wait for p2p_listen=, poll for
#                                    .cookie, register cleanup. Returns an
#                                    EmblndHandle (alias, db, datadir,
#                                    p2p_port, proc).


@pytest.fixture
def make_runner():
    """Run `python -m <module> <argv...>` from the project root with
    PYTHONPATH set. Returns the CompletedProcess and asserts the return code.

    `stdin=DEVNULL` always — otherwise the child inherits pytest's stdin,
    which is a TTY when running tests in a terminal. CLI commands that
    branch on `sys.stdin.isatty()` (e.g. interactive prompts in
    `walletcreate`) would then fire and block the subprocess waiting for
    input that never comes."""

    def _run(module: str, *argv, env_overrides=None, expect_exit=0):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT)
        env.pop("EMBLN_LOGLEVEL", None)  # tests opt in explicitly
        if env_overrides:
            env.update(env_overrides)
        result = subprocess.run(
            [sys.executable, "-m", module, *argv],
            cwd=str(PROJECT),
            env=env,
            stdin=subprocess.DEVNULL,
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

    return _run


@pytest.fixture
def make_embln_cli(make_runner):
    """One-shot `python -m src.bin.embln_cli <argv...>`. Returns the
    CompletedProcess; tests parse `r.stdout` as JSON themselves.

    Use this directly when there's no daemon involved (argparse / log-level
    surface tests). For tests that talk to a spawned emblnd, use the
    `emblncli(handle, *argv)` helper which auto-injects EMBLN_DATADIR."""

    def _run(*argv, **kwargs):
        return make_runner("src.bin.embln_cli", *argv, **kwargs)

    return _run


@pytest.fixture
def emblncli(make_embln_cli):
    """Run embln-cli against a spawned emblnd handle. Auto-injects
    `EMBLN_DATADIR=<handle.datadir>` so the CLI finds the daemon's
    cookie file without needing a `--datadir` flag (which the user-facing
    CLI no longer exposes — the convention is `~/.embln/<alias>`)."""

    def _run(handle, *argv, env_overrides=None, **kwargs):
        env = {"EMBLN_DATADIR": handle.datadir}
        if env_overrides:
            env.update(env_overrides)
        return make_embln_cli(*argv, env_overrides=env, **kwargs)

    return _run


@dataclass
class EmblndHandle:
    """All the bits a CLI test needs to talk to a spawned emblnd."""

    alias: str
    db: str
    datadir: str
    p2p_port: int
    proc: subprocess.Popen


def _wait_for_listen(proc, key="p2p_listen=127.0.0.1:", timeout=10):
    """Poll the subprocess's stderr for `<key><PORT>` and return the port."""
    deadline = time.time() + timeout
    captured = []
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        captured.append(line)
        if key in line:
            port = int(line.split(key)[1].strip())
            return port, "".join(captured)
    raise TimeoutError(
        "emblnd did not log '%s' within %ss; output:\n%s"
        % (key, timeout, "".join(captured))
    )


@pytest.fixture
def make_emblnd(tmp_path):
    """Spawn a long-running emblnd subprocess with isolated `--db` and
    `--datadir` under tmp_path. The daemon starts in **uninit** state.
    By default the fixture also calls `walletcreate` over RPC so the
    returned handle is ready for wallet/peer/channel ops; pass
    `bootstrap=False` to receive an uninit handle (for tests that
    exercise the bootstrap CLI/RPC path themselves).

    The bootstrap call goes direct RPC (`EmblnRpc.from_cookie(...).
    walletcreate(...)`) — no extra subprocess for the fixture path. CLI
    tests that want to exercise `embln-cli wallet create` itself should
    spawn with `bootstrap=False` and run the binary explicitly.

    For one-shot emblnd runs (e.g. testing exit codes on bad args), use
    `make_runner("src.bin.emblnd", ...)` directly."""
    # Local import — keeps `EmblnRpc` out of pytest's collection-time
    # import graph for tests that don't need it.
    from src.lightning.rpc import EmblnRpc  # pylint: disable=import-outside-toplevel

    procs: list[subprocess.Popen] = []

    def _spawn(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        alias: str,
        password: str = "test",
        p2p_port: int = None,
        rpc_port: int = None,
        bootstrap: bool = True,
    ) -> EmblndHandle:
        db = tmp_path / ("%s.db" % alias)
        datadir = tmp_path / ("%s-datadir" % alias)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT)
        argv = [
            sys.executable,
            "-m",
            "src.bin.emblnd",
            "--db",
            str(db),
            "--datadir",
            str(datadir),
        ]
        if p2p_port is not None:
            argv += ["--p2p-port", str(p2p_port)]
        if rpc_port is not None:
            argv += ["--rpc-port", str(rpc_port)]
        # `with` would short-circuit progressive stderr reads + signal
        # teardown — open the process imperatively and clean up below.
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            argv,
            cwd=str(PROJECT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        port, _ = _wait_for_listen(proc)
        # Cookie is written after the RPC listener binds; poll briefly.
        cookie = datadir / ".cookie"
        deadline = time.time() + 5
        while time.time() < deadline and not cookie.is_file():
            time.sleep(0.05)
        assert cookie.is_file(), "cookie not written within 5s"
        if bootstrap:
            EmblnRpc.from_cookie(str(datadir)).walletcreate(alias, password)
        return EmblndHandle(
            alias=alias,
            db=str(db),
            datadir=str(datadir),
            p2p_port=port,
            proc=proc,
        )

    yield _spawn
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
