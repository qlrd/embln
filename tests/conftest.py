import atexit
import logging
import os
import socket
import tempfile
import time

import pytest

from src import Daemon, LightningNode, WalletManager
from src.bin._common import (
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)

_WALLET_PASSWORDS = {"alice": "alicepwd", "bob": "bobpwd"}

# Two MPSC channels — stream queue (colored stderr) + file queue (postmortem
# log file) — installed once for the whole test session. The file lives in
# the user's tmp dir so it's auto-cleaned and doesn't pollute the repo.
_log_file = os.path.join(tempfile.gettempdir(), "embln-tests.log")
_listeners = setup_mpsc_logging(level=resolve_log_level(), log_file=_log_file)
atexit.register(teardown_mpsc_logging, _listeners)

# Backstop for leaked LN listener ports: even if a test crashes hard before
# its fixture teardown, this atexit hook walks the LightningNode weak-ref
# registry and stops anything still bound. SO_REUSEADDR + random ports keep
# us mostly self-healing, but this catches the rest.
atexit.register(LightningNode.stop_all_listeners)
atexit.register(Daemon.stop_all_running)


@pytest.fixture(scope="session", autouse=True)
def _assert_rlimit_unchanged():
    """Regression guard: Daemon's `ulimit ... && exec` must not leak back to the
    parent. The bitcoind subprocess raises its own RLIMIT_NOFILE; Python's must
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


class NodeManager:
    """Owns the daemons spawned during one test and stops them all on teardown."""

    def __init__(self, datadir_root: str):
        self._datadir_root = datadir_root
        self._nodes: dict[str, Daemon] = {}

    def add_and_start(self, name: str) -> Daemon:
        if name in self._nodes:
            raise ValueError(f"node '{name}' already added")
        daemon = Daemon(
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


@pytest.fixture
def make_logger():
    def _make(name: str) -> logging.Logger:
        return logging.getLogger(name)

    return _make


@pytest.fixture
def logger(request) -> logging.Logger:
    """Logger named after the running test (e.g. 'test_alice_opens_bob_accepts')."""
    return logging.getLogger(request.node.name)


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
def bitcoind_node(node_manager) -> Daemon:
    return node_manager.add_and_start("alice")


@pytest.fixture
def bitcoind_pair(node_manager) -> tuple[Daemon, Daemon]:
    a = node_manager.add_and_start("alice")
    b = node_manager.add_and_start("bob")
    return a, b


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
    """Returns a LightningNode bound to the named wallet.
      - with_daemon=True: attach a backing bitcoind via node_manager (auto-stop).
      - with_server=True: start the LN P2P listener (auto-stop on teardown).
    Same name → same instance for the lifetime of the test."""
    cache: dict[str, LightningNode] = {}
    started: list[LightningNode] = []

    def _make(
        name: str, with_daemon: bool = False, with_server: bool = False
    ) -> LightningNode:
        if name not in cache:
            daemon = None
            if with_daemon:
                node_manager = request.getfixturevalue("node_manager")
                daemon = node_manager.add_and_start(name)
            cache[name] = LightningNode(make_wallet(name), daemon=daemon)
        node = cache[name]
        if with_server and node._server_thread is None:
            node.start()
            started.append(node)
        return node

    yield _make
    for n in started:
        n.stop()
        time.sleep(2)
