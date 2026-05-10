"""embln-cli — short-lived CLI for embln operations, all over JSON-RPC.

`embln-cli` never touches the SQLite DB directly — every command goes
through the running emblnd's RPC listener, mirroring `bitcoin-cli` ↔ bitcoind.
The datadir is derived from `--alias` (default `~/.embln/<alias>`); cookie
credentials and the RPC endpoint are then read from `<datadir>/.cookie`.
For non-default locations (tests, multi-instance setups) export
`EMBLN_DATADIR`.

CLI commands match the RPC method names 1:1 (no aliasing, no subgroups).
All output is JSON to stdout — pipe through `jq` for human reading.

Commands:
  walletcreate     Bootstrap the running emblnd's wallet.
                   Refuses if a wallet for the alias exists in the DB; pass
                   --force or answer `y` at the prompt to wipe + recreate.
  walletaddress    Derive the next on-chain BIP84 address.
  walletinfo       Print wallet config (alias, fingerprint, addresses).
  getinfo          Print emblnd's runtime config (alias, listen ports, peers).
  connectpeer      Connect to a peer at host:port.
  disconnectpeer   Close the persistent connection to a peer.
  listpeers        List currently-connected peers.
  listchannels     List on-chain channel funding outputs known to the node.
"""

import argparse
import json
import sys

from src.bin._common import (
    add_log_level_args,
    resolve_datadir,
    resolve_log_level,
    resolve_password,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)
from src.lightning.rpc import EmblnRpc
from src.rpc.base import RpcError


def _emit(payload):
    """JSON-encode `payload` to stdout with a trailing newline."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def _open_rpc(args) -> EmblnRpc:
    """Build an EmblnRpc from the cookie at the resolved datadir."""
    return EmblnRpc.from_cookie(resolve_datadir(args))


def _confirm(prompt: str) -> bool:
    """Read `y`/`Y` from stdin (default: no). Used to gate destructive ops."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer == "y"


# ---- command handlers (1:1 with RPC method names) ----


def cmd_walletcreate(args) -> int:
    rpc = _open_rpc(args)
    password = resolve_password(args)
    try:
        _emit(rpc.walletcreate(args.alias, password, args.force))
        return 0
    except RpcError as exc:
        msg = str(exc)
        if "already exists" not in msg or args.force:
            raise
        # Non-interactive (scripts, CI, tests): don't hang waiting for
        # input on a prompt nobody can answer. Print a clear hint and exit.
        if not sys.stdin.isatty():
            sys.stderr.write(
                "wallet '%s' already exists. Pass --force to overwrite.\n" % args.alias
            )
            return 1
        # Interactive: prompt the user before the destructive overwrite.
        if not _confirm("wallet '%s' already exists. Overwrite? [y/N]: " % args.alias):
            sys.stderr.write("aborted\n")
            return 1
        _emit(rpc.walletcreate(args.alias, password, True))
        return 0


def cmd_walletaddress(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.walletaddress(args.branch))
    return 0


def cmd_walletinfo(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.walletinfo())
    return 0


def cmd_getinfo(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.getinfo())
    return 0


def cmd_connectpeer(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.connectpeer(args.peer_alias, args.host, args.port))
    return 0


def cmd_disconnectpeer(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.disconnectpeer(args.peer_alias))
    return 0


def cmd_listpeers(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.listpeers())
    return 0


def cmd_listchannels(args) -> int:
    rpc = _open_rpc(args)
    _emit(rpc.listchannels())
    return 0


def _add_alias(p):
    """Every subcommand needs --alias. The datadir is derived from it
    (`~/.embln/<alias>`); override with `EMBLN_DATADIR` env var if needed."""
    p.add_argument(
        "--alias",
        required=True,
        help="wallet alias (datadir derived as ~/.embln/<alias>)",
    )


def build_parser() -> argparse.ArgumentParser:  # pylint: disable=too-many-locals,too-many-statements
    parser = argparse.ArgumentParser(
        prog="embln-cli", description=__doc__.split("\n", 1)[0]
    )
    add_log_level_args(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser(
        "walletcreate", help="bootstrap the wallet on the running daemon"
    )
    _add_alias(p_create)
    p_create.add_argument("--password", help="wallet password (or env EMBLN_PASSWORD)")
    p_create.add_argument(
        "--force",
        action="store_true",
        help="wipe + recreate if a wallet for this alias exists (destructive)",
    )
    p_create.set_defaults(func=cmd_walletcreate)

    p_addr = sub.add_parser("walletaddress", help="derive next on-chain address")
    _add_alias(p_addr)
    p_addr.add_argument(
        "--branch",
        type=int,
        choices=[0, 1],
        default=0,
        help="0 = receive (default), 1 = change",
    )
    p_addr.set_defaults(func=cmd_walletaddress)

    p_winfo = sub.add_parser("walletinfo", help="print wallet info")
    _add_alias(p_winfo)
    p_winfo.set_defaults(func=cmd_walletinfo)

    p_getinfo = sub.add_parser("getinfo", help="print emblnd runtime config")
    _add_alias(p_getinfo)
    p_getinfo.set_defaults(func=cmd_getinfo)

    p_conn = sub.add_parser("connectpeer", help="connect to a peer")
    _add_alias(p_conn)
    p_conn.add_argument("peer_alias", help="alias of the remote peer")
    p_conn.add_argument("host", help="peer host (e.g. 127.0.0.1)")
    p_conn.add_argument("port", type=int, help="peer LN P2P port")
    p_conn.set_defaults(func=cmd_connectpeer)

    p_disc = sub.add_parser("disconnectpeer", help="disconnect a peer")
    _add_alias(p_disc)
    p_disc.add_argument("peer_alias", help="alias of the remote peer")
    p_disc.set_defaults(func=cmd_disconnectpeer)

    p_lp = sub.add_parser("listpeers", help="list connected peers")
    _add_alias(p_lp)
    p_lp.set_defaults(func=cmd_listpeers)

    p_lc = sub.add_parser("listchannels", help="list channels known to the node")
    _add_alias(p_lc)
    p_lc.set_defaults(func=cmd_listchannels)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    listeners = setup_mpsc_logging(level=resolve_log_level(args))
    try:
        return args.func(args)
    finally:
        teardown_mpsc_logging(listeners)


if __name__ == "__main__":
    sys.exit(main())
