"""embln-cli connectpeer — open a persistent TCP connection to a peer
(via HANDSHAKE) and register it on both sides."""

import json


def test_connect_registers_outbound(make_emblnd, emblncli, make_logger):
    """connectpeer returns the canonical {alias, host, port} echo."""
    logger = make_logger("test.connectpeer.outbound")
    alice = make_emblnd("alice")
    bob = make_emblnd("bob")
    r = emblncli(
        alice,
        "connectpeer",
        "bob",
        "127.0.0.1",
        str(bob.p2p_port),
        "--alias",
        alice.alias,
    )
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload == {"alias": "bob", "host": "127.0.0.1", "port": bob.p2p_port}
