"""One-shot registry dump for consumers that don't need the MCP loop.

Designed for invocation with a project's own Python:

    /path/to/project/.venv/bin/python -m odoo_model_mcp.dump \
        --project-path /path/to/project \
        --output /tmp/dump.jsonl

Emits a JSON summary on stdout on success. Intended for orchestrators
(e.g. bemade-rag's `ingest` command) that want to run discovery in the
target project's venv without maintaining a persistent worker process.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .extractor import list_models as _list_models
from .loader import detect_project, load_registry
from .worker import _dump_registry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump an Odoo project's model registry to a JSONL file.",
    )
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument(
        "--addons",
        default=None,
        help="Comma-separated addons paths (overrides auto-detection)",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated module names to skip loading",
    )
    parser.add_argument(
        "--no-methods",
        action="store_true",
        help="Omit per-method override records (model records only)",
    )
    parser.add_argument(
        "--list-addons",
        action="store_true",
        help="Print resolved addons paths and exit",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    project = detect_project(args.project_path)
    if not project["odoo_path"]:
        print(
            json.dumps({"error": f"Could not find Odoo source in {args.project_path}"}),
            file=sys.stderr,
        )
        return 2

    addons_paths = (
        args.addons.split(",") if args.addons else project["addons_paths"]
    )
    exclude_modules = args.exclude.split(",") if args.exclude else None

    if args.list_addons:
        print(json.dumps({
            "odoo_path": project["odoo_path"],
            "addons_paths": addons_paths,
            "odoo_version": project["odoo_version"],
        }))
        return 0

    pool = load_registry(project["odoo_path"], addons_paths, exclude_modules=exclude_modules)

    result = _dump_registry(
        pool,
        {
            "output_path": args.output,
            "include_methods": not args.no_methods,
        },
    )
    # Annotate with detected project metadata for the orchestrator.
    result["addons_paths"] = addons_paths
    result["odoo_path"] = project["odoo_path"]
    result["odoo_version"] = project["odoo_version"]
    result["model_catalog_count"] = len(_list_models(pool))
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
