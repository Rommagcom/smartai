from __future__ import annotations

import importlib.util
import json
import traceback
from pathlib import Path
from typing import Any


def _load_callable(module_path: Path, function_name: str):
    if not module_path.exists() or not module_path.is_file():
        raise FileNotFoundError(str(module_path))

    module_name = f"dynamic_skill_sandbox_{abs(hash(str(module_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fn = getattr(module, function_name, None)
    if not callable(fn):
        raise TypeError(f"Function {function_name} is not callable")
    return fn


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=True)
    except TypeError:
        return str(value)
    return value


def main() -> int:
    raw = ""
    try:
        import sys

        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload must be object")

        module_path = Path(str(payload.get("module_path") or "")).resolve()
        function_name = str(payload.get("function") or "").strip()
        kwargs = payload.get("kwargs")

        if not function_name:
            raise ValueError("function is required")
        if not isinstance(kwargs, dict):
            raise ValueError("kwargs must be object")

        fn = _load_callable(module_path, function_name)
        result = fn(**kwargs)
        response = {"ok": True, "result": _json_safe(result)}
        sys.stdout.write(json.dumps(response, ensure_ascii=True))
        return 0
    except Exception as exc:
        import sys

        response = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=5),
        }
        sys.stdout.write(json.dumps(response, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
