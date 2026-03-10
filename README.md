# odoo-model-mcp

MCP server that exposes Odoo's model registry (fields, inheritance, methods)
without needing a database connection.

Point it at any Odoo project directory and get instant, structured answers about
models, fields, method override chains, and inheritance graphs — powered by
Odoo's own `MetaModel` registry loaded in-memory.

> **Status:** Pre-alpha (0.1.0a1). Works with Odoo 16–18. Odoo 19 support is
> in progress.

## How it works

1. **MCP server** (this package) receives tool calls via stdio.
2. It auto-detects the Odoo project structure (odoo-bin, addons paths, venv).
3. A **persistent worker subprocess** is spawned in the project's own Python
   venv, loading the full model registry once (~4 seconds).
4. Subsequent queries are served over a Unix domain socket in **1–15 ms**.
5. Workers auto-shutdown after 10 minutes of inactivity.

Each project gets its own isolated worker process, so you can work with multiple
Odoo versions simultaneously without conflicts.

## Installation

```bash
# With uv (recommended)
uv tool install odoo-model-mcp

# With pip
pip install odoo-model-mcp
```

## Usage with Claude Code

Add to your `~/.claude.json` (global) or project `.claude/settings.local.json`:

```json
{
  "mcpServers": {
    "odoo-model-registry": {
      "type": "stdio",
      "command": "odoo-model-mcp",
      "args": []
    }
  }
}
```

If installed with `uv` and not on PATH:

```json
{
  "mcpServers": {
    "odoo-model-registry": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/odoo-model-mcp", "odoo-model-mcp"]
    }
  }
}
```

## Tools

All tools accept a `project_path` pointing to the root of an Odoo project.
Addons paths and the Odoo source location are auto-detected from the project
structure (or can be overridden with `addons_paths`). Modules can be excluded
by name with `exclude_modules` (useful when a module has unresolvable Python
dependencies).

### `detect_project_info`

Detect an Odoo project's structure without loading the registry. Returns
`odoo_path`, `addons_paths`, `python_bin`, and `odoo_version`.

### `search_models`

Search models by name or description substring.

```
search_models(project_path="/path/to/project", query="sale.order")
```

### `model_info`

Full metadata for a model: all fields (with types, compute methods, related
fields, module overrides), inheritance chain, extending modules (from MRO),
and decorated methods.

```
model_info(project_path="/path/to/project", model_name="sale.order")
```

### `field_info`

Detailed info for a single field: type, compute method, depends, store, index,
related, groups, and which modules defined or overrode it.

```
field_info(project_path="/path/to/project", model_name="sale.order", field_name="amount_total")
```

### `method_overrides`

Override chain for a method across the MRO, with source file locations (file
path and line number) for each override.

```
method_overrides(project_path="/path/to/project", model_name="sale.order", method_name="_compute_amounts")
```

### `model_graph`

Inheritance graph around a model: which modules extend it (same `_name`),
mixin parents (different `_name`), delegation parents (`_inherits`), and child
models that inherit from it.

```
model_graph(project_path="/path/to/project", model_name="sale.order")
```

## Project auto-detection

The server detects project structure automatically:

- **Odoo source**: looks for `odoo-bin` or the `odoo/` Python package
- **Addons paths**: parses `odoo.conf` / `.odoorc`, or scans for directories
  containing modules (subdirs with `__manifest__.py`)
- **Python venv**: checks `.venv/`, `venv/`, `env/`
- **Odoo version**: reads from `.env` (`ODOO_VERSION=`) or `odoo/release.py`

## Architecture

```
Claude Code
    |
    | stdio (MCP protocol)
    v
odoo-model-mcp server (lightweight Python process)
    |
    | Unix domain socket (JSON lines)
    v
Worker process (project's own venv)
    - Loads Odoo registry via MetaModel._build_model()
    - Resolves full inheritance (MRO, __bases__, _build_model_attributes)
    - Serves queries from in-memory registry
```

## Development

```bash
git clone https://github.com/bemade/odoo-model-mcp.git
cd odoo-model-mcp
uv sync
uv run pytest
```

## License

LGPL-3.0-only
