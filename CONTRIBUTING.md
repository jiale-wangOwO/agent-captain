# Contributing to AgentCaptain

Thanks for helping make personal multi-machine agent management more reliable.
Keep contributions small, reproducible, and explicit about authority and failure
states. This project is licensed under MIT; contribute only code and assets you
have permission to share.

## Development checks

The Python application and tests use the standard library. Install Node.js 22
and Python 3.11 or newer, then run from the repository root:

```sh
npm ci
python scripts/check_publication.py
python -m unittest discover -s tests -v
npm run test:terminal
npm run test:ui
```

`node-pty` may require the platform's native build tools when a compatible
prebuilt binary is unavailable. CI covers Windows and Linux with Python 3.11 and
3.13. Test results are evidence for those environments, not a guarantee of every
shell, tunnel, provider, or remote-machine combination.

The default suites use fake transports, temporary directories, and isolated
loopback HTTP services. Do not enable live test environment variables in CI.
Files ending in `_live.cjs` are explicitly invoked diagnostics, not part of the
normal Node suites. The opt-in Python live tests must only be used with informed
consent and disposable terminals. Never point a diagnostic at somebody else's
active work or publish its runtime files.

## Before a commit

1. Add regression tests for the relevant state transitions. A successful SSH
   banner is not enough to establish a persistent terminal workflow.
2. Keep model/provider credentials, local configuration, host aliases, absolute
   personal paths, session IDs, and logs out of source and examples. Use clearly
   synthetic fixtures such as `example-ssh-host` and `/home/example/project`.
3. Inspect the diff. Stage only intended source files, then run
   `python scripts/check_publication.py --tracked` to inspect the actual Git index.
   Review test fixture paths manually: the checker's generic personal-path
   heuristic excludes `tests/`, but its key, host/session, and private-file rules
   still apply. Do not copy real deployment data into synthetic fixtures.
4. Document compatibility limits and any opt-in or migration step. Do not claim
   a repair is deployed or a remote session is restored without checking it.

## Reliability and safety requirements

- Preserve existing sessions and other users' changes during reconnects.
- Keep remote transports behind the SSH interface; tunnel-provider-specific
  integration belongs in the independent transport tool.
- Do not automatically restart shared background services, fork locked sessions,
  delete session locks, approve startup menus, or change account/provider settings.
- Do not silently resend terminal input after an ambiguous delivery. Preserve
  stable request identifiers and verify a delivery before deciding to retry.
- Distinguish a terminal screen snapshot from persisted conversation output and
  make pagination or incomplete reads visible.
- Keep HTTP/broker authorization and loopback/same-origin checks intact. Avoid
  logging tokens, environment variables, full process arguments, or transcripts
  merely to debug a connection.

Security-sensitive reports should follow [SECURITY.md](SECURITY.md). Include
platform versions and a sanitized minimal reproduction for ordinary bug reports.
