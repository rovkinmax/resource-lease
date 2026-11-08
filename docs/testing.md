# Testing and packaging

## Automated checks

Install `requirements-dev.txt` in a virtual environment, then run:

```sh
python -m ruff check .
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover lease expiry, concurrent admission, compatible FIFO scheduling,
stale receipts, cancellation during boot, cleanup failures, cross-pool guards,
service-crash quarantine, persistent AVD reuse, expiry retention, and
rejection of unknown or incompatible persistent state. The fake Android SDK
launches real subprocesses and opens local TCP ports. It does not start an
Android VM or download an SDK.

CI runs these checks on Linux and macOS. Its job timeouts limit hung checks,
not how long a client may hold a renewed lease.

## Build and check the packages

From a clean build directory:

```sh
python -m build
python -m twine check --strict dist/*.whl dist/*.tar.gz
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
emulator leases in the selected clean or persistent mode. It does not install
SDK components.

Before running:

1. Choose an installed system image compatible with your host and a working JDK.
2. Choose the mode explicitly. For clean mode, copy `examples/android.json`;
   for persistent mode, copy `examples/android-persistent.json`. Fill in the
   paths, use exactly one slot and `lease_seconds: 30`, and reserve dedicated
   unused ports. Persistent mode requires a private state directory with an
   existing owner-owned parent.
3. Choose a **new, nonexistent** pool directory. Do not point this check at a
   developer's pool or change the shared guard directory to bypass a quarantine.
   Persistent mode may reuse only state previously created by this tool with a
   matching complete manifest and image profile; it is never reset.
4. Build the zipapp and make sure no other emulator manager uses those ports.

```sh
python scripts/smoke-android \
  --mode clean \
  --package dist/resource-lease.pyz \
  --config /absolute/path/to/smoke-config.json \
  --root "$HOME/.local/state/resource-lease/smoke-unique-run"
```

For persistent-mode acceptance, use `--mode persistent` and the persistent
example configuration. The script writes a unique marker through exact-serial
ADB and requires it to be absent on later clean leases or present on later
persistent leases. Persistent state remains on disk after pool shutdown.

The script acquires A, renews it, writes a device-local marker, queues B, and
releases A. B must receive the next generation. In clean mode its AVD must not
contain A's marker; in persistent mode it must. B then stops renewing; C must
receive the next generation after B expires and obey the same mode-specific
marker expectation. Finally, the script releases C and shuts down the pool
through the public CLI.

It uses only exact-serial ADB commands. On failure it requests shutdown, but does
not force-clear quarantine or kill a process by a recorded PID. Inspect any
failure before retrying. Do not bypass an unresolved guard with another root.

Successful output is a small JSON report without receipt secrets. Receipts and
pool state remain private under the selected root; never upload them as artifacts.
Afterward, check that no emulator/helper process or listener remains on the
chosen ports. Only bounded diagnostic logs should remain in `generations/`.

### Runtime qualification

The original standalone build passed this three-generation clean-mode lifecycle
on October 25, 2026 with:

- macOS 27.0.1, Apple Silicon;
- Android Emulator 37.2.10.0;
- Java 17;
- `system-images;android-34;google_apis;arm64-v8a`, 2048 MiB.

The service stopped, both ports were free, and disposable AVD data was removed.
That initial run did not qualify persistent mode. This is not a claim that every
Android SDK version or Linux host has been qualified with a real emulator.

#### Storage readiness and owned transport

On November 8, 2026, the candidate with storage readiness and CLI-owned netsimd
lifetime passed separate clean and persistent three-generation lifecycles with:

- macOS 27.0.1 on Apple Silicon;
- Android Emulator 37.2.10 and netsimd 1.0.23;
- Java 17, `system-images;android-34;google_apis;arm64-v8a`, revision 14;
- 2048 MiB requested memory, a 180-second boot deadline and 30-second leases.

Each clean acquisition was followed immediately by a PDF push into `Download`,
digest comparison and removal. All three passed without creating the directory
or adding a boot grace period. In a new dedicated persistent AVD, its empty
`Download` was deliberately removed; later release/reacquire and expiry/reacquire
preserved both a device-local marker and the directory's absence.

In each mode, the first lease was renewed beyond its original TTL, the second
waited in the queue until release, and the third followed the second's expiry.
Both services stopped with idle slots, no cleanup pending and no quarantine.
Independent process/listener checks confirmed teardown and release of all four
ports. Disposable generation homes were removed; the dedicated persistent AVD
was retained with a complete manifest.

This qualification covered Android-shell readiness and resource lifecycle, not
application UI tests, account flows, audio playback, or radio feature behavior.
