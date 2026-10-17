# Repository layout

AgentCaptain keeps the UI, local service, management tools, and SSH terminal
boundary in one small repository. Start with the area relevant to your change;
there is no need to reorganize running installations to work on one component.

| Area | Entry points | Responsibility |
| --- | --- | --- |
| Web interface | `web/`, `app.py` | Workbench presentation, HTTP routes, same-origin and loopback checks |
| Application and state | `hub.py`, `terminal_hub.py`, `store.py`, `catalog.py` | Registered workers, operations, persistence, and lifecycle state |
| Captain integration | `controllers.py`, `management_policy.py`, `mcp_bridge.py` | Management identity, tool access, creation boundaries, and dispatch |
| Terminal boundary | `terminal_client.py`, `terminal_service.py`, `terminal_broker.cjs`, `input_activity.cjs` | Owned terminal services, safe input, sizing, and display attachment |
| Remote transport helpers | `remote_*.py`, `directory_browser.py` | SSH-side probes, terminal history, and explicit directory selection |
| Conversation output | `conversation_output.py`, `session_binding.py` | Exact native-session association and persisted output, distinct from terminal screenshots |
| Local service lifecycle | `service.py`, `background_process.py`, `process_identity.py` | Background launch, identity checks, and safe status/stop behavior |
| Plugin source | `plugins/agent-captain/`, `.agents/plugins/marketplace.json` | Machine-independent plugin template and catalog; not a configured live connection |
| Developer tools | `scripts/` | Explicit plugin configuration, maintenance helpers, and publication checks |
| Verification | `tests/`, `.github/workflows/ci.yml` | Isolated regression tests and the Windows/Linux CI matrix |
| Documentation and visuals | `docs/`, `assets/` | Product guidance, operation contracts, and reusable brand assets |

For task-oriented reading, start at the [documentation index](index.md).
The [offline visual demo](guides/visual-demo.md) reproduces showcase views using
the shipped UI and synthetic fixtures; it does not contact running workers.
Canonical artwork and its usage rules live in [brand assets](../assets/brand/README.md).

## What stays outside source control

`.runtime/` and ignored machine-local configuration belong to an installation,
not the reusable project. They may contain capabilities, worker references,
personal paths, transcripts, or delivery records. Do not copy them into fixtures,
issue reports, screenshots, or an example configuration.

Repository screenshots must come from the isolated demo, not a real Hub at
port 8766. See the [screenshot-update instructions](guides/visual-demo.md#repository-screenshots).

Generated plugin output also belongs in private local state. The committed
plugin source is intentionally unconfigured; a successful configuration command
does not prove a running chat has loaded that plugin.

## Change by boundary, not by file count

- A UI-only change should not require a real SSH connection or a model restart.
- A reconnect change must preserve the exact original terminal and distinguish a
  missing remote tmux from a failed display connection.
- A conversation reader must not treat visible screen text as complete saved
  history or guess a native Session ID.
- A rename must preserve working protocol identifiers and existing runtime paths;
  see [rename compatibility](rebranding.md).
- A publication check inspects the reviewed Git index with `--tracked`; it is not
  a complete audit of Git history, artwork, or deployment state.

Flat Python modules and compatibility launchers are retained deliberately.
Moving them merely to make the tree look shorter can break imports, installed
plugin paths, or existing service launch references. Prefer a focused change and
the checks in [CONTRIBUTING.md](../CONTRIBUTING.md).
