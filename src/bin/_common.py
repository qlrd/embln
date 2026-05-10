"""Shared CLI helpers for emblnd / embln-cli."""

import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener

from src.bitcoin.manager import WalletManager
from src.db import default_db_path

# ---- ANSI color formatter (also used by tests) ----


class ColorFormatter(logging.Formatter):
    GRAY = "\x1b[90m"
    BLUE = "\x1b[94m"
    YELLOW = "\x1b[93m"
    WHITE = "\x1b[97m"
    RESET = "\x1b[0m"
    LEVEL_COLOR = {
        logging.DEBUG: "\x1b[96m",
        logging.INFO: "\x1b[92m",
        logging.WARNING: "\x1b[93m",
        logging.ERROR: "\x1b[91m",
        logging.CRITICAL: "\x1b[95m",
    }

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        level_color = self.LEVEL_COLOR.get(record.levelno, self.WHITE)
        msg = record.getMessage()
        if "=" in msg:
            label, _, value = msg.partition("=")
            body = "%s%s%s=%s%s%s" % (
                self.WHITE,
                label,
                self.RESET,
                self.YELLOW,
                value,
                self.RESET,
            )
        else:
            body = "%s%s%s" % (self.WHITE, msg, self.RESET)
        return "%s%s%s %s%-8s%s %s%s%s: %s" % (
            self.GRAY,
            time_str,
            self.RESET,
            level_color,
            record.levelname,
            self.RESET,
            self.BLUE,
            record.name,
            self.RESET,
            body,
        )


# ---- MPSC logging plumbing ----


def _build_listener(sink: logging.Handler) -> tuple[queue.Queue, QueueListener]:
    q: queue.Queue = queue.Queue(-1)  # unbounded
    listener = QueueListener(q, sink, respect_handler_level=True)
    return q, listener


def setup_mpsc_logging(level: int = logging.INFO, log_file: str = None) -> list:
    """Install two MPSC log channels on the root logger:
      - stream queue → colored stderr at `level`.
      - file queue   → plain log file (default: $EMBLN_LOGFILE or 'embln.log').

    Producers (any thread calling logger.*) only call `queue.put_nowait`,
    so socket/RPC handler threads never block on I/O. Each listener runs
    on its own consumer thread.

    Returns the list of QueueListeners; caller MUST `.stop()` each one
    on shutdown (drains the queue and joins the thread).
    """
    if log_file is None:
        log_file = os.getenv("EMBLN_LOGFILE") or "embln.log"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    # let handlers filter — root must let everything through
    root.setLevel(logging.DEBUG)

    # stream sink — color only when stderr is a TTY so subprocess/CI capture
    # gets plain text (substring assertions in tests, clean CI logs).
    stream_sink = logging.StreamHandler(stream=sys.stderr)
    if sys.stderr.isatty():
        stream_sink.setFormatter(ColorFormatter())
    else:
        stream_sink.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    stream_sink.setLevel(level)
    stream_q, stream_listener = _build_listener(stream_sink)

    # file sink (always on; even DEBUG records — file is for postmortem)
    file_sink = logging.FileHandler(log_file)
    file_sink.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    file_sink.setLevel(logging.DEBUG)
    file_q, file_listener = _build_listener(file_sink)

    root.addHandler(QueueHandler(stream_q))
    root.addHandler(QueueHandler(file_q))

    stream_listener.start()
    file_listener.start()
    return [stream_listener, file_listener]


def teardown_mpsc_logging(listeners: list):
    """Stop every QueueListener — drains its queue and joins the consumer
    thread. Idempotent."""
    for ln in listeners:
        try:
            ln.stop()
        except Exception:  # pylint: disable=broad-exception-caught
            pass


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def resolve_log_level(args=None) -> int:
    """Pick log level. Precedence:
    --log-level X  >  --verbose  >  EMBLN_LOGLEVEL  >  INFO.
    """
    if args is not None:
        explicit = getattr(args, "log_level", None)
        if explicit:
            level = _LEVELS.get(explicit.lower())
            if level is None:
                raise SystemExit(
                    "unknown --log-level '%s'; choose from: %s"
                    % (explicit, ", ".join(sorted(set(_LEVELS))))
                )
            return level
        if getattr(args, "verbose", False):
            return logging.DEBUG
    env = os.getenv("EMBLN_LOGLEVEL", "").lower()
    return _LEVELS.get(env, logging.INFO)


def add_log_level_args(p):
    """Attach --log-level and -v/--verbose to a parser. Both feed
    `resolve_log_level(args)`; --log-level wins when both are passed."""
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warn", "warning", "error"],
        help="logging level (overrides EMBLN_LOGLEVEL and --verbose)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="shortcut for --log-level debug",
    )


def default_emblnd_datadir(alias: str) -> str:
    """Stable per-alias datadir used by emblnd / embln-cli at runtime.

    Tests construct EmblndDaemon directly with explicit `datadir=`, so they
    bypass this helper — keeping `EmblndDaemon._default_datadir` (PID-isolated)
    intact for test fixtures."""
    return os.path.join(os.path.expanduser("~/.embln"), alias)


def resolve_datadir(args) -> str:
    """`--datadir` (if the parser exposes it) > `EMBLN_DATADIR` env var
    > `~/.embln/<alias>` (the default emblnd uses).

    embln-cli does not expose `--datadir` to users — datadir comes from
    `--alias` via the default convention or from `EMBLN_DATADIR` for
    non-default locations (tests, multi-instance setups). emblnd still
    accepts `--datadir` since the daemon is what actually picks the path."""
    return (
        getattr(args, "datadir", None)
        or os.getenv("EMBLN_DATADIR")
        or default_emblnd_datadir(args.alias)
    )


def resolve_db(args) -> str:
    return args.db or os.getenv("EMBLN_DB") or default_db_path()


def resolve_password(args) -> str:
    pw = args.password or os.getenv("EMBLN_PASSWORD")
    if not pw:
        raise SystemExit("password required: pass --password or set EMBLN_PASSWORD")
    return pw


def open_wallet(args) -> WalletManager:
    return WalletManager(
        alias=args.alias, password=resolve_password(args), db_path=resolve_db(args)
    )


def add_common_args(p):
    p.add_argument("--alias", required=True, help="wallet alias")
    p.add_argument("--password", help="wallet password (or env EMBLN_PASSWORD)")
    p.add_argument("--db", help="SQLite path (or env EMBLN_DB)")


def add_datadir_arg(p):
    p.add_argument(
        "--datadir",
        help="emblnd datadir (or env EMBLN_DATADIR; default ~/.embln/<alias>)",
    )
