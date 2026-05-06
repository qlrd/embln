"""LN P2P listener / connection tests.

Pure in-process: no bitcoind, no noise transport. The transport here is a
plain TCP socket on 127.0.0.1, framing is fixed-length per BOLT message type
(see src.lightning.messages.MSG_LENGTH).
"""

import socket

import pytest

from src.lightning.messages import ACCEPT_CHANNEL_TYPE


def test_peers_listen_on_distinct_ports(make_ln_node):
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    assert alice.port != bob.port

    # both ports are actually bound
    for n in (alice, bob):
        with socket.create_connection(("127.0.0.1", n.port), timeout=2):
            pass


def test_either_peer_can_initiate(make_ln_node, logger):
    """Both directions of the open_channel handshake work."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)

    a_to_b = alice.open_channel(bob, funding_satoshis=100_000_000)
    b_to_a = bob.open_channel(alice, funding_satoshis=200_000_000)

    logger.info("alice→bob temp_id=%s" % a_to_b["temporary_channel_id"].hex())
    logger.info("bob→alice temp_id=%s" % b_to_a["temporary_channel_id"].hex())

    # each accept came from the responder's funding pubkey
    assert a_to_b["funding_pubkey"] == bob.derive_funding_pubkey().sec()
    assert b_to_a["funding_pubkey"] == alice.derive_funding_pubkey().sec()

    # temp_ids are independent random values
    assert a_to_b["temporary_channel_id"] != b_to_a["temporary_channel_id"]

    # each initiator records its own accept
    assert a_to_b["temporary_channel_id"] in alice.handshakes
    assert b_to_a["temporary_channel_id"] in bob.handshakes


def test_server_survives_unknown_message_type(make_ln_node):
    """Send a bogus 2-byte type prefix. Server warns and closes the connection,
    then keeps accepting valid traffic."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)

    # bogus message type 0xFFFF — not in MSG_LENGTH
    with socket.create_connection(("127.0.0.1", bob.port), timeout=2) as s:
        s.sendall(b"\xff\xff" + b"\x00" * 64)
        # bob's server reads 2 bytes, fails the lookup, drops the connection
        # we may or may not get an EOF immediately; either way, no hang

    # follow-up valid handshake must still work
    accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    assert accept["type"] == ACCEPT_CHANNEL_TYPE


def test_server_survives_short_frame(make_ln_node):
    """Open a connection, send 0 bytes, close. Server must not crash."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)

    with socket.create_connection(("127.0.0.1", bob.port), timeout=2):
        pass  # immediate close

    accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    assert accept["type"] == ACCEPT_CHANNEL_TYPE


def test_stop_refuses_new_connections(make_ln_node):
    alice = make_ln_node("alice", with_server=True)
    port = alice.port
    alice.stop()

    with pytest.raises((ConnectionRefusedError, OSError)):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


def test_open_channel_against_unstarted_peer_raises(make_ln_node):
    """Initiator must not silently fail when the peer hasn't started its listener."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob")  # no server

    with pytest.raises(RuntimeError, match="has not started its server"):
        alice.open_channel(bob, funding_satoshis=100_000_000)


def test_idempotent_start(make_ln_node):
    """Calling start() twice on the same node is a no-op."""
    alice = make_ln_node("alice", with_server=True)
    port = alice.port
    alice.start()  # second call: no-op
    assert alice.port == port

    # listener still works
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
