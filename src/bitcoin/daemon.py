import logging
import os
import shutil
import signal
import socket
import subprocess
import time
import weakref

from .rpc import Rpc


class Daemon:
    rpcuser = "bitcoin"
    rpcpassword = "secret"
    retry_count = 30
    binary = "bitcoind"
    # Defense-in-depth registry — every live instance is tracked weakly so a
    # session-end atexit hook (or test teardown) can call stop_all_running()
    # and reap any bitcoind that wasn't explicitly stopped (e.g. on Ctrl-C).
    _instances: "weakref.WeakSet[Daemon]" = weakref.WeakSet()

    def __init__(
        self,
        name: str = "Bitcoin Core",
        datadir: str = None,
        rpcport: int = 18778,
        port: int = 18779,
    ):
        backend = os.getenv("EMBLN_BACKEND")
        if not backend:
            raise RuntimeError("EMBLN_BACKEND environment variable must be set")
        self.basedir = os.path.abspath(backend)
        self.name = name
        if datadir is None:
            raise ValueError("datadir is required (must differ across instances)")
        self.datadir = os.path.abspath(datadir)
        self.rpcport = rpcport
        self.port = port
        self._rpc = None
        self._address = None
        self.proc = None
        self._logger = None
        type(self)._instances.add(self)

    @classmethod
    def stop_all_running(cls):
        """Stop every live Daemon whose subprocess is still running. Safe to
        call from atexit / session teardown to reap leaked bitcoind processes."""
        for d in list(cls._instances):
            if d.proc is not None and d.proc.poll() is None:
                try:
                    d.stop()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

    @property
    def cmd(self):
        # bitcoind v31+ refuses to start when RLIMIT_NOFILE is below ~256 (it needs
        # ~160 fds plus headroom). macOS launches Python with a low default soft limit;
        # raise it just for this subprocess via `ulimit` so the parent's limit is
        # untouched. `exec` then replaces the shell with bitcoind so the PID we hold
        # is bitcoind itself.
        argv = " ".join(
            [
                f"{self.basedir}/{self.binary}",
                f"-datadir={self.datadir}",
                "-regtest",
                "-fallbackfee=0.0001",
                f"-rpcuser={self.rpcuser}",
                f"-rpcpassword={self.rpcpassword}",
                f"-rpcport={self.rpcport}",
                f"-port={self.port}",
            ]
        )
        return f"ulimit -n 10240 && exec {argv}"

    @property
    def rpc(self):
        if self._rpc is None:
            # name= makes Rpc log under a child of the daemon's logger
            # ("alice.rpc"), so JSON-RPC traffic streams alongside daemon lifecycle.
            self._rpc = Rpc(
                self.rpcuser,
                self.rpcpassword,
                port=self.rpcport,
                timeout=2,
                name=f"{self.name}.rpc",
            )
        return self._rpc

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(self.name)
        return self._logger

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True

    def start(self):
        for label, p in (("rpcport", self.rpcport), ("port", self.port)):
            if self._port_in_use(p):
                raise RuntimeError(
                    f"{self.name}: {label} {p} already in use — likely a stale "
                    f"bitcoind. Try: pkill -f 'bitcoind.*-{label}={p}'"
                )

        print()
        self.logger.info(
            f"Starting bitcoind (rpcport={self.rpcport}, datadir={self.datadir})"
        )
        try:
            shutil.rmtree(self.datadir)
        except FileNotFoundError:
            pass
        os.makedirs(self.datadir, exist_ok=True)
        # DEVNULL (not PIPE): we never read bitcoind's stdout, and a PIPE that's
        # never drained leaks an open file object that surfaces as a
        # ResourceWarning if the test gets Ctrl-C'd before stop() runs.
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.DEVNULL, shell=True, start_new_session=True
        )

        last_err = None
        for i in range(self.retry_count):
            time.sleep(1)
            try:
                info = self.rpc.getblockchaininfo()
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
        if "" not in self.rpc.listwallets():
            self.rpc.createwallet("")
        self.mine(101)
        assert self.rpc.getbalance(wallet="") > 0

    def mine(self, n=1):
        # _address is lazily resolved: create the default wallet on first use so
        # callers can invoke mine() independently without calling get_coins() first.
        if self._address is None:
            if "" not in self.rpc.listwallets():
                self.rpc.createwallet("")
            self._address = self.rpc.getnewaddress(wallet="")
        self.rpc.generatetoaddress(n, self._address)

    def stop(self):
        self.logger.info(f"Shutting down {self.name}")
        if self.proc is None:
            return
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            self.proc.wait(timeout=5)
