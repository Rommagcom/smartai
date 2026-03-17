from __future__ import annotations

import base64
import json
import os
import traceback
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse


def _emit(payload: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    raise SystemExit(code)


def _resolve_kwargs_invocation(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, dict[str, Any], bool]:
    payload_options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
    system = str(kwargs.get("system") or "")
    user = str(kwargs.get("user") or "")
    if not user and args:
        user = str(args[0] or "")
    return system, user, payload_options, False


def _resolve_args_invocation(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, dict[str, Any], bool]:
    payload_options = kwargs.get("options") if isinstance(kwargs.get("options"), dict) else {}
    if len(args) == 1:
        return "", str(args[0] or ""), payload_options, True
    if len(args) >= 2:
        if len(args) >= 3 and isinstance(args[2], dict):
            payload_options = args[2]
        return str(args[0] or ""), str(args[1] or ""), payload_options, False
    raise TypeError("llm.chat expects either (prompt) or (system, user, options)")


def _resolve_chat_invocation(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, dict[str, Any], bool]:
    if "system" in kwargs or "user" in kwargs:
        return _resolve_kwargs_invocation(args, kwargs)
    return _resolve_args_invocation(args, kwargs)


def _post_llm_callback(*, callback_url: str, callback_secret: str, payload: dict[str, Any]) -> str:
    parsed = urlparse(callback_url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("llm callback URL must use http or https")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        callback_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Runner-Secret": callback_secret,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as response:  # nosec B310
            raw = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        raise RuntimeError(f"llm callback request failed: {exc}") from exc

    parsed_response = json.loads(raw or "{}")
    return str(parsed_response.get("text") or "")


def _post_http_callback(*, callback_url: str, callback_secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    parsed = urlparse(callback_url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("http callback URL must use http or https")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        callback_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Runner-Secret": callback_secret,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as response:  # nosec B310
            raw = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        raise RuntimeError(f"http callback request failed: {exc}") from exc

    parsed_response = json.loads(raw or "{}")
    if not isinstance(parsed_response, dict):
        raise RuntimeError("http callback returned invalid payload")
    return parsed_response


def _decode_request_context_from_env() -> tuple[str, str, str]:
    encoded = str(os.getenv("SKILL_REQUEST_B64", "")).strip()
    if not encoded:
        return "", "", ""

    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        request_payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return "", "", ""

    request_context = request_payload.get("context") if isinstance(request_payload, dict) else {}
    if not isinstance(request_context, dict):
        return "", "", ""

    return (
        str(request_context.get("user_id") or ""),
        str(request_context.get("tool_name") or ""),
        str(request_context.get("execution_id") or ""),
    )


def _build_llm_proxy(context: dict[str, Any]):
    callback_url = str(os.getenv("SKILL_RUNNER_CALLBACK_URL", "")).strip()
    callback_secret = str(os.getenv("SKILL_RUNNER_SECRET", "")).strip()
    capabilities = context.get("capabilities") if isinstance(context, dict) else {}
    user_id = str(context.get("user_id") or "")
    tool_name = str(context.get("tool_name") or "")
    execution_id = str(context.get("execution_id") or "")

    def llm_chat(*args: Any, **kwargs: Any) -> dict | str:
        system, user, payload_options, simple_prompt_mode = _resolve_chat_invocation(args, kwargs)
        text = _post_llm_callback(
            callback_url=callback_url,
            callback_secret=callback_secret,
            payload={
                "system": system,
                "user": user,
                "options": payload_options,
                "capabilities": capabilities if isinstance(capabilities, dict) else {},
                "user_id": user_id,
                "tool_name": tool_name,
                "execution_id": execution_id,
            },
        )
        return text if simple_prompt_mode else {"text": text}

    return {"chat": llm_chat}


def _build_http_proxy() -> dict[str, Any]:
    callback_url = str(os.getenv("SKILL_RUNNER_HTTP_CALLBACK_URL", "")).strip()
    callback_secret = str(os.getenv("SKILL_RUNNER_SECRET", "")).strip()
    request_user_id, request_tool_name, request_execution_id = _decode_request_context_from_env()

    def http_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.get("headers") if isinstance(kwargs.get("headers"), dict) else {}
        params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else {}
        body = kwargs.get("json") if "json" in kwargs else kwargs.get("body")
        return _post_http_callback(
            callback_url=callback_url,
            callback_secret=callback_secret,
            payload={
                "method": str(method or "GET"),
                "url": str(url or ""),
                "headers": {str(key): str(value) for key, value in headers.items()},
                "params": params,
                "body": body,
                "user_id": request_user_id,
                "tool_name": request_tool_name,
                "execution_id": request_execution_id,
            },
        )

    def http_get(url: str, **kwargs: Any) -> dict[str, Any]:
        return http_request("GET", url, **kwargs)

    def http_post(url: str, **kwargs: Any) -> dict[str, Any]:
        return http_request("POST", url, **kwargs)

    def http_put(url: str, **kwargs: Any) -> dict[str, Any]:
        return http_request("PUT", url, **kwargs)

    def http_patch(url: str, **kwargs: Any) -> dict[str, Any]:
        return http_request("PATCH", url, **kwargs)

    def http_delete(url: str, **kwargs: Any) -> dict[str, Any]:
        return http_request("DELETE", url, **kwargs)

    return {
        "request": http_request,
        "get": http_get,
        "post": http_post,
        "put": http_put,
        "patch": http_patch,
        "delete": http_delete,
    }


def main() -> None:
    encoded = str(os.getenv("SKILL_REQUEST_B64", "")).strip()
    if not encoded:
        _emit({"success": False, "error": "missing SKILL_REQUEST_B64"}, 2)

    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        request = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        _emit({"success": False, "error": f"invalid request payload: {exc}"}, 2)

    skill_code = str(request.get("skill_code") or "")
    function_name = str(request.get("function_name") or "run")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    context = request.get("context") if isinstance(request.get("context"), dict) else {}
    llm_caps = context.get("capabilities", {}).get("llm") if isinstance(context.get("capabilities"), dict) else {}
    if isinstance(llm_caps, dict) and bool(llm_caps.get("enabled")):
        context["llm"] = _build_llm_proxy(context)
    context["http"] = _build_http_proxy()

    runtime_globals: dict[str, Any] = {}
    try:
        exec(compile(skill_code, "skill.py", "exec"), runtime_globals, runtime_globals)  # nosec B102
        fn = runtime_globals.get(function_name)
        if not callable(fn):
            _emit({"success": False, "error": f"function '{function_name}' is not callable"}, 1)

        result = fn(params, context)
        _emit({"success": True, "result": result}, 0)
    except Exception as exc:
        _emit(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=5),
            },
            1,
        )


if __name__ == "__main__":
    main()
