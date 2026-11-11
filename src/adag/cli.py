"""``adag`` — run a DAG pipeline file from the console.

The pipeline file is a plain Python module that wires steps into one or more
module-level ``StepResult`` targets (the same files you already write). The
command imports the file, picks the target(s) you name, and resolves them,
coercing ``--param KEY=VALUE`` arguments to each target's ``InputParam`` type.

    adag run pipeline.py --target total --param base=5 --param bump=2
    adag run pipeline.py -h               # list targets in the file
    adag run pipeline.py --target total -h  # list that target's params

Also runnable as ``python -m adag``.
"""

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

from adag.deferred import InputParam, PipelineObserver
from adag.fan_out import FanOutStep
from adag.observers import RunTreeObserver
from adag.pipeline import Pipeline
from adag.step import StepResult


def load_pipeline_module(pipeline_path: Path) -> ModuleType:
    """Import a pipeline file by path and return the executed module.

    The file's own directory is added to ``sys.path`` so it can import sibling
    modules. The module name is derived from the absolute path so two files
    with the same stem don't collide in ``sys.modules``.
    """
    resolved = pipeline_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"pipeline file not found: {pipeline_path}")

    module_name = "adag_pipeline_" + re.sub(r"\W", "_", str(resolved))
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load pipeline file: {pipeline_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    parent = str(resolved.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(module)
    return module


def discover_targets(module: ModuleType) -> dict[str, StepResult[Any]]:
    """Map each public module-level ``StepResult`` to its variable name.

    Variables starting with ``_`` are skipped, and only ``StepResult`` values
    are targets — bare ``InputParam``s and plain values are ignored.
    """
    targets: dict[str, StepResult[Any]] = {}
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(value, StepResult):
            targets[name] = value
    return targets


def _param_type(param: InputParam[Any]) -> type[Any] | None:
    """Best-effort concrete type for an InputParam, for coercion and help."""
    declared = getattr(param, "deferred_type", None)
    if isinstance(declared, type):
        return declared
    origin = getattr(param, "__orig_class__", None)
    if origin is not None:
        args = get_args(origin)
        if args and isinstance(args[0], type):
            return args[0]
    if param.has_default and param.default is not None:
        default_value: object = param.default
        return type(default_value)
    return None


def _to_bool(raw_value: str) -> bool:
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _infer_scalar(raw_value: str) -> Any:
    """Best-effort scalar coercion when the InputParam type can't be recovered.

    ``InputParam[int]("x")`` is a frozen dataclass, and CPython does not set
    ``__orig_class__`` on frozen instances, so a declared-but-defaultless type
    is unrecoverable at runtime. Fall back to inferring int -> float -> bool
    from the literal, leaving anything else as a string.
    """
    try:
        return int(raw_value)
    except ValueError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        pass
    if raw_value.strip().lower() in {"true", "false"}:
        return _to_bool(raw_value)
    return raw_value


def _coerce(raw_value: str, target_type: type[Any] | None) -> Any:
    """Coerce a CLI string to the InputParam's type, inferring when unknown."""
    if target_type is bool:
        return _to_bool(raw_value)
    if target_type is int:
        return int(raw_value)
    if target_type is float:
        return float(raw_value)
    if target_type is str:
        return raw_value
    return _infer_scalar(raw_value)


def _parse_assignments(items: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` strings into a dict, preserving the raw value."""
    assignments: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--param expects KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        assignments[key.strip()] = value
    return assignments


def run_pipeline(
    pipeline_path: Path,
    target_names: list[str],
    param_assignments: list[str],
    cache_dir: Path | None,
    observer: PipelineObserver | None = None,
) -> dict[str, Any]:
    """Load the file, resolve each named target, and return name -> result.

    Pass an ``observer`` (e.g. a ``RunTreeObserver``) to watch the run; it's used
    for every target so a single observer captures the whole invocation.

    Raises ``KeyError`` for an unknown target name and ``ValueError`` for an
    unknown or missing parameter.
    """
    module = load_pipeline_module(pipeline_path)
    available = discover_targets(module)

    chosen: list[StepResult[Any]] = []
    for name in target_names:
        if name not in available:
            raise KeyError(f"target {name!r} not found. Available: {sorted(available)}")
        chosen.append(available[name])

    pipeline = Pipeline.from_targets(chosen, cache_dir=cache_dir)
    raw = _parse_assignments(param_assignments)

    unknown = set(raw) - pipeline.input_names
    if unknown:
        raise ValueError(
            f"unknown param(s) {sorted(unknown)}. Expected: {sorted(pipeline.input_names)}"
        )

    type_by_name = {param.name: _param_type(param) for param in pipeline.inputs}
    params = {key: _coerce(value, type_by_name[key]) for key, value in raw.items()}

    results: dict[str, Any] = {}
    for name in target_names:
        results[name] = pipeline.resolve(available[name], observer=observer, **params)
    return results


def _fan_out_step(target: StepResult[Any], var: str) -> Any:
    """Return a target's step, asserting it's a ``FanOutStep``.

    Returns ``Any``: ``isinstance``-narrowing a generic ``FanOutStep`` leaks
    ``FanOutStep[Unknown, Unknown]`` (K/V are unrecoverable from a bare
    ``StepResult[Any]``), and strict pyright rejects that everywhere it flows.
    The worker glue is legitimately dynamic, so ``Any`` is the honest type here.
    """
    step: Any = target.step
    if not isinstance(target.step, FanOutStep):
        raise TypeError(f"target {var!r} is not a FanOutStep (got {type(target.step).__name__}).")
    return step


def run_branch(
    pipeline_path: Path, fan_out_var: str, key: str, param_assignments: list[str], cache_dir: Path
) -> Any:
    """Worker entry: resolve ONE fan-out branch into the shared cache.

    Re-imports the pipeline file, finds the ``FanOutStep`` bound to ``fan_out_var``,
    looks up its own ``over[key]`` (so the value never had to cross the wire),
    re-derives the branch via ``factory(key, value)``, and resolves it under a
    plain in-process context. ``StepResult.resolve`` writes the result to
    ``cache_dir`` keyed by the branch content hash — the rendezvous the
    orchestrator polls. Raises ``KeyError``/``TypeError``/``ValueError`` on bad input.
    """
    module = load_pipeline_module(pipeline_path)
    targets = discover_targets(module)
    if fan_out_var not in targets:
        raise KeyError(f"fan-out target {fan_out_var!r} not found. Available: {sorted(targets)}")

    fan_out = _fan_out_step(targets[fan_out_var], fan_out_var)
    matches = [
        (original, value) for original, value in fan_out.over.items() if str(original) == key
    ]
    if not matches:
        raise KeyError(
            f"branch key {key!r} not in fan-out {fan_out_var!r}. Keys: {sorted(str(branch_key) for branch_key in fan_out.over)}"
        )
    original_key, value = matches[0]

    sub: StepResult[Any] = fan_out.factory(original_key, value)
    pipeline: Pipeline[Any] = Pipeline(sub, cache_dir=cache_dir)
    raw = _parse_assignments(param_assignments)
    unknown = set(raw) - pipeline.input_names
    if unknown:
        raise ValueError(
            f"unknown param(s) {sorted(unknown)}. Expected: {sorted(pipeline.input_names)}"
        )
    type_by_name = {param.name: _param_type(param) for param in pipeline.inputs}
    params = {
        key_name: _coerce(value_raw, type_by_name[key_name]) for key_name, value_raw in raw.items()
    }
    return pipeline.resolve(sub, **params)


def _format_targets(targets: dict[str, StepResult[Any]]) -> str:
    if not targets:
        return "No targets found (the file defines no module-level StepResult)."
    lines = ["Available targets:"]
    lines.extend(f"  {name}  ({targets[name].step.__class__.__name__})" for name in sorted(targets))
    return "\n".join(lines)


def _type_name(target_type: type[Any] | None) -> str:
    return target_type.__name__ if target_type is not None else "any"


def _format_params(pipeline: Pipeline[Any]) -> str:
    required = [param for param in pipeline.inputs if not param.has_default]
    optional = [param for param in pipeline.inputs if param.has_default]
    lines: list[str] = []

    if required:
        lines.append("Required params:")
        lines.extend(
            f"  --param {param.name}=<{_type_name(_param_type(param))}>" for param in required
        )
    if optional:
        lines.append("Optional params:")
        lines.extend(
            f"  --param {param.name}=<{_type_name(_param_type(param))}>   [default: {param.default!r}]"
            for param in optional
        )
    if not lines:
        lines.append("This target needs no params.")
    return "\n".join(lines)


def _run_command(args: argparse.Namespace) -> int:
    pipeline_path = Path(args.pipeline)

    if args.help:
        try:
            module = load_pipeline_module(pipeline_path)
        except (FileNotFoundError, ImportError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        targets = discover_targets(module)
        if not args.target:
            print(_format_targets(targets))
            return 0
        missing = [name for name in args.target if name not in targets]
        if missing:
            print(f"error: target(s) not found: {missing}", file=sys.stderr)
            print(_format_targets(targets))
            return 1
        pipeline = Pipeline.from_targets([targets[name] for name in args.target], cache_dir=None)
        print(_format_params(pipeline))
        return 0

    if not args.target:
        print("error: at least one --target is required (use -h to list targets)", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    observer = RunTreeObserver() if args.tree else None
    try:
        results = run_pipeline(pipeline_path, args.target, args.param, cache_dir, observer=observer)
    except (FileNotFoundError, ImportError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for name, value in results.items():
        print(f"{name}: {value!r}")
    if observer is not None:
        print()
        print(observer.render_tree())
    return 0


def _resolve_branch_command(args: argparse.Namespace) -> int:
    try:
        result = run_branch(
            Path(args.pipeline), args.fan_out, args.key, args.param, Path(args.cache_dir)
        )
    except (FileNotFoundError, ImportError, KeyError, ValueError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"branch {args.key!r} resolved: {type(result).__name__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``adag`` console script."""
    parser = argparse.ArgumentParser(
        prog="adag", description="Run a DAG pipeline file from the console."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", add_help=False, help="Resolve target(s) from a pipeline file"
    )
    run_parser.add_argument("pipeline", help="Path to the pipeline .py file")
    run_parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="NAME",
        help="Target variable to resolve (repeatable)",
    )
    run_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pipeline parameter (repeatable)",
    )
    run_parser.add_argument("--cache-dir", default=None, help="Pipeline cache directory")
    run_parser.add_argument(
        "--tree",
        action="store_true",
        help="Print the execution tree (per-step timing + cache hits) after running",
    )
    run_parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        dest="help",
        help="List targets, or a target's params if --target is set",
    )

    branch_parser = subparsers.add_parser(
        "resolve-branch", help="Worker entry: resolve one fan-out branch into the shared cache"
    )
    branch_parser.add_argument("pipeline", help="Path to the pipeline .py file")
    branch_parser.add_argument(
        "--fan-out",
        dest="fan_out",
        required=True,
        metavar="VAR",
        help="Module-level variable bound to the FanOutStep",
    )
    branch_parser.add_argument("--key", required=True, help="The fan-out branch key to resolve")
    branch_parser.add_argument(
        "--cache-dir", required=True, help="Shared cache directory (the rendezvous)"
    )
    branch_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pipeline parameter (repeatable)",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "resolve-branch":
        return _resolve_branch_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
