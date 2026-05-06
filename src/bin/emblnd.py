"""emblnd — embln Lightning daemon.

Long-running process: opens a wallet, starts the LN P2P listener (and
optionally a bitcoind backend), and blocks until SIGINT or SIGTERM.

Stateless wallet operations (create, address, info) live in `embln-cli`.
"""

import argparse
import signal
import sys
import threading

from src.bin._common import (
    add_common_args,
    add_log_level_args,
    open_wallet,
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)
from src.lightning.node import LightningNode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="emblnd", description=__doc__.split("\n", 1)[0]
    )
    add_common_args(parser)
    parser.add_argument(
        "--port", type=int, help="LN listen port (default: random free port)"
    )
    parser.add_argument(
        "--log-file",
        help="path for the file MPSC sink (default: $EMBLN_LOGFILE or embln.log)",
    )
    add_log_level_args(parser)
    args = parser.parse_args(argv)

    listeners = setup_mpsc_logging(
        level=resolve_log_level(args), log_file=args.log_file
    )

    # Install signal handlers BEFORE starting the listener. Otherwise a
    # SIGTERM arriving between the bind log and signal.signal() registration
    # hits Python's default handler and kills the process with exit -15.
    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())

    wm = open_wallet(args)
    try:
        ln = LightningNode(wm, port=args.port)
        ln.start()  # logs full config
        try:
            shutdown.wait()
        finally:
            ln.stop()
    finally:
        wm.close()
        teardown_mpsc_logging(listeners)
    return 0


if __name__ == "__main__":
    sys.exit(main())
