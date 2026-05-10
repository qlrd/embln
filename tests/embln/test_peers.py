"""Persistent peer-connection tests: HANDSHAKE handoff, registry sync between
EmblnNode + EmblndDaemon, and the EmblnRpc client surface."""

import time

import pytest

from src.lightning.rpc import EmblnRpc
from src.rpc.base import RpcError


def _wait_for(predicate, timeout=2.0, interval=0.02):
    """Poll until `predicate()` is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_connect_peer_registers_on_both_sides(make_ln_node, logger):
    """alice.connect_peer(bob) → both nodes have the other as a peer (alice
    outbound, bob inbound) once the HANDSHAKE round-trips."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    # ports already logged by EmblndDaemon._log_config — demote.
    logger.debug("alice.port=%s bob.port=%s", alice.port, bob.port)

    alice.connect_peer("bob", host="127.0.0.1", port=bob.port)

    # bob's side registers asynchronously after reading the HANDSHAKE frame
    assert _wait_for(lambda: "alice" in bob.peers), (
        "bob did not register alice within timeout; peers=%s" % list(bob.peers)
    )

    logger.info(
        "alice.peers[bob]=%s",
        {k: v for k, v in alice.peers["bob"].items() if k != "sock"},
    )
    logger.info(
        "bob.peers[alice]=%s",
        {k: v for k, v in bob.peers["alice"].items() if k != "sock"},
    )
    assert alice.peers["bob"]["direction"] == "outbound"
    assert alice.peers["bob"]["port"] == bob.port
    assert bob.peers["alice"]["direction"] == "inbound"


def test_connect_peer_is_idempotent(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    e1 = alice.connect_peer("bob", host="127.0.0.1", port=bob.port)
    e2 = alice.connect_peer("bob", host="127.0.0.1", port=bob.port)
    logger.debug("e1 is e2 -> %s", e1 is e2)  # restated by the next assert
    assert e1 is e2  # second call returns the cached entry


def test_disconnect_peer_drops_both_sides(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)

    alice.connect_peer("bob", host="127.0.0.1", port=bob.port)
    assert _wait_for(lambda: "alice" in bob.peers)
    logger.info(
        "connected: alice.peers=%s bob.peers=%s", list(alice.peers), list(bob.peers)
    )

    assert alice.disconnect_peer("bob") is True
    # outbound side: gone immediately (we control the call)
    assert "bob" not in alice.peers
    # inbound side: bob's reader thread sees EOF and unregisters
    assert _wait_for(lambda: "alice" not in bob.peers)
    logger.info(
        "after disconnect: alice.peers=%s bob.peers=%s",
        list(alice.peers),
        list(bob.peers),
    )


def test_disconnect_unknown_peer_returns_false(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    result = alice.disconnect_peer("nobody")
    logger.debug("disconnect_peer(nobody)=%s", result)  # restated by the next assert
    assert result is False


def test_list_peers_snapshot(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    alice.connect_peer("bob", host="127.0.0.1", port=bob.port)

    snapshot = alice.list_peers()
    logger.info("list_peers=%s", snapshot)
    aliases = {p["alias"] for p in snapshot}
    assert aliases == {"bob"}
    # the raw socket object must NOT leak through list_peers
    assert all("sock" not in p for p in snapshot)


def test_rpc_connectpeer_listpeers_disconnectpeer(make_ln_node, logger):
    """The full lncli-shaped flow over the JSON-RPC HTTP listener."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    rpc = EmblnRpc.from_cookie(alice._emblnd.datadir, port=alice._emblnd.rpc_port)

    rpc.connectpeer("bob", "127.0.0.1", bob.port)
    assert _wait_for(lambda: "alice" in bob.peers)

    listed = rpc.listpeers()
    logger.info("listpeers=%s", listed)
    aliases = {p["alias"] for p in listed["peers"]}
    assert aliases == {"bob"}

    res = rpc.disconnectpeer("bob")
    logger.info("disconnectpeer=%s", res)
    assert res == {"alias": "bob", "disconnected": True}
    assert _wait_for(lambda: "alice" not in bob.peers)


def test_getinfo_reports_num_peers(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    rpc = EmblnRpc.from_cookie(alice._emblnd.datadir, port=alice._emblnd.rpc_port)

    info = rpc.getinfo()
    logger.info("before connect: num_peers=%s", info["num_peers"])
    assert info["num_peers"] == 0
    alice.connect_peer("bob", host="127.0.0.1", port=bob.port)
    info = rpc.getinfo()
    logger.info("after connect: num_peers=%s", info["num_peers"])
    assert info["num_peers"] == 1


def test_disconnectpeer_rpc_unknown_returns_false(make_ln_node, logger):
    alice = make_ln_node("alice", with_server=True)
    rpc = EmblnRpc.from_cookie(alice._emblnd.datadir, port=alice._emblnd.rpc_port)
    res = rpc.disconnectpeer("nobody")
    logger.debug("disconnectpeer(nobody)=%s", res)  # restated by the next assert
    assert res == {"alias": "nobody", "disconnected": False}


def test_connectpeer_rpc_bad_args_returns_error(make_ln_node, logger):
    """Server-side validation: missing port → JSON-RPC error envelope.
    Routed through __getattr__ (not the typed stub) so we can send the wrong
    arity on purpose; the dynamic dispatch raises RpcError on error envelopes."""
    alice = make_ln_node("alice", with_server=True)
    rpc = EmblnRpc.from_cookie(alice._emblnd.datadir, port=alice._emblnd.rpc_port)
    # Direct dunder call here is intentional: we want to bypass the typed
    # stub's signature so we can send the wrong arity to the server.
    bad_call = rpc.__getattr__("connectpeer")  # pylint: disable=unnecessary-dunder-call
    with pytest.raises(RpcError) as exc_info:
        bad_call("bob", "127.0.0.1")  # missing port
    logger.warning("RpcError (expected) = %s", exc_info.value)
    assert "connectpeer" in str(exc_info.value)
