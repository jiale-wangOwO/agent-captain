# PRON Hub

<p align="center">
  <img src="assets/pron-hub-logo.png" alt="PRON Hub" width="640">
</p>

**Personal Remote Ops & Nodes Hub** — one Codex conversation to coordinate the working agents you choose, on your computer and remote Linux machines.

[中文说明](README.zh-CN.md) · [SSH transport contract](docs/ssh-transport.md) · [Codex maintenance](docs/codex-maintenance.md) · [Security](SECURITY.md)

PRON Hub is a single-user, local-first connection service for Codex. Open a real terminal for each worker, start Codex using your normal workflow, and activate the terminals you want your current Codex chat to manage. You can still type directly in those same terminals.

The **controller is the model already running in your Codex chat**, acting as your CEO: collect evidence, convey tasks, and summarize what needs your decision. The human retains final authority. The Hub runs no separate management model, does not inject a CEO role into workers, and does not automatically wake a dormant Codex conversation.

## What it does

- Connects local terminals and SSH machines configured in the UI.
- Opens a shell in a selected project directory, or the user's home directory.
- Lets the human and controller read and write the same worker terminal.
- Limits controller access to an explicit activation list; it never scans every machine or conversation on startup.
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
| Codex controller | A local Codex client supporting plugins and stdio MCP |

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

## Connect your workers

1. Select or configure a machine, then open a terminal. Use the directory chooser, enter an absolute directory on that machine, or leave it blank and `cd` from the home directory.
2. In that terminal, run `codex`, `codex-switch`, or your usual launcher. The human handles provider selection, login, secrets, OTPs, and approvals.
3. To continue an existing conversation, use the on-demand history finder and follow the displayed `codex resume <session-id>` command yourself. Exit the original client first; opening another terminal is not a transfer of ownership.
4. Activate the connected terminal for controller access. Activation does not start a model. Inactive terminals remain directly usable by the human.
5. Install the local plugin below, select it in a Codex chat, and tell that chat what to coordinate.

For example:

> Act as my CEO. Read the activated slides and server workers, summarize actual progress and evidence, and list decisions I need to make. Do not send new tasks yet.

> Send this fix request to the activated server worker, then summarize its result and verification.

There is one shared activation list per local Hub. Multiple controller chats see the same list; this is not tenant or controller isolation.

## Connect the Codex plugin

First prepare a **machine-local** marketplace from the repository root:

```sh
python scripts/configure_plugin.py
```

The script prints `marketplace_root` and installation commands. Register the printed root, not the source checkout:

```sh
codex plugin marketplace add "<printed marketplace_root>"
codex plugin add pron-hub@pron-hub-local
```

The generator only creates files. It does **not** install the plugin, edit Codex settings, read a service token, or restart anything. The generated MCP command uses the Python interpreter that ran the script and an absolute path to its retained bridge.

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

## MCP tools

| Tool | Purpose |
|---|---|
| `list_active_agents` | Read only the activated registry |
| `get_agent` | Inspect one terminal's connection and foreground state |
| `read_agent` | Read its exact-session output with pagination |
| `send_agent_message` | Deliver ordinary user task text |
| `send_terminal_input` | Send explicitly authorized raw terminal input |
| `create_agent` | Open a worker terminal on a UI-connected machine |
| `get_delivery` | Reconcile a durable submission |
| `poll_events` | Read incremental activated-worker events |

The service does not automatically approve worker actions, widen activation scope, or start a new controller turn.

## SSH and tunnels

Remote operation remains ordinary OpenSSH. An external tool such as TunnelTerm may expose a loopback SSH port or `ProxyCommand`; PRON Hub does not import that tool or manage its login/window lifecycle.

Validate a **persistent display plus concurrent short SSH requests**, not just one successful banner or `ssh host true`. Keep host-key checking enabled. See [the SSH transport contract](docs/ssh-transport.md) for stdin, concurrency, resize, and disconnect requirements.

## Local data and compatibility

The service binds only to loopback. Browser mutations require a matching Host/Origin and an HttpOnly SameSite cookie. MCP uses a private local capability descriptor, rejects browser-Origin requests, and does not forward the token through proxies or redirects.

Machine configuration, activation, and durable deliveries are stored in `.runtime/hub.sqlite`. `config.example.json` and optional ignored `config.local.json` initialize a new registry; an existing saved registry takes precedence.

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
npm run test:terminal
npm run test:ui
python scripts/check_publication.py
```

Real PTY/remote integration tests are opt-in and are not enabled by ordinary unit tests. Run them only against disposable sessions. The test suite covers activation, exact-session reads, durable delivery, human-draft protection, terminal rendering, authentication, and lifecycle boundaries; it does not certify every provider or tunnel implementation.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for reporting concerns.

## License

PRON Hub is MIT licensed. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). OpenAI, Codex, and third-party service names belong to their respective owners; this is an independent project.
