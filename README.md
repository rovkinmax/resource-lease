# resource-lease

A small CLI for jobs that need exclusive access to an Android emulator.

Each job asks for an emulator and gets a lease. If all matching emulators are
busy, the job waits in a queue. When a job finishes—or stops renewing its
lease—the service stops that emulator. The next job gets a fresh one.

Set how many emulators may run at once; jobs wait their turn.
The tool runs locally and has no third-party Python runtime dependencies.
It does not run your tests or build commands.

## What you need

- Python 3.11 or newer on macOS or Linux. Windows is not supported.
- An Android SDK with `avdmanager`, `emulator`, `adb`, and a system image already
  installed. The tool does not download SDK components.
- A JDK compatible with your SDK command-line tools.
- Hardware acceleration and enough memory to run your configured emulators.

The real-device lifecycle has been checked on Apple Silicon with Android API 34.
The [verification notes](docs/testing.md) distinguish that check from the
fake-SDK tests run in CI.

## Install from a checkout

```sh
git clone https://github.com/rovkinmax/resource-lease.git
cd resource-lease
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/resource-lease --help
```

The examples below assume `.venv/bin` is on your `PATH`:

```sh
source .venv/bin/activate
```

Prefer a single file? [Build a zipapp](docs/testing.md#build-and-check-the-packages)
and use `python3 resource-lease.pyz` in place of `resource-lease`.

## Start a pool

Copy [examples/android.json](examples/android.json) to a file outside this
checkout. Set `sdk_root` and `java_home` to absolute paths, and choose an installed
`system_image`. The example uses an ARM64 image; use an image that matches your
host. Reserve an unused console port and its following port for ADB.

A *slot* is capacity for one emulator, not an existing device. The example has
one slot. Add slots with distinct port pairs to allow more jobs at once.

```sh
export RESOURCE_POOL="$HOME/.local/state/resource-lease/android"

resource-lease --root "$RESOURCE_POOL" init --config /absolute/path/to/android.json
resource-lease --root "$RESOURCE_POOL" start
```

`start` runs a background service. It starts an emulator only when a job needs
one. `init` is a one-time operation and will not overwrite an existing pool.

## Use an emulator

Give each new job a new lease file. Keep that file private: it contains the
credential needed to renew or release the lease.

```sh
export LEASE="$HOME/.local/state/resource-lease/job-42.lease.json"

resource-lease --root "$RESOURCE_POOL" acquire \
  --owner job-42 \
  --capability android-api-34 \
  --lease "$LEASE"
```

The command waits until the emulator is ready, then prints JSON. Use the
`endpoint.adb_serial` from that response for device commands:

```sh
# Use the serial returned by acquire, not a hard-coded serial in a shared script.
adb -s emulator-5574 shell getprop ro.build.version.sdk
```

`--owner` is your label for the job. `--capability` selects a matching slot.
Neither requires a particular CI system or task tracker.

### Keep the lease alive

**After `acquire` returns, your job must renew the lease.** With the default
120-second lease, renew at least every 40 seconds:

```sh
resource-lease --root "$RESOURCE_POOL" renew --owner job-42 --lease "$LEASE"
```

For unattended work, your job runner needs to repeat this command and stop using
the emulator if renewal fails. A shorter configured lease needs a shorter
renewal interval. There is no automatic renewal after acquisition returns.

Finish all device work before releasing:

```sh
resource-lease --root "$RESOURCE_POOL" release --owner job-42 --lease "$LEASE"
```

Release waits for teardown. If the job disappears, the service notices that
renewal has stopped and cleans up after the lease expires. The next job receives
a new emulator with no data from this one.

### Check or stop the pool

```sh
resource-lease --root "$RESOURCE_POOL" status
resource-lease --root "$RESOURCE_POOL" inspect --owner job-42 --lease "$LEASE"

# Administrative operation: ends all leases in this pool.
resource-lease --root "$RESOURCE_POOL" shutdown
```

See the [command reference](docs/reference.md) for cancellation, retries, wait
timeouts, and the JSON fields.

## Know the limits

- **Jobs must respect lease loss.** The tool controls its own emulator processes,
  not arbitrary programs using ADB. It is not a security boundary between jobs.
- **A service crash needs attention.** If the service loses track of an emulator,
  it blocks reuse of the affected ports. It does not guess which old process to
  kill. See [recovery](docs/reference.md#failure-and-recovery-boundary).
- **Do not share its ports with another emulator manager.** Pools on the same
  machine must use the same shared lock directory.
- **Android is the only included backend.** The queue and lease code are
  resource-independent, but another kind of resource needs its own adapter.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Tests use temporary directories and fake SDK processes; they do not need an
Android installation. Pull-request CI also checks package installation and
the standalone zipapp. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full local
checks and [docs/testing.md](docs/testing.md) for an opt-in real-emulator check.

## License

[MIT](LICENSE).
