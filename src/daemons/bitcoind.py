"""BitcoindDaemon — manages a bitcoind subprocess on regtest."""

import os
import shutil
import signal
import subprocess
import time

from src.daemons.base import BaseDaemon
from src.rpc.bitcoin import BitcoinRpc


class BitcoindDaemon(BaseDaemon):
    """Owns a `bitcoind` subprocess with its own datadir / rpcport / port."""

    rpcuser = "bitcoin"
    rpcpassword = "secret"
    retry_count = 30
    binary = "bitcoind"

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        name: str = "Bitcoin Core",
        datadir: str = None,
        rpcport: int = 18778,
        port: int = 18779,
    ):
        super().__init__(name=name)
        backend = os.getenv("EMBLN_BACKEND")
        if not backend:
            raise RuntimeError("EMBLN_BACKEND environment variable must be set")
        self.basedir = os.path.abspath(backend)
        if datadir is None:
            raise ValueError("datadir is required (must differ across instances)")
        self.datadir = os.path.abspath(datadir)
        self.rpcport = rpcport
        self.port = port
        self._rpc = None
        self._address = None
        self.proc = None

    @property
    def cmd(self):
        # bitcoind v31+ refuses to start when RLIMIT_NOFILE is below ~256 (it needs
        # ~160 fds plus headroom). macOS launches Python with a low default soft limit;
        # raise it just for this subprocess via `ulimit` so the parent's limit is
        # untouched. `exec` then replaces the shell with bitcoind so the PID we hold
        # is bitcoind itself.
        argv = " ".join(
            [
                "%s/%s" % (self.basedir, self.binary),
                "-datadir=%s" % self.datadir,
                "-regtest",
                "-fallbackfee=0.0001",
                "-rpcuser=%s" % self.rpcuser,
                "-rpcpassword=%s" % self.rpcpassword,
                "-rpcport=%s" % self.rpcport,
                "-port=%s" % self.port,
            ]
        )
        return "ulimit -n 10240 && exec %s" % argv

    @property
    def rpc(self) -> BitcoinRpc:
        if self._rpc is None:
            # name= lets BitcoinRpc log under a child of the daemon's logger
            # ("alice.rpc"), so JSON-RPC traffic streams alongside lifecycle events.
            self._rpc = BitcoinRpc(
                user=self.rpcuser,
                password=self.rpcpassword,
                port=self.rpcport,
                timeout=2,
                name="%s.rpc" % self.name,
            )
        return self._rpc

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        for label, p in (("rpcport", self.rpcport), ("port", self.port)):
            if self._port_in_use(p):
                raise RuntimeError(
                    "%s: %s %s already in use — likely a stale "
                    "bitcoind. Try: pkill -f 'bitcoind.*-%s=%s'"
                    % (self.name, label, p, label, p)
                )

        self.logger.info(
            "Starting bitcoind (rpcport=%s, datadir=%s)", self.rpcport, self.datadir
        )
        try:
            shutil.rmtree(self.datadir)
        except FileNotFoundError:
            pass
        os.makedirs(self.datadir, exist_ok=True)
        # DEVNULL (not PIPE): we never read bitcoind's stdout, and a PIPE that's
        # never drained leaks an open file object that surfaces as a
        # ResourceWarning if the test gets Ctrl-C'd before stop() runs.
        self.proc = subprocess.Popen(  # pylint: disable=consider-using-with
            self.cmd, stdout=subprocess.DEVNULL, shell=True, start_new_session=True
        )

        last_err = None
        for i in range(self.retry_count):
            time.sleep(1)
            try:
                info = self.rpc.getblockchaininfo()  # pylint: disable=no-member
                self._log_config(info)
                return
            except Exception as e:  # pylint: disable=broad-exception-caught
                # connection refused, auth, timeout, RpcError, etc.
                last_err = e
                if i == 0 or (i + 1) % 5 == 0:
                    self.logger.debug(
                        "%s not ready (attempt %s/%s): %s: %s",
                        self.name,
                        i + 1,
                        self.retry_count,
                        type(e).__name__,
                        e,
                    )
        raise RuntimeError(
            "%s did not become ready in %ss; last error: %s: %s"
            % (self.name, self.retry_count, type(last_err).__name__, last_err)
        ) from last_err

    def _log_config(self, info: dict):
        """One key=value per line so the color formatter highlights values."""
        self.logger.info("name=%s", self.name)
        self.logger.info("rpcport=%s", self.rpcport)
        self.logger.info("port=%s", self.port)
        self.logger.info("datadir=%s", self.datadir)
        self.logger.info("rpcuser=%s", self.rpcuser)
        self.logger.info("chain=%s", info.get("chain"))
        self.logger.info("blocks=%s", info.get("blocks"))
        self.logger.info("bestblockhash=%s", info.get("bestblockhash"))
        self.logger.info("ready")

    def get_coins(self):
        if "" not in self.rpc.listwallets():  # pylint: disable=no-member
            self.rpc.createwallet("")  # pylint: disable=no-member
        self.mine(101)
        assert self.rpc.getbalance(wallet="") > 0  # pylint: disable=no-member

    def mine(self, n: int = 1):
        self.rpc.generatetoaddress(n, self._address)  # pylint: disable=no-member

    def stop(self):
        self.logger.info("Shutting down %s", self.name)
        if self.proc is None:
            return
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            self.proc.wait(timeout=5)
