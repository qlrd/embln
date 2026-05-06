# embln

Embit-powered Lightning learning sandbox: a small Python codebase that wires
[diybitcoinhardware/embit](https://github.com/diybitcoinhardware/embit) up to a
real `bitcoind` running in regtest, and walks through the BOLT specs by building
the pieces from scratch — wallet, channel keys, funding-tx output, BOLT 2
`open_channel` / `accept_channel` messages.

The goal is pedagogical, not production. Every shortcut (plaintext mnemonic in
SQLite, stub per-commitment-secret derivation, simplified entropy story) is
called out in code comments.

## Running it

Install dependencies (uv recommended; submodule must be initialised):

```sh
git clone --recurse-submodules <this-repo>
cd embln
uv sync
```

Build a `bitcoind` binary from source into `./build/bin/`:

```sh
uv run poe build-core # default: v31.0
BITCOIN_REVISION=30.0 uv run poe build-core
```

Run the test suite (the poe task auto-sets `EMBLN_BACKEND=./build/bin`):

```sh
uv run poe tests
uv run poe tests --verbose                        # + tee-sys + color logs
uv run poe tests --coverage                       # writes coverage.xml + htmlcov/
uv run poe tests --catch ResourceWarning          # promote a warning to error
```

## CLI

Two binaries live at `./bin/`:

```sh
./bin/embln-cli wallet create  --alias alice --password p --db /tmp/a.db
./bin/embln-cli wallet address --alias alice --password p --db /tmp/a.db
./bin/embln-cli node info      --alias alice --password p --db /tmp/a.db

./bin/emblnd --alias alice --password p --db /tmp/a.db   # long-running listener
```

Common env vars: `EMBLN_PASSWORD`, `EMBLN_DB`, `EMBLN_LOGFILE`,
`EMBLN_LOGLEVEL=debug|info|warn|error`. CLI flag `--log-level X` overrides
the env var.
