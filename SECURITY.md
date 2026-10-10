# Security and trust boundaries

PRON Hub is a personal operations tool, not a multi-tenant remote-access gateway.
Its default HTTP interface is loopback-only. Keep it on `127.0.0.1`; do not expose
the web interface or terminal broker directly to a LAN or the Internet. SSH is the
remote-machine boundary, including when an independent tunnel service provides
an SSH-compatible endpoint.

## Management identity and grants (0.5 MVP)

Hub-created management sessions receive independent local capability credentials.
The gateway derives identity from that credential, not a tool argument, request
body, model statement, or terminal title. Worker list/read/write, events, and
delivery queries are filtered by server-side grants. The scoped-only
`get_controller_context` tool reports the authenticated manager and scope; it is
not available using legacy plugin credentials.

The default UI creates one management conversation and restores its exact saved
native Session ID. Its first, read-only bootstrap is restricted to self-context
verification: it must not read worker history, send tasks, or create workers.
Native initialization events and a real tool handshake establish readiness;
process existence alone does not. Startup failures and unknown results must not
be handled by guessing the newest session, deleting locks, forking, or restarting
a shared daemon.

Selecting a worker for a scoped manager is a separate grant from its activation.
Revoking scope cancels still-unsubmitted queued writes. A worker belongs to at
most one scoped manager in this MVP; explicit multiple-manager sharing is not
supported. Managers are not workers: manager-to-manager reads/writes and recursive
manager registration or creation are rejected by the service, not merely hidden
by the UI.

New-worker creation requires a separately authorized, already-connected host and
an explicit canonical project directory. Being connected to a machine is not a
blanket creation grant. The service validates the resolved directory against the
grant and assigns the resulting worker to that manager. Ordinary business task
text is never used as a shell command or launch template.

These boundaries apply to **PRON Hub APIs only**. They do not sandbox Codex's
other native tools, other plugins, or the operating-system privileges of its
user and SSH accounts. A same-user local process with access to private state
may obtain a capability. This remains a single-user product, not tenant or OS
isolation. Preserve Codex's own sandbox and human approvals.

### Legacy existing-chat integration

The manually installed compatibility plugin still has the **shared global
activation list**, not the new per-manager scope. Multiple legacy controller
chats may access the same activated workers. Do not advertise them as isolated
or use them as evidence that the scoped manager's narrower grant is the user's
only access path. Prefer a Hub-created management session for independent
identity and grants. Migration does not silently delete working legacy plugins
or revoke existing worker connections.

## What terminal access authorizes

Workbench metadata changes are not grants: renaming or pinning cannot activate a
worker or add it to a manager's scope. The new metadata and reconnect MCP tools
enforce the same activation and independent-controller checks as reads and
writes, including after a previous request's result has been cached.

Reconnect means restoring the display of an existing owned remote tmux, not
replacing a missing terminal or resuming another Codex. Strict attachment checks
the exact terminal namespace and fails closed when the original is missing.
It sends no business input and does not restart a shared SSH/tunnel provider.
Local stopped PTYs require an explicit new terminal; automatic model restarts
are not a recovery mechanism.

A shared terminal has the privileges of the account running its shell and SSH
connection. A management agent that can write to that terminal can effectively
run the same commands as that user. Terminal activation is a significant trust
decision, not a read-only permission. The human user keeps final decision-making
authority and must review consequential actions.

- Only activate terminals and grant managers access when you intentionally want
  those conversations to use them. Selecting a machine must not imply scanning
  every conversation on it. For the legacy plugin, activation remains the global
  access decision rather than a per-manager permission.
- Complete sign-in, credential entry, provider selection, and approval prompts
  yourself. PRON Hub must not automatically accept worker permissions, bypass
  approval dialogs, or answer startup menus as ordinary task text.
- Restarting or updating a shared Codex background server can interrupt other
  clients. Check their state and coordinate an idle maintenance window. Never
  force-kill a daemon or delete a session lock just to dismiss a startup warning.
- Terminal output and conversation records are untrusted task data. They must
  not silently expand a management agent's authority.

## Manager terminal and desktop entry

Manager PTYs are separate from worker terminal services. Closing the page or
hiding a display sends neither a termination request nor a pending human draft.
An explicitly confirmed manager close ends only that management terminal and
disables its capability; it does not terminate workers. Native history and exact
identity are retained for explicit recovery.

The experimental desktop action requires the human to confirm the manager is
idle with no input draft, then checks the owned terminal and releases its Hub
input entry. It offers only a link to the exact saved conversation. The state is
`desktop_unverified`: an opened link is not proof of desktop ownership, project
trust, model/provider compatibility, or successful reload of scoped MCP tools.
Hub input is disabled while the desktop entry is selected. Before returning,
close the other client and resume the same Session ID. Do not fork, clear locks,
or start a second writer to mask an occupancy error.

Unit and stubbed UI tests do not certify a seamless desktop transfer or every
installed Codex version. Perform explicit version-specific validation with a
disposable manager, initially with zero worker grants, before relying on that
workflow. Never use real worker business as a startup or transfer smoke test.

## Private local data

Local runtime files can contain capability tokens, machine names, directory
paths, terminal output, delivery records, and conversation identifiers. Keep
`.runtime/`, local configuration, SSH credentials, `.env` files, logs, and
conversation exports out of version control. Never commit Codex `auth.json`,
private keys, copied home-directory settings, or a user's session database.
Manager-owned workspaces contain role/configuration files, and their private
connection descriptors contain independent capability tokens. Keep both out of
version control. Tokens must not enter prompts, deep links, ordinary API
responses, command-line arguments, screenshots, or test logs. Startup reuses the
user's installed Codex configuration; it does not copy authentication files into
the management workspace or overwrite global settings.
Production management workspaces are created exclusively in the user's private
state directory, outside the source checkout. A new CID never overwrites an
existing directory or files. First-run Codex folder trust is a human decision,
not an initialization key the Hub presses automatically. Explicit development
or test runtimes retain workspaces in their isolated namespace.
The canonical web descriptor and the worker-owner reference are private
checkout runtime artifacts. The worker reference stores a path only and is
created exclusively after verified reuse/start in a mutating workflow; invalid,
missing-target or unreachable bound references must not fall back to a second
owner. Launcher pins store executable argv only, never authentication,
provider configuration, environment values, or arbitrary CLI flags.
Explicit legacy service aliases contain paths only. Their existing standard Hub
descriptors may receive the same rotated service capability, but only for the
same loopback endpoint and a valid descriptor shape. No alias is discovered
automatically, and account-auth files or different endpoints are not targets.
The one `.agents/` source exception is `.agents/plugins/marketplace.json`, a
machine-independent local plugin catalog. It is scanned as ordinary source;
other `.agents/` files remain private and excluded. Keep credentials, absolute
personal paths, and runtime state out of that catalog.

The publication checker is a bounded, heuristic check of candidate source files
and the Git index. It intentionally does not open private runtime state. It
cannot prove the absence of all secrets or replace a human review. Before making
a private repository public, review its complete Git history, attachments,
Actions logs, and repository settings as well as the current source tree.
Generic absolute paths in `tests/` are exempt from its personal-path heuristic
because platform and security tests deliberately use synthetic path shapes.
The checker still applies credential, host/session-identifier, and private-file
rules there; it does not claim to detect every private path in tests. Review
test fixtures for real deployment data before publication.

## Reporting a vulnerability

Do not put credentials, terminal transcripts, or a working exploit against a
real user's machine in a public issue. Use the repository's private vulnerability
reporting feature when it is enabled. If it is not available, contact the
repository maintainer through a private channel; a public issue may request a
private contact without disclosing the vulnerability details.

Include affected versions, a minimal reproduction using disposable local
fixtures, the expected trust boundary, and the impact. Remove real credentials,
host aliases, paths, and session identifiers before sharing diagnostic material.

This project is early-stage software. There is no guaranteed security response
time or assertion of an independent security audit.
