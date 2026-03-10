"""CLI entry point for odoo-model-mcp."""

import argparse
import logging
import sys

from .server import create_server


def main():
    parser = argparse.ArgumentParser(
        description="MCP server exposing Odoo model registry without a DB"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
