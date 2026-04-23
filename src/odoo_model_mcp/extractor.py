"""Extract structured metadata from the Odoo model registry."""

import inspect
import logging

logger = logging.getLogger(__name__)


def extract_field_info(model_cls, field_name: str) -> dict | None:
    """Extract metadata for a single field from a model's registry class."""
    # Walk _field_definitions across MRO to find the field
    field_defs = []
    for klass in reversed(model_cls.__mro__):
        for f in getattr(klass, "_field_definitions", []):
            if f.name == field_name:
                field_defs.append((klass, f))

    if not field_defs:
        return None

    # Last definition wins for most attributes
    _, field = field_defs[-1]
    info = {
        "name": field_name,
        "type": field.type,
        "module_overrides": [],
    }

    # Collect attributes, handling missing ones gracefully
    for attr in (
        "string",
        "help",
        "required",
        "readonly",
        "store",
        "index",
        "copy",
        "comodel_name",
        "inverse_name",
        "compute",
        "inverse",
        "search",
        "related",
        "company_dependent",
        "groups",
        "default",
        "precompute",
    ):
        val = getattr(field, attr, None)
        if val is not None and val is not False:
            # Skip callables (like default functions) — just note they exist
            if callable(val) and attr == "default":
                info[attr] = "<callable>"
            else:
                info[attr] = val

    # Collect depends from the compute method if present
    compute_name = getattr(field, "compute", None)
    if compute_name and isinstance(compute_name, str):
        method = getattr(model_cls, compute_name, None)
        if method:
            depends = getattr(method, "_depends", None)
            if depends and not callable(depends):
                info["depends"] = (
                    list(depends) if not isinstance(depends, str) else [depends]
                )
            elif callable(depends):
                info["depends"] = "<dynamic>"

    # Track which modules defined/overrode this field
    for klass, f in field_defs:
        mod = getattr(klass, "_module", None)
        if mod and mod not in info["module_overrides"]:
            info["module_overrides"].append(mod)

    return info


def extract_method_info(model_cls, method_name: str) -> list[dict]:
    """Extract override chain for a method across the MRO."""
    overrides = []
    seen_modules = set()

    for klass in model_cls.__mro__:
        if method_name not in klass.__dict__:
            continue

        method = klass.__dict__[method_name]
        if not callable(method):
            continue

        module = getattr(klass, "_module", None)
        if module in seen_modules:
            continue
        seen_modules.add(module)

        entry = {
            "module": module,
            "class": klass.__qualname__,
        }

        # Extract decorator metadata
        for attr, key in (
            ("_depends", "depends"),
            ("_constrains", "constrains"),
            ("_onchange", "onchange"),
            ("_ondelete", "ondelete"),
        ):
            val = getattr(method, attr, None)
            if val:
                if callable(val):
                    entry[key] = "<dynamic>"
                elif isinstance(val, str):
                    entry[key] = [val]
                elif isinstance(val, bool):
                    entry[key] = val
                else:
                    try:
                        entry[key] = list(val)
                    except TypeError:
                        entry[key] = repr(val)

        # Source location
        try:
            source_file = inspect.getfile(method)
            _, line_no = inspect.getsourcelines(method)
            entry["file"] = source_file
            entry["line"] = line_no
        except (TypeError, OSError):
            pass

        overrides.append(entry)

    return overrides


def extract_model_info(pool: dict, model_name: str) -> dict | None:
    """Extract full metadata for a model."""
    model_cls = pool.get(model_name)
    if model_cls is None:
        return None

    info = {
        "name": model_name,
        "description": getattr(model_cls, "_description", None),
        "module": getattr(model_cls, "_original_module", None),
        "table": getattr(model_cls, "_table", None),
        "abstract": getattr(model_cls, "_abstract", False),
        "transient": getattr(model_cls, "_transient", False),
    }

    # Inheritance
    inherit = getattr(model_cls, "_inherit", None)
    if inherit:
        info["inherit"] = (
            inherit if isinstance(inherit, list) else [inherit]
        )
    inherits = getattr(model_cls, "_inherits", None)
    if inherits:
        info["inherits"] = dict(inherits)

    # Modules that extend this model (from MRO), with source location per class
    extending_modules = []
    class_locations = []
    seen_modules = set()
    for order, klass in enumerate(model_cls.__mro__):
        mod = getattr(klass, "_module", None)
        if not mod or mod in seen_modules:
            continue
        seen_modules.add(mod)
        extending_modules.append(mod)

        loc: dict = {"module": mod, "class": klass.__qualname__, "order": order}
        try:
            loc["file"] = inspect.getfile(klass)
            lines, line_start = inspect.getsourcelines(klass)
            loc["line_start"] = line_start
            loc["line_end"] = line_start + len(lines) - 1
        except (TypeError, OSError):
            pass
        class_locations.append(loc)
    info["extending_modules"] = extending_modules
    info["class_locations"] = class_locations

    # Models with a different _name that inherit from this one
    children = getattr(model_cls, "_inherit_children", None)
    if children:
        info["inherited_by"] = sorted(children)
    inherits_children = getattr(model_cls, "_inherits_children", None)
    if inherits_children:
        info["delegation_children"] = sorted(inherits_children)

    # Fields — collect unique field names from all definition classes
    field_names = set()
    for klass in model_cls.__mro__:
        for f in getattr(klass, "_field_definitions", []):
            field_names.add(f.name)

    info["fields"] = {}
    for fname in sorted(field_names):
        finfo = extract_field_info(model_cls, fname)
        if finfo:
            info["fields"][fname] = finfo

    # Methods with decorators (the "interesting" ones)
    interesting_methods = []
    seen = set()
    for klass in model_cls.__mro__:
        for name, method in klass.__dict__.items():
            if name in seen or not callable(method) or name.startswith("__"):
                continue
            has_decorator = any(
                hasattr(method, attr)
                for attr in ("_depends", "_constrains", "_onchange", "_ondelete")
            )
            if has_decorator:
                seen.add(name)
                interesting_methods.append(name)
    info["decorated_methods"] = sorted(interesting_methods)

    return info


def extract_model_graph(pool: dict, model_name: str) -> dict | None:
    """Extract the inheritance graph around a model."""
    model_cls = pool.get(model_name)
    if model_cls is None:
        return None

    graph = {
        "model": model_name,
        "extending_modules": [],
        "mixin_parents": [],
        "children": [],
    }

    # Modules that extend this model (same _name, via MRO)
    for klass in model_cls.__mro__:
        mod = getattr(klass, "_module", None)
        if mod and mod not in graph["extending_modules"]:
            graph["extending_modules"].append(mod)

    # Parent models with a different _name (mixins, delegation)
    # _inherit_module maps parent model name -> module that introduced the inheritance
    inherit_module = getattr(model_cls, "_inherit_module", {})
    for parent_model, introducing_module in inherit_module.items():
        if parent_model != "base" and parent_model in pool:
            graph["mixin_parents"].append({
                "model": parent_model,
                "type": "inherit",
                "introduced_by": introducing_module,
            })

    inherits = getattr(model_cls, "_inherits", None)
    if inherits:
        for parent, field in inherits.items():
            if parent in pool:
                graph["mixin_parents"].append({
                    "model": parent,
                    "type": "inherits",
                    "field": field,
                })

    # Models with a different _name that inherit from this one
    children = getattr(model_cls, "_inherit_children", None)
    if children:
        for child in sorted(children):
            if child in pool and child != model_name:
                graph["children"].append({
                    "model": child,
                    "type": "inherit",
                    "module": getattr(pool[child], "_original_module", None),
                })

    inherits_children = getattr(model_cls, "_inherits_children", None)
    if inherits_children:
        for child in sorted(inherits_children):
            if child in pool:
                graph["children"].append({
                    "model": child,
                    "type": "inherits",
                    "module": getattr(pool[child], "_original_module", None),
                })

    return graph


def list_models(pool: dict) -> list[dict]:
    """Return lightweight metadata for every model in the registry."""
    results = []
    for name, cls in sorted(pool.items()):
        results.append({
            "name": name,
            "description": getattr(cls, "_description", "") or "",
            "module": getattr(cls, "_original_module", None),
            "field_count": len(set(
                f.name
                for k in cls.__mro__
                for f in getattr(k, "_field_definitions", [])
            )),
            "abstract": getattr(cls, "_abstract", False),
            "transient": getattr(cls, "_transient", False),
        })
    return results


def search_models(pool: dict, query: str) -> list[dict]:
    """Search models by name or description substring."""
    query_lower = query.lower()
    results = []
    for name, cls in sorted(pool.items()):
        desc = getattr(cls, "_description", "") or ""
        if query_lower in name.lower() or query_lower in desc.lower():
            results.append({
                "name": name,
                "description": desc,
                "module": getattr(cls, "_original_module", None),
                "field_count": len(set(
                    f.name
                    for k in cls.__mro__
                    for f in getattr(k, "_field_definitions", [])
                )),
                "abstract": getattr(cls, "_abstract", False),
                "transient": getattr(cls, "_transient", False),
            })
    return results
