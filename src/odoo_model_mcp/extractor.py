"""Extract structured metadata from the Odoo model registry."""

import ast
import inspect
import logging

logger = logging.getLogger(__name__)


_CLASS_LOCATION_CACHE: dict[int, dict[str, object] | None] = {}

# file_path -> { (class_qualname, class_line_start): [field_dict, ...] }
# inspect.getsourcelines can't locate class-body assignments, so we
# AST-parse each source file once per registry load and cache the
# resulting index.
_FILE_FIELDS_CACHE: dict[str, dict[tuple[str, int], list[dict]]] = {}


def _class_location(klass: type) -> dict[str, object] | None:
    """Return {file, line_start, line_end} for a class, or None.

    Cached by id(klass): the same class object appears in many MROs
    (common mixins like BaseModel, MailThread), and inspect's tokenizer
    is the main hot path of a bulk registry dump.
    """
    key = id(klass)
    if key in _CLASS_LOCATION_CACHE:
        return _CLASS_LOCATION_CACHE[key]
    result: dict[str, object] | None
    try:
        source_file = inspect.getfile(klass)
        lines, line_start = inspect.getsourcelines(klass)
        result = {
            "file": source_file,
            "line_start": line_start,
            "line_end": line_start + len(lines) - 1,
        }
    except (TypeError, OSError):
        result = None
    _CLASS_LOCATION_CACHE[key] = result
    return result


def _is_fields_call(node: ast.AST) -> tuple[bool, str | None]:
    """Return (is_fields_call, field_type).

    Matches both ``fields.Many2one(...)`` and ``fields.Many2one(...)``
    with dotted callee forms, accepting any attribute name off of a
    name or attribute ending in ``fields``.
    """
    if not isinstance(node, ast.Call):
        return False, None
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "fields":
            return True, func.attr
        if isinstance(owner, ast.Attribute) and owner.attr == "fields":
            return True, func.attr
    return False, None


_RELATIONAL_FIELD_TYPES = {"Many2one", "One2many", "Many2many", "Reference"}


def _extract_field_kwargs(call: ast.Call, field_type: str | None) -> dict[str, object]:
    """Pull a minimal set of literal kwargs off a fields.*(...) call.

    We don't evaluate expressions; anything non-literal is skipped so
    AST parsing never runs user code.
    """
    out: dict[str, object] = {}
    for kw in call.keywords:
        if kw.arg is None:
            continue
        if kw.arg not in {"compute", "related", "comodel_name", "inverse_name", "string", "store"}:
            continue
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError):
            continue
    # For relational fields the first positional arg is comodel_name;
    # for other field types it's typically `string` (label). Only
    # claim it as comodel_name when the field type supports it.
    if (
        call.args
        and field_type in _RELATIONAL_FIELD_TYPES
        and "comodel_name" not in out
    ):
        try:
            first = ast.literal_eval(call.args[0])
            if isinstance(first, str):
                out["comodel_name"] = first
        except (ValueError, SyntaxError):
            pass
    return out


def _index_file_fields(file_path: str) -> dict[tuple[str, int], list[dict]]:
    """Index ``file_path`` and return {(class_qualname, class_line_start): [fields]}."""
    try:
        with open(file_path, encoding="utf-8") as fp:
            source = fp.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return {}

    result: dict[tuple[str, int], list[dict]] = {}

    def _walk(node: ast.AST, prefix: str = "") -> None:
        if isinstance(node, ast.ClassDef):
            qualname = f"{prefix}{node.name}" if prefix else node.name
            key = (qualname, node.lineno)
            fields: list[dict] = []
            for stmt in node.body:
                # fields show up as `name = fields.X(...)` — one or
                # more targets on a simple Assign / AnnAssign node.
                if isinstance(stmt, ast.Assign):
                    call = stmt.value
                    targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    call = stmt.value
                    targets = [stmt.target]
                else:
                    continue
                is_field, field_type = _is_fields_call(call) if call else (False, None)
                if not is_field or call is None:
                    continue
                assert isinstance(call, ast.Call)
                kwargs = _extract_field_kwargs(call, field_type)
                for target in targets:
                    fields.append({
                        "name": target.id,
                        "line_start": stmt.lineno,
                        "line_end": getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno,
                        "field_type": field_type,
                        **kwargs,
                    })
            result[key] = fields
            # Recurse for nested classes (rare but possible).
            for child in node.body:
                _walk(child, f"{qualname}.")
        else:
            for child in ast.iter_child_nodes(node):
                _walk(child, prefix)

    _walk(tree)
    return result


def _fields_in_class(file_path: str, class_qualname: str, class_line_start: int) -> list[dict]:
    file_index = _FILE_FIELDS_CACHE.get(file_path)
    if file_index is None:
        file_index = _index_file_fields(file_path)
        _FILE_FIELDS_CACHE[file_path] = file_index
    return file_index.get((class_qualname, class_line_start), [])


def _method_location(method: object) -> dict[str, object] | None:
    key = id(method)
    if key in _CLASS_LOCATION_CACHE:
        return _CLASS_LOCATION_CACHE[key]
    result: dict[str, object] | None
    try:
        source_file = inspect.getfile(method)
        lines, line_start = inspect.getsourcelines(method)
        result = {
            "file": source_file,
            "line_start": line_start,
            "line_end": line_start + len(lines) - 1,
        }
    except (TypeError, OSError):
        result = None
    _CLASS_LOCATION_CACHE[key] = result
    return result


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

        loc = _method_location(method)
        if loc is not None:
            entry["file"] = loc["file"]
            entry["line"] = loc["line_start"]
            entry["line_end"] = loc["line_end"]

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
        cached = _class_location(klass)
        if cached is not None:
            loc.update(cached)
            # AST-index field assignments in this class. Requires both
            # a resolved file and a known class-start line.
            file_path = cached.get("file")
            line_start = cached.get("line_start")
            if isinstance(file_path, str) and isinstance(line_start, int):
                loc["fields"] = _fields_in_class(
                    file_path, klass.__qualname__, line_start
                )
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
