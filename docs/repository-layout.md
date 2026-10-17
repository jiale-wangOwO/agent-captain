# Repository layout

AgentCaptain separates application policy, native-session evidence, process
ownership, and user interfaces. Python implementation belongs to the
`agent_captain` package; the repository root contains project metadata and
user-facing entry points, not a flat collection of implementation modules.

```text
agent-captain/
├── src/agent_captain/       Python application package and command entry point
│   ├── core/               Worker lifecycle, management and creation policy
│   ├── server/             Loopback HTTP API and web-service lifecycle
│   ├── sessions/           Native-session binding, catalog and saved output
│   ├── transport/          Terminal client, SSH probes and remote helpers
│   ├── infrastructure/     Persistence, background launch and process identity
│   └── integrations/       Stdio MCP bridge
├── terminal/               Node PTY broker and input-activity tracking
├── web/                    Browser workbench and shared-terminal interface
├── plugins/agent-captain/   Unconfigured existing-chat plugin template
├── scripts/                Checkout entry point and explicit developer tools
├── tests/                  Isolated Python and Node regression suites
├── docs/                   Product contracts, operation guides and demo images
├── assets/                 Canonical brand artwork and provenance
├── .github/                CI and contribution templates
├── pyproject.toml          Python package metadata and installed CLI
└── package.json            Node dependencies and verification commands
```

The hidden `.agents/plugins/marketplace.json` is a source-only plugin catalog,
not an authenticated live connection. Private `.runtime/`, local configuration,
and dependency directories are not part of this source tree.

## Boundaries and entry points

| Component | Responsibility | Important boundary |
| --- | --- | --- |
| `core/` | Registered workers, management identities, dispatch, delivery and creation policy | Policy is separate from shell transport; submitted input is not verified completion. |
| `server/` | HTTP routes, loopback/same-origin checks and web-process startup | Restarting the web service must not end independently owned terminals. |
| `sessions/` | Exact native-session association and paginated persisted output | Screen text is not complete saved conversation output; never select a newest file as identity. |
| `transport/` | Owned terminal client, SSH-side probes, history and directory selection | Reconnect the original persistent terminal; do not silently create a replacement or resume a model. |
| `infrastructure/` | State storage, durable background launch and process-birth checks | A PID alone is not a trustworthy process identity. |
| `integrations/` | MCP schemas, authentication and tool dispatch | The bridge uses the same management contract; it does not host a second model. |
| `terminal/` | Node PTY ownership, rendering state, geometry and safe input | Preserve delivery IDs and human drafts across uncertain connections. |
| `web/` | Workbench presentation and direct terminal interaction | UI changes should be testable without real SSH or model access. |

Use the source-checkout command from the repository root:

```sh
python scripts/captain.py serve
python scripts/captain.py service start
python scripts/captain.py service status
python scripts/captain.py terminal status
```

The first runs in the foreground; the next two start or inspect the web service.
`terminal status` inspects the separate display service. It does not reconnect
workers or resume models. `run_agentcaptain.cmd` is the Windows convenience
launcher.

For development, run `python -m pip install -e .`. The installed
`agent-captain` command and `python -m agent_captain` expose the same command
groups. This remains a checkout-based application: keep `web/`, `assets/`,
`terminal/`, and npm dependencies available beside the package source. An
editable install is not a standalone binary or a bundled desktop installer.

## Existing-installation migration

Source movement does not rename or relocate installation state. `.runtime/`
stays under the same checkout. Registered machines, exact terminals, native
Session IDs, delivery keys, management workspaces, environment-variable
overrides and wire identifiers retain their existing meanings.

An older deployment may still have saved absolute references to root-level
scripts such as `app.py`, `mcp_bridge.py`, or `terminal_broker.cjs`. Before
removing those old entry paths in a running installation, use the explicit
local compatibility preparation:

```sh
python scripts/prepare_legacy_entrypoints.py
# Review the dry-run list, then explicitly materialize the entries:
python scripts/prepare_legacy_entrypoints.py --apply
```

The first command only reports a dry run. With `--apply` it creates ignored,
installation-local compatibility entries; these are not
the canonical implementation and are not committed back into the root. Review
its reported result before replacing a service. It is not an instruction to
restart a broker, close terminals, restore Codex, or replay tasks. Fresh clones
do not need this compatibility layer. Use the new command for future launches;
retain required old paths until the clients that reference them have been
deliberately migrated.

New existing-chat plugin packages copy the canonical MCP bridge and its pure
management policy into a retained standalone directory. Cached plugin startup
does not depend on the source package being importable or the current working
directory. Existing plugin outputs are not rewritten in place; generate a new
output, register it explicitly, and verify the client loaded the tools. See the
[operator guide](guides/operator-guide.md#optional-connect-an-existing-codex-chat-with-the-legacy-plugin)
and [rename compatibility](rebranding.md).

## Private state and verification

`.runtime/` and ignored machine-local configuration belong to an installation,
not the reusable project. They may contain capabilities, worker references,
personal paths, transcripts, or delivery records. Never copy them into fixtures,
issue reports, screenshots, or example configuration. Generated plugin output
also stays private; creating a marketplace does not prove a chat loaded it.

Repository screenshots come from the isolated synthetic demo, not a real Hub
at port 8766. See the [screenshot-update guide](guides/visual-demo.md#repository-screenshots).
Canonical artwork and its usage rules live in [brand assets](../assets/brand/README.md).

Run the [contribution checks](../CONTRIBUTING.md#development-checks) for changes
to imports, subprocess entry points and helper delivery. Publication checks use
the reviewed Git index with `--tracked`; they are not a complete audit of Git
history, artwork or deployment state. Start at the [documentation index](index.md)
for task-oriented product guidance.
