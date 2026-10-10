# PRON Hub Codex plugin

This package provides the `pron-hub` skill for acting as the user's CEO in the
current Codex chat. It does not start another model or select a model provider.

The management tools provide a scoped cached overview, worker display aliases /
pins (`update_agent`), and user-requested display reattachment to an existing
SSH tmux session (`reconnect_agent`). A reconnect never starts Codex, resumes a
conversation, replays input, restarts an external tunnel, or substitutes a new
worker if the original tmux session is missing. Metadata changes do not grant
access. Inspect live evidence for the selected worker rather than treating the
cached overview as a fresh connectivity check.

This is the **legacy existing-chat integration**. Its service credential can
access the Hub's shared global activation list; it is not automatically narrowed
by a new management session's per-controller grants. For scoped management,
start a management conversation in the Hub UI instead. That workflow prepares
its own MCP connection and does not require installing this package.

The source package deliberately contains no machine-specific MCP command. Its
empty `.mcp.json` makes a direct source install **skill-only**, not a connected Hub.
The repository catalog is named `pron-hub-source` so it cannot accidentally
shadow the configured `pron-hub-local` marketplace. **Do not install the source
catalog for Hub control.** Prepare the local MCP bridge before installing the
full plugin:

```sh
python scripts/configure_plugin.py
```

Run that command from the repository root. It generates a separate local
marketplace under `.runtime/plugin-marketplace/`, with a copy of this package
and the standard-library MCP bridge. The generated `.mcp.json` uses the Python
interpreter that ran the command and an absolute path to that bridge. The script
prints the marketplace registration and plugin installation commands; it does
not execute them, change Codex settings, or read any service/authentication token.

Keep the generated directory while using the installed plugin: installed plugin
caches still refer to its bridge path. Moving the clone, removing that directory,
or replacing the interpreter requires generating a new marketplace and
reinstalling from it. Existing output directories are never overwritten. For an
update, choose a new output directory, for example:

```sh
python scripts/configure_plugin.py --output .runtime/plugin-marketplace-v2
```

To upgrade an existing `ceo-hub@ceo-hub-local` installation without introducing
a second plugin identity, explicitly generate a compatibility package instead:

```sh
python scripts/configure_plugin.py --legacy-ceo-identity --output .runtime/plugin-marketplace-ceo-v0.6.0
```

This keeps the plugin name `ceo-hub`, skill name `ceo-hub`, marketplace name
`ceo-hub-local`, and MCP server key `ceo_hub`, while the displayed product name
is PRON Hub. It only creates a new retained directory: it does not inspect or
edit installed caches, `config.toml`, authentication, or running services. The
normal command still generates `pron-hub@pron-hub-local`. Install only the
identity you intend to use, not both, to avoid duplicate tools.

Review the generated commands before registration. A running MCP process and
its discovered tool list do not hot-reload after source changes: reload that
MCP connection or reopen the controlling chat and verify `tools/list` includes
`update_agent` and `reconnect_agent`, with server version `0.6.0`. Updating the
installed skill/manifest is separate from replacing a bridge file; local plugin
cache loading may require an app reload or restart. Do not restart the terminal
broker or a worker to refresh plugin tools.

This is a local desktop/CLI integration. It is not a hosted plugin listing and
does not make a local terminal service available to cloud clients. The existing
`ceo-hub` plugin identity and installed caches are not modified merely by
generating this package; any explicit installation is a separate user action.
