import configparser
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


class RpcError(Exception):
    """Specifically created for error-handling of the BitcoiCore-API
    if thrown, check for errors like this:
    try:
        rpc.does_not_exist()
    except RpcError as rpce:
        assert rpce.error_code == -32601
        assert rpce.error_msg == "Method not found"
    See for error_codes https://github.com/bitcoin/bitcoin/blob/v0.15.0.1/src/rpc/protocol.h#L32L87
    """

    def __init__(self, message, response):
        super(Exception, self).__init__(message)
        self.status_code = 500  # default
        try:
            self.status_code = response.status_code
            error = response.json()
        except Exception:
            # it's a dict already
            error = response
        try:
            self.error_code = error["error"]["code"]
            self.error_msg = error["error"]["message"]
        except Exception as exc:
            self.error = "Error '%s': %s" % (exc.to_string(), response.text)


def load_rpcconfig(filepath: str):
    try:
        config = configparser.ConfigParser()
        config.read(filepath)
        logger.info(config.sections())

        # for now only regtest supported
        for net in ["regtest"]:
            if net in config:
                return config
    except Exception as exc:
        logger.error("Can't write '%s': %s" % (filepath, exc.to_string()))


def get_rpcconfig(datadir=os.getenv("EMBLN_BACKEND")):
    """returns the bitcoin.conf configurations (multiple) in a datastructure
    for all networks of a specific datadir.
    """
    config = {
        "cookies": [],
    }

    # we don't know where to search for files
    if not os.path.isdir(datadir):
        return config

    # load content from bitcoin.conf
    bitcoin_conf_file = os.path.join(datadir, "bitcoin.conf")
    if not os.path.exists(bitcoin_conf_file):
        _rpc = load_rpcconfig("bitcoin.conf")

    else:
        _rpc = load_rpcconfig(bitcoin_conf_file)

    folders = dict.fromkeys(_rpc.keys())

    for chain in folders:
        fname = os.path.join(datadir, folders[chain], ".cookie")
        if os.path.exists(fname):
            try:
                with open(fname, "r") as f:
                    content = f.read()
                    user, password = content.split(":")
                    obj = {
                        "user": user,
                        "password": password,
                        "rpcport": config["bitcoin.conf"][chain]["rpcport"],
                    }
                    config["cookies"].append(obj)
            except Exception as exc:
                logger.error("Can't open %s file: %s" % (fname, exc.to_string()))
    return config


def get_configs(config=None, datadir=os.getenv("EMBLN_BACKEND")):
    if config is None:
        config = get_rpcconfig(datadir=datadir)
    confs = []
    default = {}
    for network in config["bitcoin.conf"]:
        if "rpcuser" in config["bitcoin.conf"][network]:
            default["user"] = config["bitcoin.conf"][network]["rpcuser"]
        if "rpcpassword" in config["bitcoin.conf"][network]:
            default["password"] = config["bitcoin.conf"][network]["rpcpassword"]
        if "rpcconnect" in config["bitcoin.conf"][network]:
            default["host"] = config["bitcoin.conf"][network]["rpcconnect"]
        if "rpcport" in config["bitcoin.conf"][network]:
            default["port"] = int(config["bitcoin.conf"][network]["rpcport"])
        if "user" in default and "password" in default:
            if (
                "rpcport" not in config["bitcoin.conf"]["default"]
            ):  # only one rpc makes sense in this case
                raise ValueError("no rpcport defined")

            confs.append(default.copy())

    # try cookies now
    for cookie in config["cookies"]:
        o = {}
        o.update(default)
        o.update(cookie)
        confs.append(o)
    return confs


def detect_rpc_confs(config=None, datadir=os.getenv("EMBLN_BACKEND")):
    if config is None:
        config = get_rpcconfig(datadir=datadir)
    rpcconfs = get_configs(config)
    rpc_arr = []
    for conf in rpcconfs:
        rpc_arr.append(conf)
    return rpc_arr


def detect_rpc_confs_via_env():
    """returns an array which might contain one configmap derived from Env-Vars
    Env-Vars: BTC_RPC_USER, BTC_RPC_PASSWORD, BTC_RPC_HOST, BTC_RPC_PORT
    configmap: {"user":"user","password":"password","host":"host","port":"port","protocol":"https"}
    """
    rpc_arr = []
    if (
        os.getenv("EMBLN_BACKEND_RPC_USER")
        and os.getenv("EMBLN_BACKEND_RPC_PASSWORD")
        and os.getenv("EMBLN_BACKEND_RPC_HOST")
        and os.getenv("EMBLN_BACKEND_RPC_PORT")
    ):
        logger.info("Detected RPC-Config on Environment-Variables")
        env_conf = {
            "user": os.getenv("EMBLN_BACKEND_RPC_USER"),
            "password": os.getenv("EMBLN_BACKEND_RPC_PASSWORD"),
            "host": os.getenv("EMBLN_BACKEND_RPC_HOST"),
            "port": os.getenv("EMBLN_BACKEND_RPC_PORT"),
            "protocol": os.getenv(
                "EMBLN_BACKEND_PROTOCOL", "https"
            ),  # https by default
        }
        rpc_arr.append(env_conf)
    return rpc_arr


def autodetect_rpc_confs(
    datadir=os.getenv("EMBLN_BACKEND"),
    port=None,
    proxy_url="socks5h://localhost:9050",
    only_tor=False,
):
    """Returns an array of valid and working configurations which
    got autodetected.
    autodetection checks env-vars and bitcoin-data-dirs
    """
    if port == "":
        port = None
    if port is not None:
        port = int(port)
    conf_arr = []
    conf_arr.extend(detect_rpc_confs_via_env())
    conf_arr.extend(detect_rpc_confs(datadir=datadir))
    available_conf_arr = []
    if len(conf_arr) > 0:
        for conf in conf_arr:
            rpc = Rpc(**conf, proxy_url="socks5h://localhost:9050", only_tor=False)
            if port is not None:
                if int(rpc.port) != port:
                    continue
            try:
                rpc.getmininginfo()
                available_conf_arr.append(conf)
            except requests.exceptions.RequestException as exc:
                logger.log(exc.to_string())
            except RpcError as exc:
                logger.log(exc.to_string())
    return available_conf_arr


class Rpc:
    def __init__(
        self,
        user="bitcoin",
        password="secret",
        host="127.0.0.1",
        port=18443,
        protocol="http",
        path="",
        timeout=None,
        session=None,
        proxy_url="socks5h://localhost:9050",
        only_tor=False,
        name=None,
        **kwargs,
    ):
        path = path.replace("//", "/")  # just in case
        self.user = user
        self._password = password
        self.port = port
        self.protocol = protocol
        self.host = host
        self.path = path
        self.timeout = timeout
        self.proxy_url = proxy_url
        self.only_tor = only_tor
        self.r = None
        # logger name (default e.g. "rpc:18778"; daemons inject "<alias>.rpc")
        self._name = name or "rpc:%s" % port
        # monotonic JSON-RPC id; survives across calls within this Rpc instance
        # (which lives the entire bitcoin daemon's lifetime).
        self._next_id = 0
        self._logger = None

        # session reuse speeds up requests
        if session is None:
            self._create_session()
        else:
            self.session = session

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(self._name)
        return self._logger

    def _take_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _create_session(self):
        session = requests.Session()
        session.auth = (self.user, self.password)
        # check if we need to connect over Tor
        self.session = session

    def wallet(self, name=""):
        return Rpc(
            user=self.user,
            password=self.password,
            port=self.port,
            protocol=self.protocol,
            host=self.host,
            path="{}/wallet/{}".format(self.path, name),
            timeout=self.timeout,
            session=self.session,
            proxy_url=self.proxy_url,
            only_tor=self.only_tor,
        )

    @property
    def url(self):
        return "{s.protocol}://{s.host}:{s.port}{s.path}".format(s=self)

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._password = value
        self._create_session()

    def test_connection(self):
        """returns a boolean depending on whether getblockchaininfo() succeeds"""
        try:
            self.getblockchaininfo()
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.debug("test_connection failed: %s: %s", type(exc).__name__, exc)
            return False

    def clone(self):
        """
        Returns a clone of self.
        Useful if you want to mess with the properties
        """
        return Rpc(
            self.user,
            self.password,
            self.host,
            self.port,
            self.protocol,
            self.path,
            self.timeout,
            self.session,
            self.proxy_url,
            self.only_tor,
        )

    def request(self, calls: list, **kwargs):
        """Makes batch request to Core. Each call gets a fresh monotonic id
        from `self._take_id()`, persisted across requests in the daemon lifetime.
        """
        headers = {"content-type": "application/json"}
        payload = [
            {
                "method": method,
                "params": args if args != [None] else [],
                "jsonrpc": "2.0",
                "id": self._take_id(),
            }
            for method, *args in calls
        ]
        timeout = self.timeout
        if "timeout" in kwargs:
            timeout = kwargs["timeout"]
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
                raise RpcError("Request error: %s" % err["message"], r)
            return r["result"]

        return fn
