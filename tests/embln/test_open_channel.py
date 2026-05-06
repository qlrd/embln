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

    assert msg[:2] == OPEN_CHANNEL_TYPE.to_bytes(2, "big")
    assert len(msg) == 321
    parsed = parse_open_channel(msg)

    logger.info("chain=%s" % parsed["chain_hash"].hex())
    logger.info("funding_satoshis=%s" % parsed["funding_satoshis"])
    logger.info("funding_pubkey=%s" % parsed["funding_pubkey"].hex())
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

    logger.info("alice open_channel=%s" % open_msg.hex())
    assert open_parsed["type"] == OPEN_CHANNEL_TYPE
    assert open_parsed["funding_satoshis"] == 100_000_000
    assert open_parsed["funding_pubkey"] == alice.derive_funding_pubkey().sec()

    accept_msg = bob.create_accept_channel_msg(open_parsed, minimum_depth=3)
    accept_parsed = parse_accept_channel(accept_msg)
    logger.info("bob accept_channel=%s" % accept_msg.hex())

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

    logger.info("alice open_channel=%s" % open_msg.hex())
    logger.info("bob accept_channel=%s" % accept_msg.hex())

    bob_pk = bob.derive_funding_pubkey()
    txout = alice.create_fundingtx_output(bob_pk, 100_000_000)
    logger.info("alice get bob pubkey=%s" % accept_parsed["funding_pubkey"].hex())
    logger.info("alice build txout.value=%s" % txout.value)
    assert bob_pk.sec() == accept_parsed["funding_pubkey"]

    ws = alice.channels[0]["witness_script"].data
    logger.info("alice build witness_script=%s" % ws.hex())

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

    accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    logger.info(
        "alice received accept_channel funding_pubkey=%s"
        % accept["funding_pubkey"].hex()
    )

    assert accept["type"] == ACCEPT_CHANNEL_TYPE
    assert accept["funding_pubkey"] == bob.derive_funding_pubkey().sec()
    assert accept["minimum_depth"] == 3
    assert accept["temporary_channel_id"] in alice.handshakes
    assert alice.handshakes[accept["temporary_channel_id"]] == accept
