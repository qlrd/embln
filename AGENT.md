# AGENT.md

Notes for AI assistants (and humans) working in this repo. The codebase is small;
read the source. This file captures the *non-obvious* decisions that aren't
visible from the file tree alone.

## What this project is

A regtest-only Python sandbox for learning BOLT specs by reimplementing them on
top of [embit](https://github.com/diybitcoinhardware/embit). Pedagogical, not
production. When in doubt, prefer **explicit and BOLT-faithful** over **clever
or abbreviated** — the reader is here to understand the protocol.

## Key architectural split

Three roles, one wallet seed:

- `WalletManager` (`src/bitcoin/manager.py`) — pure on-chain wallet. BIP84 at
  `m/84'/1'/0'` (regtest = coin type 1). Holds the master `HDKey` as
  `self.root` so other components can derive sibling sub-trees. Identified by
  `self.alias` (matches LN parlance — the human-readable label real nodes
  advertise via `node_announcement`). The alias is the row key in the
  `wallets` table. **Constructor takes `alias=`, not `name=`.**
- `Daemon` (`src/bitcoin/daemon.py`) — bitcoind subprocess lifecycle and an
  `Rpc` client. The chain backend. Keeps `name` (it's a bitcoind instance
  label, not an LN concept).
- `EmblnNode(wallet, daemon=None, port=None, alias=None, color_rgb=None)`
  (`src/lightning/node.py`) — bound to exactly one wallet, optionally one
  daemon. **Two roles in one class:**
  - **RPC *client*** to bitcoind via `self.rpc` (chain access, multisig parity
    checks, future tx broadcast).
  - **RPC *server*** for incoming LN P2P messages (TCP listener thread,
    started by `start()`, stopped by `stop()`).

  Carries its own `alias` (defaults to `wallet.alias`) and a 3-byte
  `color_rgb` (default `b"\x00\x00\x00"`), mirroring the
  `node_announcement` identity fields. Owns channel keys at LND-style paths
  `m/1017'/<coin_type>'/<family>'/0/<channel_idx>` derived off `wallet.root`,
  the funding-tx 2-of-2 P2WSH output, and BOLT 2 message construction.

Do not put channel logic on `WalletManager`. Do not derive on-chain addresses
through `EmblnNode`. The seed is shared; the responsibilities are not.

## Channel-policy defaults

Each `EmblnNode` carries two override-friendly dicts:

- `self.open_channel_defaults` — every BOLT 2 mandatory policy field
  (funding_satoshis, push_msat, dust_limit_satoshis, htlc_minimum_msat,
  feerate_per_kw, to_self_delay, max_accepted_htlcs, channel_flags).
- `self.accept_channel_defaults` — `minimum_depth`.

Per-call kwargs win: `node.create_open_channel_msg(funding_satoshis=50_000_000)`
overrides only that field, the rest come from the instance defaults. Both
defaults dicts are also forwarded into `messages.build_open_channel` /
`messages.build_accept_channel`, which now accept every field as a keyword
arg (no more hardcoded literals in the function body).

Override at construction or any time:
```python
alice.open_channel_defaults["feerate_per_kw"] = 500
alice.color_rgb = b"\xff\xa5\x00"
```

## Module dependency arrows

`src/lightning/messages.py` is a **leaf module**: constants + pure
build/parse helpers (`build_open_channel`, `build_accept_channel`,
`parse_open_channel`, `parse_accept_channel`). It must **never** import
`EmblnNode` — we hit a circular import doing exactly that. Functions take
primitive arguments (a `basepoints` dict, the parsed peer message) and return
bytes / dicts. The dependency arrow points `node.py → messages.py`; node
methods are thin wrappers that pull `self._basepoints(channel_idx)` and
forward to the leaf.

## LN P2P transport

`EmblnNode.start()` binds a TCP socket on `self.port` (auto-allocated via
`_free_port()` if not passed) and spawns a daemon thread that accepts
connections and dispatches per BOLT message type:

- `_handle_open_channel(parsed)` — *responder*: returns accept_channel bytes,
  written back on the same connection.
- `_handle_accept_channel(parsed)` — *initiator's late path*: stores in
  `self.handshakes[temp_id]`. (Currently unused — see below.)

The high-level initiator API is `node.open_channel(peer, **kwargs)`: opens a
TCP connection to `peer.port`, sends `open_channel`, reads the reply
synchronously, parses it, **and stores it in `self.handshakes`** keyed by
`temporary_channel_id`. So both initiator and responder converge on the same
state slot.

Framing is **fixed-length** by message type (`MSG_LENGTH` table in
`messages.py`). One message per direction per connection. **No BOLT 8 noise
transport** — frames are plaintext over `127.0.0.1`. When HTLC work needs real
wire compatibility, that's the next layer to add (handshake_act_one/two/three,
AEAD-encrypted frames with 2-byte length prefix). Until then the code can stay
this readable.

## Persistence

SQLite via `src/db.py` (`Db` class). Three tables: `wallets`, `addresses`,
`channels`. Default DB path is `${EMBLN_BACKEND}/data/embln.db`; tests inject a
`tmp_path`-based path through the `db_path` fixture. The `wallets` PK is
`alias` (was `name` historically — never reintroduce the old column name).

`Db` emits **DEBUG**-level logs on every operation through
`logging.getLogger("db")` in `key=value` format. Silent at INFO/WARN; flip to
DEBUG to trace what hits SQLite during a flow:
```python
logging.getLogger("db").setLevel(logging.DEBUG)
```

`WalletManager.__init__` is **load-or-create**: if the named row exists, the
mnemonic and address state are rehydrated; otherwise a fresh 12-word BIP39
mnemonic is generated from `os.urandom(16)` and persisted.
`EmblnNode.__init__` rehydrates `self.channels` from the same DB.

`WalletManager.close()` releases the SQLite connection. The `make_wallet`
fixture calls it on teardown for every cached instance; tests that
instantiate `WalletManager` directly (the recovery tests) own the cleanup
themselves with `try / finally`. The whole suite runs under
`pytest -W error::ResourceWarning` (when invoked with `--catch
ResourceWarning`), so a leaked connection fails CI immediately.

Mnemonic is stored as **plaintext**. This is a deliberate, scoped trade-off
for a regtest learning project. Never log it; never echo it to stdout. The
WalletManager class never prints `self.mnemonic` and you should keep it that
way.

## Loggers

Each instance has a lazy `self.logger` property that wraps `logging.getLogger`:

- `Daemon.logger` → `getLogger(self.name)` → e.g. `"alice"`.
- `WalletManager.logger` → `getLogger(self.alias)` — same channel as the
  daemon for the same persona, intentionally.
- `EmblnNode.logger` → `getLogger(f"{self.alias}.ln")` → child logger of
  the wallet's, so adjusting the parent's level cascades.
- `Rpc.logger` → `getLogger(self._name)` — `Daemon` injects
  `name=f"{self.name}.rpc"` so JSON-RPC traffic logs as e.g. `alice.rpc`.
  `Db` uses module-level `getLogger("db")` and emits DEBUG on every op.

Tests get a per-test logger via the `logger` fixture in `tests/conftest.py`,
named after `request.node.name`. Use `key=value` in log messages — the colored
formatter highlights the value (yellow) separately from the label (white).

`ColorFormatter` lives in `src/bin/_common.py` (used by both the CLI
binaries and `tests/conftest.py`).

### Log levels — what's INFO, DEBUG, WARN

- **INFO** = lifecycle markers users care about: `started`, `stopped`,
  `ready`, `recv open_channel funding_satoshis=…`, the config-dump key
  fields (`alias=`, `listen=`, `rpcport=`, `chain=`, `bestblockhash=`).
- **DEBUG** = high-volume detail: per-frame wire events
  (`recv msg_type=…`, `sent msg_type=…`), the per-policy-field config
  lines (`open_channel.funding_satoshis=…`), every `Db` operation, every
  RPC `request id=… methods=…` / `response id=… status=…`, "already
  started; ignoring" / "not running; ignoring" idempotent paths,
  bitcoind readiness-poll attempts.
- **WARNING** = recoverable problem: `bad incoming frame`,
  `unhandled msg type`, slow LN thread join.
- **ERROR** = something genuinely went wrong but we kept going (e.g.
  closing the LN listener socket failed).

When adding a new log line, classify the same way: would a user care
on every run (INFO) or only when debugging (DEBUG)? Anything that fires
once per wire frame, per RPC call, or per DB row is DEBUG.

### Two MPSC log channels

Production logging in CLIs and tests goes through `setup_mpsc_logging()`
in `src/bin/_common.py`. It installs **two** `logging.handlers.QueueHandler`s
on the root logger, each backed by an unbounded `queue.Queue` and a
dedicated `QueueListener` consumer thread:

- **Stream queue** → `ColorFormatter` to stderr at the resolved level
  (filtered).
- **File queue** → plain text to a log file at DEBUG (always full
  detail). Path: `EMBLN_LOGFILE` env, or the `--log-file` CLI flag, or
  `embln.log` next to cwd.

Producers (any thread calling `logger.x(...)`) only do
`queue.put_nowait` — socket / RPC / wallet handler threads never block
on stderr or disk I/O. `teardown_mpsc_logging(listeners)` drains both
queues and joins both consumer threads. The CLI binaries call it in
`finally`; `tests/conftest.py` registers it via `atexit`.

Do not call `logging.basicConfig` from inside `src/`. The three
modules that historically did (`bitcoin/daemon.py`,
`bitcoin/manager.py`, `bitcoin/rpc.py`) have all been cleaned up — a
module-level `basicConfig` at import time wins over the CLI's later
`setup_mpsc_logging` and silently breaks `--log-level`.

## Tests

All test files live under `tests/embln/` and follow `test_*.py` naming
(pytest defaults; no custom `python_files` whitelist needed). Fixtures are
in `tests/conftest.py`:

- `db_path` — per-test SQLite file (auto-cleaned via `tmp_path`).
- `make_wallet(name)` — cached factory, closes its SQLite connections on
  teardown.
- `make_ln_node(name, with_daemon=False, with_server=False)` — single
  factory that opt-in attaches a backing `Daemon` (via `node_manager`)
  and/or starts the LN listener. Auto-stop on teardown for whichever was
  started.
- `node_manager` — owns daemons spawned in one test, stops them all on
  teardown. Skips when `EMBLN_BACKEND` is unset. Fetched lazily via
  `request.getfixturevalue("node_manager")` only when `with_daemon=True`,
  so unit tests that don't need bitcoind aren't dragged into the skip.
- `bitcoind_node` / `bitcoind_pair` — convenience layers over
  `node_manager` for legacy direct-daemon tests.
- `_assert_rlimit_unchanged` — session-scope, autouse: snapshots Python's
  `RLIMIT_NOFILE` at session start and asserts it's identical at session
  end. Catches anyone who slips a `resource.setrlimit` into the parent.

When daemons are spawned, **register them in `NodeManager` before calling
`start()`** so teardown still cleans them up if `start()` raises. The fixture
already does this; do not invert the order.

`_free_port()` allocates random ephemeral ports per daemon AND per LN
listener — no leaked process from a previous run can ever collide on the
same port. The helper is duplicated between `tests/conftest.py` and
`src/lightning/node.py` because the LN port is an LN-specific concept and
shouldn't pull a test dependency into `src/`.

### Test files

- `test_funding_tx.py` — 2-of-2 P2WSH wire layout + parity vs Core's
  `createmultisig(..., "bech32")`.
- `test_open_channel.py` — BOLT 2 open / accept handshake (in-process, no
  socket) plus the integration variant that goes through the TCP server.
- `test_p2p.py` — LN listener lifecycle, error paths (unknown msg type,
  short frames), distinct ports, idempotent `start()`, plus an integration
  test that combines the socket handshake with bitcoind RPC parity.
- `test_recovery.py` — drop-and-reload round-trips for wallet and channel
  state from SQLite.

## bitcoind setup

`poe build-core` clones Bitcoin Core at the requested tag (default `v31.0`,
override via `BITCOIN_REVISION`) into `build/bitcoin/` and produces
`build/bin/bitcoind`. `poe tests` sets `EMBLN_BACKEND=${POE_ROOT}/build/bin`
so `Daemon` finds the binary. The same env var also drives the default DB path.

`Daemon.cmd` prefixes the bitcoind invocation with `ulimit -n 10240 && exec`.
The shell child raises *its own* `RLIMIT_NOFILE` to 10240 (above bitcoind's
~256 minimum) and `exec`s into bitcoind so the PID stored on `self.proc` is
bitcoind itself. Python's RLIMIT_NOFILE is *never* modified — confirmed by
the `_assert_rlimit_unchanged` fixture. This works around macOS's low
default soft limit; on Linux the same prefix is harmless.

`Daemon.start()`:
1. Pre-checks `rpcport` and `port` are free with `socket.bind` — fails fast
   with a clear `pkill` hint instead of letting bitcoind error with the
   cryptic *"Unable to start HTTP server"*.
2. Wipes and recreates `self.datadir`.
3. Launches via `subprocess.Popen` with `start_new_session=True` so
   `os.killpg` cleanly tears down the whole process group on `stop()`.
4. Polls `rpc.getblockchaininfo()` with a 2-second per-call timeout. The
   timeout matters — without it, a single hung request blocks the entire
   retry loop.

## RPC client subtlety

Modern Bitcoin Core (v28+) omits `"error"` from JSON-RPC responses on success
instead of returning `"error": null`. `Rpc.__getattr__` uses `r.get("error")`
to handle both shapes. Don't revert to `r["error"]` — older Cores tolerate it,
v28+ raises `KeyError`.

## BOLT 2 messages — what is and isn't implemented

`messages.py` parses and serializes `open_channel` (type 32) and
`accept_channel` (type 33) — mandatory fields only, no TLV extension. Both
serializers are big-endian per spec. The full handshake (open ↔ accept ↔
funding_created ↔ funding_signed ↔ channel_ready) stops at accept; the
remaining three messages need real signatures and the per-commitment-secret
ratchet.

The `first_per_commitment_point` slot is filled with a stub pubkey derived at
`_FAMILY_REVOCATION_ROOT` (LND family 5). The real BOLT 3
per-commitment-secret SHA256 ratchet is **not** implemented; do not assume the
returned point is a valid first secret. If a task asks for HTLC handling,
commitment signing, or `funding_created`/`funding_signed`, you'll need the
ratchet first — call that out and scope the work.

## CI and tooling

`.github/workflows/ci.yml` runs **parallel** jobs on every push and PR, each
mirroring one pre-commit hook:

| Job                  | Mirrors hook(s)                          |
|----------------------|------------------------------------------|
| `typos`              | `typos`                                  |
| `isort`              | local `isort` (`uv run poe isort --check`) |
| `format`             | local `format` (`uv run poe format --check`) |
| `pylint`             | local `pylint`                           |
| `conventional-commits` | `conventional-pre-commit` (PR-only) |
| `tests`              | local `build-core` + `tests` (cached bitcoind, Codecov upload) |

CI does **not** invoke pre-commit itself — the user wanted parallel jobs over a
single sequential pre-commit run. File-hygiene hooks (trailing-whitespace,
EOF, yaml/toml syntax, merge conflicts, large files) are local-only — pushing
them past pre-commit isn't a quality regression worth a CI job.

Locally, install pre-commit with **both** stages:
```sh
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```
The `commit-msg` stage is for `conventional-pre-commit`, which validates that
every commit message follows the Conventional Commits spec (`feat:`, `fix:`,
`chore:`, etc.). Mirrored on the CI side by `webiny/action-conventional-commits`.

`poe tests` accepts:
- `--verbose` / `-v` — `--show-capture all --capture tee-sys` for log
  streaming.
- `--coverage` / `-c` — produces `coverage.xml` + HTML + terminal report.
- `--catch <WarningClass>` — promotes the named warning class to error
  (`-W error::<class>`). Use `--catch ResourceWarning` to fail fast on
  unclosed sockets / files / SQLite handles.

`poe isort` and `poe format` default to **applying** fixes; pass `--check` to
verify without modifying. Pre-commit and CI both invoke them with `--check`.

## CLI: emblnd + embln-cli

Two binaries, mirroring the `bitcoind` / `bitcoin-cli` (and `lnd` / `lncli`)
convention:

- **`emblnd`** (`src/bin/emblnd.py`) — long-running daemon. Opens the
  wallet, starts the LN P2P listener, blocks until SIGINT/SIGTERM. Logs
  the full config dump on start (`alias`, `color_rgb`, `listen`,
  `network`, `fingerprint`, all `open_channel.*` and `accept_channel.*`
  defaults). **Signal handlers are installed BEFORE `start()`** —
  otherwise SIGTERM arriving between the listen-bind log and the
  `signal.signal()` call hits Python's default handler and kills the
  process with exit -15. Don't reorder.
- **`embln-cli`** (`src/bin/embln_cli.py`) — short-lived, stateless CLI.
  Subcommands: `wallet create`, `wallet address [--branch 0|1]`,
  `wallet info`, `node info`.

Project-root wrappers at `./bin/emblnd` and `./bin/embln-cli` (executable)
insert the project root on `sys.path` so they can be invoked as
`uv run ./bin/emblnd …` standalone.

Shared helpers in `src/bin/_common.py` (do not duplicate):
- `add_common_args(p)` / `add_log_level_args(p)` — argparse boilerplate.
- `open_wallet(args)`, `resolve_db(args)`, `resolve_password(args)` —
  precedence: CLI arg → env var → default.
- `resolve_log_level(args)` — `--log-level` > `--verbose` >
  `EMBLN_LOGLEVEL` > `INFO`. Used by both CLIs and conftest.
- `setup_mpsc_logging(level, log_file)` /
  `teardown_mpsc_logging(listeners)` — see "Two MPSC log channels"
  above.
- `ColorFormatter` — the ANSI color formatter used by the stream sink.

Common options:
- `--alias` (required) — wallet alias.
- `--password` or env `EMBLN_PASSWORD`.
- `--db` or env `EMBLN_DB`; default `$EMBLN_BACKEND/data/embln.db`.
- `--port` (`emblnd` only) — LN listen port; default = random free port.
- `--log-file` (`emblnd` only) — file MPSC sink path; default
  `EMBLN_LOGFILE` env or `embln.log` next to cwd.
- `--log-level debug|info|warn|warning|error` — overrides
  `EMBLN_LOGLEVEL`.
- `-v` / `--verbose` — alias for `--log-level debug`.

The CLI never prints the mnemonic — it stays in the DB. Stateful operations
that need a running daemon (e.g. open a channel against a peer in real time)
are not implemented yet; when added, they go in `embln-cli` and talk to
`emblnd` over an IPC mechanism (Unix socket / loopback HTTP — TBD).

## Cleanup safety nets

Both `Daemon` and `EmblnNode` keep a class-level
`weakref.WeakSet[_instances]`; `__init__` registers `self`. Class
methods `Daemon.stop_all_running()` and
`EmblnNode.stop_all_listeners()` walk the registry and stop
anything still alive. `tests/conftest.py` registers both via
`atexit.register(...)` so a Ctrl-C'd test session still reaps its
bitcoind subprocesses and LN listener sockets.

Other cleanup invariants:
- `Daemon` uses `subprocess.Popen(stdout=subprocess.DEVNULL)` (not
  `PIPE`). We never read bitcoind's stdout, and an undrained PIPE
  surfaces as a `ResourceWarning: unclosed file` on test interrupt.
- `Daemon` uses `start_new_session=True` so `os.killpg` reaps the
  whole process group on `stop()`. The `Daemon.cmd` shell prefixes
  with `ulimit -n 10240 && exec` so the child raises its own
  `RLIMIT_NOFILE` (macOS default is too low for bitcoind v31+);
  Python's parent limit stays untouched — verified by the
  `_assert_rlimit_unchanged` session fixture.
- `WalletManager.close()` releases the SQLite connection;
  `make_wallet` fixture closes every cached instance on teardown;
  tests that instantiate `WalletManager` directly (recovery tests)
  use `try / finally: x.close()`. The whole suite runs under
  `pytest -W error::ResourceWarning` (when invoked with
  `--catch ResourceWarning`).

## Things to ask before doing

- Replacing the SQLite mnemonic-plaintext with encrypted storage. Adds setup
  cost and changes the recovery story.
- Switching channel-key derivation away from LND's `m/1017'/...` scheme.
  The current scheme is well-documented; switching to CLN's HKDF or Eclair's
  custom subtree is a substantial rewrite.
- Adding TLV extensions to `open_channel` / `accept_channel`. Pure
  mandatory-fields keeps the wire layout easy to read; TLV is only worth it
  when there's a concrete feature behind it.
- Adding BOLT 8 noise transport. Significant work (Curve25519 handshake,
  ChaCha20-Poly1305 frames, key rotation). Until HTLC/commitment work is
  in scope, plaintext frames are clearer.

## Things to avoid

- Logging or printing `WalletManager.mnemonic`.
- Module-level `logging.basicConfig` in test files (conftest installs the
  color formatter; per-file basicConfig is a no-op at best, an inconsistency
  at worst).
- Hardcoded ports for daemons or LN listeners (use `_free_port()`).
- Conflating `derive_funding_pubkey` (`EmblnNode`, family 0) with on-chain
  receive addresses (`WalletManager.create_new_address`, BIP84). They live on
  different sub-trees on purpose.
- Importing `EmblnNode` from `messages.py`. The leaf direction is
  enforced; reversing it produces a circular import at collection time.
- `r["error"]` in any RPC dispatch path — use `r.get("error")`.

## Code conventions

**Tooling.** `isort` sorts imports, `ruff format` formats, `pylint` lints,
`typos` catches misspellings. All four run via pre-commit and CI. Don't
bypass with `--no-verify`; if a hook fires legitimately, fix the
underlying issue.

**Naming.** Classes `PascalCase` (`WalletManager`, `EmblnNode`).
Functions and methods `snake_case`. Module-level constants `UPPER_SNAKE`
(`OP_2`, `_FAMILY_MULTISIG`). Module-private prefixed with `_`
(`_FAMILY_*`, `_REGTEST_CHAIN_HASH`, `_free_port`). Internal-only helpers
prefixed with `_` even when on a class (`_channel_pubkey`, `_basepoints`,
`_load_branch`, `_handle_open_channel`).

**Imports.** Inside `src/`, use absolute imports rooted at the package
(`from src.bitcoin.manager import WalletManager`, not relative). Tests import
through the public re-exports of `src/__init__.py` (`from src import …`)
where possible.

**Typing.** Annotate all public method signatures and module-level helpers.
Use built-in generics (`list[dict]`, `tuple[Daemon, Daemon]`) — the project
targets Python ≥ 3.10. `Optional[X]` is fine when an explicit `None` default
makes the signature clearer than `X | None`.

**Wire formats.** All BOLT 1/2 integers are encoded big-endian — match the
spec, never `"little"`. Public-key bytes are always 33-byte SEC compressed
(`pubkey.sec()`), never the 65-byte uncompressed form.

**Magic numbers.** Hex byte literals in scripts go through
`src/bitcoin/opcodes.py`; prefer `OP_CHECKMULTISIG` over `0xAE`. Push
prefixes (length bytes) computed via `len(...)` so the script construction
visibly matches BOLT 3.

**Comments.** Comment the *why*, not the *what*. Stub implementations
(`first_per_commitment_point`, plaintext mnemonic, plaintext LN frames) must
carry an inline note explaining that the simplification is deliberate, with
a pointer to what real LN would do.

**Errors.** Fail fast with descriptive `RuntimeError` / `ValueError`. Never
catch with bare `except:` (silently eats `KeyboardInterrupt` and masks
real bugs); when broad catching is genuinely needed (e.g. RPC poll loop or
the LN connection handler), use `except Exception as e` and log the type +
message.

**Logging.**
- Components: lazy `self.logger` property, named after the instance.
  `Daemon` uses `self.name`; `WalletManager` uses `self.alias`;
  `EmblnNode` uses `f"{self.alias}.ln"` for child-logger inheritance.
- `Db` uses module-level `logging.getLogger("db")` and emits **DEBUG** on
  every operation. INFO and above stay quiet by default.
- `EmblnNode.start()` and `stop()` each emit a config dump / lifecycle
  trail (start: `alias=`, `color_rgb=`, `listen=`, all `open_channel.*` and
  `accept_channel.*` defaults; stop: `stopping`, errors on socket close,
  warnings on slow thread join, `stopped`).
- Tests: take the `logger` fixture (auto-named after `request.node.name`)
  rather than calling `logging.getLogger` directly.
- Message format: `"key=value"` (printf-style with `%s`) so the color
  formatter highlights the value separately. Multi-value lines: one log
  per value, not one big dict repr.

**String formatting.** Project convention is **printf-style** `%s`, not
f-strings or `.format()` — keeps code copy-pasteable to micropython/embit
contexts where f-strings are sometimes unsupported. The
`consider-using-f-string` pylint rule is disabled. For logging
specifically, use the **lazy** form `logger.x("k=%s", v)` (W1203 enforces
this).

**Tests.**
- Files follow `test_*.py` (no custom whitelist).
- Use the cached factories `make_wallet(name)` / `make_ln_node(name, ...)`
  — same `name` returns the same instance for the lifetime of the test.
- `with_daemon=True` for chain access, `with_server=True` for the LN
  listener; both opt-in.
- For daemons, always go through `node_manager.add_and_start(name)` (or
  `make_ln_node(..., with_daemon=True)`); never construct `Daemon` ad-hoc
  in a test.
- Network/chain-dependent tests carry `@pytest.mark.integration` and skip
  cleanly when `EMBLN_BACKEND` is unset.
- Resource hygiene: tests that create their own DB connections use
  `try / finally: x.close()`.
