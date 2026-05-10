"""Base class for managed daemons (bitcoind, emblnd, …).

Subclasses provide their own start/stop/is_running. The base class supplies:
  - a shared weakref registry of every live instance
  - `BaseDaemon.stop_all_running()` for atexit / session-end cleanup
  - lazy logger plumbing
  - `_port_in_use(port)` helper used by both backends
  - `free_port()` module helper for ephemeral allocation
"""

import logging
import socket
import weakref


def free_port() -> int:
    """Pick an unused ephemeral TCP port. Tiny race window between close
    and re-bind; callers that care should set SO_REUSEADDR."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BaseDaemon:
    # Defense-in-depth registry — every live instance is tracked weakly so a
    # session-end atexit hook (or test teardown) can call stop_all_running()
    # and reap any daemon that wasn't explicitly stopped (e.g. on Ctrl-C).
    _instances: "weakref.WeakSet[BaseDaemon]" = weakref.WeakSet()

    def __init__(self, name: str):
        self.name = name
        self._logger = None
        BaseDaemon._instances.add(self)

    @property
    def logger(self):
        if self._logger is None:
            self._logger = logging.getLogger(self.name)
        return self._logger

    @classmethod
    def stop_all_running(cls):
        """Stop every live daemon (any subclass) that is still running. Safe
        to call from atexit / session teardown."""
        for d in list(BaseDaemon._instances):
            try:
                if d.is_running():
                    d.stop()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    # ---- subclass contract ----

    def is_running(self) -> bool:
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    # ---- helpers ----

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True
