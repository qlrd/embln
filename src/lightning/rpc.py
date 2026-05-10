"""EmblnRpc — JSON-RPC 2.0 client for emblnd, modelled on lncli ↔ lnd.

All methods dispatch dynamically through `BaseRpc.__getattr__`. The stub
methods declared on the class document the expected RPC surface (so IDEs
and grep show what's available) without committing to a wire format. They
all delegate to `__getattr__` via `super().__getattr__`, so the actual
network call is unchanged.

Server side: `EmblndDaemon` handles the matching method names. Methods
that are still unimplemented return JSON-RPC error -32601 ("not yet
implemented") with the method name in the message.

`EmblnRpc.from_cookie(datadir)` reads bitcoind-style `<datadir>/.cookie`
credentials so embln-cli auto-authenticates against a running emblnd
without long-lived secrets in config files.
"""

import os

from src.rpc.base import BaseRpc

COOKIE_FILENAME = ".cookie"

# pylint: disable=unnecessary-dunder-call
# The stubs below intentionally route through `self.__getattr__(name)` so
# that adding/removing a method here changes the documented surface
# without changing dispatch semantics. Replacing with `getattr(self, name)`
# would resolve back to the stub (recursion) since the same name is bound
# on the class. This is the documented pattern; quiet pylint module-wide.


class EmblnRpc(BaseRpc):
    """Talks to emblnd's HTTP JSON-RPC endpoint (control plane)."""

    DEFAULT_USER = "__cookie__"
    DEFAULT_PASSWORD = ""
    DEFAULT_PORT = 10009  # mirrors lnd's default gRPC port

    def walletcreate(self, alias: str, password: str, force: bool = False):
        """Bootstrap the wallet on a fresh emblnd. Daemon must be uninit.

        With `force=True`, an existing wallet for `alias` (and all of its
        addresses + channels) is wiped from the DB before the new one is
        created — destructive. The CLI `walletcreate` command surfaces
        this via a `--force` flag and a y/N prompt when force isn't set."""
        return self.__getattr__("walletcreate")(alias, password, force)

    def walletinfo(self):
        """Return alias, fingerprint, network, derivation path, addresses."""
        return self.__getattr__("walletinfo")()

    def walletaddress(self, branch: int = 0):
        """Derive next on-chain BIP84 address. branch=0 receive, 1 change."""
        return self.__getattr__("walletaddress")(branch)

    @classmethod
    def from_cookie(cls, datadir: str, **kwargs) -> "EmblnRpc":
        """Construct a client from `<datadir>/.cookie`.

        Format (semicolon-separated fields):
            `<user>:<password>; host=<host>; port=<port>; ...`

        Field 0 is the bitcoind-compatible auth token; subsequent
        `key=value` fields supply host/port (and any future extensions).
        Caller-supplied `host=` / `port=` kwargs win over cookie values."""
        path = os.path.join(datadir, COOKIE_FILENAME)
        with open(path, "r", encoding="ascii") as f:
            content = f.read().strip()
        fields = [s.strip() for s in content.split(";") if s.strip()]
        user, _, password = fields[0].partition(":")
        for field in fields[1:]:
            key, _, value = field.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "port":
                kwargs.setdefault("port", int(value))
            elif key:
                kwargs.setdefault(key, value)
        return cls(user=user, password=password, **kwargs)

    # ---- lnd-style stub surface ----
    #
    # These are dispatched through __getattr__ at runtime (so the server
    # doesn't need to recognize them by their wrapped form). The stubs
    # exist only to document what the server is expected to implement.

    def getinfo(self):
        """Return node identity (alias, color, listen ports, num_channels)."""
        return self.__getattr__("getinfo")()

    def listchannels(self):
        """Return all known on-chain channel funding outputs."""
        return self.__getattr__("listchannels")()

    def listpeers(self):
        """Return all currently-connected peers."""
        return self.__getattr__("listpeers")()

    def pendingchannels(self):
        """Return channels currently in the open/close handshake (NOT YET IMPLEMENTED)."""
        return self.__getattr__("pendingchannels")()

    def connectpeer(self, alias: str, host: str, port: int):
        """Open a persistent TCP connection to peer `alias` at host:port and
        send our HANDSHAKE frame so they register us as inbound."""
        return self.__getattr__("connectpeer")(alias, host, port)

    def disconnectpeer(self, alias: str):
        """Close the TCP connection for peer `alias`."""
        return self.__getattr__("disconnectpeer")(alias)

    def openchannel(self, peer_pubkey: str, local_funding_amount: int):
        """Initiate the BOLT 2 open_channel handshake (NOT YET IMPLEMENTED)."""
        return self.__getattr__("openchannel")(peer_pubkey, local_funding_amount)

    def closechannel(self, channel_point: str):
        """Begin a cooperative or force close (NOT YET IMPLEMENTED)."""
        return self.__getattr__("closechannel")(channel_point)
