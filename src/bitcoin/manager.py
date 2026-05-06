import logging
import os

from embit import bip32, bip39, hashes
from embit.descriptor import Descriptor
from embit.descriptor.checksum import add_checksum
from embit.networks import NETWORKS

from src.db import Db, default_db_path


class WalletManager:
    """An on-chain wallet identified by a human-readable alias, backed by embit
    and persisted to a SQLite DB.

    `alias` matches LN parlance (the human-readable label nodes advertise via
    node_announcement). The alias is the row key in the `wallets` table.

    On construction:
      - if a wallet row with this alias already exists, load its mnemonic and
        rehydrate addresses/changes from the DB;
      - otherwise generate a fresh 128-bit-entropy BIP39 mnemonic and persist
        an empty wallet row.

    The mnemonic is stored as plaintext (suitable for a learning regtest project,
    not production) and never logged or printed.
    """

    def __init__(
        self,
        alias: str = "Alice",
        version: str = "zprv",
        password: str = "",
        db_path: str = None,
    ):
        if password == "":
            raise ValueError("You must set a password")

        self.alias = alias
        self.network = NETWORKS["regtest"]
        self.derivation = ("84h", "1h", "0h")
        self.version = version

        self.db = Db(db_path or default_db_path())

        record = self.db.get_wallet(alias)
        if record is None:
            entropy = os.urandom(16)
            self.mnemonic = bip39.mnemonic_from_bytes(entropy)
            self.db.save_wallet(alias, self.mnemonic, self.version)
        else:
            self.mnemonic = record["mnemonic"]
            self.version = record["version"]

        seed = bip39.mnemonic_to_seed(self.mnemonic, password)
        self.root = bip32.HDKey.from_seed(seed, version=self.network[self.version])
        self.prvkey = self.root.derive(self.parsed_derivation())
        self.pubkey = self.prvkey.to_public()
        self.fingerprint = hashes.hash160(self.pubkey.sec())[:4].hex()

        desc_str = "".join(
            [
                "wpkh([",
                self.fingerprint,
                "/",
                self.derivation[0],
                "/",
                self.derivation[1],
                "/",
                self.derivation[2],
                "]",
                self.pubkey.to_string(),
                "/{0,1}/*)",
            ]
        )
        self.descriptor = Descriptor.from_string(add_checksum(desc_str))

        # rehydrate addresses & changes from DB
        self.limit = 100
        self.addresses = self._load_branch(0)
        self.changes = self._load_branch(1)

        self._logger = None

    def close(self):
        """Close the underlying SQLite connection. Safe to call repeatedly."""
        if self.db is not None:
            self.db.close()
            self.db = None

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(self.alias)
        return self._logger

    def _load_branch(self, branch: int) -> list[dict]:
        out = []
        for r in self.db.list_addresses(self.alias, branch):
            derived = self.descriptor.derive(r["idx"], branch_index=branch)
            out.append(
                {
                    "pubkey": derived,
                    "script": derived.witness_script(),
                    "address": r["address"],
                    "used": r["used"],
                }
            )
        return out

    def create_new_address(self, branch: int = 0):
        bucket = self.addresses if branch == 0 else self.changes
        idx = len(bucket)
        derived = self.descriptor.derive(idx, branch_index=branch)
        address = derived.address(self.network)
        bucket.append(
            {
                "pubkey": derived,
                "script": derived.witness_script(),
                "address": address,
                "used": False,
            }
        )
        self.db.save_address(self.alias, branch, idx, address)
        return len(bucket)

    def parsed_derivation(self):
        return "/".join(d for d in self.derivation)
