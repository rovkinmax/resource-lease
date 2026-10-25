# Testing and packaging

## Automated checks

Install `requirements-dev.txt` in a virtual environment, then run:

```sh
python -m ruff check .
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover lease expiry, concurrent admission, compatible FIFO scheduling,
stale receipts, cancellation during boot, cleanup failures, cross-pool guards,
and service-crash quarantine. The fake Android SDK launches real subprocesses
and opens local TCP ports. It does not start an Android VM or download an SDK.

CI runs these checks on Linux and macOS. Its job timeouts limit hung checks,
not how long a client may hold a renewed lease.

## Build and check the packages

From a clean build directory:

```sh
python -m build
python -m twine check --strict dist/*
python scripts/build-resource-lease-package --output dist/resource-lease.pyz

python -m venv .venv-installed
.venv-installed/bin/python -m pip install --no-deps dist/*.whl
python scripts/check-distribution \
  --python .venv-installed/bin/python \
  --zipapp dist/resource-lease.pyz
```

The distribution check runs from temporary directories outside the checkout.
It exercises acquisition, renewal, release and detached service shutdown through
the installed command, `python -m resourcelease`, and the zipapp.

The zipapp builder refuses to replace an existing file. Choose a new output
path when rebuilding. Build artifacts are local; these commands do not publish
to a package registry or GitHub Releases.

## Real Android smoke check

This is an explicit local operation, not a PR check. It starts and stops three
disposable emulator generations. It does not install SDK components.

Before running:

1. Choose an installed system image compatible with your host and a working JDK.
2. Copy `examples/android.json` outside the checkout and fill in the paths.
   Use exactly one slot, `lease_seconds: 30`, and dedicated unused ports.
3. Choose a **new, nonexistent** pool directory. Do not point this check at a
   developer's pool or change the shared guard directory to bypass a quarantine.
4. Build the zipapp and make sure no other emulator manager uses those ports.

```sh
python scripts/smoke-android \
  --package dist/resource-lease.pyz \
  --config /absolute/path/to/smoke-config.json \
  --root "$HOME/.local/state/resource-lease/smoke-unique-run"
```

The script acquires A, renews it, writes a device-local marker, queues B, and
releases A. B must receive a fresh device without the marker. B then stops
renewing; C must receive the next generation after B expires. Finally, the
script releases C and shuts down the pool through the public CLI.

It uses only exact-serial ADB commands. On failure it requests shutdown, but does
not force-clear quarantine or kill a process by a recorded PID. Inspect any
failure before retrying. Do not bypass an unresolved guard with another root.

Successful output is a small JSON report without receipt secrets. Receipts and
pool state remain private under the selected root; never upload them as artifacts.
Afterward, check that no emulator/helper process or listener remains on the
chosen ports. Only bounded diagnostic logs should remain in `generations/`.

### Runtime qualification

The original standalone build passed this three-generation lifecycle on
October 25, 2026 with:

- macOS 27.0.1, Apple Silicon;
- Android Emulator 37.2.10.0;
- Java 17;
- `system-images;android-34;google_apis;arm64-v8a`, 2048 MiB.

The service stopped, both ports were free, and disposable AVD data was removed.
This is not a claim that every Android SDK version or Linux host has been
qualified with a real emulator.
