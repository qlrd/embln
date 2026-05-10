"""embln-cli disconnectpeer — close the persistent connection to a peer."""

import json


def test_disconnects_known_peer(make_emblnd, emblncli, make_logger):
    """After a successful connectpeer, disconnectpeer returns disconnected=True."""
    logger = make_logger("test.disconnectpeer.known")
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
    r = emblncli(alice, "disconnectpeer", "bob", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload == {"alias": "bob", "disconnected": True}


def test_unknown_peer_returns_false(make_emblnd, emblncli, make_logger):
    """disconnecting a peer that was never connected returns disconnected=False."""
    logger = make_logger("test.disconnectpeer.unknown")
    alice = make_emblnd("alice")
    r = emblncli(alice, "disconnectpeer", "nobody", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload == {"alias": "nobody", "disconnected": False}
