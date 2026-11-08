# Standalone resource lease CLI

This tool manages a local resource pool without a CI server, task tracker,
build runner, or package registry. The core is platform-neutral; the
included Android adapter manages disposable emulators by default and supports
an explicitly configured persistent AVD.
Python 3.11+ and POSIX file locking are required. It does not install itself.

## Quick start

Copy `examples/android.json` to a private configuration file.
Set absolute paths to your installed SDK and Java home, an already installed
system image, and unused console/ADB ports. No SDK downloads are performed.
Each configured slot has capabilities and one image; add slots to configure
other images or parallel capacity. A slot reserves its console port, the
following ADB port, and transport ports at console+10000 (gRPC) and
console+20000 (HCI). All four use the existing shared physical-port guards.

From a checkout:

```sh
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" \
  init --config /absolute/path/to/pool.json
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" start
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" \
  acquire --owner worker-a --capability android-api-34 \
  --lease /absolute/private/path/worker-a.json
```

`acquire` waits in the compatible FIFO queue, renews its own waiting request,
and returns JSON only once the emulator is ready. The result includes `id`,
`state`, `generation`, `expires_in`, and `endpoint.adb_serial`.
The default wait limit is 300 seconds; `--wait` changes the client's waiting
timeout, not the resource lease. Timing out cancels that request and initiates
cleanup if boot already started. Inspect the receipt to observe cleanup.

After acquisition returns, the caller must renew:

```sh
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" \
  renew --owner worker-a --lease /absolute/private/path/worker-a.json
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" \
  release --owner worker-a --lease /absolute/private/path/worker-a.json
./scripts/resource-lease --root "$HOME/.local/state/resource-lease/demo" shutdown
```

The default lease is 120 seconds; renew at least every 40 seconds.
Neither status reads nor the service renew a client's lease. This timeout
detects a missing caller; it does not limit how long a renewing job may run.
Acquisition's wait loop
renews only while the acquiring client process is actually running.
Long work can continue indefinitely with valid renewals.

## Android acquisition readiness

An Android endpoint is published only after ADB is online, Android has booted,
Package Manager responds, and the primary emulated storage is mounted. The
adapter also writes, reads back and removes its own exclusive temporary probe.
A mounted label alone is not considered writable storage.

For clean state, it waits for Android to initialize `Download` and performs
the probe there. It does not create that standard directory or sleep for a
fixed grace period. Persistent state is probed at the storage root: a deliberately
removed `Download` is not recreated and does not prevent acquisition.
Neither mode monitors `Download` after the lease has been issued.

These checks share the existing `boot_timeout` with transport and emulator
startup. A failure or cancellation follows normal owned-child cleanup; it
does not publish an endpoint or extend the boot deadline. Probe failures appear
in the bounded ADB diagnostic log. An interrupted or failed probe can leave its
uniquely named temporary file in preserved persistent state; the adapter never
deletes a pre-existing file to make readiness pass.

Use exact-serial ADB commands while holding the lease. Complete device work
before release. Losing the lease means stop using the endpoint immediately.
The tool does not run, stop, or inspect Gradle, IDE, or arbitrary caller jobs.

## Android launch defaults

The selected SDK must include executable `emulator`, `netsimd`, `adb` and
`avdmanager` tools. Each lease owns an emulator and one same-SDK netsimd direct
child. An explicit packet-streamer endpoint connects them without shared
daemon discovery. Even an image with no active netsim-backed capabilities
gets its own transport process; the adapter does not duplicate SDK feature
resolution.

SDK/image defaults select Bluetooth, UWB, NFC, Wi-Fi, graphics, cameras and
other hardware. The adapter no longer forces radio features or audio off.
It removes ambient `ANDROID_EMULATOR_FEATURES` and `NETSIM_*` overrides from
these two child environments so unrelated shell experiments cannot redirect
the default profile. Ordinary proxy/DNS environment remains inherited.
It does not change the image or existing AVD hardware configuration.

The default remains headless, with cold boot, no snapshot load/save, and no
boot animation. Metrics and external crash reporting are disabled for
noninteractive operation. Explicit memory configuration remains a requested
resource budget, not a hard limit: the SDK can increase undersized requests.
The normal audio backend may require host permissions; this tool does not
grant permissions or dismiss prompts.

The transport has explicit per-slot gRPC/HCI ports and instance, bounded
stderr logs, no CLI/web control UI or synthetic test beacons. The adapter's
transport startup and emulator readiness share the configured boot deadline.
SDK automatic shutdown is disabled: a lack of connected netsim devices must
not shorten a renewing lease. Either child exiting makes the resource unhealthy.
Every teardown attempts to stop both owned children before checking all four
ports. A child that ignores termination is killed through its owned live
handle; uncertain teardown quarantines the slot rather than granting another owner.

Android preferences stay in the private Android home. Transport temporary
state is generation-local, including in persistent mode. Real HOME remains
unchanged. Release removes only marked disposable state after child teardown;
persistent AVD data is preserved.

This launch policy changes default Wi-Fi transport and restores normal audio;
removing feature overrides does not imply every image supports every radio.
The [testing guide](testing.md#runtime-qualification) records the tested SDK,
image and actual lifecycle/storage coverage. Fake-SDK tests alone do not
establish Bluetooth/network functionality, audio playback, NFC/UWB operation
or real persistent-mode compatibility.
The earlier smoke result in `docs/testing.md` describes the previous launch
policy, not this candidate.

## Android state modes

Android slots accept `state_mode: "clean" | "persistent"`. Omitting
`state_mode` keeps the existing clean, disposable behavior. Clean mode rejects
`state_directory`; persistent mode requires an absolute `state_directory`
whose parent already exists and is owned by the current user. The state
directory itself must be a private, real directory, not a symlink.

Persistent mode creates one tool-owned AVD under that directory, outside
`generations/`. Its manifest records the canonical state identity, installed
image identity, memory profile, and initialization status. Reuse requires a
complete matching manifest and intact AVD metadata. A newly created AVD becomes
reusable only after a successful boot. If initialization fails, its partial
directory and manifest remain; later requests fail closed rather than adopting
or deleting them. Unknown directories, incompatible image or memory profiles,
and unexpected state-directory entries are likewise preserved and rejected.

Release and lease expiry stop both owned children but preserve persistent AVD
data. Persistent directories are not included in generation diagnostic pruning.
The physical guard includes the canonical state identity as well as all four
emulator/transport ports, so two pools cannot start the same AVD simultaneously even
when their configured ports differ. Sequential leases may use different ports.
A state directory inside any existing pool's `generations/` tree is rejected,
including parent symlinks and case aliases of the same physical directory.
Directly below a pool root, that name and its case variants are reserved even
before the first generation exists. Unrelated directories named `generations`
outside pools are not reserved. Rejection preserves existing data; it does not
relocate or repair a previously misplaced state directory.

Keep a persistent state directory dedicated to this tool and profile. Do not
point it at a developer AVD or open persistent-mode configuration with an
older binary. There is no automatic reset or recovery command for incomplete
or incompatible state.

## Commands and results

All commands take global `--root` before the subcommand.

- `init --config FILE`: initialize a new private pool; never overwrite state.
- `start`: start its detached service, or report an already healthy service.
- `serve`: run the same service in the foreground.
- `status`: observe service, slots and requests; no initialization or renewal.
- `acquire --owner KEY --capability NAME --lease FILE [--wait SECONDS]`.
- `inspect --owner KEY --lease FILE`: inspect one receipt without renewal.
- `renew --owner KEY --lease FILE`: renew a still-valid request or lease.
- `release --owner KEY --lease FILE [--wait SECONDS]`: fence further client
  operations, stop the owned emulator, then await cleanup.
- `cancel --owner KEY --lease FILE [--wait SECONDS]`: cancel waiting/starting
  work, or stop an active lease.
- `shutdown [--wait SECONDS]`: revoke requests, stop owned resources, and stop
  the service. This is an explicit administrative operation, not a caller lease.

Successful commands emit one JSON value on stdout and exit zero. Rejections
and failed cleanup exit nonzero with a bounded diagnostic on stderr.
`inspect`/`status` can successfully report failed or quarantined state; read
their fields. Administrative shutdown stops the service but does not clear
existing quarantines or historical crash markers.

`cleanup_pending=true` means a request is fenced but its resource is not yet
free. A wait timeout does not force release or grant the next caller a slot.
Repeated release does not affect a later owner. Old receipts are never renewed
after expiry, cancellation, service restart, or completion.

## Identity, retries and storage

`owner` is a caller label, not an OS authentication boundary.
The client persists a random secret, request UUID, service epoch and immutable
request parameters to an owner-private receipt before publishing the request.
The database stores only the secret's hash. Never print or share receipt files.
Secrets are not supplied through command arguments or emitted by ordinary status.

Retry the same acquisition with the same receipt file to recover a lost
response. Do not create a replacement receipt for an ambiguous request.
Changing its owner or capability is rejected. A receipt can first register only
within 60 seconds of creation and in its original service epoch. Recent
terminal records survive that window, so pruning cannot resurrect old work.
After terminal history is pruned, inspection reports not-found, not success
or authority to replay. A new intentional operation uses a new receipt file.

The private pool contains one SQLite authority, a service singleton lock,
bounded service logs, and disposable generation directories. Persistent AVD
state, when configured, lives separately from those generations. Slots have
`idle`, `starting`, `leased`, `stopping`, or `quarantined` states.
Only the service owns backend child handles. Clients mutate intent through
short SQLite transactions; there is no external coordinator or RPC dependency.

Default limits are 1024 stored requests, 16 slots, one live generation per slot,
and one hour of terminal request history. Pending requests and recent retry
tombstones can fill capacity; this rejects new admission but never prevents
existing release. Only a bounded number of completed generation diagnostics
are retained (at most 16, fewer for a small configured request limit).
Quarantined generations are retained. Each backend bounds its process logs.

Physical locks and crash interlocks live in the shared per-user directory
`~/.local/state/resource-lease/guards`, independently of pool roots.
`RESOURCE_LEASE_HOME` changes this shared namespace for an entire installation,
including tests; every cooperating pool must use the same value. Changing it
while resources exist defeats cross-pool exclusion and is unsupported.
Pool and slot labels do not change physical port keys. Persistent Android
state adds its canonical directory identity to the physical guard. This tool is
not compatible with other emulator managers on the same ports: reserve a
separate port range.

## Failure and recovery boundary

When renewal stops, the healthy service revokes the lease, stops only its own
emulator and transport using their live child handles, and verifies teardown. A compatible
waiting caller gets a new disposable AVD in clean mode or the retained AVD in
persistent mode. A cancellation during boot cannot publish a late endpoint.
An ordinary start failure is reported only after cleanup; uncertain cleanup
quarantines that slot.

The Android backend creates AVD state with the installed SDK. Clean mode keeps
that data in its disposable generation; persistent mode creates a private,
manifest-owned AVD outside the generation tree. It does not copy live disk
images, adopt existing AVDs, reset developer devices, kill adb-server, or
signal a PID restored from a file. Successful clean teardown removes only its
explicitly disposable AVD data. SDK helper controls are part of this adapter's
qualified launch profile.

**Service crash is different from client loss.** On service restart, previous
active custody is quarantined; a durable marker prevents another pool from
claiming the same physical ports. A free port or elapsed time does not erase
that marker. This version deliberately has no force-clear or automatic orphan
adoption command. Inspect and arrange explicit operator recovery; do not edit
the database, delete locks, change guard namespaces, or move to another pool
to bypass unresolved custody.

This is cooperative ownership for trusted same-user tooling, not an OS sandbox.
Lease credentials fence calls through this CLI; they cannot prevent an old
arbitrary process from running direct ADB against a reused serial. Callers and
future wrappers must obey lease loss. This version makes no claim that it
contains arbitrary build children or validates their results.

## Standalone packaging and verification

```sh
./scripts/build-resource-lease-package --output /absolute/new/path/resource-lease.pyz
python3 /absolute/new/path/resource-lease.pyz --help
```

The zipapp includes only the standalone core and Android backend. Run it from
outside the checkout; no orchestration tool or third-party Python packages are
required. Package construction does not install anything or start a resource.
Existing output files are not replaced.

Focused source checks:

```sh
python3 -m unittest discover -s tests -p 'test_resource_lease_*.py'
```

Fake-SDK tests prove scheduling and process failure paths, not real device
qualification. Real qualification must separately exercise boot, explicit
lease renewal, release, compatible waiting, fresh successor state, owner-loss
recovery, and final process/port cleanup. Report only the SDK/OS combinations
actually tested. A consumer's application tests remain its own responsibility.

The [testing guide](testing.md#runtime-qualification) records the qualified SDK
and host combination, with a procedure for repeating the real lifecycle check.

## Integrating a job runner

Choose a shared guard directory and non-conflicting ports. The runner needs to
renew leases, stop device work when a lease is lost, and leave unresolved
service-crash quarantine for an operator. Installing the CLI does not set up
these behaviors for a consumer.
