"""embln-cli listpeers — list currently-connected peers."""

import json


def test_empty_when_no_peers(make_emblnd, emblncli, make_logger):
    logger = make_logger("test.listpeers.empty")
    alice = make_emblnd("alice")
    r = emblncli(alice, "listpeers", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload == {"peers": []}


def test_lists_connected_peer(make_emblnd, emblncli, make_logger):
    """After connectpeer, listpeers shows the peer with direction=outbound."""
    logger = make_logger("test.listpeers.one")
    alice = make_emblnd("alice")
    bob = make_emblnd("bob")
    emblncli(
        alice,
        "connectpeer",
        "bob",
        "127.0.0.1",
        str(bob.p2p_port),
        "--alias",
        alice.alias,
    )
    r = emblncli(alice, "listpeers", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    peers = payload["peers"]
    assert [p["alias"] for p in peers] == ["bob"]
    assert peers[0]["direction"] == "outbound"
    assert peers[0]["port"] == bob.p2p_port


def test_listpeers_then_disconnect_then_empty(make_emblnd, emblncli, make_logger):
    """End-to-end: connect → list (one) → disconnect → list (empty)."""
    logger = make_logger("test.listpeers.cycle")
    alice = make_emblnd("alice")
    bob = make_emblnd("bob")
    emblncli(
        alice,
        "connectpeer",
        "bob",
        "127.0.0.1",
        str(bob.p2p_port),
        "--alias",
        alice.alias,
    )
    listed = json.loads(emblncli(alice, "listpeers", "--alias", alice.alias).stdout)
    logger.info("after_connect=%s", listed)
    assert len(listed["peers"]) == 1
    emblncli(alice, "disconnectpeer", "bob", "--alias", alice.alias)
    listed = json.loads(emblncli(alice, "listpeers", "--alias", alice.alias).stdout)
    logger.info("after_disconnect=%s", listed)
    assert listed == {"peers": []}
