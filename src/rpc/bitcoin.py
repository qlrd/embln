"""BitcoinRpc — JSON-RPC 2.0 client for bitcoind.

All methods are dispatched dynamically through `BaseRpc.__getattr__`, so any
RPC the running bitcoind exposes (e.g. `getblockchaininfo`, `generatetoaddress`)
just works without registering it here. The static helpers below are
quality-of-life additions (per-wallet client, connection check).
"""

from src.rpc.base import BaseRpc


class BitcoinRpc(BaseRpc):
    """Talks to bitcoind's HTTP JSON-RPC endpoint."""

    DEFAULT_USER = "bitcoin"
    DEFAULT_PASSWORD = "secret"
    DEFAULT_PORT = 18443  # regtest default

    def wallet(self, name: str = "") -> "BitcoinRpc":
        """Return a clone of this client scoped to a specific wallet path
        (`/wallet/<name>` suffix), reusing the underlying HTTP session."""
        return type(self)(
            user=self.user,
            password=self._password,
            host=self.host,
            port=self.port,
            protocol=self.protocol,
            path="%s/wallet/%s" % (self.path, name),
            timeout=self.timeout,
            session=self.session,
            name=self._name,
        )

    def test_connection(self) -> bool:
        """True if `getblockchaininfo` succeeds. Used by the daemon readiness
        loop to know when bitcoind has finished its startup."""
        try:
            self.getblockchaininfo()  # pylint: disable=no-member
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.debug("test_connection failed: %s: %s", type(exc).__name__, exc)
            return False
