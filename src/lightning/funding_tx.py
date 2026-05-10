"""BOLT 3 funding-output construction, extracted from EmblnNode.

`FundingTransaction(node)` wraps a EmblnNode and owns:
  - LND-style channel-key derivation (m/1017'/<coin>'/<family>'/0/<idx>)
  - The 2-of-2 P2WSH funding output, with BOLT 3 lex-ordered pubkeys
  - Persisting the channel row to the wallet's SQLite Db

The node is the source of truth for `wallet` and `channels`; this class
mutates `node.channels` and `node.wallet.db` rather than holding its own
state, so a node can have at most one funding-tx helper.
"""

from embit import ec, script
from embit.transaction import TransactionOutput

# LND key-family identifiers used for per-channel basepoint derivation:
#   m/1017'/<coin_type>'/<family>'/0/<channel_idx>
FAMILY_MULTISIG = 0  # funding pubkey
FAMILY_REVOCATION = 1  # revocation_basepoint
FAMILY_HTLC = 2  # htlc_basepoint
FAMILY_PAYMENT = 3  # payment_basepoint
FAMILY_DELAYED_PAYMENT = 4  # delayed_payment_basepoint
FAMILY_REVOCATION_ROOT = 5  # source of per-commitment-secret seed (stub here)

# regtest = testnet coin type per SLIP-44
COIN_TYPE_TESTNET = 1


class FundingTransaction:
    def __init__(self, node):
        self._node = node

    def channel_pubkey(self, family: int, channel_idx: int) -> ec.PublicKey:
        path = "m/1017h/%sh/%sh/0/%s" % (
            COIN_TYPE_TESTNET,
            family,
            channel_idx,
        )
        return self._node.wallet.root.derive(path).key.get_public_key()

    def derive_funding_pubkey(self, channel_idx: int = None) -> ec.PublicKey:
        if channel_idx is None:
            channel_idx = len(self._node.channels)
        return self.channel_pubkey(FAMILY_MULTISIG, channel_idx)

    def create_fundingtx_output(
        self, peer_funding_pubkey: ec.PublicKey, amount_sat: int
    ) -> TransactionOutput:
        node = self._node
        channel_idx = len(node.channels)
        pk1 = self.derive_funding_pubkey(channel_idx)
        ordered = sorted([pk1, peer_funding_pubkey], key=lambda k: k.sec())

        witness_script = script.multisig(2, ordered)
        script_pubkey = script.p2wsh(witness_script)
        address = script_pubkey.address(node.wallet.network)

        node.channels.append(
            {
                "witness_script": witness_script,
                "script_pubkey": script_pubkey,
                "address": address,
                "amount_sat": amount_sat,
                "used": False,
            }
        )
        node.wallet.db.save_channel(
            node.wallet.alias,
            channel_idx,
            address,
            witness_script.data,
            script_pubkey.data,
            amount_sat,
        )
        return TransactionOutput(amount_sat, script_pubkey)
