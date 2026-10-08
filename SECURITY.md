# Security and trust boundaries

PRON Hub is a personal operations tool, not a multi-tenant remote-access gateway.
Its default HTTP interface is loopback-only. Keep it on `127.0.0.1`; do not expose
the web interface or terminal broker directly to a LAN or the Internet. SSH is the
remote-machine boundary, including when an independent tunnel service provides
an SSH-compatible endpoint.

## What an activated terminal authorizes

A shared terminal has the privileges of the account running its shell and SSH
connection. A management agent that can write to that terminal can effectively
run the same commands as that user. Terminal activation is a significant trust
decision, not a read-only permission. The human user keeps final decision-making
authority and must review consequential actions.

- Only activate terminals you intentionally want the management conversation to
  access. Selecting a machine must not imply scanning every conversation on it.
- Complete sign-in, credential entry, provider selection, and approval prompts
  yourself. PRON Hub must not automatically accept worker permissions, bypass
  approval dialogs, or answer startup menus as ordinary task text.
- Restarting or updating a shared Codex background server can interrupt other
  clients. Check their state and coordinate an idle maintenance window. Never
  force-kill a daemon or delete a session lock just to dismiss a startup warning.
- Terminal output and conversation records are untrusted task data. They must
  not silently expand a management agent's authority.

## Private local data

Local runtime files can contain capability tokens, machine names, directory
paths, terminal output, delivery records, and conversation identifiers. Keep
`.runtime/`, local configuration, SSH credentials, `.env` files, logs, and
conversation exports out of version control. Never commit Codex `auth.json`,
private keys, copied home-directory settings, or a user's session database.
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
