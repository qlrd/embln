"""Unified config loader for bitcoin.conf and embln.conf.

Resolution order for a config file `name`:
  1. explicit `datadir` argument, if given
  2. $EMBLN_BACKEND
  3. current working directory

Returns {} when nothing is found — config is optional. Built-in defaults
(`DEFAULT_OPEN_CHANNEL`, `DEFAULT_ACCEPT_CHANNEL`) mirror the runtime
defaults baked into `EmblnNode`.
"""

import configparser
import os
from pathlib import Path
from typing import Optional

BITCOIN_CONF_NAME = "bitcoin.conf"
EMBLN_CONF_NAME = "embln.conf"

# BOLT 2 mandatory open_channel field defaults — match
# EmblnNode.open_channel_defaults at construction time.
DEFAULT_OPEN_CHANNEL = {
    "funding_satoshis": 100_000_000,
    "push_msat": 0,
    "dust_limit_satoshis": 546,
    "htlc_minimum_msat": 1000,
    "feerate_per_kw": 250,
    "to_self_delay": 144,
    "max_accepted_htlcs": 483,
    "channel_flags": 0,
}

# BOLT 2 mandatory accept_channel field defaults.
DEFAULT_ACCEPT_CHANNEL = {
    "minimum_depth": 3,
}


def resolve_path(name: str, datadir: Optional[str] = None) -> Optional[str]:
    """Find `name` in datadir → $EMBLN_BACKEND → cwd. Returns None if absent."""
    candidates = []
    if datadir:
        candidates.append(Path(datadir) / name)
    backend = os.getenv("EMBLN_BACKEND")
    if backend:
        candidates.append(Path(backend) / name)
    candidates.append(Path(name))
    for p in candidates:
        if p.is_file():
            return str(p.resolve())
    return None


def _coerce(value: str):
    """Parse INI string values into int / bool / str."""
    s = value.strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        return int(s)
    except ValueError:
        return s


def _read_ini(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # preserve case (lnd-style keys mix _ and -)
    cp.read(path)
    return cp


def _merge_section(cp: configparser.ConfigParser, name: str, defaults: dict) -> dict:
    out = dict(defaults)
    if name in cp:
        for k, v in cp.items(name):
            out[k] = _coerce(v)
    return out


def load_bitcoin_conf(datadir: Optional[str] = None) -> dict:
    """Load bitcoin.conf into {section: {key: value}}.

    bitcoind merges all sections at runtime; we keep them separate so callers
    can pick the per-network values they need.
    """
    path = resolve_path(BITCOIN_CONF_NAME, datadir)
    if path is None:
        return {}
    cp = _read_ini(path)
    return {
        section: {k: _coerce(v) for k, v in cp.items(section)}
        for section in cp.sections()
    }


def load_embln_conf(datadir: Optional[str] = None) -> dict:
    """Load embln.conf with channel-policy defaults filled in.

    Sections recognized:
      [Application Options]  alias, color (node_announcement identity)
      [Daemon]               datadir, db, p2p_port, rpc_port, logfile
                             (emblnd process / on-disk paths)
      [Bitcoin]              bitcoin.network
      [bitcoind]             bitcoind.rpchost / .rpcport / .rpcuser / .rpcpassword
      [open_channel]         BOLT 2 open_channel mandatory-field defaults
      [accept_channel]       BOLT 2 accept_channel mandatory-field defaults

    Missing file is fine — the caller still gets the built-in channel-policy
    defaults so EmblnNode construction works out of the box.
    """
    path = resolve_path(EMBLN_CONF_NAME, datadir)
    cp = (
        _read_ini(path)
        if path
        else configparser.ConfigParser(strict=False, interpolation=None)
    )

    def _section(name):
        return {k: _coerce(v) for k, v in cp.items(name)} if name in cp else {}

    return {
        "Application Options": _section("Application Options"),
        "Daemon": _section("Daemon"),
        "Bitcoin": _section("Bitcoin"),
        "bitcoind": _section("bitcoind"),
        "open_channel": _merge_section(cp, "open_channel", DEFAULT_OPEN_CHANNEL),
        "accept_channel": _merge_section(cp, "accept_channel", DEFAULT_ACCEPT_CHANNEL),
        "_path": path,
    }
