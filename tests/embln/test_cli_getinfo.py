"""embln-cli getinfo — print emblnd's runtime config (alias, listen ports, peers).

Works in both initialized and uninit daemon states (the only RPC method
that does)."""

import json


def test_shows_defaults(make_emblnd, emblncli, make_logger):
    logger = make_logger("test.getinfo.defaults")
    alice = make_emblnd("alice")
    r = emblncli(alice, "getinfo", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload["alias"] == "alice"
    assert payload["initialized"] is True
    assert payload["color"] == "000000"
    assert payload["network"] == "Regtest"


def test_uninit_daemon(make_emblnd, emblncli, make_logger):
    """Pre-bootstrap, getinfo reports `initialized=false` and a null alias."""
    logger = make_logger("test.getinfo.uninit")
    alice = make_emblnd("alice", bootstrap=False)
    r = emblncli(alice, "getinfo", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload["initialized"] is False
    assert payload["alias"] is None
