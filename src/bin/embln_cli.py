"""embln-cli — short-lived CLI for stateless embln operations.

Subcommands:
  wallet create   Create a wallet (or load existing) and print its config.
  wallet address  Derive the next on-chain BIP84 address.
  wallet info     Print wallet config (alias, fingerprint, address counts).
  node info       Print the LightningNode config without starting any server.

Long-running operations (start the LN listener) live in `emblnd`.
"""

import argparse
import sys

from src.bin._common import (
    add_common_args,
    add_log_level_args,
    open_wallet,
    resolve_log_level,
    setup_mpsc_logging,
    teardown_mpsc_logging,
)
from src.lightning.node import LightningNode


def cmd_wallet_create(args) -> int:
    wm = open_wallet(args)
    try:
        print("alias=%s" % wm.alias)
        print("fingerprint=%s" % wm.fingerprint)
        print("network=%s" % wm.network["name"])
        print("addresses=%s" % len(wm.addresses))
        print("changes=%s" % len(wm.changes))
        print("(mnemonic is persisted to %s; never displayed)" % wm.db.path)
    finally:
        wm.close()
    return 0


def cmd_wallet_address(args) -> int:
    wm = open_wallet(args)
    try:
        wm.create_new_address(branch=args.branch)
        addr = wm.addresses[-1] if args.branch == 0 else wm.changes[-1]
        print(addr["address"])
    finally:
        wm.close()
    return 0


def cmd_wallet_info(args) -> int:
    wm = open_wallet(args)
    try:
        print("alias=%s" % wm.alias)
        print("fingerprint=%s" % wm.fingerprint)
        print("network=%s" % wm.network["name"])
        print("derivation=%s" % "/".join(wm.derivation))
        print("addresses=%s" % len(wm.addresses))
        for i, a in enumerate(wm.addresses):
            print("  addr[%s]=%s" % (i, a["address"]))
        print("changes=%s" % len(wm.changes))
        for i, a in enumerate(wm.changes):
            print("  change[%s]=%s" % (i, a["address"]))
    finally:
        wm.close()
    return 0


def cmd_node_info(args) -> int:
    wm = open_wallet(args)
    try:
        ln = LightningNode(wm)
        print("alias=%s" % ln.alias)
        print("color_rgb=%s" % ln.color_rgb.hex())
        print("channels=%s" % len(ln.channels))
        print("open_channel defaults:")
        for k, v in ln.open_channel_defaults.items():
            print("  %s=%s" % (k, v))
        print("accept_channel defaults:")
        for k, v in ln.accept_channel_defaults.items():
            print("  %s=%s" % (k, v))
    finally:
        wm.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embln-cli", description=__doc__.split("\n", 1)[0]
    )
    add_log_level_args(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    wallet = sub.add_parser("wallet", help="wallet operations")
    wallet_sub = wallet.add_subparsers(dest="subcmd", required=True)

    p_create = wallet_sub.add_parser("create", help="create or load a wallet")
    add_common_args(p_create)
    p_create.set_defaults(func=cmd_wallet_create)

    p_address = wallet_sub.add_parser("address", help="derive next address")
    add_common_args(p_address)
    p_address.add_argument(
        "--branch",
        type=int,
        choices=[0, 1],
        default=0,
        help="0 = receive (default), 1 = change",
    )
    p_address.set_defaults(func=cmd_wallet_address)

    p_info = wallet_sub.add_parser("info", help="print wallet info")
    add_common_args(p_info)
    p_info.set_defaults(func=cmd_wallet_info)

    node = sub.add_parser("node", help="lightning node operations")
    node_sub = node.add_subparsers(dest="subcmd", required=True)

    p_node_info = node_sub.add_parser("info", help="print LN node config")
    add_common_args(p_node_info)
    p_node_info.set_defaults(func=cmd_node_info)

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
