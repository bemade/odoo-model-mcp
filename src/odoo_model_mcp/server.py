"""MCP server exposing Odoo model registry tools.

Dispatches to persistent worker servers — one per project — communicating
over Unix domain sockets. The first call for a project spawns a worker
(~5s registry load), subsequent calls connect instantly.

Workers auto-shutdown after 10 minutes of inactivity.
"""

import atexit
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

from mcp.server.fastmcp import FastMCP

from .loader import detect_project

logger = logging.getLogger(__name__)

SOCKET_DIR = os.path.join(tempfile.gettempdir(), "odoo-model-mcp")
WORKER_STARTUP_TIMEOUT = 60
WORKER_IDLE_TIMEOUT = 600


class WorkerConnection:
    """Manages a persistent worker server for one project."""

    def __init__(self, project_path: str, python_bin: str,
                 addons_paths: list[str] | None = None,
                 exclude_modules: list[str] | None = None):
        self.project_path = project_path
        self.python_bin = python_bin
        self.addons_paths = addons_paths
        self.exclude_modules = exclude_modules
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

        # Derive a stable socket path from the project config
        key = project_path
        if addons_paths:
            key += ":" + ",".join(sorted(addons_paths))
        if exclude_modules:
            key += ":" + ",".join(sorted(exclude_modules))
        h = hashlib.sha256(key.encode()).hexdigest()[:12]
        os.makedirs(SOCKET_DIR, exist_ok=True)
        self.socket_path = os.path.join(SOCKET_DIR, f"worker-{h}.sock")
        self.status_path = self.socket_path + ".status"

    def _build_env(self) -> dict:
        """Build environment with our package on PYTHONPATH."""
        from pathlib import Path
        our_src = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{our_src}:{existing}" if existing else our_src
        return env

    def _ensure_running(self):
        """Start the worker server if it's not already running."""
        # Quick check: is the socket already live?
        if os.path.exists(self.socket_path):
            try:
                self._ping()
                return
            except (ConnectionError, OSError):
                # Stale socket — clean up and restart
                self._cleanup_stale()

        # Start worker process
        cmd = [self.python_bin, "-u", "-m", "odoo_model_mcp.worker",
               "--project-path", self.project_path,
               "--socket", self.socket_path,
               "--idle-timeout", str(WORKER_IDLE_TIMEOUT)]
        if self.addons_paths:
            cmd.extend(["--addons", ",".join(self.addons_paths)])
        if self.exclude_modules:
            cmd.extend(["--exclude", ",".join(self.exclude_modules)])

        logger.info("Starting worker for %s", self.project_path)

        # Remove stale status file
        if os.path.exists(self.status_path):
            os.unlink(self.status_path)

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._build_env(),
        )

        # Wait for status file (worker writes it when ready)
        deadline = time.monotonic() + WORKER_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            # Check if process died
            if self._proc.poll() is not None:
                # Check status file for structured error
                if os.path.exists(self.status_path):
                    with open(self.status_path) as f:
                        status = json.load(f)
                    os.unlink(self.status_path)
                    raise RuntimeError(
                        f"Worker failed: {status.get('error', 'unknown')}"
                    )
                raise RuntimeError("Worker died unexpectedly")

            if os.path.exists(self.status_path):
                with open(self.status_path) as f:
                    status = json.load(f)
                if "error" in status:
                    raise RuntimeError(f"Worker error: {status['error']}")
                logger.info("Worker ready for %s (%d models)",
                            self.project_path, status.get("model_count", 0))
                return

            time.sleep(0.1)

        # Timeout
        self._proc.kill()
        raise RuntimeError(
            f"Worker startup timed out after {WORKER_STARTUP_TIMEOUT}s"
        )

    def _ping(self):
        """Quick liveness check via the socket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            sock.connect(self.socket_path)
            sock.sendall(json.dumps({"action": "ping"}).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Worker closed connection")
                data += chunk
            json.loads(data.split(b"\n", 1)[0])
        finally:
            sock.close()

    def _cleanup_stale(self):
        """Remove stale socket and status files."""
        for path in [self.socket_path, self.status_path]:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def send(self, command: dict, timeout: float = 30) -> dict:
        """Send a command to the worker and return the result. Thread-safe."""
        with self._lock:
            try:
                self._ensure_running()
            except RuntimeError as e:
                return {"error": str(e)}

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(self.socket_path)
                sock.sendall(json.dumps(command).encode() + b"\n")

                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return {"error": "Worker closed connection"}
                    data += chunk

                return json.loads(data.split(b"\n", 1)[0])
            except (ConnectionError, OSError, json.JSONDecodeError) as e:
                return {"error": f"Worker communication failed: {e}"}
            finally:
                sock.close()

    def shutdown(self):
        """Shut down the worker process."""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                logger.info("Worker stopped for %s", self.project_path)
            self._cleanup_stale()
            self._proc = None


class WorkerPool:
    """Manages persistent workers keyed by project config."""

    def __init__(self):
        self._workers: dict[str, WorkerConnection] = {}
        self._lock = threading.Lock()

    def get(self, project_path: str,
            addons_paths: list[str] | None = None,
            exclude_modules: list[str] | None = None) -> WorkerConnection:
        """Get or create a worker connection for the given project."""
        resolved = os.path.realpath(project_path)

        key = resolved
        if addons_paths:
            key += ":addons=" + ",".join(sorted(addons_paths))
        if exclude_modules:
            key += ":exclude=" + ",".join(sorted(exclude_modules))

        with self._lock:
            if key not in self._workers:
                project = detect_project(resolved)
                if not project["odoo_path"]:
                    raise ValueError(
                        f"Could not find Odoo source in {project_path}"
                    )
                python_bin = project.get("python_bin") or sys.executable
                effective_addons = addons_paths or project["addons_paths"]

                self._workers[key] = WorkerConnection(
                    project_path=resolved,
                    python_bin=python_bin,
                    addons_paths=effective_addons,
                    exclude_modules=exclude_modules,
                )
            return self._workers[key]

    def shutdown_all(self):
        """Shut down all workers."""
        with self._lock:
            for worker in self._workers.values():
                worker.shutdown()
            self._workers.clear()


# Global worker pool
_pool = WorkerPool()
atexit.register(_pool.shutdown_all)


def _run_worker(command: dict, timeout: float = 30) -> dict:
    """Send a command to the appropriate persistent worker."""
    project_path = command["project_path"]
    addons_paths = command.get("addons_paths")
    exclude_modules = command.get("exclude_modules")

    try:
        worker = _pool.get(project_path, addons_paths, exclude_modules)
    except ValueError as e:
        return {"error": str(e)}

    return worker.send(command, timeout=timeout)


def create_server() -> FastMCP:
    """Create an MCP server with tools that dispatch to persistent workers."""
    mcp = FastMCP("odoo-model-registry")

    @mcp.tool()
    def model_info(
        project_path: str,
        model_name: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Get full metadata for an Odoo model: fields, inheritance, methods.

        Args:
            project_path: Path to the Odoo project root
            model_name: Dotted model name, e.g. 'sale.order'
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "model_info",
            "project_path": project_path,
            "model_name": model_name,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def field_info(
        project_path: str,
        model_name: str,
        field_name: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Get detailed info for a field: type, compute, depends, overrides.

        Args:
            project_path: Path to the Odoo project root
            model_name: Dotted model name, e.g. 'sale.order'
            field_name: Field name, e.g. 'amount_total'
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "field_info",
            "project_path": project_path,
            "model_name": model_name,
            "field_name": field_name,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def method_overrides(
        project_path: str,
        model_name: str,
        method_name: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Get all modules that override a method, in MRO order, with source locations.

        Args:
            project_path: Path to the Odoo project root
            model_name: Dotted model name, e.g. 'sale.order'
            method_name: Method name, e.g. '_compute_tax_totals'
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "method_overrides",
            "project_path": project_path,
            "model_name": model_name,
            "method_name": method_name,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def model_graph(
        project_path: str,
        model_name: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Get the inheritance graph for a model: extending modules, mixin parents, children.

        Args:
            project_path: Path to the Odoo project root
            model_name: Dotted model name, e.g. 'sale.order'
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "model_graph",
            "project_path": project_path,
            "model_name": model_name,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def search_models(
        project_path: str,
        query: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Search models by name or description.

        Args:
            project_path: Path to the Odoo project root
            query: Search string to match against model names and descriptions
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "search_models",
            "project_path": project_path,
            "query": query,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def list_models(
        project_path: str,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """List every model in the project registry with lightweight metadata.

        Returns: name, description, module, field_count, abstract, transient.
        Cheap to call; does not include per-field or per-method detail.

        Args:
            project_path: Path to the Odoo project root
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker({
            "action": "list_models",
            "project_path": project_path,
            "addons_paths": addons_paths,
            "exclude_modules": exclude_modules,
        })
        return json.dumps(result, default=str)

    @mcp.tool()
    def dump_registry(
        project_path: str,
        output_path: str,
        include_methods: bool = True,
        addons_paths: list[str] | None = None,
        exclude_modules: list[str] | None = None,
    ) -> str:
        """Bulk export the entire registry to a JSONL file.

        Emits one JSON object per line. Record types:
          {"type": "model", "data": {...model_info...}}
          {"type": "method_overrides", "model": "...", "method": "...", "overrides": [...]}

        Designed for initial ingestion: one call replaces thousands of
        per-model round trips. Writes to a file on disk (where the worker
        runs) to avoid oversized MCP responses.

        Args:
            project_path: Path to the Odoo project root
            output_path: Absolute path where the JSONL file will be written
            include_methods: If True, emit method_overrides records for every
                decorated method. Default True.
            addons_paths: Optional explicit addons paths (auto-detected if omitted)
            exclude_modules: Optional list of module names to skip loading
        """
        result = _run_worker(
            {
                "action": "dump_registry",
                "project_path": project_path,
                "output_path": output_path,
                "include_methods": include_methods,
                "addons_paths": addons_paths,
                "exclude_modules": exclude_modules,
            },
            timeout=1800,
        )
        return json.dumps(result, default=str)

    @mcp.tool()
    def detect_project_info(project_path: str) -> str:
        """Detect an Odoo project's structure without loading the registry.

        Args:
            project_path: Path to the Odoo project root
        """
        info = detect_project(project_path)
        return json.dumps(info, default=str)

    return mcp
