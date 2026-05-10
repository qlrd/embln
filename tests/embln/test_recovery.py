from src import EmblnNode, WalletManager


def test_wallet_recovers_addresses(db_path, logger):
    """A second WalletManager(same name, same password, same DB) reloads state."""
    alice1 = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        alice1.create_new_address(branch=0)
        alice1.create_new_address(branch=0)
        alice1.create_new_address(branch=1)

        addrs = [a["address"] for a in alice1.addresses]
        changes = [a["address"] for a in alice1.changes]
        mnemonic = alice1.mnemonic
        logger.info("alice1 receive_addrs=%s", addrs)
        logger.info("alice1 change_addrs=%s", changes)
        logger.debug("alice1 mnemonic_words=%s", len(mnemonic.split()))
    finally:
        alice1.close()

    # discard alice1, simulate process restart
    alice2 = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        logger.info(
            "alice2 reload receive=%s change=%s",
            [a["address"] for a in alice2.addresses],
            [a["address"] for a in alice2.changes],
        )
        assert alice2.mnemonic == mnemonic
        assert [a["address"] for a in alice2.addresses] == addrs
        assert [a["address"] for a in alice2.changes] == changes
    finally:
        alice2.close()


def test_lightning_node_recovers_channels(db_path, logger):
    """Channels persisted by one EmblnNode are visible to a fresh instance."""
    alice = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    bob = WalletManager(alias="bob", password="bobpwd", db_path=db_path)
    try:
        alice_ln = EmblnNode(alice)
        bob_ln = EmblnNode(bob)
        bob_pk = bob_ln.derive_funding_pubkey()
        alice_ln.create_fundingtx_output(bob_pk, 100_000_000)

        expected_addr = alice_ln.channels[0]["address"]
        expected_amount = alice_ln.channels[0]["amount_sat"]
        expected_ws = alice_ln.channels[0]["witness_script"].data
        logger.info("persisted addr=%s amount=%s", expected_addr, expected_amount)
        logger.info("persisted witness_script=%s", expected_ws.hex())
    finally:
        alice.close()
        bob.close()

    # rehydrate alice from the same DB
    alice_reload = WalletManager(alias="alice", password="alicepwd", db_path=db_path)
    try:
        alice_ln_reload = EmblnNode(alice_reload)
        logger.info("reloaded num_channels=%s", len(alice_ln_reload.channels))
        logger.info("reloaded addr=%s", alice_ln_reload.channels[0]["address"])
        assert len(alice_ln_reload.channels) == 1
        assert alice_ln_reload.channels[0]["address"] == expected_addr
        assert alice_ln_reload.channels[0]["amount_sat"] == expected_amount
        assert alice_ln_reload.channels[0]["witness_script"].data == expected_ws
    finally:
        alice_reload.close()
