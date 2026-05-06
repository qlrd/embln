import pytest

from src.bitcoin.opcodes import OP_2, OP_CHECKMULTISIG


def test_funding_output_is_p2wsh(make_ln_node, logger):
    """The funding output's scriptPubKey is exactly OP_0 <0x20> <32-byte sha256>."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    bob_pk = bob.derive_funding_pubkey()
    tx_out = alice.create_fundingtx_output(bob_pk, 10_000_000)

    logger.info("txout=%s" % tx_out.value)
    logger.info("scriptPubkey=%s" % tx_out.script_pubkey.data.hex())
    assert tx_out.value == 10_000_000
    assert tx_out.script_pubkey.data[:2] == bytes([0x00, 0x20])
    assert len(tx_out.script_pubkey.data) == 34


def test_witness_script_uses_bolt3_lex_order(make_ln_node, logger):
    """BOLT 3: pubkey1 in the 2-of-2 is the lexicographically lesser SEC."""
    alice = make_ln_node("alice")
    bob = make_ln_node("bob")

    alice_pk = alice.derive_funding_pubkey(channel_idx=0)
    bob_pk = bob.derive_funding_pubkey()
    alice.create_fundingtx_output(bob_pk, 10_000_000)

    a, b = sorted([alice_pk.sec(), bob_pk.sec()])
    expected = (
        bytes([OP_2, len(a)])
        + a
        + bytes([len(b)])
        + b
        + bytes([OP_2, OP_CHECKMULTISIG])
    )

    logger.info("alice witness=%s" % alice.channels[0]["witness_script"].data.hex())
    logger.info("expec witness=%s" % expected.hex())
    assert alice.channels[0]["witness_script"].data == expected


@pytest.mark.integration
def test_p2p_handshake_then_funding_address_matches_core(make_ln_node, logger):
    """End-to-end: alice opens a channel to bob over the LN socket, then uses
    the funding_pubkey announced in bob's accept_channel to build the on-chain
    2-of-2 P2WSH output. Core's createmultisig must agree on the address.

    Exercises both LightningNode roles simultaneously: P2P server (responding to
    the open) and bitcoind RPC client (validating the resulting multisig).
    """
    from embit import ec

    # 0. make nodes with bitcoin-core and a ln server
    alice = make_ln_node("alice", with_daemon=True, with_server=True)
    bob = make_ln_node("bob", with_daemon=True, with_server=True)

    # 1. P2P open_channel over the LN socket.
    bob_accept = alice.open_channel(bob, funding_satoshis=100_000_000)
    logger.info("alice funding_pubkey=%s" % bob_accept["funding_pubkey"].hex())

    # 2. Reconstruct bob's pubkey from the wire bytes alice received and use it
    #    to build the on-chain funding output (no shortcut to bob's local keys).
    bob_pk = ec.PublicKey.parse(bob_accept["funding_pubkey"])
    alice.create_fundingtx_output(bob_pk, 100_000_000)
    embln_addr = alice.channels[0]["address"]
    embln_redeem_script = alice.channels[0]["witness_script"].data.hex()
    logger.info("embln generated addr=%s" % embln_addr)
    logger.info("embln generated redeemScript=%s" % embln_redeem_script)

    # 3. Independent verification: ask Core to recompute the same multisig.
    sorted_pks_hex = sorted(
        [alice.derive_funding_pubkey(0).sec().hex(), bob_accept["funding_pubkey"].hex()]
    )
    core = alice.rpc.createmultisig(2, sorted_pks_hex, "bech32")
    logger.info("bitcoin-core generated addr=%s" % core["address"])

    assert core["address"] == embln_addr
    assert core["redeemScript"] == embln_redeem_script
