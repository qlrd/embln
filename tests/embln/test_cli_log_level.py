"""argparse / log-level surface for embln-cli — `--log-level` precedence
and what records each level emits to stderr."""


def test_debug_emits_rpc_records(make_emblnd, emblncli):
    """At --log-level debug, BaseRpc.request DEBUG logs reach stderr."""
    alice = make_emblnd("alice")
    r = emblncli(
        alice,
        "--log-level",
        "debug",
        "getinfo",
        "--alias",
        alice.alias,
    )
    assert "request id=0 methods=getinfo" in r.stderr


def test_error_suppresses_info_and_debug(make_emblnd, emblncli):
    alice = make_emblnd("alice")
    r = emblncli(
        alice,
        "--log-level",
        "error",
        "getinfo",
        "--alias",
        alice.alias,
    )
    assert "INFO" not in r.stderr
    assert "DEBUG" not in r.stderr


def test_env_loglevel_works(make_emblnd, emblncli):
    """EMBLN_LOGLEVEL takes effect when --log-level isn't passed."""
    alice = make_emblnd("alice")
    r = emblncli(
        alice,
        "getinfo",
        "--alias",
        alice.alias,
        env_overrides={"EMBLN_LOGLEVEL": "debug"},
    )
    assert "request id=0 methods=getinfo" in r.stderr


def test_arg_overrides_env(make_emblnd, emblncli):
    """Explicit --log-level wins over EMBLN_LOGLEVEL env."""
    alice = make_emblnd("alice")
    r = emblncli(
        alice,
        "--log-level",
        "error",
        "getinfo",
        "--alias",
        alice.alias,
        env_overrides={"EMBLN_LOGLEVEL": "debug"},
    )
    assert "request id=" not in r.stderr


def test_unknown_level_value_errors(make_embln_cli):
    """argparse choices validation rejects unknown levels with exit 2."""
    r = make_embln_cli(
        "--log-level",
        "trace",  # not a valid choice
        "getinfo",
        "--alias",
        "alice",
        expect_exit=2,
    )
    assert "invalid choice" in r.stderr
