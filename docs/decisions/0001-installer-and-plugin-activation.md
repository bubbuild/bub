# Installer selection and plugin activation

- Status: installer accepted; runtime activation proposed
- Date: 2026-08-31

## Context

Bub plugins are Python distributions discovered through the `bub` entry-point group. Today, `BubFramework.load_hooks()` imports and registers every entry point in the active environment before constructing the CLI.

That makes three decisions look deceptively similar:

1. whether a plugin package is installed;
2. whether its hooks are active in one process;
3. which execution surface starts that process.

They are not interchangeable. For example, `bub-acp-server` supplies an ACP command, but it also supplies steering, state, tape-context, and system-prompt hooks. Installing it into a shared environment can therefore affect a normal chat process even when that process never starts the ACP command.

PDM provides a useful ownership boundary rather than a direct runtime-toggle design: its bootstrap installer owns the application environment, `pdm self` manages application plugins, and project plugins belong to a project. Dependency groups select packages; they do not decide which installed hooks run inside one process.

## Installer decision

The Bub installer uses two independent inputs:

```text
installation = preset.required_plugins ∪ user.optional_plugins ∪ user.extra_requirements
```

- A **preset** represents an execution surface and owns its required packages.
- An **optional plugin** is selected by the user independently of the surface.
- A required package cannot be disabled through the optional-plugin toggle.

The initial presets are:

| Preset | Required packages |
| --- | --- |
| `chat` | none |
| `lody` | `bub-acp-server` |

The initial optional catalog contains only published packages that make sense in either preset:

- `bub-web-search`
- `bub-mcp`
- `bub-semantic-memory`

The installer is a standalone, standard-library Python script. It creates one uv tool environment with `uv tool install --with`; it does not create named runtime profiles. Interactive and non-interactive invocations produce the same `InstallPlan`. The exact command can be inspected with `--dry-run`.

The installer writes the user's intent to `$BUB_HOME/install.json`, while uv's receipt remains authoritative for the environment contents. Keeping these roles separate avoids treating a preset label as package-manager state.

## Why not runtime profiles now

Multiple managed environments provide strong dependency isolation but introduce another launcher, update policy, path model, and configuration location. There is no current evidence that ordinary Bub plugins need conflicting dependency versions. Environment profiles are therefore reserved for a future isolation requirement, not used as the default installation model.

## Runtime activation research

The installer solves deployment selection, but it cannot make one installed environment safe for multiple surfaces. Runtime activation needs a separate contract.

The recommended direction is staged entry-point loading:

1. `bub` remains the backward-compatible group for context-independent hooks.
2. `bub.cli` contains command registration that is safe to load before a surface is selected.
3. `bub.context.<name>` contains hooks that are active only for that execution context.
4. `BubFramework` receives an explicit context such as `chat`, `acp`, or `gateway` and loads `bub` plus the matching context group.

Under that model, `bub-acp-server` would expose two small entry points:

```toml
[project.entry-points."bub.cli"]
acp = "bub_acp_server.cli_plugin:ACPCommandPlugin"

[project.entry-points."bub.context.acp"]
acp-server = "bub_acp_server.plugin:ACPServerPlugin"
```

The CLI entry point would only launch the ACP surface. The ACP runtime hooks would not be registered in `chat`.

### Alternatives considered

- **Entry-point allow/deny lists:** useful as a migration or diagnostic control, but package names do not describe activation semantics and users would have to maintain fragile lists.
- **An `enabled` flag in config:** still requires importing and often constructing every plugin before deciding whether it is enabled; it also gives third-party packages no declarative context metadata.
- **Infer context from `sys.argv`:** couples framework construction to one CLI and does not work for embedded Bub runtimes.
- **One environment per preset:** isolates dependencies and hooks, but carries substantially more operational state than activation alone requires.

## Open validation work

Before implementing staged entry points:

1. inventory existing plugins and classify them as common, CLI-only, or surface-specific;
2. verify that Pluggy ordering is unchanged when common and context groups are combined;
3. design a compatibility warning for a surface plugin still published in the legacy `bub` group;
4. test embedded framework construction, not only the Typer CLI;
5. decide whether `gateway` is one context or a context parameterized by enabled channels.

No runtime loader change is part of the installer change.

## References

- [uv: Tools](https://docs.astral.sh/uv/concepts/tools/)
- [PDM: Write a PDM plugin](https://pdm-project.org/latest/dev/write/)
- [PDM: Dependency groups](https://pdm-project.org/latest/usage/dependency/)
