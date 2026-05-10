import pytest

from src.lightning.messages import (
    ACCEPT_CHANNEL_TYPE,
    OPEN_CHANNEL_TYPE,
    REGTEST_CHAIN_HASH,
    parse_accept_channel,
    parse_open_channel,
)


def test_open_channel_msg_shape(make_ln_node, logger):
    """Mandatory-fields-only open_channel is exactly 321 bytes (2 type + 319 body)."""
    alice = make_ln_node("alice")
    msg = alice.create_open_channel_msg(funding_satoshis=100_000_000)
    logger.info("open_channel msg=%s", msg.hex())
    logger.debug("open_channel len=%s", len(msg))

    assert msg[:2] == OPEN_CHANNEL_TYPE.to_bytes(2, "big")
    assert len(msg) == 321
    parsed = parse_open_channel(msg)

    logger.info("chain=%s", parsed["chain_hash"].hex())
    logger.info("funding_satoshis=%s", parsed["funding_satoshis"])
    logger.info("funding_pubkey=%s", parsed["funding_pubkey"].hex())
    assert parsed["chain_hash"] == REGTEST_CHAIN_HASH
    assert parsed["funding_satoshis"] == 100_000_000
    assert len(parsed["funding_pubkey"]) == 33
    assert parsed["funding_pubkey"][0] in (0x02, 0x03)


def test_alice_opens_bob_accepts(make_ln_node, logger):
    """Full BOLT 2 open_channel + accept_channel round-trip via parse helpers."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    open_msg = alice.create_open_channel_msg(funding_satoshis=100_000_000)
    open_parsed = parse_open_channel(open_msg)
    logger.info("alice open_channel=%s", open_msg.hex())
    logger.info("alice temp_id=%s", open_parsed["temporary_channel_id"].hex())
    logger.info("alice funding_pubkey=%s", open_parsed["funding_pubkey"].hex())

    assert open_parsed["type"] == OPEN_CHANNEL_TYPE
    assert open_parsed["funding_satoshis"] == 100_000_000
    assert open_parsed["funding_pubkey"] == alice.derive_funding_pubkey().sec()

    accept_msg = bob.create_accept_channel_msg(open_parsed, minimum_depth=3)
    accept_parsed = parse_accept_channel(accept_msg)
    logger.info("bob accept_channel=%s", accept_msg.hex())
    logger.info("bob funding_pubkey=%s", accept_parsed["funding_pubkey"].hex())
    logger.info("bob minimum_depth=%s", accept_parsed["minimum_depth"])

    assert accept_parsed["type"] == ACCEPT_CHANNEL_TYPE
    assert accept_parsed["temporary_channel_id"] == open_parsed["temporary_channel_id"]
    assert accept_parsed["funding_pubkey"] == bob.derive_funding_pubkey().sec()
    assert accept_parsed["funding_pubkey"] != open_parsed["funding_pubkey"]
    assert accept_parsed["dust_limit_satoshis"] == open_parsed["dust_limit_satoshis"]
    assert (
        accept_parsed["channel_reserve_satoshis"]
        == open_parsed["channel_reserve_satoshis"]
    )
    assert accept_parsed["to_self_delay"] == open_parsed["to_self_delay"]
    assert accept_parsed["minimum_depth"] == 3


def test_open_channel_funding_pubkey_matches_create_fundingtx_output(
    make_ln_node, logger
):
    """The funding_pubkey announced in open_channel is the same one used when
    Alice later builds the funding-tx 2-of-2 output for the same channel slot."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    open_msg = alice.create_open_channel_msg(funding_satoshis=100_000_000)
    open_parsed = parse_open_channel(open_msg)
    accept_msg = bob.create_accept_channel_msg(open_parsed)
    accept_parsed = parse_accept_channel(accept_msg)

    logger.info("alice open_channel=%s", open_msg.hex())
    logger.info("bob accept_channel=%s", accept_msg.hex())

    bob_pk = bob.derive_funding_pubkey()
    txout = alice.create_fundingtx_output(bob_pk, 100_000_000)
    logger.info("bob_pk_from_accept=%s", accept_parsed["funding_pubkey"].hex())
    logger.info("bob_pk_local=%s", bob_pk.sec().hex())
    logger.info("txout.value=%s", txout.value)
    assert bob_pk.sec() == accept_parsed["funding_pubkey"]

    ws = alice.channels[0]["witness_script"].data
    logger.info("alice witness_script=%s", ws.hex())

    # OP_2 (1) + len (1) + pk_a (33) + len (1) + pk_b (33) + OP_2 (1) + OP_CHECKMULTISIG (1) = 71
    pk_a = ws[2:35]
    pk_b = ws[36:69]
    assert open_parsed["funding_pubkey"] in (pk_a, pk_b)
    assert accept_parsed["funding_pubkey"] in (pk_a, pk_b)


def test_open_channel_handshake_over_socket(make_ln_node, logger):
    """Alice opens a TCP connection to bob's LN listener, sends open_channel,
    bob's server thread responds with accept_channel. Pure in-process simulation
    — no noise transport, no encryption."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    # listen ports already logged by EmblndDaemon — demote.
    logger.debug("alice.port=%s bob.port=%s", alice.port, bob.port)

    accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    logger.info("accept funding_pubkey=%s", accept["funding_pubkey"].hex())
    logger.info("accept temp_id=%s", accept["temporary_channel_id"].hex())
    logger.info("accept minimum_depth=%s", accept["minimum_depth"])

    assert accept["type"] == ACCEPT_CHANNEL_TYPE
    assert accept["funding_pubkey"] == bob.derive_funding_pubkey().sec()
    assert accept["minimum_depth"] == 3
    assert accept["temporary_channel_id"] in alice.pending_accepts
    assert alice.pending_accepts[accept["temporary_channel_id"]] == accept


@pytest.mark.integration
def test_open_channel(make_ln_node, logger):
    """End-to-end: alice opens a channel to bob over the LN socket, then uses
    the funding_pubkey announced in bob's accept_channel to build the on-chain
    2-of-2 P2WSH output. Core's createmultisig must agree on the address.

    Exercises both EmblnNode roles simultaneously: P2P server (responding to
    the open) and bitcoind RPC client (validating the resulting multisig).
    """
    from embit import ec

    # 0. make nodes with bitcoin-core and a ln server
    alice = make_ln_node("alice", with_daemon=True, with_server=True)
    bob = make_ln_node("bob", with_daemon=True, with_server=True)
    # bitcoind + emblnd setup is logged by their own loggers — demote.
    logger.debug("alice.rpcport=%s ln_port=%s", alice.bitcoind.rpcport, alice.port)
    logger.debug("bob.rpcport=%s ln_port=%s", bob.bitcoind.rpcport, bob.port)

    # 1. P2P open_channel over the LN socket.
    bob_accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    logger.info("alice received funding_pubkey=%s", bob_accept["funding_pubkey"].hex())

    # 2. Reconstruct bob's pubkey from the wire bytes alice received and use it
    #    to build the on-chain funding output (no shortcut to bob's local keys).
    bob_pk = ec.PublicKey.parse(bob_accept["funding_pubkey"])
    alice.create_fundingtx_output(bob_pk, 100_000_000)
    embln_addr = alice.channels[0]["address"]
    embln_redeem_script = alice.channels[0]["witness_script"].data.hex()
    logger.info("embln addr=%s", embln_addr)
    logger.info("embln redeemScript=%s", embln_redeem_script)

    # 3. Independent verification: ask Core to recompute the same multisig.
    sorted_pks_hex = sorted(
        [alice.derive_funding_pubkey(0).sec().hex(), bob_accept["funding_pubkey"].hex()]
    )
    core = alice.rpc.createmultisig(2, sorted_pks_hex, "bech32")
    logger.info("bitcoin-core addr=%s", core["address"])
    logger.info("bitcoin-core redeemScript=%s", core["redeemScript"])

    assert core["address"] == embln_addr
    assert core["redeemScript"] == embln_redeem_script
