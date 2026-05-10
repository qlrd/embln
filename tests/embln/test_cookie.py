"""Cookie-file plumbing for emblnd, mirroring bitcoind's `<datadir>/.cookie`."""

import os
import stat

import pytest

from src.daemons.emblnd import COOKIE_FILENAME, COOKIE_USER, EmblndDaemon
from src.lightning.rpc import EmblnRpc


@pytest.fixture
def emblnd(make_ln_node, tmp_path):
    """Start an EmblndDaemon with an explicit datadir under tmp_path."""
    node = make_ln_node("alice")
    daemon = EmblndDaemon(node, datadir=str(tmp_path))
    daemon.start()
    yield daemon
    daemon.stop()


def test_cookie_written_on_start(emblnd, tmp_path, logger):
    """Cookie format: `<user>:<password>; host=<host>; port=<port>`."""
    cookie = tmp_path / COOKIE_FILENAME
    assert cookie.is_file()
    fields = [s.strip() for s in cookie.read_text().strip().split(";")]
    user, _, password = fields[0].partition(":")
    extras = dict(f.partition("=")[::2] for f in fields[1:])
    logger.debug("cookie_path=%s", cookie)
    logger.info("cookie_user=%s", user)
    logger.info("cookie_password_len=%s", len(password))
    logger.info("cookie_extras=%s", extras)
    assert user == COOKIE_USER
    assert len(password) == 64  # 32-byte hex
    assert extras["host"] == "127.0.0.1"
    assert int(extras["port"]) == emblnd.rpc_port


@pytest.mark.usefixtures("emblnd")
def test_cookie_perms_are_owner_only(tmp_path, logger):
    """0600 — only the owner can read/write, mirroring bitcoind."""
    cookie = tmp_path / COOKIE_FILENAME
    mode = stat.S_IMODE(os.stat(cookie).st_mode)
    logger.info("cookie_path=%s mode=%o", cookie, mode)
    assert mode == 0o600


def test_cookie_password_rotates_per_start(make_ln_node, tmp_path, logger):
    """Each start() generates a fresh password — the file isn't kept across
    restarts, so a leaked old cookie doesn't grant access."""
    node = make_ln_node("alice")
    daemon = EmblndDaemon(node, datadir=str(tmp_path))
    daemon.start()
    p1 = daemon.rpcpassword
    daemon.stop()
    daemon.start()
    p2 = daemon.rpcpassword
    daemon.stop()
    logger.info("password_len=%s p1==p2=%s", len(p1), p1 == p2)
    assert p1 != p2


def test_cookie_removed_on_stop(make_ln_node, tmp_path, logger):
    node = make_ln_node("alice")
    daemon = EmblndDaemon(node, datadir=str(tmp_path))
    daemon.start()
    cookie = tmp_path / COOKIE_FILENAME
    logger.info("cookie before stop exists=%s", cookie.is_file())
    assert cookie.is_file()
    daemon.stop()
    logger.info("cookie after stop exists=%s", cookie.exists())
    assert not cookie.exists()


def test_emblnrpc_from_cookie_reads_credentials(emblnd, tmp_path, logger):
    """Cookie auto-discovers host:port from the extension fields — no
    need to pass `port=` separately."""
    rpc = EmblnRpc.from_cookie(str(tmp_path))
    logger.info("rpc user=%s host=%s port=%s", rpc.user, rpc.host, rpc.port)
    assert rpc.user == COOKIE_USER
    assert rpc.password == emblnd.rpcpassword
    assert rpc.host == "127.0.0.1"
    assert rpc.port == emblnd.rpc_port


@pytest.mark.usefixtures("emblnd")
def test_emblnrpc_from_cookie_kwargs_override_extensions(tmp_path, logger):
    """Caller-supplied port wins over the cookie's port extension."""
    rpc = EmblnRpc.from_cookie(str(tmp_path), port=99999)
    logger.info("rpc.port=%s (override applied)", rpc.port)
    assert rpc.port == 99999


def test_unauthorized_request_returns_401(emblnd, logger):
    """Direct HTTP POST without Authorization gets 401."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", emblnd.rpc_port, timeout=2)
    try:
        conn.request("POST", "/", body='{"method":"getinfo"}')
        resp = conn.getresponse()
        logger.warning(
            "unauthorized status=%s www_auth=%s",
            resp.status,
            resp.getheader("WWW-Authenticate", ""),
        )
        assert resp.status == 401
        assert resp.getheader("WWW-Authenticate", "").startswith("Basic")
    finally:
        conn.close()


def test_authorized_getinfo_succeeds(emblnd, tmp_path, logger):
    rpc = EmblnRpc.from_cookie(str(tmp_path), port=emblnd.rpc_port)
    info = rpc.getinfo()
    logger.info("getinfo=%s", info)
    assert info["alias"] == "alice"
    assert info["rpc_listen"] == "127.0.0.1:%s" % emblnd.rpc_port


def test_unimplemented_method_returns_32601(emblnd, tmp_path, logger):
    """Stub methods like openchannel return -32601 with method name."""
    from src.rpc.base import RpcError

    rpc = EmblnRpc.from_cookie(str(tmp_path), port=emblnd.rpc_port)
    with pytest.raises(RpcError) as exc_info:
        rpc.openchannel("dummy_pubkey", 100_000)
    logger.warning("RpcError (expected) = %s", exc_info.value)
    # message includes the method name + 'not yet implemented'
    assert "openchannel" in str(exc_info.value).lower()
