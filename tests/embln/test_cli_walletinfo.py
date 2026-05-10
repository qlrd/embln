"""embln-cli walletinfo — print wallet config (alias, fingerprint, addresses)."""

import json


def test_lists_addresses(make_emblnd, emblncli, make_logger):
    """walletinfo['addresses'] is the cumulative receive-branch list."""
    logger = make_logger("test.walletinfo.addresses")
    alice = make_emblnd("alice")
    for _ in range(10):
        emblncli(alice, "walletaddress", "--alias", alice.alias)
    r = emblncli(alice, "walletinfo", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert len(payload["addresses"]) == 10
    assert all(a.startswith("bcrt1") for a in payload["addresses"])


def test_uninit_daemon_errors(make_emblnd, emblncli, make_logger):
    """Pre-bootstrap, walletinfo returns the daemon's 'wallet not initialized'
    error envelope and the CLI exits 1."""
    logger = make_logger("test.walletinfo.uninit")
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(
        alice,
        "walletinfo",
        "--alias",
        alice.alias,
        expect_exit=1,
    )
    logger.warning("expected failure: %s", r.stderr.strip())
    assert "wallet not initialized" in r.stderr
