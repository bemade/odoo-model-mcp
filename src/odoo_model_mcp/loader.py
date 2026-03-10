"""Load Odoo model registry without a database connection."""

import logging
import sys
import time
from graphlib import TopologicalSorter
from pathlib import Path

logger = logging.getLogger(__name__)


def _has_odoo_package(path: Path) -> bool:
    """Check if a directory contains the Odoo Python package.

    Handles both Odoo <=18 (__init__.py) and Odoo 19+ (init.py).
    """
    return (
        path.is_dir()
        and (
            (path / "__init__.py").exists()
            or (path / "init.py").exists()
        )
        and (
            (path / "models.py").exists()
            or (path / "models").is_dir()
        )
    )


def detect_project(project_path: str) -> dict:
    """Auto-detect an Odoo project's structure from its root path.

    Discovery strategy:
    1. Find `odoo-bin` or the `odoo` Python package to locate Odoo source
    2. Find addons paths by looking for directories that contain modules
       (subdirs with `__manifest__.py`)
    3. Respect `odoo.conf` if present
    4. Find the project's Python virtualenv

    Returns a dict with:
        odoo_path: str — path to Odoo source root (parent of odoo/ package)
        addons_paths: list[str] — all addons paths found
        python_bin: str — path to the project's Python interpreter
        odoo_version: str | None — if detectable
    """
    root = Path(project_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project path does not exist: {root}")

    result = {
        "odoo_path": None,
        "addons_paths": [],
        "python_bin": None,
        "odoo_version": None,
    }

    # --- Find Odoo source root ---
    # Strategy 1: Look for odoo-bin (standard Odoo source layout)
    for candidate in [root, root / "odoo"]:
        if (candidate / "odoo-bin").exists() and _has_odoo_package(
            candidate / "odoo"
        ):
            result["odoo_path"] = str(candidate)
            break

    # Strategy 2: Look for odoo package directly
    if not result["odoo_path"]:
        if _has_odoo_package(root / "odoo" / "odoo"):
            result["odoo_path"] = str(root / "odoo")
        elif _has_odoo_package(root / "odoo"):
            result["odoo_path"] = str(root)

    # --- Find addons paths ---
    # Strategy 1: Parse odoo.conf if present
    conf_paths = _parse_odoo_conf(root)
    if conf_paths:
        result["addons_paths"] = conf_paths
    else:
        # Strategy 2: Scan immediate subdirectories for ones that contain
        # Odoo modules (dirs with __manifest__.py or __openerp__.py)
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if _is_addons_path(child):
                result["addons_paths"].append(str(child))

    # --- Find Python interpreter ---
    for venv_dir in [".venv", "venv", "env"]:
        python = root / venv_dir / "bin" / "python"
        if python.exists():
            result["python_bin"] = str(python)
            break

    # --- Detect Odoo version ---
    # From .env file
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ODOO_VERSION="):
                result["odoo_version"] = line.split("=", 1)[1].strip()
                break

    # From odoo/release.py if available
    if not result["odoo_version"] and result["odoo_path"]:
        release_file = Path(result["odoo_path"]) / "odoo" / "release.py"
        if release_file.exists():
            for line in release_file.read_text().splitlines():
                if line.startswith("version_info"):
                    # e.g., version_info = (18, 0, 0, FINAL, 0, '')
                    try:
                        parts = line.split("(")[1].split(")")[0].split(",")
                        result["odoo_version"] = (
                            f"{parts[0].strip()}.{parts[1].strip()}"
                        )
                    except (IndexError, ValueError):
                        pass
                    break

    return result


def _is_addons_path(path: Path) -> bool:
    """Check if a directory is an Odoo addons path.

    An addons path contains at least one subdirectory with __manifest__.py
    or __openerp__.py.
    """
    try:
        for child in path.iterdir():
            if child.is_dir() and (
                (child / "__manifest__.py").exists()
                or (child / "__openerp__.py").exists()
            ):
                return True
    except PermissionError:
        pass
    return False


def _parse_odoo_conf(project_root: Path) -> list[str] | None:
    """Parse addons_path from an odoo.conf file if present."""
    for conf_name in ["odoo.conf", ".odoorc", "openerp-server.conf"]:
        conf_file = project_root / conf_name
        if conf_file.exists():
            import configparser

            config = configparser.ConfigParser()
            try:
                config.read(str(conf_file))
                addons_path = config.get("options", "addons_path", fallback=None)
                if addons_path:
                    paths = [
                        str(Path(p.strip()).resolve())
                        for p in addons_path.split(",")
                        if p.strip()
                    ]
                    return [p for p in paths if Path(p).is_dir()]
            except Exception:
                logger.debug("Failed to parse %s", conf_file)
    return None


def _detect_odoo_major_version(odoo_root: Path) -> int:
    """Detect the major Odoo version from release.py."""
    release_file = odoo_root / "odoo" / "release.py"
    if release_file.exists():
        for line in release_file.read_text().splitlines():
            if line.startswith("version_info"):
                try:
                    parts = line.split("(")[1].split(")")[0].split(",")
                    return int(parts[0].strip())
                except (IndexError, ValueError):
                    pass
    return 0


def configure_odoo(odoo_path: str, addons_paths: list[str]):
    """Add Odoo to sys.path and configure addons paths."""
    odoo_root = Path(odoo_path).resolve()
    if not _has_odoo_package(odoo_root / "odoo"):
        raise FileNotFoundError(
            f"No odoo package found at {odoo_root}/odoo/"
        )

    # Odoo's root must be on sys.path so `import odoo` works
    root_str = str(odoo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    import odoo
    from odoo.tools import config

    # Resolve all addons paths
    resolved = [str(Path(p).resolve()) for p in addons_paths]
    # Always include odoo's built-in addons
    builtin = str(odoo_root / "odoo" / "addons")
    if builtin not in resolved:
        resolved.append(builtin)
    # And the top-level addons dir if it exists
    top_addons = str(odoo_root / "addons")
    if Path(top_addons).is_dir() and top_addons not in resolved:
        resolved.append(top_addons)

    config["addons_path"] = ",".join(resolved)

    from odoo.modules.module import initialize_sys_path
    initialize_sys_path()


def discover_modules(
    module_filter: list[str] | None = None,
    exclude_modules: list[str] | None = None,
) -> list[str]:
    """Discover available modules and return them in dependency order."""
    from odoo.modules.module import get_manifest, get_modules

    all_modules = get_modules()
    logger.info("Discovered %d modules", len(all_modules))

    # Build dependency graph
    manifests = {}
    dep_graph = {}
    for mod in all_modules:
        try:
            manifest = get_manifest(mod)
            if not manifest:
                continue
            manifests[mod] = manifest
            dep_graph[mod] = set(manifest.get("depends", []))
        except Exception:
            logger.debug("Failed to read manifest for %s, skipping", mod)

    # If filtering, compute transitive closure of dependencies
    if module_filter:
        needed = set()
        queue = [m for m in module_filter if m in dep_graph]
        while queue:
            mod = queue.pop()
            if mod in needed:
                continue
            needed.add(mod)
            for dep in dep_graph.get(mod, []):
                if dep not in needed:
                    queue.append(dep)
        dep_graph = {m: dep_graph[m] for m in needed if m in dep_graph}

    # Topological sort — filter deps to only known modules
    filtered_graph = {}
    for mod, deps in dep_graph.items():
        filtered_graph[mod] = {d for d in deps if d in dep_graph}

    sorter = TopologicalSorter(filtered_graph)
    try:
        order = list(sorter.static_order())
    except Exception as e:
        logger.error("Dependency cycle detected: %s", e)
        order = list(dep_graph.keys())

    # Apply exclusions
    if exclude_modules:
        excluded = set(exclude_modules)
        order = [m for m in order if m not in excluded]
        logger.info("Excluded %d modules", len(excluded))

    logger.info("Will load %d modules in dependency order", len(order))
    return order


def import_modules(modules: list[str]) -> dict[str, str]:
    """Import modules, triggering MetaModel class registration.

    Returns dict of failed module names to error messages.
    """
    from odoo.modules.module import load_openerp_module

    failures = {}
    for mod in modules:
        try:
            load_openerp_module(mod)
        except Exception as e:
            logger.warning("Failed to import module %s: %s", mod, e)
            failures[mod] = str(e)
    return failures


def _get_module_to_models() -> dict:
    """Get the MetaModel module-to-models mapping (version-agnostic)."""
    try:
        # Odoo 19+: odoo.orm.models.MetaModel._module_to_models__
        from odoo.orm.models import MetaModel
        return MetaModel._module_to_models__
    except (ImportError, AttributeError):
        pass

    # Odoo 16–18: odoo.models.MetaModel.module_to_models
    from odoo.models import MetaModel
    return MetaModel.module_to_models


def _is_odoo_19() -> bool:
    """Check if the loaded Odoo version is 19+."""
    try:
        from odoo.orm import model_classes  # noqa: F401
        return True
    except ImportError:
        return False


def _build_registry_v19(modules: list[str]) -> dict:
    """Build registry using Odoo 19's model_classes.add_to_registry."""
    from odoo.orm import model_classes

    module_to_models = _get_module_to_models()

    # Odoo 19's add_to_registry expects a registry object that supports
    # dict-like access plus a .descendants() method. We use a simple
    # wrapper around a dict.
    pool = _RegistryProxy()
    failed_models = {}

    for mod in modules:
        for cls in module_to_models.get(mod, []):
            try:
                model_classes.add_to_registry(pool, cls)
            except Exception as e:
                model_name = getattr(cls, "_name", None) or getattr(
                    cls, "_inherit", ["unknown"]
                )
                logger.warning(
                    "Failed to build model %s from %s: %s", model_name, mod, e
                )
                failed_models[str(model_name)] = str(e)

    # Apply __bases__ fixup from _base_classes__ (Odoo 19 uses this name
    # without Python name-mangling)
    fixup_count = 0
    for name, model_cls in pool.items():
        base_classes = model_cls.__dict__.get("_base_classes__")
        if base_classes and model_cls.__bases__ != base_classes:
            try:
                model_cls.__bases__ = base_classes
                fixup_count += 1
            except TypeError as e:
                logger.debug("Could not fix __bases__ for %s: %s", name, e)

    logger.info(
        "Built %d models (%d failed, %d bases fixed)",
        len(pool),
        len(failed_models),
        fixup_count,
    )
    return dict(pool)


def _build_registry_v18(modules: list[str]) -> dict:
    """Build registry using Odoo 16–18's _build_model."""
    module_to_models = _get_module_to_models()

    pool = {}
    failed_models = {}

    for mod in modules:
        for cls in module_to_models.get(mod, []):
            try:
                cls._build_model(pool, None)
            except Exception as e:
                model_name = getattr(cls, "_name", None) or getattr(
                    cls, "_inherit", "unknown"
                )
                logger.warning(
                    "Failed to build model %s from %s: %s", model_name, mod, e
                )
                failed_models[model_name] = str(e)

    # Apply __bases__ fixup: _build_model sets __base_classes (name-mangled
    # to _BaseModel__base_classes) but defers the actual __bases__ assignment
    # to _prepare_setup(). We do it here to get the full MRO.
    fixup_count = 0
    for name, model_cls in pool.items():
        base_classes = model_cls.__dict__.get("_BaseModel__base_classes")
        if base_classes and model_cls.__bases__ != base_classes:
            try:
                model_cls.__bases__ = base_classes
                fixup_count += 1
            except TypeError as e:
                logger.debug("Could not fix __bases__ for %s: %s", name, e)

    # Also call _build_model_attributes to resolve _description, _table,
    # _inherits, _inherit_children, etc.
    for name, model_cls in pool.items():
        try:
            model_cls._build_model_attributes(pool)
        except Exception as e:
            logger.debug("_build_model_attributes failed for %s: %s", name, e)

    logger.info(
        "Built %d models (%d failed, %d bases fixed)",
        len(pool),
        len(failed_models),
        fixup_count,
    )
    return pool


class _RegistryProxy(dict):
    """Minimal dict subclass that satisfies Odoo 19's Registry interface
    for model building (supports dict ops + descendants())."""

    def descendants(self, model_names, *args):
        """Yield model names and their transitive children."""
        from collections import deque
        todo = deque(model_names)
        result = set()
        while todo:
            name = todo.popleft()
            if name in result:
                continue
            result.add(name)
            model_cls = self.get(name)
            if model_cls:
                for attr in args:
                    children = getattr(model_cls, attr, None)
                    if children:
                        todo.extend(children)
        return result


def build_registry(modules: list[str]) -> dict:
    """Build the model registry from imported modules.

    Detects the Odoo version and uses the appropriate build strategy.
    Returns the pool dict mapping model names to registry classes.
    """
    if _is_odoo_19():
        return _build_registry_v19(modules)
    return _build_registry_v18(modules)


def load_registry(
    odoo_path: str,
    addons_paths: list[str],
    module_filter: list[str] | None = None,
    exclude_modules: list[str] | None = None,
) -> dict:
    """Full pipeline: configure, discover, import, build.

    Returns the pool dict.
    """
    t0 = time.monotonic()

    configure_odoo(odoo_path, addons_paths)
    modules = discover_modules(module_filter, exclude_modules)
    failures = import_modules(modules)
    pool = build_registry(modules)

    elapsed = time.monotonic() - t0
    logger.info(
        "Registry loaded in %.1fs: %d models, %d module import failures",
        elapsed,
        len(pool),
        len(failures),
    )
    return pool
