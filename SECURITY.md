# Security

This tool coordinates trusted jobs running as the same operating-system user.
It does not isolate untrusted workloads. A process that bypasses the CLI can
still use ADB directly, and a process running as the same user may read private
files. Keep lease receipts private and stop device work when renewal fails.

Only the current default branch is maintained at this stage.

For a suspected vulnerability, use **Security → Report a vulnerability** on the
repository if private reporting is enabled. Do not put credentials, lease files,
device data, or exploit details in a public issue. If private reporting is not
available, open an issue asking for a private contact without sensitive details.
