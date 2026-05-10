"""Logger plumbing tests: EMBLN_LOGLEVEL parsing, per-instance logger names,
the MPSC queue/listener pair, and that lifecycle / DB events actually emit
records at the expected levels."""

import logging
import threading

import pytest

from src.bin._common import (
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)

# ---- resolve_log_level ----


def test_resolve_log_level_default_is_info(monkeypatch):
    monkeypatch.delenv("EMBLN_LOGLEVEL", raising=False)
    assert resolve_log_level() == logging.INFO


@pytest.mark.parametrize(
    "value,expected",
    [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("warn", logging.WARNING),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        ("DEBUG", logging.DEBUG),  # case-insensitive
        ("Warn", logging.WARNING),
    ],
)
def test_resolve_log_level_env(monkeypatch, value, expected):
    monkeypatch.setenv("EMBLN_LOGLEVEL", value)
    assert resolve_log_level() == expected


def test_resolve_log_level_unknown_value_falls_back(monkeypatch):
    monkeypatch.setenv("EMBLN_LOGLEVEL", "trace")
    assert resolve_log_level() == logging.INFO


def test_resolve_log_level_verbose_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("EMBLN_LOGLEVEL", "error")
    args = type("A", (), {"verbose": True})()
    assert resolve_log_level(args) == logging.DEBUG


def test_resolve_log_level_args_without_verbose_attr(monkeypatch):
    monkeypatch.setenv("EMBLN_LOGLEVEL", "warn")
    args = type("A", (), {})()
    assert resolve_log_level(args) == logging.WARNING


# ---- per-instance logger names ----


def test_wallet_logger_name(make_wallet):
    alice = make_wallet("alice")
    assert alice.logger.name == "alice"


def test_lightning_node_logger_is_child_of_wallet(make_ln_node):
    alice = make_ln_node("alice")
    assert alice.logger.name == "alice.ln"
    # python's logging hierarchy: parent of "alice.ln" is "alice"
    assert alice.logger.parent.name == "alice"


# ---- EmblnNode lifecycle emits at the right level ----


def test_lightning_node_start_emits_lifecycle_at_info(make_ln_node, caplog):
    """Lifecycle (alias / color / listen ports / 'started') is logged by the
    underlying EmblndDaemon under '<alias>.emblnd', not by EmblnNode."""
    alice = make_ln_node("alice")
    with caplog.at_level(logging.INFO, logger="alice.emblnd"):
        alice.start()
        try:
            info_msgs = [
                r.getMessage()
                for r in caplog.records
                if r.name == "alice.emblnd" and r.levelno == logging.INFO
            ]
        finally:
            alice.stop()
    assert any("alias=alice" in m for m in info_msgs)
    assert any("color_rgb=000000" in m for m in info_msgs)
    assert any(m == "started" for m in info_msgs)


def test_lightning_node_channel_policy_defaults_at_debug(make_ln_node, caplog):
    """Per-policy-field log lines from `EmblnNode.log_policy_defaults`
    should be DEBUG-only — they'd bloat INFO output. Logged under '<alias>.ln'
    (the node's own logger) since they describe protocol-level state."""
    alice = make_ln_node("alice")
    with caplog.at_level(logging.DEBUG, logger="alice.ln"):
        alice.start()
        try:
            records = [r for r in caplog.records if r.name == "alice.ln"]
        finally:
            alice.stop()
    debug_msgs = [r.getMessage() for r in records if r.levelno == logging.DEBUG]
    info_msgs = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert any("open_channel.funding_satoshis=" in m for m in debug_msgs)
    # INFO must NOT carry the per-policy-field detail
    assert not any("open_channel.funding_satoshis=" in m for m in info_msgs)


def test_lightning_node_stop_emits_lifecycle_at_info(make_ln_node, caplog):
    alice = make_ln_node("alice")
    alice.start()
    with caplog.at_level(logging.INFO, logger="alice.emblnd"):
        alice.stop()
    info_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.name == "alice.emblnd" and r.levelno == logging.INFO
    ]
    assert "stopping" in info_msgs
    assert "stopped" in info_msgs


def test_lightning_node_double_stop_is_debug_not_info(make_ln_node, caplog):
    """A no-op stop must not pollute the lifecycle stream — DEBUG only."""
    alice = make_ln_node("alice")
    # node never started; stop() takes the early-return path
    with caplog.at_level(logging.DEBUG, logger="alice.ln"):
        alice.stop()
    records = [r for r in caplog.records if r.name == "alice.ln"]
    assert any(
        "not running" in r.getMessage() and r.levelno == logging.DEBUG for r in records
    )
    assert not any(r.levelno == logging.INFO for r in records)


# ---- EmblnNode P2P incoming/outgoing wire events at DEBUG ----


def test_lightning_node_recv_send_logs_at_debug(make_ln_node, caplog):
    """Wire-level recv/sent events come from EmblndDaemon (the listener),
    so the records appear under '<alias>.emblnd'."""
    alice = make_ln_node("alice", with_server=True)
    bob = make_ln_node("bob", with_server=True)
    with caplog.at_level(logging.DEBUG, logger="bob.emblnd"):
        alice.open_channel(bob, funding_satoshis=100_000_000)
    debug_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.name == "bob.emblnd" and r.levelno == logging.DEBUG
    ]
    # bob received the open_channel and sent the accept_channel
    assert any("recv msg_type=32" in m for m in debug_msgs)
    assert any("sent msg_type=33" in m for m in debug_msgs)


# ---- Db emits DEBUG on operations ----


def test_db_emits_debug_on_save_address(make_wallet, caplog):
    alice = make_wallet("alice")
    with caplog.at_level(logging.DEBUG, logger="db"):
        alice.create_new_address(branch=0)
    db_msgs = [r.getMessage() for r in caplog.records if r.name == "db"]
    assert any("save_address" in m and "wallet=alice" in m for m in db_msgs)


def test_db_save_wallet_logs_only_alias_and_version(make_wallet, caplog):
    """Mnemonic must not appear in any log record — even DEBUG."""
    with caplog.at_level(logging.DEBUG, logger="db"):
        wm = make_wallet("alice")
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    # the actual mnemonic words should not appear in any log
    assert wm.mnemonic not in full_log
    # but the operation itself should have been recorded
    assert "save_wallet alias=alice" in full_log


# ---- MPSC queue plumbing ----


def test_setup_mpsc_logging_returns_two_listeners(tmp_path):
    """Two queues, two consumer threads — one per sink."""
    listeners = setup_mpsc_logging(
        level=logging.DEBUG, log_file=str(tmp_path / "mpsc.log")
    )
    try:
        assert len(listeners) == 2
        # each listener has its own private thread
        assert listeners[0]._thread is not None
        assert listeners[1]._thread is not None
        assert listeners[0]._thread is not listeners[1]._thread
        # root logger has exactly two QueueHandlers (one per queue)
        from logging.handlers import QueueHandler

        root_qh = [
            h for h in logging.getLogger().handlers if isinstance(h, QueueHandler)
        ]
        assert len(root_qh) == 2
    finally:
        teardown_mpsc_logging(listeners)


def test_mpsc_file_sink_receives_all_records_from_many_threads(tmp_path):
    """Multi-producer / single-consumer: 8 threads each emit 50 records,
    every record reaches the file sink."""
    log_file = tmp_path / "mpsc.log"
    listeners = setup_mpsc_logging(level=logging.DEBUG, log_file=str(log_file))
    try:
        log = logging.getLogger("mpsc.test")
        n_threads, n_per = 8, 50

        def worker(i):
            for j in range(n_per):
                log.info("worker=%s seq=%s", i, j)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        teardown_mpsc_logging(listeners)  # drains queue, joins consumer threads

    # parse each line and collect (worker, seq) pairs — substring counting
    # would overcount via prefix matches like "seq=1" inside "seq=10..19".
    import re

    contents = log_file.read_text()
    pairs = set()
    for line in contents.splitlines():
        m = re.search(r"worker=(\d+) seq=(\d+)", line)
        if m:
            pairs.add((int(m.group(1)), int(m.group(2))))
    assert len(pairs) == n_threads * n_per, (
        "expected %s unique (worker,seq) records, got %s"
        % (n_threads * n_per, len(pairs))
    )


def test_mpsc_stream_sink_filtered_by_level(tmp_path, capsys):
    """Stream sink at INFO suppresses DEBUG; file sink keeps everything."""
    log_file = tmp_path / "mpsc.log"
    listeners = setup_mpsc_logging(level=logging.INFO, log_file=str(log_file))
    try:
        log = logging.getLogger("mpsc.split")
        log.debug("debug-line")
        log.info("info-line")
    finally:
        teardown_mpsc_logging(listeners)

    err = capsys.readouterr().err
    assert "info-line" in err
    assert "debug-line" not in err  # stream sink filtered

    contents = log_file.read_text()
    assert "info-line" in contents
    assert "debug-line" in contents  # file sink keeps DEBUG


def test_teardown_is_idempotent(tmp_path):
    listeners = setup_mpsc_logging(
        level=logging.DEBUG, log_file=str(tmp_path / "mpsc.log")
    )
    teardown_mpsc_logging(listeners)
    teardown_mpsc_logging(listeners)  # second call must not raise
