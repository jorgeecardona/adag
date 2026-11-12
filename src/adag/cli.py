"""``adag``: run a DAG pipeline file from the console.

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
import importlib
import importlib.util
import json
import re
import sys
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any, cast, get_args

from adag.cache_store import store_from_url
from adag.deferred import InputParam, PipelineObserver
from adag.deprecation import AdagDeprecationWarning
from adag.dispatch import EXIT_BAD_BRANCH, BranchReferenceError
from adag.fan_out import FanOutStep
from adag.observers import RunTreeObserver
from adag.pipeline import Pipeline
from adag.step import StepResult

_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


def load_pipeline_module(pipeline: Path | str) -> ModuleType:
    """Import a pipeline by file path or by module name, and return the module.

    A path to an existing file is loaded from that file. Its directory is added to
    ``sys.path`` so it can import sibling modules, and its module name is derived from the
    absolute path so two files with the same stem do not collide in ``sys.modules``.

    Anything else is imported as a module name (``pkg.pipeline``). Use a module name for
    remote workers: a file path on the main process's machine does not exist in a container.

    Raises:
        FileNotFoundError: ``pipeline`` is neither a file nor an importable module.
    """
    resolved = Path(pipeline).resolve()
    if not resolved.is_file():
        name = str(pipeline)
        if _MODULE_NAME.fullmatch(name):
            try:
                return importlib.import_module(name)
            except ModuleNotFoundError as error:
                if error.name is None or not name.startswith(error.name):
                    raise  # the module exists but one of its own imports is missing
        raise FileNotFoundError(f"pipeline file or module not found: {pipeline}")

    module_name = "adag_pipeline_" + re.sub(r"\W", "_", str(resolved))
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load pipeline file: {pipeline}")

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
    are targets: bare ``InputParam``s and plain values are ignored.
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
    if param.value_type is not None:
        return param.value_type
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
    pipeline: Path | str,
    fan_out_var: str,
    key: str,
    params: dict[str, Any] | list[str],
    cache_dir: Path | None,
    expect_hash: str | None = None,
    store_url: str | None = None,
) -> Any:
    """Worker entry: resolve ONE fan-out branch into the shared cache.

    Re-imports the pipeline, finds the ``FanOutStep`` bound to ``fan_out_var``, looks up its
    own ``over[key]`` (so the value never had to cross the wire), re-derives the branch via
    ``factory(key, value)``, and resolves it under a plain in-process context.
    ``StepResult.resolve`` writes the result to ``cache_dir`` (or the store at ``store_url``)
    under the branch content hash, which the main process reads back.

    Raises:
        BranchReferenceError: the fan-out or key is not in the pipeline, or the re-derived
            branch hash is not ``expect_hash`` (the worker runs different code or params than
            the main process). A retry cannot fix either.
        ValueError: a param is not an input of the branch.

    ``params`` as a list of ``KEY=VALUE`` strings is the 0.1 form. It still works, coerced to each
    ``InputParam`` type, and warns.
    """
    module = load_pipeline_module(pipeline)
    targets = discover_targets(module)
    if fan_out_var not in targets:
        raise BranchReferenceError(
            f"fan-out target {fan_out_var!r} not found. Available: {sorted(targets)}"
        )

    fan_out = _fan_out_step(targets[fan_out_var], fan_out_var)
    matches = [
        (original, value) for original, value in fan_out.over.items() if str(original) == key
    ]
    if not matches:
        keys = sorted(str(branch_key) for branch_key in fan_out.over)
        raise BranchReferenceError(
            f"branch key {key!r} not in fan-out {fan_out_var!r}. Keys: {keys}"
        )
    original_key, value = matches[0]

    sub: StepResult[Any] = fan_out.factory(original_key, value)
    store = store_from_url(store_url) if store_url is not None else None
    pipeline_obj: Pipeline[Any] = Pipeline(sub, cache_dir=cache_dir, store=store)
    if isinstance(params, list):
        params = _coerce_legacy_assignments(pipeline_obj, params)
    unknown = set(params) - pipeline_obj.input_names
    if unknown:
        raise ValueError(
            f"unknown param(s) {sorted(unknown)}. Expected: {sorted(pipeline_obj.input_names)}"
        )
    if expect_hash is not None:
        actual = sub.content_hash(params=params)
        if actual != expect_hash:
            raise BranchReferenceError(
                f"branch {key!r} hash mismatch: main process expects {expect_hash}, worker "
                f"derived {actual}. The worker runs different code or params."
            )
    return pipeline_obj.resolve(sub, **params)


def _coerce_legacy_assignments(pipeline: Pipeline[Any], items: list[str]) -> dict[str, Any]:
    """Turn 0.1-style ``KEY=VALUE`` strings into typed params. Deprecated: pass a dict."""
    warnings.warn(
        "KEY=VALUE params (run_branch list form, resolve-branch --param) are deprecated and are "
        "removed in adag 1.0. Pass a dict, or --params-json, instead.",
        AdagDeprecationWarning,
        stacklevel=3,
    )
    raw = _parse_assignments(items)
    type_by_name = {param.name: _param_type(param) for param in pipeline.inputs}
    return {name: _coerce(value, type_by_name.get(name)) for name, value in raw.items()}


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


def _parse_params_json(raw: str) -> dict[str, Any]:
    """Parse the worker's ``--params-json`` object; JSON keeps each value's type."""
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError(f"--params-json must be a JSON object, got {type(parsed).__name__}")
    return cast("dict[str, Any]", parsed)


def _branch_params(args: argparse.Namespace) -> dict[str, Any] | list[str]:
    """Params from ``--params-json``, or the deprecated ``--param KEY=VALUE`` list."""
    if not args.param:
        return _parse_params_json(args.params_json)
    if args.params_json != "{}":
        raise ValueError("pass either --param or --params-json, not both")
    return list(args.param)


def _resolve_branch_command(args: argparse.Namespace) -> int:
    if args.cache_dir is None and args.store_url is None:
        print("error: resolve-branch needs --cache-dir or --store-url", file=sys.stderr)
        return 1
    try:
        params = _branch_params(args)
        result = run_branch(
            args.pipeline,
            args.fan_out,
            args.key,
            params,
            Path(args.cache_dir) if args.cache_dir is not None else None,
            expect_hash=args.expect_hash,
            store_url=args.store_url,
        )
    except BranchReferenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_BAD_BRANCH
    except (FileNotFoundError, ImportError, KeyError, ValueError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"branch {args.key!r} resolved: {type(result).__name__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the ``adag`` argument parser (``run`` and ``resolve-branch``)."""
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
    branch_parser.add_argument(
        "pipeline", help="Path to the pipeline .py file, or its module name (pkg.pipeline)"
    )
    branch_parser.add_argument(
        "--fan-out",
        dest="fan_out",
        required=True,
        metavar="VAR",
        help="Module-level variable bound to the FanOutStep",
    )
    branch_parser.add_argument("--key", required=True, help="The fan-out branch key to resolve")
    branch_parser.add_argument(
        "--cache-dir", default=None, help="Shared cache directory (the rendezvous)"
    )
    branch_parser.add_argument(
        "--store-url",
        dest="store_url",
        default=None,
        metavar="URL",
        help="Shared cache store instead of a directory (sqlite:///, redis://, postgresql://)",
    )
    branch_parser.add_argument(
        "--params-json",
        dest="params_json",
        default="{}",
        metavar="JSON",
        help="Pipeline parameters as one JSON object (keeps value types)",
    )
    branch_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Deprecated: one pipeline parameter; use --params-json",
    )
    branch_parser.add_argument(
        "--expect-hash",
        dest="expect_hash",
        default=None,
        metavar="HASH",
        help="Exit 2 if the re-derived branch hash differs from HASH",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``adag`` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "resolve-branch":
        return _resolve_branch_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
