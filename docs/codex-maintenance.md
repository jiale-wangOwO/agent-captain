# Codex CLI and shared daemon maintenance

The CLI installation and the separately managed `app-server daemon` package can have different versions and defaults. Updating one does not prove the other was updated. A startup compatibility menu is not an SSH transport failure.

AgentCaptain includes the optional, standard-library-only `scripts/codex_sync_daemon.py` helper for **Linux, the current user, and an existing trusted CLI installation**. It is not a general process manager and is not run automatically by the Hub.

## Run on the worker machine

Use an ordinary shell on the machine whose Codex daemon you intend to maintain. Copy or clone the helper there as part of your own deployment; the Hub does not install it automatically.

```sh
# Default: read-only status and compatibility check
python3 scripts/codex_sync_daemon.py --check

# Explicit maintenance, after checking that nobody will start new clients
python3 scripts/codex_sync_daemon.py --sync --timeout 60
```

The default CLI entry is `~/.local/bin/codex`. If your current-user installation uses another trusted home-directory entry, provide it explicitly:

```sh
python3 scripts/codex_sync_daemon.py --check --cli "$HOME/.npm-global/bin/codex"
```

The timeout is a single total budget, at most 60 seconds. Output is JSON. `--check` performs no update; `--sync` is the only mutation mode.

## What synchronization means

The helper compares the installed CLI, the managed package, and a running daemon when present. It checks file ownership, path identity, supported status shape, and the current user's Unix socket. It reads the CLI and managed binaries' feature defaults without reading provider or authentication files.

For a running daemon, it connects to the existing Unix WebSocket, initializes a read-only client, and queries `thread/loaded/list`. It never requests history or conversation bodies. **Any loaded session, including an idle one, blocks synchronization.** A client may leave an idle thread loaded for a while after exiting; this is not justification to kill the process or delete a lock.

Immediately before a needed update, it checks identity/status again and checks the loaded-session set again. If those checks succeed, it invokes the installed CLI's official command exactly once:

```sh
codex app-server daemon update --from-cli --yes
```

The target is the **currently installed CLI**, not a separately selected latest release. Updating the CLI itself remains the user's normal installation workflow. The helper does not edit model providers, authentication, configuration, or feature flags; it does not kill independent worker or stdio app-server processes.

## Important results

| Action | Meaning and next step |
|---|---|
| `checked` | Read-only check completed; inspect `syncNeeded` and evidence fields |
| `already_synchronized` | Supported checks agree; no update was performed |
| `blocked_loaded_threads` | At least one thread is loaded; no update, even if it appears idle |
| `synchronized` | One update was performed and supported post-update checks passed |
| `refused` | Preflight was unknown, unsupported, or unsafe; no update was attempted |
| `update_unverified_do_not_retry` | The update may have happened, but verification did not finish; do not retry blindly |

For an unverified update, inspect the installed CLI's `codex app-server daemon version` before deciding what to do next. Do not repeatedly confirm restart menus, kill every Codex process, delete session locks, fork a replacement conversation, or change the provider merely to hide a compatibility warning.

## Maintenance is not an atomic server lock

The final probe and the official update are **not a single atomic server-side transaction**. A new client could appear between them. Coordinate the maintenance window yourself and do not open new shared-daemon clients while synchronization is in progress.

Exit clients only after their work and drafts are safely handled. This does not require closing the Hub's shell/tmux terminal. After maintenance, restart or resume Codex yourself using the intended model/provider and session; the helper does not send a business task or choose a conversation.

## Verification limits

- The running `backend=pid` schema and real synchronization were verified with Codex CLI **0.161.0** on Linux. Unknown backend/status/socket schemas fail closed.
- A stopped `backend=pid` daemon path has isolated regression coverage, but was **not validated against a live stopped daemon**. Do not generalize the running-case result to all lifecycle states or future releases.
- Feature comparison establishes **CLI and managed-package defaults**. The daemon-version response does not expose its active feature overrides; `cliAndManagedDefaultsMatch` is not an audit of all running overrides. The report explicitly marks `runningFeatureMode` as unreported.
- Personal-group path checks use enumerable NSS user/group information. They cannot prove the absence of hidden directory-service/LDAP membership and do not replace an identity or ACL audit. Use only a CLI installation you already trust.
- The helper checks the current user's managed daemon, not arbitrary external installations or every independently running app-server.

For the supported protocol and commands, see [official Codex App Server documentation](https://learn.chatgpt.com/docs/app-server) and the [official CLI reference](https://learn.chatgpt.com/docs/cli/reference). Local implementation behavior is determined by the installed CLI and this helper's explicit guards, not by a promise of cross-version compatibility.
