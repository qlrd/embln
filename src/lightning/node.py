import logging
import socket
import threading
import weakref

from embit import ec, script
from embit.transaction import TransactionOutput

from src.bitcoin.daemon import Daemon
from src.bitcoin.manager import WalletManager
from src.lightning.messages import (
    ACCEPT_CHANNEL_TYPE,
    MSG_LENGTH,
    OPEN_CHANNEL_TYPE,
    build_accept_channel,
    build_open_channel,
    parse_accept_channel,
    parse_open_channel,
)

# LND key-family identifiers used for per-channel basepoint derivation:
#   m/1017'/<coin_type>'/<family>'/0/<channel_idx>
_FAMILY_MULTISIG = 0  # funding pubkey
_FAMILY_REVOCATION = 1  # revocation_basepoint
_FAMILY_HTLC = 2  # htlc_basepoint
_FAMILY_PAYMENT = 3  # payment_basepoint
_FAMILY_DELAYED_PAYMENT = 4  # delayed_payment_basepoint
_FAMILY_REVOCATION_ROOT = 5  # source of per-commitment-secret seed (stub here)

# regtest = testnet coin type per SLIP-44
_COIN_TYPE_TESTNET = 1


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_full(conn: socket.socket, n: int, chunk: int = 4096) -> bytes:
    buf = b""
    while len(buf) < n:
        got = conn.recv(min(chunk, n - len(buf)))
        if not got:
            raise ConnectionError(f"short read: got {len(buf)} of {n}")
        buf += got
    return buf


def _read_message(conn: socket.socket) -> bytes:
    """Read one BOLT message: 2-byte type, then the type-specific length."""
    header = _read_full(conn, 2)
    msg_type = int.from_bytes(header, "big")
    expected = MSG_LENGTH.get(msg_type)
    if expected is None:
        raise ValueError(f"unknown message type {msg_type}")
    rest = _read_full(conn, expected - 2)
    return header + rest


class LightningNode:  # pylint: disable=too-many-instance-attributes
    # Defense-in-depth registry — every live instance is tracked weakly so a
    # session-end atexit hook (or test teardown) can call stop_all_listeners()
    # and clean up any port that wasn't explicitly stopped (e.g. on crash).
    _instances: "weakref.WeakSet[LightningNode]" = weakref.WeakSet()

    """A Lightning node bound to exactly one WalletManager and (optionally) one
    bitcoind Daemon as its chain backend, plus an in-process TCP server for
    incoming LN P2P messages (open_channel, accept_channel for now).

    Channel keys live at LND-style paths
    (m/1017'/<coin_type>'/<family>'/0/<channel_idx>).

    Chain interactions go
    through `self.rpc` —- daemon must be attached.

    The LN listener is started by `start()` and stopped by `stop()`; until then
    the node is construction-only state.
    """

    def __init__(
        self,
        wallet: WalletManager,
        daemon: Daemon = None,
        port: int = None,
        alias: str = None,
        color_rgb: bytes = None,
    ):
        self.wallet = wallet
        self.daemon = daemon
        self.port = port  # LN listen port; allocated lazily in start()
        # node_announcement-style identity (real LN advertises both)
        self.alias = alias if alias is not None else wallet.alias
        self.color_rgb = color_rgb if color_rgb is not None else b"\x00\x00\x00"
        self.channels = self._load_channels()
        # last accept_channel received as initiator, keyed by temporary_channel_id
        self.handshakes: dict[bytes, dict] = {}
        # BOLT 2 channel-policy defaults — override per-instance to customize.
        # Both message builders consume these as kwargs; per-call **kwargs win.
        self.open_channel_defaults = {
            "funding_satoshis": 100_000_000,
            "push_msat": 0,
            "dust_limit_satoshis": 546,
            "htlc_minimum_msat": 1000,
            "feerate_per_kw": 250,
            "to_self_delay": 144,
            "max_accepted_htlcs": 483,
            "channel_flags": 0,
        }
        self.accept_channel_defaults = {
            "minimum_depth": 3,
        }
        self._server_socket: socket.socket = None
        self._server_thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._logger = None
        type(self)._instances.add(self)

    @classmethod
    def stop_all_listeners(cls):
        """Stop every live LightningNode that has an open listener. Safe to
        call from atexit / session teardown to reclaim leaked ports."""
        for node in list(cls._instances):
            if node._server_socket is not None or node._server_thread is not None:
                try:
                    node.stop()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

    # ---- chain backend ----

    @property
    def rpc(self):
        if self.daemon is None:
            raise RuntimeError(
                f"LightningNode '{self.alias}' has no daemon attached; "
                "construct with LightningNode(wallet, daemon=...) for chain access."
            )
        return self.daemon.rpc

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(f"{self.alias}.ln")
        return self._logger

    # ---- LN P2P server lifecycle ----

    def start(self):
        if self._server_thread is not None:
            self.logger.debug("already started; ignoring start()")
            return
        if self.port is None:
            self.port = _free_port()
        self._stop_event.clear()
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", self.port))
        self._server_socket.listen(8)
        self._log_config()
        self._server_thread = threading.Thread(
            target=self._serve, name=f"{self.alias}.ln-server", daemon=True
        )
        self._server_thread.start()
        self.logger.info("started")

    def _log_config(self):
        """Log this node's runtime configuration; one key=value per line so the
        color formatter in tests highlights each value separately."""
        self.logger.info("alias=%s", self.alias)
        self.logger.info("color_rgb=%s", self.color_rgb.hex())
        self.logger.info("listen=127.0.0.1:%s", self.port)
        self.logger.info("wallet=%s", self.wallet.alias)
        self.logger.info("network=%s", self.wallet.network["name"])
        self.logger.info("fingerprint=%s", self.wallet.fingerprint)
        self.logger.info("channels=%s", len(self.channels))
        if self.daemon is not None:
            self.logger.info("daemon=attached")
            self.logger.info("rpcport=%s", self.daemon.rpcport)
            self.logger.info("datadir=%s", self.daemon.datadir)
        else:
            self.logger.info("daemon=none")
        for k, v in self.open_channel_defaults.items():
            self.logger.debug("open_channel.%s=%s", k, v)
        for k, v in self.accept_channel_defaults.items():
            self.logger.debug("accept_channel.%s=%s", k, v)

    def stop(self):
        if self._server_thread is None and self._server_socket is None:
            self.logger.debug("not running; ignoring stop()")
            return
        self.logger.info("stopping")
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError as e:
                self.logger.error("error closing server socket: %s", e)
            self._server_socket = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=2)
            if self._server_thread.is_alive():
                self.logger.warning("server thread did not join within 2s")
            self._server_thread = None
        self.logger.info("stopped")

    def _serve(self):
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._server_socket.accept()
            except OSError:
                return  # socket closed during shutdown
            threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            ).start()

    def _handle_connection(self, conn: socket.socket):
        with conn:
            try:
                msg = _read_message(conn)
            except (ConnectionError, ValueError) as e:
                self.logger.warning("bad incoming frame: %s", e)
                return
            msg_type = int.from_bytes(msg[:2], "big")
            self.logger.debug("recv msg_type=%s len=%s", msg_type, len(msg))
            if msg_type == OPEN_CHANNEL_TYPE:
                response = self._handle_open_channel(parse_open_channel(msg))
                conn.sendall(response)
                self.logger.debug(
                    "sent msg_type=%s len=%s",
                    int.from_bytes(response[:2], "big"),
                    len(response),
                )
            elif msg_type == ACCEPT_CHANNEL_TYPE:
                self._handle_accept_channel(parse_accept_channel(msg))
            else:
                self.logger.warning("unhandled msg type %s", msg_type)

    # ---- LN message handlers ----

    def _handle_open_channel(self, parsed: dict) -> bytes:
        """Return accept_channel bytes built in response to a peer's open_channel."""
        self.logger.info(
            "recv open_channel funding_satoshis=%s", parsed["funding_satoshis"]
        )
        return self.create_accept_channel_msg(parsed)

    def _handle_accept_channel(self, parsed: dict):
        """Store accept_channel by temporary_channel_id for the initiating side."""
        self.logger.info(
            "recv accept_channel temp_id=%s", parsed["temporary_channel_id"].hex()
        )
        self.handshakes[parsed["temporary_channel_id"]] = parsed

    # ---- LN P2P client ----

    def open_channel(self, peer: "LightningNode", **kwargs) -> dict:
        """Send open_channel to peer and return the parsed accept_channel reply.

        Synchronous request/response over a single TCP connection. The accept
        is also stored in `self.handshakes`, keyed by temporary_channel_id.
        """
        if peer.port is None:
            raise RuntimeError(f"peer '{peer.alias}' has not started its server")
        msg = self.create_open_channel_msg(**kwargs)
        with socket.create_connection(("127.0.0.1", peer.port)) as s:
            s.sendall(msg)
            response = _read_message(s)
        parsed = parse_accept_channel(response)
        self.handshakes[parsed["temporary_channel_id"]] = parsed
        return parsed

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

    def _channel_pubkey(self, family: int, channel_idx: int) -> ec.PublicKey:
        path = f"m/1017h/{_COIN_TYPE_TESTNET}h/{family}h/0/{channel_idx}"
        return self.wallet.root.derive(path).key.get_public_key()

    def derive_funding_pubkey(self, channel_idx: int = None) -> ec.PublicKey:
        if channel_idx is None:
            channel_idx = len(self.channels)
        return self._channel_pubkey(_FAMILY_MULTISIG, channel_idx)

    def create_fundingtx_output(
        self, peer_funding_pubkey: ec.PublicKey, amount_sat: int
    ) -> TransactionOutput:
        channel_idx = len(self.channels)
        pk1 = self.derive_funding_pubkey(channel_idx)
        ordered = sorted([pk1, peer_funding_pubkey], key=lambda k: k.sec())

        witness_script = script.multisig(2, ordered)
        script_pubkey = script.p2wsh(witness_script)
        address = script_pubkey.address(self.wallet.network)

        self.channels.append(
            {
                "witness_script": witness_script,
                "script_pubkey": script_pubkey,
                "address": address,
                "amount_sat": amount_sat,
                "used": False,
            }
        )
        self.wallet.db.save_channel(
            self.wallet.alias,
            channel_idx,
            address,
            witness_script.data,
            script_pubkey.data,
            amount_sat,
        )
        return TransactionOutput(amount_sat, script_pubkey)

    # ---- BOLT 2 message construction (thin wrappers around messages.py) ----

    def _basepoints(self, channel_idx: int) -> dict:
        return {
            "funding_pubkey": self.derive_funding_pubkey(channel_idx).sec(),
            "revocation_basepoint": self._channel_pubkey(
                _FAMILY_REVOCATION, channel_idx
            ).sec(),
            "payment_basepoint": self._channel_pubkey(
                _FAMILY_PAYMENT, channel_idx
            ).sec(),
            "delayed_payment_basepoint": self._channel_pubkey(
                _FAMILY_DELAYED_PAYMENT, channel_idx
            ).sec(),
            "htlc_basepoint": self._channel_pubkey(_FAMILY_HTLC, channel_idx).sec(),
            # Stub: real LN derives this from a per-commitment-secret seed.
            "first_per_commitment_point": self._channel_pubkey(
                _FAMILY_REVOCATION_ROOT, channel_idx
            ).sec(),
        }

    def create_open_channel_msg(self, **kwargs) -> bytes:
        merged = {**self.open_channel_defaults, **kwargs}
        return build_open_channel(self._basepoints(len(self.channels)), **merged)

    def create_accept_channel_msg(self, open_channel: dict, **kwargs) -> bytes:
        merged = {**self.accept_channel_defaults, **kwargs}
        return build_accept_channel(
            self._basepoints(len(self.channels)), open_channel, **merged
        )
