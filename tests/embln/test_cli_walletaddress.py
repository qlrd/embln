"""embln-cli walletaddress — derive the next on-chain BIP84 address."""

import json


def test_returns_bech32(make_emblnd, emblncli, make_logger):
    logger = make_logger("test.walletaddress.bech32")
    alice = make_emblnd("alice")
    r = emblncli(alice, "walletaddress", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload["branch"] == 0
    assert payload["address"].startswith("bcrt1"), (
        "expected regtest bech32, got: %r" % payload["address"]
    )


def test_change_branch(make_emblnd, emblncli, make_logger):
    """`--branch 1` derives from the change branch (0 = receive default)."""
    logger = make_logger("test.walletaddress.change")
    alice = make_emblnd("alice")
    r = emblncli(alice, "walletaddress", "--alias", alice.alias, "--branch", "1")
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload["branch"] == 1
    assert payload["address"].startswith("bcrt1")
