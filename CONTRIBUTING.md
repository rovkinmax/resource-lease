# Contributing

Start with a focused issue or pull request. For a new backend, storage-format
change, or recovery mechanism, discuss the behavior before implementing it.
The project intentionally does not manage build jobs.

## Local setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-build-isolation -e .
python -m ruff check .
python -m unittest discover -s tests -p 'test_*.py'
```

The tests do not need an Android SDK. They run fake tools as real subprocesses
and use temporary directories for state. See [testing](docs/testing.md) for
package verification and the separate real-emulator smoke check.

## Pull requests

Explain the behavior being changed and how you verified it. Bug fixes should
include a regression test. If a command, JSON field, or configuration option
changes, update its documentation in the same pull request.

CI runs on pull requests and pushes to `main`. It checks Python code, tests on
Linux and macOS, builds distributions, and exercises an installed wheel and a
zipapp outside the checkout. CI does not publish packages or start real Android
emulators. The stable summary check is named **CI**.

Do not include lease receipts, SDK data, local pool state, or private logs in
commits. [AGENTS.md](AGENTS.md) documents the lifecycle invariants for both human
contributors and coding agents.
