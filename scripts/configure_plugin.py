"""Prepare a machine-local AgentCaptain plugin without editing Codex settings.

The source repository contains no personal interpreter paths or credentials.
Generated stdio commands refer to a retained directory rather than assuming a
plugin cache working directory or undocumented plugin-root interpolation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PLUGIN_NAME = "agent-captain"
MARKETPLACE_NAME = "agent-captain-local"
SOURCE_MARKETPLACE_NAME = "agent-captain-source"
GENERATOR_VERSION = 1
SOURCE_FILES = (
    ".codex-plugin/plugin.json",
    "skills/agent-captain/SKILL.md",
    "README.md",
)


class ConfigurationError(Exception):
    """A safe configuration error that never includes credentials."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def configure_plugin(output: Path | str | None = None, *,
                     repo_root: Path | str | None = None,
                     python_executable: Path | str | None = None,
                     legacy_ceo_identity: bool = False,
                     legacy_pron_identity: bool = False) -> dict[str, Any]:
    """Generate only into a new directory; never read live Hub/Codex state.

    ``repo_root`` and ``python_executable`` are injectable for isolated tests.
    The CLI always uses its own interpreter and the source checkout containing
    this script. No install, refresh, or service restart is performed.
    """
    if legacy_ceo_identity and legacy_pron_identity:
        raise ConfigurationError("Choose only one compatibility identity. No output was generated.")
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    destination = Path(output) if output is not None else root / ".runtime" / "plugin-marketplace"
    destination = destination.expanduser().absolute()
    # Check before resolve, including dangling symlinks: an unknown existing
    # filesystem object is never a generation target.
    if os.path.lexists(destination):
        raise ConfigurationError("Output already exists. Choose a new --output directory; no files were overwritten.")
    destination = destination.resolve()
    if os.path.lexists(destination):
        raise ConfigurationError("Resolved output already exists. Choose a new --output directory.")
    interpreter = Path(python_executable or sys.executable).expanduser().resolve()
    if not interpreter.is_file():
        raise ConfigurationError("Python interpreter is unavailable. Run this script with a working Python installation.")

    plugin_source = root / "plugins" / PLUGIN_NAME
    prepared: dict[str, bytes] = {}
    try:
        for relative in SOURCE_FILES:
            source = plugin_source / relative
            if source.is_symlink() or not source.is_file():
                raise ConfigurationError("A required plugin source file is missing or is a symlink.")
            prepared[relative] = source.read_bytes()
        manifest = json.loads(prepared[".codex-plugin/plugin.json"])
        source_mcp = plugin_source / ".mcp.json"
        if source_mcp.is_symlink() or not source_mcp.is_file():
            raise ConfigurationError("The source MCP template is missing or is a symlink.")
        mcp_template = json.loads(source_mcp.read_bytes())
        marketplace_source = root / ".agents" / "plugins" / "marketplace.json"
        if marketplace_source.is_symlink() or not marketplace_source.is_file():
            raise ConfigurationError("The source marketplace is missing or is a symlink.")
        marketplace = json.loads(marketplace_source.read_bytes())
        bridge_source = root / "mcp_bridge.py"
        if bridge_source.is_symlink() or not bridge_source.is_file():
            raise ConfigurationError("The standalone MCP bridge source is missing or is a symlink.")
        bridge_bytes = bridge_source.read_bytes()
        policy_source = root / "management_policy.py"
        if policy_source.is_symlink() or not policy_source.is_file():
            raise ConfigurationError("The standalone management policy source is missing or is a symlink.")
        policy_bytes = policy_source.read_bytes()
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError("Cannot read valid plugin source files. No output was generated.") from None
    if (not isinstance(manifest, dict) or manifest.get("name") != PLUGIN_NAME
            or manifest.get("skills") != "./skills/"
            or manifest.get("mcpServers") != "./.mcp.json"
            or not isinstance(manifest.get("version"), str)):
        raise ConfigurationError("The plugin manifest does not match the supported AgentCaptain package layout.")
    if mcp_template != {"mcpServers": {}}:
        raise ConfigurationError("The repository MCP template must not contain machine-specific commands or credentials.")
    if (not isinstance(marketplace, dict) or marketplace.get("name") != SOURCE_MARKETPLACE_NAME
            or not isinstance(marketplace.get("plugins"), list)
            or len(marketplace["plugins"]) != 1
            or not isinstance(marketplace["plugins"][0], dict)
            or marketplace["plugins"][0].get("name") != PLUGIN_NAME
            or marketplace["plugins"][0].get("source") != {"source": "local", "path": "./plugins/agent-captain"}):
        raise ConfigurationError("The marketplace does not match the supported AgentCaptain package layout.")
    # Keep the repo-discovered skill-only source catalog distinct from the
    # configured marketplace. Otherwise source selection could silently install
    # the unconfigured template under the same marketplace identity.
    if legacy_ceo_identity:
        plugin_name, marketplace_name, server_name = "ceo-hub", "ceo-hub-local", "ceo_hub"
        connection_env = "CEO_HUB_CONNECTION_FILE"
    elif legacy_pron_identity:
        plugin_name, marketplace_name, server_name = "pron-hub", "pron-hub-local", "pron_hub"
        connection_env = "PRON_HUB_CONNECTION_FILE"
    else:
        plugin_name, marketplace_name, server_name = PLUGIN_NAME, MARKETPLACE_NAME, "agent_captain"
        connection_env = "AGENTCAPTAIN_CONNECTION_FILE"
    if legacy_ceo_identity or legacy_pron_identity:
        # Explicit compatibility output only. Never inspect installed caches or
        # infer identity from user settings, and never copy any credentials.
        try:
            skill = prepared.pop("skills/agent-captain/SKILL.md").decode("utf-8")
            readme = prepared["README.md"].decode("utf-8")
        except UnicodeError:
            raise ConfigurationError("The compatibility source text is not valid UTF-8. No output was generated.") from None
        manifest["name"] = plugin_name
        prepared[".codex-plugin/plugin.json"] = _json_bytes(manifest)
        if not skill.startswith("---\nname: agent-captain\n") and not skill.startswith("---\r\nname: agent-captain\r\n"):
            raise ConfigurationError("The source skill identity cannot be converted safely. No output was generated.")
        prepared[f"skills/{plugin_name}/SKILL.md"] = skill.replace(
            "name: agent-captain", "name: " + plugin_name, 1).encode("utf-8")
        prepared["README.md"] = readme.replace(
            "`agent-captain` skill", f"`{plugin_name}` compatibility skill", 1).encode("utf-8")
    marketplace["name"] = marketplace_name
    marketplace["interface"] = {"displayName": "AgentCaptain local tools"}
    marketplace["plugins"][0]["name"] = plugin_name
    marketplace["plugins"][0]["source"] = {"source": "local", "path": "./plugins/" + plugin_name}

    plugin_output = destination / "plugins" / plugin_name
    bridge_output = plugin_output / "mcp_bridge.py"
    mcp = {"mcpServers": {server_name: {
        "command": str(interpreter), "args": ["-u", str(bridge_output)],
        # This is a location only, never a copied/read credential. The cached
        # bridge and ordinary-shell web service must address the same checkout.
        "env": {connection_env: str(root / ".runtime" / "service.json")}
    }}}
    marker = {"generator": "agent-captain-configure-plugin", "schema_version": GENERATOR_VERSION,
              "plugin": plugin_name, "plugin_version": manifest["version"],
              "legacy_ceo_identity": bool(legacy_ceo_identity),
              "legacy_pron_identity": bool(legacy_pron_identity),
              "retain_generated_directory": True}

    # Reserve a new target exclusively. Do not replace it atomically via rename:
    # on some platforms rename could replace an unknown empty directory.
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        raise ConfigurationError("Output appeared during generation. No existing directory was overwritten.") from None
    except OSError:
        raise ConfigurationError("Cannot create the output directory. Check permissions and choose a new --output.") from None
    try:
        for relative, content in prepared.items():
            target = plugin_output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
        with bridge_output.open("xb") as stream:
            stream.write(bridge_bytes)
        with (plugin_output / "management_policy.py").open("xb") as stream:
            stream.write(policy_bytes)
        with (plugin_output / ".mcp.json").open("xb") as stream:
            stream.write(_json_bytes(mcp))
        marketplace_output = destination / ".agents" / "plugins" / "marketplace.json"
        marketplace_output.parent.mkdir(parents=True, exist_ok=True)
        with marketplace_output.open("xb") as stream:
            stream.write(_json_bytes(marketplace))
        with (destination / ".agent-captain-generated.json").open("xb") as stream:
            stream.write(_json_bytes(marker))
    except OSError:
        # Retain a partial directory rather than deleting anything another
        # process might have placed there. It is not registered automatically.
        raise ConfigurationError("Generation failed; a partial output directory was retained. Choose a new --output after checking disk permissions.") from None

    # Quoted paths are display-only; the generator never invokes a shell.
    commands = [f'codex plugin marketplace add "{destination}"',
                f"codex plugin add {plugin_name}@{marketplace_name}"]
    return {"marketplace_root": str(destination), "plugin_root": str(plugin_output),
            "bridge": str(bridge_output), "plugin": f"{plugin_name}@{marketplace_name}",
            "version": manifest["version"], "commands": commands,
            "legacy_ceo_identity": bool(legacy_ceo_identity),
            "legacy_pron_identity": bool(legacy_pron_identity),
            "retain_generated_directory": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        help="New output directory (default: <checkout>/.runtime/plugin-marketplace). Existing targets are refused.")
    parser.add_argument("--json", action="store_true", help="Print generation metadata as JSON.")
    identities = parser.add_mutually_exclusive_group()
    identities.add_argument("--legacy-ceo-identity", action="store_true",
                            help="Keep ceo-hub@ceo-hub-local and ceo_hub identifiers for an explicit same-identity upgrade; displayed product remains AgentCaptain.")
    identities.add_argument("--legacy-pron-identity", action="store_true",
                            help="Keep pron-hub@pron-hub-local and pron_hub identifiers for an explicit same-identity upgrade; displayed product remains AgentCaptain.")
    args = parser.parse_args(argv)
    try:
        result = configure_plugin(args.output, legacy_ceo_identity=args.legacy_ceo_identity,
                                  legacy_pron_identity=args.legacy_pron_identity)
    except ConfigurationError as exc:
        print(f"AgentCaptain plugin: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Generated local marketplace: {result['marketplace_root']}")
        print("Keep this generated directory while the plugin is installed; the cached MCP command refers to its bridge.")
        print("No Codex settings, authentication files, or running services were modified.")
        print("To register and install it yourself:")
        for command in result["commands"]:
            print(f"  {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
