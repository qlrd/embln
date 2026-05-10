import pytest

from src.bitcoin.opcodes import OP_2, OP_CHECKMULTISIG


@pytest.fixture
def unsigned_funding():
    def _make(a, b, value):
        b_pk = b.derive_funding_pubkey()
        return a.create_fundingtx_output(b_pk, value)

    return _make


def test_funding_output_is_p2wsh(make_ln_node, logger, unsigned_funding):
    """The funding output's scriptPubKey is exactly OP_0 <0x20> <32-byte sha256>."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    tx_out = unsigned_funding(a=alice, b=bob, value=10_000_000)
    logger.info("alice_funding_pubkey=%s", alice.derive_funding_pubkey().sec().hex())
    logger.info("bob_funding_pubkey=%s", bob.derive_funding_pubkey().sec().hex())
    logger.debug("txout.value=%s", tx_out.value)  # = the input value, not interesting
    logger.info("scriptPubkey=%s", tx_out.script_pubkey.data.hex())
    assert tx_out.value == 10_000_000
    assert tx_out.script_pubkey.data[:2] == bytes([0x00, 0x20])
    assert len(tx_out.script_pubkey.data) == 34


def test_witness_script_uses_bolt3_lex_order(make_ln_node, logger, unsigned_funding):
    """BOLT 3: pubkey1 in the 2-of-2 is the lexicographically lesser SEC."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    alice_pk = alice.derive_funding_pubkey(channel_idx=0)
    bob_pk = bob.derive_funding_pubkey()
    logger.info("alice_pk=%s", alice_pk.sec().hex())
    logger.info("bob_pk=%s", bob_pk.sec().hex())
    unsigned_funding(a=alice, b=bob, value=10_000_000)

    a, b = sorted([alice_pk.sec(), bob_pk.sec()])
    expected = (
        bytes([OP_2, len(a)])
        + a
        + bytes([len(b)])
        + b
        + bytes([OP_2, OP_CHECKMULTISIG])
    )

    actual = alice.channels[0]["witness_script"].data
    logger.info("actual_witness=%s", actual.hex())
    logger.info("expected_witness=%s", expected.hex())
    assert actual == expected
