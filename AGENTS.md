# Working on resource-lease

## Scope and layout

This is a local resource lease CLI, not a build runner or a CI orchestrator.
The only shipped resource backend is Android. Keep code and documentation in
English. Do not claim support for a backend or platform that has not been tested.

- `resourcelease/model.py`: configuration validation, shared types, backend protocol.
- `resourcelease/store.py`: SQLite authority, requests, credentials, state transitions.
- `resourcelease/service.py`: scheduling, physical guards, owned worker lifecycle.
- `resourcelease/process.py`: direct-child supervision and bounded process output.
- `resourcelease/backends/android.py`: installed SDK tools and disposable AVDs.
- `resourcelease/cli.py`: commands and private receipt files.
- `resourcelease/__main__.py`: executable assembly and installed entry point.
- `tests/`: deterministic tests, including a fake SDK that runs real child processes.
- `scripts/`: packaging, distribution checks, and opt-in real Android smoke.

The core must not import Android details. Add a backend only for an approved
use case; do not add plugin discovery or a second scheduler speculatively.
Runtime code uses the Python standard library. Discuss new runtime dependencies.

## Invariants to preserve

1. SQLite is the ownership authority. Hold transactions briefly; never wait for
   SDK commands, threads, or resource teardown inside one.
2. A request is identified by its original private receipt. Retries must not
   create a second request. Expired or cancelled credentials cannot be revived.
3. Capture the current time after acquiring the relevant transaction, not before
   waiting for its lock. Expiry and publication decisions must use fresh time.
4. Fencing a lease does not prove cleanup. Keep `cleanup_pending` until teardown
   is verified. Never grant a successor while custody is uncertain.
5. Retain physical guards through startup, use, and teardown. Pool names do not
   create independent physical resources. Both emulator ports are reserved.
6. Stop processes only through owned live child handles. Do not adopt numeric
   PIDs, run broad process kills, kill adb-server, or reset developer devices.
7. Unknown cleanup means quarantine. Do not add force-clear, age-based unlock,
   PID adoption, or namespace-switching workarounds without an explicit design
   decision and failure-path tests.
8. Delete only this invocation's disposable generation data after ownership and
   teardown checks. A failed exclusive create must never delete existing data.
9. Bound logs, captured output, stored requests and retained diagnostics.
   Preserve quarantine evidence; do not hide cleanup failures.

This is cooperative ownership between trusted same-user callers, not a sandbox.
Do not describe lease tokens as preventing direct ADB use by arbitrary programs.

## Making a change

- Read the affected module, tests, and `docs/reference.md` first.
- Reproduce a bug with a deterministic failing test before changing runtime code.
- Keep fixes focused. Preserve existing user changes. Do not edit generated build
  artifacts as a substitute for changing source.
- Update the reference and examples when changing CLI or configuration behavior.
- Avoid broad refactors mixed with lifecycle fixes. Do not change receipt or
  database formats without discussing compatibility and recovery.
- Do not commit, push, publish packages, change repository settings, or run a
  real emulator unless the user or an applicable workflow authorizes that effect.
- Never put credentials, lease files, device data, private logs, or host-specific
  paths in source, issue text, commits, or CI artifacts.

## Verification

Use a local virtual environment and `requirements-dev.txt`. Before handing off:

```sh
python -m ruff check .
python -m unittest discover -s tests -p 'test_*.py'
python -m build
python -m twine check --strict dist/*.whl dist/*.tar.gz
```

For packaging, entry-point, or launch changes, also run the installed-wheel and
zipapp checks in `docs/testing.md`. They must execute outside the checkout.
Changes to expiry, cancellation, guards or cleanup need failure-path tests,
not only a successful acquisition.

Tests must clean up their own children even when an assertion fails: register
cleanup immediately after spawning or obtaining a resource. Use isolated
temporary state and guard directories. Do not weaken a test or retry away an
unexplained failure to make CI pass.

Real Android testing is opt-in. Follow `docs/testing.md`, use a fresh dedicated
pool and explicit unused ports, and do not download SDK components or touch
existing devices implicitly. Report what was actually tested and any leftover
resource or quarantine. Fake-SDK tests are not evidence of real device support.

## Writing

README readers want to run the tool, not study its internals. Lead with the
problem, show executable commands, explain renewal before users lose a lease,
and link to the reference for details. Prefer short, ordinary sentences over
marketing claims, abstract jargon, or repeated lists of guarantees.
