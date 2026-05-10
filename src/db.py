import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wallets (
    alias     TEXT PRIMARY KEY,
    mnemonic  TEXT NOT NULL,
    version   TEXT NOT NULL DEFAULT 'zprv'
);

CREATE TABLE IF NOT EXISTS addresses (
    wallet  TEXT NOT NULL,
    branch  INTEGER NOT NULL,
    idx     INTEGER NOT NULL,
    address TEXT NOT NULL,
    used    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, branch, idx),
    FOREIGN KEY (wallet) REFERENCES wallets(alias)
);

CREATE TABLE IF NOT EXISTS channels (
    wallet         TEXT    NOT NULL,
    channel_idx    INTEGER NOT NULL,
    address        TEXT    NOT NULL,
    witness_script BLOB    NOT NULL,
    script_pubkey  BLOB    NOT NULL,
    amount_sat     INTEGER NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, channel_idx),
    FOREIGN KEY (wallet) REFERENCES wallets(alias)
);
"""


def default_db_path() -> str:
    backend = os.getenv("EMBLN_BACKEND")
    if not backend:
        raise RuntimeError(
            "EMBLN_BACKEND must be set to use the default DB path; "
            "or pass db_path explicitly"
        )
    return os.path.join(backend, "data", "embln.db")


class Db:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        logger.debug("opened path=%s", path)

    def close(self):
        logger.debug("closing path=%s", self.path)
        self._conn.close()

    # --- wallets ---

    def get_wallet(self, alias: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT mnemonic, version FROM wallets WHERE alias=?", (alias,)
        ).fetchone()
        logger.debug("get_wallet alias=%s found=%s", alias, row is not None)
        if row is None:
            return None
        return {"mnemonic": row[0], "version": row[1]}

    def save_wallet(self, alias: str, mnemonic: str, version: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO wallets (alias, mnemonic, version) VALUES (?, ?, ?)",
            (alias, mnemonic, version),
        )
        self._conn.commit()
        logger.debug("save_wallet alias=%s version=%s", alias, version)

    # --- addresses ---

    def list_addresses(self, wallet: str, branch: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT idx, address, used FROM addresses "
            "WHERE wallet=? AND branch=? ORDER BY idx",
            (wallet, branch),
        ).fetchall()
        logger.debug(
            "list_addresses wallet=%s branch=%s count=%s", wallet, branch, len(rows)
        )
        return [{"idx": r[0], "address": r[1], "used": bool(r[2])} for r in rows]

    def delete_wallet(self, alias: str):
        """Cascade-delete a wallet's record + all of its addresses + channels.
        Used by `walletcreate(force=True)` so the daemon can recreate a
        wallet from scratch without colliding with the previous mnemonic."""
        self._conn.execute("DELETE FROM channels WHERE wallet=?", (alias,))
        self._conn.execute("DELETE FROM addresses WHERE wallet=?", (alias,))
        self._conn.execute("DELETE FROM wallets WHERE alias=?", (alias,))
        self._conn.commit()
        logger.warning("delete_wallet alias=%s (cascaded)", alias)

    def save_address(self, wallet: str, branch: int, idx: int, address: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO addresses (wallet, branch, idx, address) "
            "VALUES (?, ?, ?, ?)",
            (wallet, branch, idx, address),
        )
        self._conn.commit()
        logger.debug(
            "save_address wallet=%s branch=%s idx=%s address=%s",
            wallet,
            branch,
            idx,
            address,
        )

    # --- channels ---

    def list_channels(self, wallet: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT channel_idx, address, witness_script, script_pubkey, amount_sat, used "
            "FROM channels WHERE wallet=? ORDER BY channel_idx",
            (wallet,),
        ).fetchall()
        logger.debug("list_channels wallet=%s count=%s", wallet, len(rows))
        return [
            {
                "channel_idx": r[0],
                "address": r[1],
                "witness_script": r[2],
                "script_pubkey": r[3],
                "amount_sat": r[4],
                "used": bool(r[5]),
            }
            for r in rows
        ]

    def save_channel(
        self,
        wallet: str,
        channel_idx: int,
        address: str,
        witness_script: bytes,
        script_pubkey: bytes,
        amount_sat: int,
    ):
        self._conn.execute(
            "INSERT OR IGNORE INTO channels "
            "(wallet, channel_idx, address, witness_script, script_pubkey, amount_sat) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wallet, channel_idx, address, witness_script, script_pubkey, amount_sat),
        )
        self._conn.commit()
        logger.debug(
            "save_channel wallet=%s channel_idx=%s amount_sat=%s",
            wallet,
            channel_idx,
            amount_sat,
        )
