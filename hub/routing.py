"""Which project does an incoming hook belong to?

Two shapes, deliberately, and nothing in between — a rule DSL would grow into
half a workflow engine:

``{"routing": {"field": "project"}}``
    Read the project name straight out of the hook body (dotted paths are not
    needed; the body is a flat lookup here).  Zero code, covers most systems.

``{"routing": {"python": "myrules:route"}}``
    Call your own ``route(body) -> str`` — about 20 lines, any logic you want,
    evaluated inside your deployment.  Write it in your house style instead of
    learning ours.

An empty result means "no project"; the engine then uses the global defaults.
"""

from __future__ import annotations

import importlib
import importlib.util


def load_callable(target: str):
    """Import ``pkg.mod:func``, ``pkg.mod.func`` or ``/path/rules.py:func``."""
    module_name, _, attr = target.partition(":")
    if not attr:
        module_name, _, attr = target.rpartition(".")
    if not module_name or not attr:
        raise ValueError(
            f"expected 'module:function' or '/path/file.py:function', got {target!r}"
        )
    if module_name.endswith(".py"):
        # A deployment's own rules file, kept outside the package on purpose.
        spec = importlib.util.spec_from_file_location(
            "hub_rules_" + str(abs(hash(module_name))), module_name
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load rules file {module_name!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(f"{module_name} has no attribute {attr!r}") from exc


def resolve(config, body: dict | None) -> str:
    """Return the project name for a hook body ('' when none applies)."""
    body = body if isinstance(body, dict) else {}
    routing = config.routing or {}
    target = routing.get("python")
    if target:
        value = load_callable(str(target))(body)
    else:
        value = body.get(str(routing.get("field") or "project"))
    return str(value).strip() if value not in (None, "") else ""