# PRON Hub

<p align="center">
  <img src="assets/pron-hub-logo.png" alt="PRON Hub" width="640">
</p>

**Personal Remote Ops & Nodes Hub** — one Codex conversation to coordinate the working agents you choose, on your computer and remote Linux machines.

[中文说明](README.zh-CN.md) · [Management-session design and limits](docs/management-session-product.md) · [SSH transport contract](docs/ssh-transport.md) · [Codex maintenance](docs/codex-maintenance.md) · [Security](SECURITY.md)

PRON Hub is a single-user, local-first connection service for Codex. **Version 0.5 adds a management-session MVP:** create a real local Codex conversation from the Hub, with its role, tools, and explicit worker scope initialized for you. It is experimental; version-specific live integration and desktop transfer are not certified by the unit tests.

The **controller is the model in that native Codex conversation**, acting as your CEO: collect evidence, convey tasks, and summarize what needs your decision. The human retains final authority. The Hub runs no separate model backend, does not inject a CEO role into workers, and does not automatically wake a dormant conversation. Each worker still has a real terminal in which you can use your normal Codex workflow or talk directly.

## What it does

- Connects local terminals and SSH machines configured in the UI.
- Creates and restores one default local management conversation, using an exact native Session ID rather than a newest-file guess.
- Prepares a dedicated management workspace, persistent role guidance, and a scoped MCP tool connection without manual plugin installation.
- Opens a shell in a selected project directory, or the user's home directory.
- Lets the human and controller read and write the same worker terminal.
- Limits new management sessions to server-enforced worker and project-directory grants; startup does not scan every machine or history.
- Reads persisted output from an exactly associated Codex session, including paginated long messages and optional tool records.
- Preserves delivery identity across uncertain submissions and gives human typing priority.
- Keeps terminals independent of the browser and Python UI service.
- Uses standard SSH aliases, including aliases backed by an external tunnel adapter.

This is **Codex-focused**, not a universal CLI automation framework, a multi-user control plane, or a hosted cloud service. It does not seamlessly take over arbitrary terminals already running elsewhere.

## Requirements

| Where | Requirements |
|---|---|
| Control computer | Python 3.11+, Node.js 22+, npm, a modern browser |
| Local workers | An installed Codex CLI and the user's normal authentication/provider setup |
| Remote workers | Linux, OpenSSH access, Python 3, tmux, and Codex installed for the remote user |
| Hub-created manager | A configured local Codex CLI supporting `exec --json`, exact-session `resume`, and stdio MCP |
| Optional existing-chat plugin | A local Codex client supporting plugins and stdio MCP |

The Python service uses the standard library. Terminal dependencies are installed with npm; browser assets are served locally, without a CDN. A native-build toolchain may be needed if `node-pty` cannot use a prebuilt binary.

## Quick start

```sh
git clone https://github.com/jiale-wangOwO/pron-hub.git
cd pron-hub
npm ci
python app.py serve
```

Open [http://127.0.0.1:8766/](http://127.0.0.1:8766/). If the repository is still private, cloning requires GitHub access.

On Windows, `run_pron.cmd` starts the per-user service in the background and opens the UI. `run_ceo.cmd` is a backward-compatible alias. For explicit background operation:

```sh
python service.py start
python service.py status
```

No administrator service or automatic startup is installed. A fresh configuration contains **only the local machine**. Add remote machines yourself in the UI, using a generic SSH alias such as `dev-workstation`; credentials remain in your existing SSH setup.

## Start your management conversation

1. Configure and connect the workers below. The initial scope picker uses only already-registered, activated workers; no historical discovery runs automatically. You may start with an empty scope.
2. Click **Start management session** on the home card. Keep the default name or choose one, then explicitly select the workers this manager may access. Creating additional workers is disabled by default; optionally grant a connected machine and a concrete project directory using the directory chooser.
3. The Hub prepares its own management workspace and MCP connection, then runs a one-shot, **read-only** native Codex initialization. It obtains the Session ID from the owned launch's native JSON event and asks only for `get_controller_context`. No worker task, history read, or new worker is part of initialization.
4. Continue in the real management terminal. **Ready** requires both exact identity and an authenticated tool handshake, together with a usable terminal; a PID, visible welcome message, or connected PTY alone is not enough. Complete required login, trust, or permissions yourself. Provider and authentication settings are not rewritten.
5. Thereafter use **Continue management**, which restores the original conversation. Use **Management scope** to change its grants. Repeated clicks, refreshes, and uncertain responses retain the original operation and do not force a second manager.

For this workflow, **do not manually install the legacy plugin**: the Hub-generated manager already receives its scoped tool configuration. Before starting, make sure your local Codex installation and normal account/provider setup work. If initialization fails or its outcome is unknown, inspect the reported phase and original identity; the Hub does not delete locks, fork, or restart the shared Codex background server to conceal an error.

The default management workspace is outside the source checkout: `%LOCALAPPDATA%/PRONHub/management-workspaces/<controller-id>` on Windows, or `$XDG_STATE_HOME/pron-hub/management-workspaces/<controller-id>` (default `~/.local/state/...`) on Linux/macOS. This avoids trusting the whole Hub repository just to enter a manager. Codex may still require you to confirm trust for that new owned directory; the Hub never accepts this prompt for you.

The first self-check inherits your model/reasoning settings and can take several minutes. Its asynchronous launch remains visible; the Hub does not switch models or start duplicates because it is slow. Initialization is bounded to ten minutes, preserving any verified Session ID for reconciliation and exact resume after a timeout.

Resume retains the witnessed local Codex executable entry, instead of resolving a different version from a new shell's `PATH`. If that installed entry is removed, restoration stops with an environment error; it does not silently downgrade to another CLI or alter your provider setup.

For example:

> Read the authorized slides and server workers, summarize actual progress and evidence, and list decisions I need to make. Do not send new tasks yet.

> Send this fix request to the authorized server worker, then summarize its result and verification.

### Continue in the Codex desktop app (experimental)

**Continue in Codex** requires you to confirm that the manager is idle with no unsent draft. The Hub checks its owned terminal, releases that input entry, and offers a `codex://threads/<exact-session-id>` link for the same saved conversation. It does not create a new chat or automatically send `/app` or another command.

The resulting status is **`desktop_unverified`**, not a verified seamless handoff. Opening a link does not prove the desktop app acquired the conversation or reloaded the scoped tools, project trust, model, and provider. Inspect those in the desktop app yourself; the Hub stops writing while the desktop owns the entry. To return, close the other entry and use **Continue management**, which attempts the same exact Session ID without reinitializing or resending business tasks. Occupancy and configuration errors remain explicit.

## Connect your workers

1. Select or configure a machine, then open a terminal. Use the directory chooser, enter an absolute directory on that machine, or leave it blank and `cd` from the home directory.
2. In that terminal, run `codex`, `codex-switch`, or your usual launcher. The human handles provider selection, login, secrets, OTPs, and approvals.
3. To continue an existing conversation, use the on-demand history finder and follow the displayed `codex resume <session-id>` command yourself. Exit the original client first; opening another terminal is not a transfer of ownership.
4. Activate the connected terminal, then explicitly select it in your new manager's scope. Activation does not start a model and is not, by itself, a grant to every scoped manager. Inactive terminals remain directly usable by the human.

Workers and managers are separate records and terminal services. A manager is not registered as its own worker; scoped tools reject manager-to-manager operations and recursive management creation. These are Hub API safeguards, not operating-system sandbox isolation.

## Optional: connect an existing Codex chat with the legacy plugin

This is a compatibility path for a chat you already run, not the new management-session workflow. **Legacy plugin credentials retain the shared global activation list:** multiple such chats have the same activated-worker access, not per-manager isolation. Prefer a Hub-created scoped manager when you want separate identity and grants. The scoped-only `get_controller_context` tool is not available to legacy credentials.

First prepare a **machine-local** marketplace from the repository root:

```sh
python scripts/configure_plugin.py
```

The script prints `marketplace_root` and installation commands. Register the printed root, not the source checkout:

```sh
codex plugin marketplace add "<printed marketplace_root>"
codex plugin add pron-hub@pron-hub-local
```

The generator only creates files. It does **not** install the plugin, edit Codex settings, read a service token, or restart anything. The generated MCP command uses the Python interpreter that ran the script, an absolute path to its retained bridge, and a non-secret reference to the checkout's service descriptor. If you move the checkout, regenerate the plugin configuration.

**Keep the generated directory** while the plugin is installed. Its default location is `.runtime/plugin-marketplace/`. To update or move the installation, generate a new directory and register/install from the new printed path:

```sh
python scripts/configure_plugin.py --output .runtime/plugin-marketplace-v2
```

Existing outputs are deliberately not overwritten. The repository's `pron-hub-source` catalog is an unconfigured, **skill-only** source template, not a connected Hub; do not install it as a substitute for the generated `pron-hub-local` marketplace. This integration is local, not an official public-directory listing or a cloud-accessible MCP service.

If tools do not appear in an existing chat, select the plugin in a new chat. Reload the Codex app only when necessary; installing a plugin does not replace the tool context of an already-running chat. See the [official plugin CLI reference](https://learn.chatgpt.com/docs/cli/reference) for client-side commands.

## Read output without guessing

The terminal screen is useful for checking the current foreground program, but **a visible report tail is not a full report**. The separate “Session output” view reads the exact associated Codex rollout.

Association uses bounded evidence from the selected terminal's process tree, held rollout, or an explicit resume selector. It does not choose the newest conversation in a directory. If the live session cannot be established, use “Associate session” and enter the Session ID from `/status` in that same worker yourself; association sends no worker input.

When reading through MCP:

- Follow `next_cursor` for older records or remaining chunks; use `after_cursor` for newly appended output.
- Inspect `content_complete`, `history_complete`, `has_more`, and `read_issues`. A partial page is not the full history.
- Enable `include_tools` for full tool records; default recent signals are bounded summaries.
- Treat `live_session_verified=false` as a selected/last-verified association, not proof of the current in-TUI session. Re-associate after `/new` or `/resume` when direct identity evidence is unavailable.
- Treat disconnected or closed-session records as historical evidence, not proof that the worker is online.

Long messages are chunked rather than silently clipped. Incomplete or malformed records are explicitly reported. Hidden analysis/reasoning is not exposed.

## Input and lifecycle safeguards

Ordinary task messages use bracketed paste and Enter in the same foreground Codex terminal. They are not shell commands and are blocked at a shell, login prompt, approval screen, or recognized startup menu. Raw keys are reserved for terminal actions explicitly authorized by the human.

Human drafts take precedence: `human_busy` means the controller must not overwrite them. A `submitted` delivery proves bytes were written, **not** that Codex accepted or completed the task. Keep the same `request_key`, inspect `get_delivery`, and never blindly resubmit uncertain writes with a new key.

The UI provides copy/paste, local scrollback, directory selection, explicit screen refresh, and confirmed terminal close. Mouse-wheel scrolling never becomes worker input. Clipboard fallback does not add Enter, although pasted text may itself contain newlines.

Closing a page, changing selection, deactivating a worker, or closing the Python Hub does not stop the independent terminal broker. If the broker or control computer stops, local PTYs end. Remote Hub-owned tmux sessions can survive SSH display disconnections and be reattached; reconnect does not automatically resume Codex or resend a task.

**“Close terminal” is different:** after explicit confirmation it ends that terminal's shell/agent and, remotely, its owned tmux session. It does not close other or external sessions. If the result is uncertain, the UI does not claim success.

The management terminal is separate from worker terminals. **Hide display** or closing the browser sends no termination or draft input. **End management session** explicitly ends the manager's terminal process and disables its capability; it does not stop workers. Saved native identity and history are retained for an explicit same-session recovery.

## MCP tools

| Tool | Purpose |
|---|---|
| `get_controller_context` | Verify the authenticated management identity and scope; scoped managers only |
| `list_active_agents` | Read only activated workers within the authenticated scope; legacy plugins use the global list |
| `get_agent` | Inspect one terminal's connection and foreground state |
| `read_agent` | Read its exact-session output with pagination |
| `send_agent_message` | Deliver ordinary user task text |
| `send_terminal_input` | Send explicitly authorized raw terminal input |
| `create_agent` | Open a worker terminal in an explicitly authorized connected-machine/project-directory range; scoped creations belong to that manager |
| `get_delivery` | Reconcile a durable submission |
| `poll_events` | Read incremental activated-worker events |

Read, write, delivery lookup, and event tools enforce the authenticated manager's scope. Scope revocation cancels unsubmitted queued work; one worker belongs to at most one scoped manager in this MVP. The service does not automatically approve worker actions, widen scope, recursively create managers, or start a new controller turn. Legacy credentials remain a broader trust boundary as described above.

## SSH and tunnels

Remote operation remains ordinary OpenSSH. An external tool such as TunnelTerm may expose a loopback SSH port or `ProxyCommand`; PRON Hub does not import that tool or manage its login/window lifecycle.

Validate a **persistent display plus concurrent short SSH requests**, not just one successful banner or `ssh host true`. Keep host-key checking enabled. See [the SSH transport contract](docs/ssh-transport.md) for stdin, concurrency, resize, and disconnect requirements.

## Local data and compatibility

The service binds only to loopback. Browser mutations require a matching Host/Origin and an HttpOnly SameSite cookie. MCP uses a private local capability descriptor, rejects browser-Origin requests, and does not forward the token through proxies or redirects.

Machine configuration, activation, durable deliveries, management records, scopes, and operation identities are stored in `.runtime/hub.sqlite`. Capability descriptors stay in the private runtime directory; production management workspaces use the per-user private location above. An explicitly supplied development/test runtime keeps its workspaces in that namespace. Each descriptor has an independent capability; the raw token is not put in a prompt, URL, or command-line argument. `config.example.json` and optional ignored `config.local.json` initialize a new registry; an existing saved registry takes precedence.

The web/MCP service shares `.runtime/service.json`, with explicit connection-file overrides retained. The private `.runtime/worker-terminal-reference.json` holds only the physical path to an existing worker broker descriptor, not its token. A verified default terminal-creation workflow records this reference exclusively; metadata reads do not create it. This avoids Windows packaged-app and ordinary-shell state namespaces appearing to be different machines. A broken or unreachable pinned owner fails closed and needs human repair; it never creates a second broker owner automatically. Work terminal processes remain independent of web-service restarts.

For an already-running older bridge, an explicitly registered private `.runtime/service-compatibility-references.json` may list existing standard legacy Hub descriptors. A service restart refreshes only same-endpoint, valid Hub-shaped targets so those same clients can follow token rotation. It does not discover destinations, copy account authentication, or touch different-port/non-Hub files. New installations use the canonical descriptor.

Each manager's workspace contains its own `AGENTS.md` and `.codex/config.toml`. Its scoped connection is also supplied explicitly when the Hub launches or resumes Codex, without changing the user's global configuration. Whether a desktop client loads the same project configuration and trust is part of the experimental transfer check, not an assumed guarantee. The Hub's API grants do not restrict Codex's unrelated native tools, other plugins, or the user's SSH/system permissions.

The rename deliberately preserves existing wire/state identifiers: `ceo_hub`, `ceo-hub`, `ceohub-`, `CEO_HUB_*`, `%LOCALAPPDATA%/CEOHub/`, and `~/.local/state/ceo-hub/`. Do not delete installed legacy plugin caches or rewrite working terminal IDs as a branding migration.

Browser key input is not saved in the delivery database. Terminal output may still contain secrets a user prints, and controller-submitted tasks/deliveries are local private data. Do not commit `.runtime/`, connection descriptors, authentication files, or personal configuration. This is not a multi-tenant security boundary; do not expose the service on the public network.

## Codex daemon maintenance

Updating the CLI does not necessarily update its separately managed shared background package. The optional Linux helper defaults to a read-only check:

```sh
python3 scripts/codex_sync_daemon.py --check
python3 scripts/codex_sync_daemon.py --sync --timeout 60
```

Run it **on the worker machine**, using the current user's trusted CLI installation. Any loaded session, including an idle one, blocks synchronization. It synchronizes from the current CLI, not an independently selected latest version. See [maintenance instructions and verification limits](docs/codex-maintenance.md) before using `--sync`.

## Development and tests

```sh
python -m unittest discover -s tests -v
node --check web/ui.js
node --check web/controller.js
npm run test:terminal
npm run test:ui
python scripts/check_publication.py
```

Real PTY/remote integration tests are opt-in and are not enabled by ordinary unit tests. Run them only against disposable sessions. Tests cover scope enforcement, native initialization events, stable start/resume keys, uncertain results, independent manager lifecycle, desktop-unverified UI, activation, exact-session reads, durable delivery, human-draft protection, terminal rendering, and authentication. They do not certify every Codex version, provider, tunnel implementation, or desktop tool reload. The 0.5 management MVP remains experimental pending version-specific live validation.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for reporting concerns.

## License

PRON Hub is MIT licensed. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). OpenAI, Codex, and third-party service names belong to their respective owners; this is an independent project.
