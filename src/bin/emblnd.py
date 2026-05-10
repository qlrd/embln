"""emblnd — embln Lightning daemon.

Long-running process: starts the LN P2P + JSON-RPC listeners, writes the
bitcoind-style `<datadir>/.cookie` (with embln endpoint extension), and
blocks until SIGINT or SIGTERM.

Wallet bootstrap is bitcoind/lnd-style: emblnd starts in **uninitialized**
state. Use `embln-cli wallet create --alias X --password Y` against the
running daemon to create the wallet over RPC. On a warm restart with
`--alias` + `--password` (or env equivalents) and an existing DB row,
emblnd auto-loads the wallet and skips the uninit handshake.

Defaults are merged in this order (lowest → highest precedence):

  built-in        →  embln.conf [Daemon] / [Application Options]  →  CLI flags
"""

import argparse
import os
import signal
import sys
import threading

from src.bin._common import (
    add_datadir_arg,
    add_log_level_args,
    default_db_path,
    default_emblnd_datadir,
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)
from src.bitcoin.manager import WalletManager
from src.config import load_embln_conf
from src.db import Db
from src.lightning.node import EmblnNode


def _resolve_runtime(args, cfg) -> dict:
    """Merge embln.conf [Daemon] section into CLI args. CLI values win;
    `None` from argparse means 'not provided'."""
    daemon_cfg = cfg.get("Daemon", {})
    datadir = args.datadir or daemon_cfg.get("datadir")
    if datadir is None:
        if args.alias:
            datadir = default_emblnd_datadir(args.alias)
        else:
            raise SystemExit(
                "--datadir is required when --alias is not provided "
                "(or set [Daemon] datadir= in embln.conf)"
            )
    db = args.db or daemon_cfg.get("db") or default_db_path()
    return {
        "datadir": datadir,
        "db": db,
        "p2p_port": args.p2p_port or daemon_cfg.get("p2p_port"),
        "rpc_port": args.rpc_port or daemon_cfg.get("rpc_port"),
        "log_file": args.log_file or daemon_cfg.get("logfile"),
    }


def _try_warm_restart(alias: str, password: str, db_path: str) -> WalletManager:
    """If both alias and password are given AND the wallet already exists
    in the DB, load it. Otherwise return None — emblnd starts uninit and
    awaits the walletcreate RPC."""
    if not alias or not password:
        return None
    db = Db(db_path)
    try:
        record = db.get_wallet(alias)
    finally:
        db.close()
    if record is None:
        return None
    return WalletManager(alias=alias, password=password, db_path=db_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="emblnd", description=__doc__.split("\n", 1)[0]
    )
    parser.add_argument(
        "--alias",
        help="wallet alias for warm restart "
        "(omit on a fresh datadir — daemon starts uninit)",
    )
    parser.add_argument("--password", help="wallet password (or env EMBLN_PASSWORD)")
    parser.add_argument("--db", help="SQLite path (or env EMBLN_DB)")
    add_datadir_arg(parser)
    parser.add_argument(
        "--p2p-port",
        type=int,
        dest="p2p_port",
        help="LN P2P listen port (default: from embln.conf, else random)",
    )
    parser.add_argument(
        "--rpc-port",
        type=int,
        dest="rpc_port",
        help="JSON-RPC listen port (default: from embln.conf, else random)",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        help="path for the file MPSC sink "
        "(default: embln.conf [Daemon] logfile, else $EMBLN_LOGFILE)",
    )
    add_log_level_args(parser)
    args = parser.parse_args(argv)

    # Pull password from env if not on the CLI (matches embln-cli convention).
    if args.password is None:
        args.password = os.getenv("EMBLN_PASSWORD")

    cfg = load_embln_conf()
    runtime = _resolve_runtime(args, cfg)

    listeners = setup_mpsc_logging(
        level=resolve_log_level(args), log_file=runtime["log_file"]
    )

    # Install signal handlers BEFORE starting the listener. Otherwise a
    # SIGTERM arriving between the bind log and signal.signal() registration
    # hits Python's default handler and kills the process with exit -15.
    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())

    wallet = _try_warm_restart(args.alias, args.password, runtime["db"])
    try:
        ln = EmblnNode(wallet=wallet, db_path=runtime["db"], config=cfg)
        ln.start(
            p2p_port=runtime["p2p_port"],
            rpc_port=runtime["rpc_port"],
            datadir=runtime["datadir"],
        )
        try:
            shutdown.wait()
        finally:
            ln.stop()
    finally:
        if wallet is not None:
            wallet.close()
        teardown_mpsc_logging(listeners)
    return 0


if __name__ == "__main__":
    sys.exit(main())
