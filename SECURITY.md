# Security and trust boundaries

PRON Hub is a personal operations tool, not a multi-tenant remote-access gateway.
Its default HTTP interface is loopback-only. Keep it on `127.0.0.1`; do not expose
the web interface or terminal broker directly to a LAN or the Internet. SSH is the
remote-machine boundary, including when an independent tunnel service provides
an SSH-compatible endpoint.

## Personal management identity and automatic worker access (0.7)

Hub-created management sessions receive independent local capability credentials.
The gateway derives identity from that credential, not a tool argument, request
body, model statement, or terminal title. The service checks that identity,
native Session ID and tool readiness before allowing worker operations. Every
registered, open shared **worker** terminal is automatically available to ready,
authenticated management sessions. This includes newly connected workers and
temporarily disconnected workers bound to their original terminal; it does not
scan remote machines or enroll their other conversations. Independent manager
credentials identify callers, but do **not** isolate workers from other managers
of this single-user personal Hub. `get_controller_context` reports the verified
identity and effective automatic-worker policy. Delivery queries retain their
authenticated manager's request namespace rather than exposing another
manager's private delivery records.

The default UI creates one management conversation and restores its exact saved
native Session ID. Its first, read-only bootstrap is restricted to self-context
verification: it must not read worker history, send tasks, or create workers.
Native initialization events and a real tool handshake establish readiness;
process existence alone does not. Startup failures and unknown results must not
be handled by guessing the newest session, deleting locks, forking, or restarting
a shared daemon.

There is no separate worker activation, ownership assignment, or per-worker grant
step. The legacy activation endpoint is a compatibility no-op: it cannot change
connections, shrink management access, revoke pending work, or submit inputs.
Only enrolled open worker terminals with their real terminal identity qualify.
History-only records, closed/closing/close-unknown terminals and manager
identities/workspaces are excluded. Managers are not workers: manager-to-manager
reads/writes and recursive manager registration or creation are rejected by the
service, not merely hidden by the UI.

Hub-created managers' new-worker creation requires a separately authorized,
already-connected host and an explicit canonical project directory. Being
connected to a machine is not a blanket creation grant. The service validates the
resolved directory against the grant; the resulting worker joins the personal
Hub. Ordinary business task
text is never used as a shell command or launch template.

The creation grant is independent of automatic existing-worker access. Removing
or narrowing a manager's allowed creation directories does not unmanage already
enrolled workers. Conversely, access to an existing worker never grants permission
to create another terminal on its host or in its directory. Newly created workers
join the same personal Hub automatically; no exclusive worker owner is assigned.

At the policy upgrade, legacy retryable plugin deliveries that were outside their
old manager grant or belonged to an inactive worker are fenced as audited
`not_sent` records. Their request identities and contents are retained. Submitted,
uncertain/`unknown`, and already cancelled/revoked records are not revived or
rewritten. Migration performs no terminal input, process start, SSH attachment,
or task replay. Automatic management is not retroactive consent to send old work.

These boundaries apply to **PRON Hub APIs only**. They do not sandbox Codex's
other native tools, other plugins, or the operating-system privileges of its
user and SSH accounts. A same-user local process with access to private state
may obtain a capability. This remains a single-user product, not tenant or OS
isolation. Preserve Codex's own sandbox and human approvals.

### Legacy existing-chat integration

The manually installed compatibility plugin has the same automatic existing-
worker access, but its shared service credential is **not** an independent
manager identity. The creation-root policy described above applies to Hub-created
management identities; legacy service credentials retain their compatibility
creation channel and must not be advertised as enforcing an independently scoped
manager's roots. Keep those credentials private and prefer a Hub-created
management session for an independently authenticated identity and explicit
creation directories. Neither integration provides worker or operating-system
isolation from other same-user management conversations. Migration does not
silently delete working legacy plugins or revoke existing worker connections.

## What terminal access authorizes

Workbench metadata changes are presentation only: renaming or pinning changes
neither worker identity, terminal/Codex Session ID, output binding nor creation
permissions. Metadata and reconnect tools enforce the same enrolled-open-worker,
authenticated-identity and initialization/readiness checks as other manager
operations, including before replaying a cached operation result.

Reconnect means restoring the display of an existing owned remote tmux, not
replacing a missing terminal or resuming another Codex. Strict attachment checks
the exact terminal namespace and fails closed when the original is missing.
It sends no business input and does not restart a shared SSH/tunnel provider.
Local stopped PTYs require an explicit new terminal; automatic model restarts
are not a recovery mechanism.

A shared terminal has the privileges of the account running its shell and SSH
connection. A management agent that can write to that terminal can effectively
run the same commands as that user. Connecting/enrolling a shared worker in this
personal Hub therefore enables management access; it is not a read-only grant.
The human user keeps final decision-making authority and must review
consequential actions. Automatic worker access does not bypass the broker's
foreground-Codex confirmation, human-typing lease, geometry checks or stable
request-key/uncertain-result fences. A normal business task cannot become a shell
command simply because the terminal is connected.

- Only connect/enroll shared worker terminals you intentionally want your
  management conversations to use. Selecting a machine must not imply scanning
  every conversation on it. Opening a normal shell is distinct from having a
  ready Codex agent; the user starts Codex and handles its startup prompts.
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
disposable manager in an isolated runtime with no enrolled workers, before relying
on that workflow. Never use real worker business as a startup or transfer smoke
test.

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
