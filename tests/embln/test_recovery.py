from src import LightningNode, WalletManager


def test_wallet_recovers_addresses(db_path):
    """A second WalletManager(same name, same password, same DB) reloads state."""
    alice1 = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        alice1.create_new_address(branch=0)
        alice1.create_new_address(branch=0)
        alice1.create_new_address(branch=1)

        addrs = [a["address"] for a in alice1.addresses]
        changes = [a["address"] for a in alice1.changes]
        mnemonic = alice1.mnemonic
    finally:
        alice1.close()

    # discard alice1, simulate process restart
    alice2 = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        assert alice2.mnemonic == mnemonic
        assert [a["address"] for a in alice2.addresses] == addrs
        assert [a["address"] for a in alice2.changes] == changes
    finally:
        alice2.close()


def test_lightning_node_recovers_channels(db_path):
    """Channels persisted by one LightningNode are visible to a fresh instance."""
    alice = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    bob = WalletManager(alias="bob", password="bobpwd", db_path=db_path)
    try:
        alice_ln = LightningNode(alice)
        bob_ln = LightningNode(bob)
        bob_pk = bob_ln.derive_funding_pubkey()
        alice_ln.create_fundingtx_output(bob_pk, 100_000_000)

        expected_addr = alice_ln.channels[0]["address"]
        expected_amount = alice_ln.channels[0]["amount_sat"]
        expected_ws = alice_ln.channels[0]["witness_script"].data
    finally:
        alice.close()
        bob.close()

    # rehydrate alice from the same DB
    alice_reload = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        alice_ln_reload = LightningNode(alice_reload)
        assert len(alice_ln_reload.channels) == 1
        assert alice_ln_reload.channels[0]["address"] == expected_addr
        assert alice_ln_reload.channels[0]["amount_sat"] == expected_amount
        assert alice_ln_reload.channels[0]["witness_script"].data == expected_ws
    finally:
        alice_reload.close()
