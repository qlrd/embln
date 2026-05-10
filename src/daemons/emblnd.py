"""EmblndDaemon — the embln Lightning daemon process.

Owns two listeners:

  - **LN P2P TCP** (default port: random free port). Accepts BOLT 2 messages
    (open_channel, accept_channel) framed by `MSG_LENGTH`, parses them,
    dispatches to the wrapped `EmblnNode` for state mutation, and
    returns the reply on the same socket.
  - **JSON-RPC HTTP** (default port: random free port). The control plane,
    consumed by `EmblnRpc` clients (embln-cli analog of lncli). Most
    method names are stubs returning JSON-RPC error -32601 ("not yet
    implemented") with their name in the message — `getinfo` and
    `listchannels` are real.

Protocol state lives on `EmblnNode`; network I/O lives here. The same
`BaseDaemon._instances` weakref registry that tracks `BitcoindDaemon`s
also tracks every `EmblndDaemon`, so `BaseDaemon.stop_all_running()`
reaps both kinds at session end.
"""

import base64
import json
import os
import secrets
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from src.bitcoin.manager import WalletManager
from src.daemons.base import BaseDaemon, free_port
from src.db import Db
from src.lightning.messages import (
    ACCEPT_CHANNEL_TYPE,
    HANDSHAKE_TYPE,
    MSG_LENGTH,
    OPEN_CHANNEL_TYPE,
    parse_accept_channel,
    parse_handshake,
    parse_open_channel,
)

# Bitcoin Core's `.cookie` convention: `<datadir>/.cookie` with perms 0600,
# regenerated each startup, removed on shutdown. emblnd keeps the file
# semantics (perms, lifecycle, regeneration) identical to bitcoind's, but
# the body is a semicolon-separated field list so embln-cli can read both
# credentials and RPC endpoint in one read:
#
#   __cookie__:<random_hex_password>; host=<host>; port=<port>
#
# Field 0 is the bitcoind-compatible `user:password` token; everything
# after `;` is `key=value` extension. A bitcoind reader doing
# `content.split(";")[0]` still gets a valid cookie back.
COOKIE_USER = "__cookie__"
COOKIE_FILENAME = ".cookie"


def _read_full(conn: socket.socket, n: int, chunk: int = 4096) -> bytes:
    buf = b""
    while len(buf) < n:
        got = conn.recv(min(chunk, n - len(buf)))
        if not got:
            raise ConnectionError("short read: got %s of %s" % (len(buf), n))
        buf += got
    return buf


def _read_message(conn: socket.socket) -> bytes:
    """Read one BOLT message: 2-byte type, then the type-specific length."""
    header = _read_full(conn, 2)
    msg_type = int.from_bytes(header, "big")
    expected = MSG_LENGTH.get(msg_type)
    if expected is None:
        raise ValueError("unknown message type %s" % msg_type)
    rest = _read_full(conn, expected - 2)
    return header + rest


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class EmblndDaemon(BaseDaemon):  # pylint: disable=too-many-instance-attributes
    """Long-running emblnd process: P2P + RPC listeners, dispatch into a node."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        node,
        p2p_port: int = None,
        rpc_port: int = None,
        datadir: str = None,
    ):
        super().__init__(name="%s.emblnd" % node.alias)
        self.node = node
        self.p2p_port = p2p_port
        self.rpc_port = rpc_port
        self.datadir = datadir or self._default_datadir(node.alias)
        self.cookie_path = os.path.join(self.datadir, COOKIE_FILENAME)
        self.rpcuser = COOKIE_USER
        self.rpcpassword: str = None
        self._p2p_socket: socket.socket = None
        self._p2p_thread: threading.Thread = None
        self._rpc_server: _ThreadingHTTPServer = None
        self._rpc_thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._handlers = self._build_rpc_handlers()

    @staticmethod
    def _default_datadir(alias: str) -> str:
        backend = os.getenv("EMBLN_BACKEND")
        root = os.path.join(backend, "data") if backend else tempfile.gettempdir()
        return os.path.join(root, "emblnd-%s-%d" % (alias, os.getpid()))

    def is_running(self) -> bool:
        return self._p2p_socket is not None or self._rpc_server is not None

    # ---- lifecycle ----

    def start(self):
        if self.is_running():
            self.logger.debug("already started; ignoring start()")
            return
        self._stop_event.clear()
        self._start_p2p()
        # rpc must come before cookie so we know the rpc_port for the endpoint line
        self._start_rpc()
        self._write_cookie()
        self._log_config()
        self.logger.info("started")

    def stop(self):
        if not self.is_running():
            self.logger.debug("not running; ignoring stop()")
            return
        self.logger.info("stopping")
        self._stop_event.set()
        self._stop_p2p()
        self._stop_rpc()
        self._delete_cookie()
        self.logger.info("stopped")

    # ---- cookie file ----

    def _write_cookie(self):
        """Write `<datadir>/.cookie`: bitcoind-compatible `user:password`
        token followed by `;`-separated `key=value` extensions (host, port).
        Perms 0600, regenerated each start so a leaked old cookie doesn't
        grant access."""
        os.makedirs(self.datadir, exist_ok=True)
        self.rpcpassword = secrets.token_hex(32)
        fields = [
            "%s:%s" % (self.rpcuser, self.rpcpassword),
            "host=127.0.0.1",
            "port=%s" % self.rpc_port,
        ]
        contents = "; ".join(fields) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self.cookie_path, flags, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(contents)
        except Exception:
            os.close(fd)
            raise

    def _delete_cookie(self):
        try:
            os.remove(self.cookie_path)
        except FileNotFoundError:
            pass
        # Best-effort: prune the datadir if it's now empty (so tests don't
        # leave residue under tempdir).
        try:
            os.rmdir(self.datadir)
        except OSError:
            pass

    def _log_config(self):
        self.logger.info("alias=%s", self.node.alias or "<uninit>")
        self.logger.info("color_rgb=%s", self.node.color_rgb.hex())
        self.logger.info("p2p_listen=127.0.0.1:%s", self.p2p_port)
        self.logger.info("rpc_listen=127.0.0.1:%s", self.rpc_port)
        self.logger.info("datadir=%s", self.datadir)
        self.logger.info("cookie=%s", self.cookie_path)
        if self.node.is_initialized:
            self.logger.info("wallet=%s", self.node.wallet.alias)
            self.logger.info("network=%s", self.node.wallet.network["name"])
            self.logger.info("fingerprint=%s", self.node.wallet.fingerprint)
            self.logger.info("channels=%s", len(self.node.channels))
        else:
            self.logger.warning("wallet=<uninit> — awaiting walletcreate RPC")
        if self.node.bitcoind is not None:
            self.logger.info("bitcoind=attached rpcport=%s", self.node.bitcoind.rpcport)
        else:
            self.logger.info("bitcoind=none")
        self.node.log_policy_defaults()

    # ---- P2P listener ----

    def _start_p2p(self):
        if self.p2p_port is None:
            self.p2p_port = free_port()
        self._p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._p2p_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._p2p_socket.bind(("127.0.0.1", self.p2p_port))
        self._p2p_socket.listen(8)
        self._p2p_thread = threading.Thread(
            target=self._serve_p2p,
            name="%s.p2p-server" % self.node.alias,
            daemon=True,
        )
        self._p2p_thread.start()

    def _stop_p2p(self):
        if self._p2p_socket is not None:
            try:
                self._p2p_socket.close()
            except OSError as e:
                self.logger.error("error closing p2p socket: %s", e)
            self._p2p_socket = None
        if self._p2p_thread is not None:
            self._p2p_thread.join(timeout=2)
            if self._p2p_thread.is_alive():
                self.logger.warning("p2p thread did not join within 2s")
            self._p2p_thread = None

    def _serve_p2p(self):
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._p2p_socket.accept()
            except OSError:
                return  # socket closed during shutdown
            threading.Thread(
                target=self._handle_p2p_connection, args=(conn,), daemon=True
            ).start()

    def _handle_p2p_connection(self, conn: socket.socket):
        try:
            msg = _read_message(conn)
        except (ConnectionError, ValueError) as e:
            self.logger.warning("bad incoming frame: %s", e)
            conn.close()
            return
        msg_type = int.from_bytes(msg[:2], "big")
        self.logger.debug("recv msg_type=%s len=%s", msg_type, len(msg))
        if msg_type == HANDSHAKE_TYPE:
            # Persistent peer session: register, then loop on this socket
            # dispatching subsequent BOLT frames until the remote hangs up.
            self._handle_persistent_peer(conn, msg)
        elif msg_type == OPEN_CHANNEL_TYPE:
            try:
                self._handle_open_channel(conn, msg)
            finally:
                conn.close()
        elif msg_type == ACCEPT_CHANNEL_TYPE:
            try:
                self._handle_accept_channel(conn, msg)
            finally:
                conn.close()
        else:
            self.logger.warning("unhandled msg type %s", msg_type)
            conn.close()

    def _handle_persistent_peer(self, conn: socket.socket, first_msg: bytes):
        """Register the inbound peer from its HANDSHAKE frame and run the
        per-connection read loop until the socket closes."""
        try:
            host, port = conn.getpeername()[:2]
        except OSError:
            host, port = ("?", 0)
        try:
            parsed = parse_handshake(first_msg)
        except ValueError as e:
            self.logger.warning("bad handshake: %s", e)
            conn.close()
            return
        alias = parsed["alias"]
        self.node.register_inbound_peer(alias, conn, host, port)
        try:
            while not self._stop_event.is_set():
                try:
                    msg = _read_message(conn)
                except (ConnectionError, ValueError) as e:
                    self.logger.debug("peer %s closed: %s", alias, e)
                    break
                msg_type = int.from_bytes(msg[:2], "big")
                self.logger.debug(
                    "peer=%s recv msg_type=%s len=%s", alias, msg_type, len(msg)
                )
                if msg_type == OPEN_CHANNEL_TYPE:
                    self._handle_open_channel(conn, msg)
                elif msg_type == ACCEPT_CHANNEL_TYPE:
                    self._handle_accept_channel(conn, msg)
                else:
                    self.logger.warning(
                        "peer=%s unhandled msg type %s", alias, msg_type
                    )
        finally:
            self.node.unregister_peer(alias)
            try:
                conn.close()
            except OSError:
                pass

    def _handle_open_channel(self, conn: socket.socket, msg):
        response = self.node.on_open_channel(parse_open_channel(msg))
        conn.sendall(response)
        self.logger.debug(
            "sent msg_type=%s len=%s",
            int.from_bytes(response[:2], "big"),
            len(response),
        )

    def _handle_accept_channel(self, _conn: socket.socket, msg):
        # No reply: accept_channel is the responder's reply to an open_channel,
        # so the initiator just records it.
        self.node.on_accept_channel(parse_accept_channel(msg))

    # ---- RPC listener (lnd-style stubs) ----

    def _start_rpc(self):
        if self.rpc_port is None:
            self.rpc_port = free_port()
        handler_cls = self._make_rpc_handler_cls()
        self._rpc_server = _ThreadingHTTPServer(
            ("127.0.0.1", self.rpc_port), handler_cls
        )
        self._rpc_thread = threading.Thread(
            target=self._rpc_server.serve_forever,
            name="%s.rpc-server" % (self.node.alias or "emblnd"),
            daemon=True,
        )
        self._rpc_thread.start()

    def _stop_rpc(self):
        if self._rpc_server is not None:
            try:
                self._rpc_server.shutdown()
                self._rpc_server.server_close()
            except OSError as e:
                self.logger.error("error closing rpc server: %s", e)
            self._rpc_server = None
        if self._rpc_thread is not None:
            self._rpc_thread.join(timeout=2)
            if self._rpc_thread.is_alive():
                self.logger.warning("rpc thread did not join within 2s")
            self._rpc_thread = None

    def _build_rpc_handlers(self) -> dict:
        """The lnd-shaped surface. Real handlers return dicts; stubs raise
        NotImplementedError, which is mapped to JSON-RPC -32601 by the
        request handler."""
        return {
            "getinfo": self._h_getinfo,
            "walletcreate": self._h_walletcreate,
            "walletinfo": self._h_walletinfo,
            "walletaddress": self._h_walletaddress,
            "listchannels": self._h_listchannels,
            "connectpeer": self._h_connectpeer,
            "disconnectpeer": self._h_disconnectpeer,
            "listpeers": self._h_listpeers,
            "openchannel": self._h_unimplemented,
            "closechannel": self._h_unimplemented,
            "pendingchannels": self._h_unimplemented,
        }

    def _require_wallet(self):
        """Reject RPC calls that need a wallet before walletcreate has run.
        Raised exceptions are mapped to JSON-RPC -32000 by the request handler."""
        if not self.node.is_initialized:
            raise RuntimeError(
                "wallet not initialized — call walletcreate(alias, password) first"
            )

    def _h_walletcreate(self, params):
        """Bootstrap the wallet. Positional args: [alias, password] or
        [alias, password, force]. With `force=True`, an existing wallet for
        the alias (and all of its addresses + channels) is wiped from the
        DB before the new one is created — destructive."""
        if not isinstance(params, list) or len(params) not in (2, 3):
            raise ValueError(
                "walletcreate expects [alias, password] or "
                "[alias, password, force], got %r" % (params,)
            )
        if self.node.is_initialized:
            raise RuntimeError("wallet already initialized as '%s'" % self.node.alias)
        alias, password = params[0], params[1]
        force = bool(params[2]) if len(params) == 3 else False
        # Open a transient Db handle to peek at the existing record. If
        # WalletManager loads it (load-or-create semantics), the password
        # would silently match a stale mnemonic — so we explicitly check
        # and either refuse or wipe.
        db = Db(self.node.db_path)
        try:
            existing = db.get_wallet(alias)
            if existing is not None and not force:
                raise RuntimeError(
                    "wallet '%s' already exists; pass force=True to overwrite" % alias
                )
            if existing is not None and force:
                self.logger.warning("walletcreate force=True wiping wallet=%s", alias)
                db.delete_wallet(alias)
        finally:
            db.close()
        wm = WalletManager(alias=alias, password=password, db_path=self.node.db_path)
        self.node.initialize(wm)
        self.logger.info("walletcreate alias=%s fingerprint=%s", alias, wm.fingerprint)
        return self._h_walletinfo(None)

    def _h_walletinfo(self, _params):
        self._require_wallet()
        wm = self.node.wallet
        return {
            "alias": wm.alias,
            "fingerprint": wm.fingerprint,
            "network": wm.network["name"],
            "derivation": "/".join(wm.derivation),
            "addresses": [a["address"] for a in wm.addresses],
            "changes": [a["address"] for a in wm.changes],
        }

    def _h_walletaddress(self, params):
        """Derive the next on-chain address. Optional positional arg:
        [branch] (0 = receive, 1 = change). Defaults to 0."""
        self._require_wallet()
        branch = 0
        if isinstance(params, list) and len(params) >= 1:
            branch = int(params[0])
        if branch not in (0, 1):
            raise ValueError("branch must be 0 (receive) or 1 (change)")
        wm = self.node.wallet
        wm.create_new_address(branch=branch)
        bucket = wm.addresses if branch == 0 else wm.changes
        return {"address": bucket[-1]["address"], "branch": branch}

    def _h_getinfo(self, _params):
        info = {
            "alias": self.node.alias,
            "initialized": self.node.is_initialized,
            "color": self.node.color_rgb.hex(),
            "num_channels": len(self.node.channels),
            "num_peers": len(self.node.peers),
            "p2p_listen": "127.0.0.1:%s" % self.p2p_port,
            "rpc_listen": "127.0.0.1:%s" % self.rpc_port,
        }
        if self.node.is_initialized:
            info["network"] = self.node.wallet.network["name"]
            info["fingerprint"] = self.node.wallet.fingerprint
        return info

    def _h_listchannels(self, _params):
        self._require_wallet()
        return {
            "channels": [
                {
                    "address": c["address"],
                    "amount_sat": c["amount_sat"],
                    "used": c["used"],
                }
                for c in self.node.channels
            ]
        }

    def _h_connectpeer(self, params):
        """Positional args (lnd-style): [alias, host, port]."""
        self._require_wallet()
        if not isinstance(params, list) or len(params) != 3:
            raise ValueError(
                "connectpeer expects [alias, host, port], got %r" % (params,)
            )
        alias, host, port = params[0], params[1], int(params[2])
        self.node.connect_peer(alias, host=host, port=port)
        return {"alias": alias, "host": host, "port": port}

    def _h_disconnectpeer(self, params):
        """Positional args: [alias]."""
        self._require_wallet()
        if not isinstance(params, list) or len(params) != 1:
            raise ValueError("disconnectpeer expects [alias], got %r" % (params,))
        alias = params[0]
        return {"alias": alias, "disconnected": self.node.disconnect_peer(alias)}

    def _h_listpeers(self, _params):
        self._require_wallet()
        return {"peers": self.node.list_peers()}

    @staticmethod
    def _h_unimplemented(_params):
        raise NotImplementedError("not yet implemented")

    def _make_rpc_handler_cls(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def parse_request(self):
                # BaseHTTPRequestHandler dispatches to `do_<METHOD>` using
                # `self.command` verbatim from the request line ("POST").
                # Lowercase it here so stdlib calls our pylint-clean
                # `do_post` directly — no `do_POST` shim needed. `command`
                # is set by the parent's parse_request, hence the disable.
                ok = super().parse_request()
                if ok:
                    self.command = self.command.lower()  # pylint: disable=attribute-defined-outside-init
                return ok

            def log_msg(self, fmt, *args):
                # Suppress BaseHTTPRequestHandler's stderr access log; route
                # at DEBUG through the daemon's logger instead.
                daemon.logger.debug("rpc: " + fmt, *args)

            def log_request(self, code="-", size="-"):
                # The only path stdlib uses to log normal requests; redirecting
                # it makes overriding `log_message` unnecessary.
                self.log_msg('"%s" %s %s', self.requestline, str(code), str(size))

            def do_post(self):
                if not self._authorized():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="emblnd"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                req = self._parse(body)
                if req is None:
                    return
                # Match bitcoind: accept both single and batch JSON-RPC.
                # BaseRpc.request always sends a list, but tests / curl might
                # send a single object — handle both shapes.
                if isinstance(req, list):
                    self._reply(200, [self._dispatch(r) for r in req])
                else:
                    self._reply(200, self._dispatch(req))

            def _dispatch(self, req: dict) -> dict:
                if not isinstance(req, dict):
                    return self._error_envelope(None, -32600, "invalid request")
                method = req.get("method")
                req_id = req.get("id")
                handler = daemon._handlers.get(method)
                if handler is None:
                    return self._error_envelope(
                        req_id, -32601, "method not found: %s" % method
                    )
                params = req.get("params") or {}
                try:
                    result = handler(params)
                except NotImplementedError as exc:
                    return self._error_envelope(
                        req_id, -32601, "%s: %s" % (method, exc)
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    return self._error_envelope(
                        req_id, -32000, "%s: %s" % (method, exc)
                    )
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            @staticmethod
            def _error_envelope(req_id, code, message):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": message},
                }

            def _authorized(self) -> bool:
                """Verify HTTP Basic auth against the cookie credentials.
                Compared with secrets.compare_digest to dodge timing oracles."""
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    return False
                try:
                    decoded = base64.b64decode(header[6:]).decode("ascii")
                except (ValueError, UnicodeDecodeError):
                    return False
                expected = "%s:%s" % (daemon.rpcuser, daemon.rpcpassword or "")
                return secrets.compare_digest(decoded, expected)

            def _parse(self, body: bytes):
                try:
                    return json.loads(body) if body else {}
                except json.JSONDecodeError:
                    self._reply(200, self._error_envelope(None, -32700, "parse error"))
                    return None

            def _reply(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler
