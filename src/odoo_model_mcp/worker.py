"""Worker server that runs inside an Odoo project's Python environment.

Loads the Odoo registry once at startup, then listens on a Unix domain socket
for JSON-line commands. Each connection handles one request-response pair.

Usage:
    /path/to/project/.venv/bin/python -m odoo_model_mcp.worker \
        --project-path /path/to/project \
        --socket /tmp/odoo-worker-XXXX.sock \
        [--addons a,b] [--exclude x,y] [--idle-timeout 600]
"""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)


def _load_pool(project_path: str, addons_paths: list[str] | None,
               exclude_modules: list[str] | None) -> tuple[dict, dict]:
    """Load the registry once and return (pool, project_info)."""
    from .loader import detect_project, load_registry

    project = detect_project(project_path)
    if not project["odoo_path"]:
        raise RuntimeError(f"Could not find Odoo source in {project_path}")

    effective_addons = addons_paths or project["addons_paths"]
    pool = load_registry(
        project["odoo_path"],
        effective_addons,
        exclude_modules=exclude_modules,
    )
    return pool, project


def _dispatch(pool: dict, project: dict, command: dict) -> dict:
    """Execute a single command against a loaded registry."""
    from . import extractor

    action = command.get("action")

    if action == "ping":
        return {"status": "ok", "model_count": len(pool)}

    if action == "detect_project":
        return project

    if action == "model_info":
        result = extractor.extract_model_info(pool, command["model_name"])
        return result or {"error": f"Model '{command['model_name']}' not found"}

    if action == "field_info":
        model_cls = pool.get(command["model_name"])
        if not model_cls:
            return {"error": f"Model '{command['model_name']}' not found"}
        result = extractor.extract_field_info(model_cls, command["field_name"])
        return result or {
            "error": f"Field '{command['field_name']}' not found on '{command['model_name']}'"
        }

    if action == "method_overrides":
        model_cls = pool.get(command["model_name"])
        if not model_cls:
            return {"error": f"Model '{command['model_name']}' not found"}
        result = extractor.extract_method_info(
            model_cls, command["method_name"]
        )
        return result or {
            "error": f"Method '{command['method_name']}' not found on '{command['model_name']}'"
        }

    if action == "model_graph":
        result = extractor.extract_model_graph(pool, command["model_name"])
        return result or {"error": f"Model '{command['model_name']}' not found"}

    if action == "search_models":
        return extractor.search_models(pool, command["query"])

    return {"error": f"Unknown action: {action}"}


def _handle_client(conn: socket.socket, pool: dict, project: dict,
                   touch_fn):
    """Handle one client connection: read request, write response, close."""
    try:
        touch_fn()
        # Read until newline
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk

        line = data.split(b"\n", 1)[0]
        try:
            command = json.loads(line)
        except json.JSONDecodeError as e:
            result = {"error": f"Invalid JSON: {e}"}
        else:
            try:
                result = _dispatch(pool, project, command)
            except Exception as e:
                result = {"error": str(e)}

        response = json.dumps(result, default=str).encode() + b"\n"
        conn.sendall(response)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--socket", required=True,
                        help="Path for the Unix domain socket")
    parser.add_argument("--addons", default=None,
                        help="Comma-separated addons paths")
    parser.add_argument("--exclude", default=None,
                        help="Comma-separated module names to exclude")
    parser.add_argument("--idle-timeout", type=int, default=600,
                        help="Seconds of inactivity before auto-shutdown")
    args = parser.parse_args()

    addons_paths = args.addons.split(",") if args.addons else None
    exclude_modules = args.exclude.split(",") if args.exclude else None

    # Clean up stale socket
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    # Load registry
    status_file = args.socket + ".status"
    try:
        pool, project = _load_pool(args.project_path, addons_paths,
                                   exclude_modules)
    except Exception as e:
        with open(status_file, "w") as f:
            json.dump({"error": str(e)}, f)
        sys.exit(1)

    # Bind socket
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    srv.listen(8)
    srv.settimeout(5)  # So accept() unblocks periodically for idle check

    # Write status file so the dispatcher knows we're ready
    with open(status_file, "w") as f:
        json.dump({"status": "ready", "model_count": len(pool)}, f)

    # Idle tracking
    last_activity = time.monotonic()
    lock = threading.Lock()
    shutdown_flag = False

    def touch():
        nonlocal last_activity
        with lock:
            last_activity = time.monotonic()

    def handle_signal(*_):
        nonlocal shutdown_flag
        shutdown_flag = True

    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Worker serving on %s (%d models)", args.socket, len(pool))

    try:
        while not shutdown_flag:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                # Check idle
                with lock:
                    idle = time.monotonic() - last_activity
                if idle > args.idle_timeout:
                    logger.info("Idle timeout, shutting down")
                    break
                continue
            except OSError:
                break

            t = threading.Thread(
                target=_handle_client,
                args=(conn, pool, project, touch),
                daemon=True,
            )
            t.start()
    finally:
        srv.close()
        try:
            os.unlink(args.socket)
        except OSError:
            pass
        try:
            os.unlink(status_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
