"""embln-cli listchannels — list on-chain channel funding outputs known
to the node."""

import json


def test_empty_for_fresh_wallet(make_emblnd, emblncli, make_logger):
    """A wallet with no channels reports an empty list."""
    logger = make_logger("test.listchannels.empty")
    alice = make_emblnd("alice")
    r = emblncli(alice, "listchannels", "--alias", alice.alias)
    payload = json.loads(r.stdout)
    logger.info("payload=%s", payload)
    assert payload == {"channels": []}
