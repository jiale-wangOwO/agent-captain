# PRON Hub Codex plugin

This package provides the `pron-hub` skill for acting as the user's CEO in the
current Codex chat. It does not start another model or select a model provider.

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

This is a local desktop/CLI integration. It is not a hosted plugin listing and
does not make a local terminal service available to cloud clients. The existing
`ceo-hub` plugin identity and installed caches are not modified by this package.
