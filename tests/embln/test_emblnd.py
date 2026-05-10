"""emblnd subprocess lifecycle — spawn, listen, exit codes."""

import socket


def test_starts_listens_then_stops_cleanly(make_emblnd):
    """Uninit emblnd should listen on its P2P port — no wallet needed."""
    alice = make_emblnd("alice", bootstrap=False)
    with socket.create_connection(("127.0.0.1", alice.p2p_port), timeout=2):
        pass


def test_explicit_p2p_port(make_emblnd):
    """`--p2p-port` makes the listener deterministic (useful for repro/testing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        wanted = s.getsockname()[1]
    alice = make_emblnd("alice", p2p_port=wanted, bootstrap=False)
    assert alice.p2p_port == wanted


def test_no_datadir_no_alias_exits(tmp_path, make_runner):
    """Uninit emblnd needs at least --datadir (or --alias to derive one)."""
    db = tmp_path / "embln.db"
    r = make_runner(
        "src.bin.emblnd",
        "--db",
        str(db),
        expect_exit=1,
    )
    assert "datadir" in r.stderr.lower()
