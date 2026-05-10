"""EmblnNode — pure protocol state.

Holds the wallet, channel list, BOLT 2 message-policy defaults and the
LND-style key-derivation logic. Knows how to *build* and *interpret* BOLT
messages but does not run any listener — that's `EmblndDaemon`'s job.

The convenience methods `start()` / `stop()` / `port` / `open_channel(peer)`
are thin facades that lazily create + own an `EmblndDaemon`. Tests and
`emblnd` either go through them or talk to `EmblndDaemon` directly. The
weakref registry that powers session-end cleanup lives on `BaseDaemon`,
so leaked LN listeners are reclaimed by `BaseDaemon.stop_all_running()`
the same way leaked bitcoind processes are.
"""

import logging
import socket
import threading

from embit import ec, script
from embit.transaction import TransactionOutput

from src.bitcoin.manager import WalletManager
from src.config import load_embln_conf
from src.daemons.bitcoind import BitcoindDaemon
from src.lightning.funding_tx import (
    FAMILY_DELAYED_PAYMENT,
    FAMILY_HTLC,
    FAMILY_PAYMENT,
    FAMILY_REVOCATION,
    FAMILY_REVOCATION_ROOT,
    FundingTransaction,
)
from src.lightning.messages import (
    MSG_LENGTH,
    build_accept_channel,
    build_handshake,
    build_open_channel,
    parse_accept_channel,
)


def _read_full(conn: socket.socket, n: int, chunk: int = 4096) -> bytes:
    buf = b""
    while len(buf) < n:
        got = conn.recv(min(chunk, n - len(buf)))
        if not got:
            raise ConnectionError("short read: got %s of %s" % (len(buf), n))
        buf += got
    return buf


def _read_message(conn: socket.socket) -> bytes:
    header = _read_full(conn, 2)
    msg_type = int.from_bytes(header, "big")
    expected = MSG_LENGTH.get(msg_type)
    if expected is None:
        raise ValueError("unknown message type %s" % msg_type)
    rest = _read_full(conn, expected - 2)
    return header + rest


class EmblnNode:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Protocol-state object: wallet, channels, key derivation, message builders.

    Two states:
      - **uninit**: `wallet=None`, `db_path` set. The node carries enough
        configuration (datadir, channel-policy defaults, peer registry) for
        the EmblndDaemon to run, but wallet-bound operations
        (`derive_funding_pubkey`, `connect_peer`, `create_open_channel_msg`,
        ...) raise until `initialize(wallet)` is called.
      - **initialized**: `wallet` set. All operations work.

    The bitcoind-style `walletcreate` RPC is what flips a freshly-spawned
    emblnd from uninit → initialized, so the CLI (`embln-cli wallet create`)
    never touches the SQLite DB directly. Server I/O lives in `EmblndDaemon`.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        wallet: WalletManager = None,
        bitcoind: BitcoindDaemon = None,
        port: int = None,
        alias: str = None,
        color_rgb: bytes = None,
        config: dict = None,
        db_path: str = None,
    ):
        if wallet is None and db_path is None:
            raise ValueError(
                "EmblnNode requires either a wallet or a db_path "
                "(for the future walletcreate RPC bootstrap)"
            )
        self.bitcoind = bitcoind

        # node_announcement-style identity. Uninit → alias=None, switched on
        # initialize() to wallet.alias (unless caller pinned alias= explicitly).
        self._explicit_alias = alias
        self.color_rgb = color_rgb if color_rgb is not None else b"\x00\x00\x00"

        # last accept_channel received as initiator, keyed by temporary_channel_id
        self.pending_accepts: dict[bytes, dict] = {}

        # BOLT 2 channel-policy defaults — sourced from embln.conf when present,
        # else the built-in defaults in src.config. Per-call **kwargs to
        # create_open_channel_msg / create_accept_channel_msg still win.
        cfg = config if config is not None else load_embln_conf()
        self.open_channel_defaults = dict(cfg["open_channel"])
        self.accept_channel_defaults = dict(cfg["accept_channel"])

        # Funding-output / channel-key helper. Mutates self.channels and
        # self.wallet.db; one helper per node.
        self.funding_tx = FundingTransaction(self)

        # Persistent peer connections, keyed by remote alias. Each entry:
        # ("alias", "host", "port", "sock", "direction") where
        # direction is "outbound" (we connected) or "inbound" (they did).
        # Mutated by connect_peer / disconnect_peer / EmblndDaemon's accept
        # loop; protected by _peer_lock.
        self.peers: dict[str, dict] = {}
        self._peer_lock = threading.Lock()

        # Lazily-owned LN server (created by start(), reaped by stop()).
        self._emblnd = None
        self._explicit_port = port
        self._logger = None

        # Wallet binding — set last so methods that key off `wallet` see a
        # consistent state if called from a subclass __init__ override.
        self.wallet = None
        self._db_path = db_path
        self.alias: str = alias  # may be None until initialize()
        self.channels: list[dict] = []
        if wallet is not None:
            self.initialize(wallet)

    # ---- initialization ----

    @property
    def is_initialized(self) -> bool:
        return self.wallet is not None

    def initialize(self, wallet: WalletManager):
        """Attach a wallet and load persisted channel state. Idempotent on
        the same wallet; raises on attempt to re-initialize with a different
        one."""
        if self.wallet is wallet:
            return
        if self.wallet is not None:
            raise RuntimeError("EmblnNode '%s' is already initialized" % self.alias)
        self.wallet = wallet
        if self._explicit_alias is None:
            self.alias = wallet.alias
        self.channels = self._load_channels()
        # Reset cached logger so the next access uses the alias-prefixed name.
        self._logger = None

    def _require_initialized(self):
        if self.wallet is None:
            raise RuntimeError("EmblnNode is uninitialized — call walletcreate first")

    @property
    def db_path(self) -> str:
        """Where the SQLite DB lives. Available even pre-init so the daemon
        can pass it to WalletManager() during walletcreate."""
        return self.wallet.db.path if self.wallet is not None else self._db_path

    # ---- chain backend ----

    @property
    def rpc(self):
        if self.bitcoind is None:
            raise RuntimeError(
                "EmblnNode '%s' has no bitcoind attached; "
                "construct with EmblnNode(wallet, bitcoind=...) for chain access."
                % self.alias
            )
        return self.bitcoind.rpc

    @property
    def logger(self):
        if self._logger is None:
            # Pre-init the alias is None — fall back to a generic 'embln.ln'
            # so log records still go somewhere. Reset on initialize().
            name = "%s.ln" % self.alias if self.alias else "embln.ln"
            self._logger = logging.getLogger(name)
        return self._logger

    def log_policy_defaults(self):
        """One DEBUG line per policy field. Called by EmblndDaemon at start so
        operators can grep the snapshot without enabling debug across the
        whole stack."""
        for k, v in self.open_channel_defaults.items():
            self.logger.debug("open_channel.%s=%s", k, v)
        for k, v in self.accept_channel_defaults.items():
            self.logger.debug("accept_channel.%s=%s", k, v)

    # ---- LN server facade (delegates to EmblndDaemon) ----

    @property
    def port(self) -> int:
        """LN P2P listen port — `None` until start()."""
        return self._emblnd.p2p_port if self._emblnd is not None else None

    def start(
        self,
        p2p_port: int = None,
        rpc_port: int = None,
        datadir: str = None,
    ):
        """Lazily create + start an EmblndDaemon owning the LN P2P + RPC
        listeners. Idempotent."""
        if self._emblnd is not None:
            self.logger.debug("already started; ignoring start()")
            return

        # emblnd imports from src.lightning.messages, and this
        # module is itself imported by emblnd's tests indirectly.
        # Defer the import to break the cycle.
        from src.daemons.emblnd import (
            EmblndDaemon,  # pylint: disable=import-outside-toplevel
        )

        self._emblnd = EmblndDaemon(
            self,
            datadir=datadir,
            p2p_port=p2p_port if p2p_port is not None else self._explicit_port,
            rpc_port=rpc_port,
        )
        self._emblnd.start()

    def stop(self):
        """Stop the underlying EmblndDaemon. Idempotent."""
        if self._emblnd is None:
            self.logger.debug("not running; ignoring stop()")
            return
        self._emblnd.stop()
        self._emblnd = None

    # ---- peer registry (persistent TCP connections) ----

    def connect_peer(
        self, alias: str, host: str = "127.0.0.1", port: int = None
    ) -> dict:
        """Open a persistent TCP connection to a peer and identify ourselves
        via a HANDSHAKE frame."""
        self._require_initialized()
        if port is None:
            raise ValueError("port is required to connect to peer '%s'" % alias)
        with self._peer_lock:
            existing = self.peers.get(alias)
            if existing is not None:
                return existing
            sock = socket.create_connection((host, port))
            sock.sendall(build_handshake(self.alias))
            entry = {
                "alias": alias,
                "host": host,
                "port": port,
                "sock": sock,
                "direction": "outbound",
            }
            self.peers[alias] = entry
            self.logger.info(
                "peer connected alias=%s addr=%s:%s direction=outbound",
                alias,
                host,
                port,
            )
            return entry

    def disconnect_peer(self, alias: str) -> bool:
        """Close the TCP connection for `alias` and drop the entry. Returns
        False if no such peer was connected."""
        with self._peer_lock:
            entry = self.peers.pop(alias, None)
        if entry is None:
            return False
        try:
            entry["sock"].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            entry["sock"].close()
        except OSError:
            pass
        self.logger.info("peer disconnected alias=%s", alias)
        return True

    def list_peers(self) -> list[dict]:
        """Snapshot of connected peers (sock object excluded)."""
        with self._peer_lock:
            return [
                {
                    "alias": e["alias"],
                    "host": e["host"],
                    "port": e["port"],
                    "direction": e["direction"],
                }
                for e in self.peers.values()
            ]

    def register_inbound_peer(
        self, alias: str, sock: socket.socket, host: str, port: int
    ):
        """Called by EmblndDaemon after parsing a HANDSHAKE on an accepted
        connection. If the alias is already registered (e.g. a stale outbound
        entry), the previous socket is closed first."""
        with self._peer_lock:
            existing = self.peers.pop(alias, None)
        if existing is not None:
            try:
                existing["sock"].close()
            except OSError:
                pass
            self.logger.warning(
                "duplicate peer alias=%s; replacing previous %s socket",
                alias,
                existing["direction"],
            )
        with self._peer_lock:
            self.peers[alias] = {
                "alias": alias,
                "host": host,
                "port": port,
                "sock": sock,
                "direction": "inbound",
            }
        self.logger.info(
            "peer connected alias=%s addr=%s:%s direction=inbound",
            alias,
            host,
            port,
        )

    def unregister_peer(self, alias: str):
        """Drop a peer from the registry without closing its socket — called
        by the daemon's reader loop after the remote side hangs up."""
        with self._peer_lock:
            self.peers.pop(alias, None)

    # ---- LN P2P client (initiator side) ----

    def open_channel(self, peer: "EmblnNode", **kwargs) -> dict:
        """Send open_channel to `peer` and return the parsed accept_channel.

        Synchronous request/response over a single TCP connection. The reply
        is also stored in `self.pending_accepts`, keyed by temporary_channel_id.
        """
        self._require_initialized()
        if peer.port is None:
            raise RuntimeError("peer '%s' has not started its server" % peer.alias)
        msg = self.create_open_channel_msg(**kwargs)
        with socket.create_connection(("127.0.0.1", peer.port)) as s:
            s.sendall(msg)
            response = _read_message(s)
        parsed = parse_accept_channel(response)
        self.pending_accepts[parsed["temporary_channel_id"]] = parsed
        return parsed

    # ---- BOLT message dispatch (called by EmblndDaemon on incoming frames) ----

    def on_open_channel(self, parsed: dict) -> bytes:
        """Return accept_channel bytes built in response to a peer's open_channel."""
        self.logger.info(
            "recv open_channel funding_satoshis=%s", parsed["funding_satoshis"]
        )
        return self.create_accept_channel_msg(parsed)

    def on_accept_channel(self, parsed: dict):
        """Store accept_channel by temporary_channel_id for the initiating side."""
        self.logger.info(
            "recv accept_channel temp_id=%s", parsed["temporary_channel_id"].hex()
        )
        self.pending_accepts[parsed["temporary_channel_id"]] = parsed

    # ---- channel state / keys ----

    def _load_channels(self) -> list[dict]:
        out = []
        for r in self.wallet.db.list_channels(self.wallet.alias):
            out.append(
                {
                    "witness_script": script.Script(r["witness_script"]),
                    "script_pubkey": script.Script(r["script_pubkey"]),
                    "address": r["address"],
                    "amount_sat": r["amount_sat"],
                    "used": r["used"],
                }
            )
        return out

    # ---- funding-tx convenience facades (delegate to self.funding_tx) ----

    def derive_funding_pubkey(self, channel_idx: int = None) -> ec.PublicKey:
        self._require_initialized()
        return self.funding_tx.derive_funding_pubkey(channel_idx)

    def create_fundingtx_output(
        self, peer_funding_pubkey: ec.PublicKey, amount_sat: int
    ) -> TransactionOutput:
        self._require_initialized()
        return self.funding_tx.create_fundingtx_output(peer_funding_pubkey, amount_sat)

    # ---- BOLT 2 message construction (thin wrappers around messages.py) ----

    def _basepoints(self, channel_idx: int) -> dict:
        ft = self.funding_tx
        return {
            "funding_pubkey": ft.derive_funding_pubkey(channel_idx).sec(),
            "revocation_basepoint": ft.channel_pubkey(
                FAMILY_REVOCATION, channel_idx
            ).sec(),
            "payment_basepoint": ft.channel_pubkey(FAMILY_PAYMENT, channel_idx).sec(),
            "delayed_payment_basepoint": ft.channel_pubkey(
                FAMILY_DELAYED_PAYMENT, channel_idx
            ).sec(),
            "htlc_basepoint": ft.channel_pubkey(FAMILY_HTLC, channel_idx).sec(),
            # Stub: real LN derives this from a per-commitment-secret seed.
            "first_per_commitment_point": ft.channel_pubkey(
                FAMILY_REVOCATION_ROOT, channel_idx
            ).sec(),
        }

    def create_open_channel_msg(self, **kwargs) -> bytes:
        self._require_initialized()
        merged = {**self.open_channel_defaults, **kwargs}
        return build_open_channel(self._basepoints(len(self.channels)), **merged)

    def create_accept_channel_msg(self, open_channel: dict, **kwargs) -> bytes:
        self._require_initialized()
        merged = {**self.accept_channel_defaults, **kwargs}
        return build_accept_channel(
            self._basepoints(len(self.channels)), open_channel, **merged
        )
