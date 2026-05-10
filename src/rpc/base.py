"""Generic JSON-RPC 2.0 client over HTTP.

Concrete clients (BitcoinRpc, EmblnRpc) subclass `BaseRpc` and customize:
  - default credentials / port
  - method shortcuts (e.g. BitcoinRpc.wallet(name))
  - typed stubs that document the server's RPC surface (lnd-style)

All otherwise-undefined attribute access is forwarded as a JSON-RPC call via
`__getattr__`, so adding a new server-side method requires no client change.
"""

import json
import logging

import requests


class RpcError(Exception):
    """Raised when a JSON-RPC server returns an error response.

    Common pattern:
        try:
            rpc.does_not_exist()
        except RpcError as e:
            assert e.error_code == -32601
            assert e.error_msg == "Method not found"
    """

    def __init__(self, message, response):
        super().__init__(message)
        self.status_code = 500
        try:
            self.status_code = response.status_code
            error = response.json()
        except Exception:  # pylint: disable=broad-exception-caught
            error = response
        try:
            self.error_code = error["error"]["code"]
            self.error_msg = error["error"]["message"]
        except Exception:  # pylint: disable=broad-exception-caught
            self.error_code = None
            self.error_msg = str(error)


class BaseRpc:  # pylint: disable=too-many-instance-attributes
    """JSON-RPC 2.0 client. Subclass to bind defaults for a specific server."""

    DEFAULT_USER = "rpcuser"
    DEFAULT_PASSWORD = "rpcpassword"
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 0
    DEFAULT_PROTOCOL = "http"

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        user: str = None,
        password: str = None,
        host: str = None,
        port: int = None,
        protocol: str = None,
        path: str = "",
        timeout: float = None,
        session: requests.Session = None,
        name: str = None,
    ):
        self.user = user or self.DEFAULT_USER
        self._password = password or self.DEFAULT_PASSWORD
        self.host = host or self.DEFAULT_HOST
        self.port = port or self.DEFAULT_PORT
        self.protocol = protocol or self.DEFAULT_PROTOCOL
        self.path = path.replace("//", "/")
        self.timeout = timeout
        self.r = None
        self._name = name or "rpc:%s" % self.port
        self._next_id = 0
        self._logger = None

        if session is None:
            self._create_session()
        else:
            self.session = session

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(self._name)
        return self._logger

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._password = value
        self._create_session()

    @property
    def url(self):
        return "%s://%s:%s%s" % (self.protocol, self.host, self.port, self.path)

    def _take_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _create_session(self):
        session = requests.Session()
        session.auth = (self.user, self._password)
        self.session = session

    def request(self, calls: list, **kwargs):
        """Batch JSON-RPC request. `calls` is a list of (method, *args) tuples."""
        headers = {"content-type": "application/json"}
        payload = [
            {
                "method": method,
                "params": list(args) if args != (None,) else [],
                "jsonrpc": "2.0",
                "id": self._take_id(),
            }
            for method, *args in calls
        ]
        timeout = kwargs.get("timeout", self.timeout)
        url = self.url
        if "wallet" in kwargs:
            url = url + "/wallet/%s" % kwargs["wallet"]

        method_names = ",".join(c[0] for c in calls)
        first_id = payload[0]["id"]
        self.logger.debug("request id=%s methods=%s", first_id, method_names)
        r = self.session.post(
            url, data=json.dumps(payload), headers=headers, timeout=timeout
        )
        self.r = r
        if r.status_code != 200:
            raise RpcError(
                "Server responded with error code %d: %s" % (r.status_code, r.text), r
            )
        self.logger.debug("response id=%s status=%s", first_id, r.status_code)
        return r.json()

    def __getattr__(self, method):
        def fn(*args, **kwargs):
            r = self.request([(method, *args)], **kwargs)[0]
            err = r.get("error")
            if err is not None:
                raise RpcError("Request error: %s" % err.get("message", err), r)
            return r["result"]

        return fn
