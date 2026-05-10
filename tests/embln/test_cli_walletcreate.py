"""embln-cli walletcreate — bootstrap the wallet on a running emblnd.

Refuses if a wallet for the alias is already initialized in the daemon
or already exists in the DB. With `--force`, wipes the existing wallet
and recreates from scratch (destructive)."""

import json

from src.bitcoin.manager import WalletManager


def test_bootstraps_uninit_daemon(make_emblnd, emblncli, make_logger):
    """Fresh daemon + fresh DB → walletcreate returns the new wallet info."""
    logger = make_logger("test.walletcreate.bootstrap")
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(
        alice,
        "walletcreate",
        "--alias",
        alice.alias,
        "--password",
        "test",
    )
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload["alias"] == "alice"
    assert payload["network"] == "Regtest"
    assert len(payload["fingerprint"]) == 8  # 4-byte hex


def test_refuses_when_initialized(make_emblnd, emblncli, make_logger):
    """A daemon already initialized rejects another walletcreate."""
    logger = make_logger("test.walletcreate.twice")
    alice = make_emblnd("alice")  # bootstrap=True, already initialized
    r = emblncli(
        alice,
        "walletcreate",
        "--alias",
        alice.alias,
        "--password",
        "test",
        expect_exit=1,
    )
    logger.warning("expected failure: %s", r.stderr.strip())
    assert "already initialized" in r.stderr or "already initialized" in r.stdout


def test_refuses_existing_wallet_in_db(tmp_path, make_emblnd, emblncli, make_logger):
    """Daemon is uninit but the DB already has a wallet for this alias.
    Without --force, walletcreate refuses with 'already exists'."""
    logger = make_logger("test.walletcreate.exists")
    pre_db = tmp_path / "alice.db"
    WalletManager(alias="alice", password="test", db_path=str(pre_db)).close()
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(
        alice,
        "walletcreate",
        "--alias",
        alice.alias,
        "--password",
        "test",
        expect_exit=1,
    )
    logger.warning("expected failure: %s", r.stderr.strip())
    assert "already exists" in r.stderr


def test_force_overwrites_existing_wallet(tmp_path, make_emblnd, emblncli, make_logger):
    """`--force` wipes the existing wallet (and its addresses + channels)
    and bootstraps a fresh one — fingerprint differs from the original."""
    logger = make_logger("test.walletcreate.force")
    pre_db = tmp_path / "alice.db"
    pre = WalletManager(alias="alice", password="test", db_path=str(pre_db))
    original_fingerprint = pre.fingerprint
    pre.close()
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(
        alice,
        "walletcreate",
        "--alias",
        alice.alias,
        "--password",
        "test",
        "--force",
    )
    payload = json.loads(r.stdout)
    logger.info("old=%s new=%s", original_fingerprint, payload["fingerprint"])
    assert payload["fingerprint"] != original_fingerprint


def test_missing_password_exits_nonzero(make_emblnd, emblncli, make_logger):
    """Without --password (and no env), the CLI exits 1 before sending RPC."""
    logger = make_logger("test.walletcreate.missing.password")
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(
        alice,
        "walletcreate",
        "--alias",
        alice.alias,
        expect_exit=1,
    )
    logger.warning("expected failure: %s", r.stderr.strip())
    assert "password required" in r.stderr
