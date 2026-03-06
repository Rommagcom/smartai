import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, urlunparse

from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")
_SINGLE_BRACE_RE = re.compile(r"\{(today|today_iso|now)\}")


def _builtin_values() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "today": now.strftime("%d.%m.%Y"),
        "today_iso": now.strftime("%Y-%m-%d"),
        "now": now.isoformat(),
    }


def resolve_url_template(url: str, params: dict | None = None) -> str:
    """Resolve ``{key}`` placeholders in *url* from *params*.

    Param values may contain built-in templates:
    - ``{{today}}`` / ``{today}``         → DD.MM.YYYY
    - ``{{today_iso}}`` / ``{today_iso}`` → YYYY-MM-DD
    - ``{{now}}`` / ``{now}``             → ISO-8601 UTC datetime

    Both single-brace and double-brace syntax are supported.

    Params that do not match any URL placeholder are appended
    as query-string parameters.
    """
    builtins = _builtin_values()

    # ---- resolve {{template}} and {builtin} inside param values ----
    resolved: dict[str, str] = {}
    for k, v in (params or {}).items():
        val = str(v)
        val = _TEMPLATE_RE.sub(lambda m: builtins.get(m.group(1), m.group(0)), val)
        val = _SINGLE_BRACE_RE.sub(lambda m: builtins.get(m.group(1), m.group(0)), val)
        resolved[k] = val

    # ---- substitute {key} placeholders in URL ----
    used: set[str] = set()
    for k, v in resolved.items():
        ph = "{" + k + "}"
        if ph in url:
            url = url.replace(ph, v)
            used.add(k)

    # also resolve bare built-in placeholders like {today} in URL
    for bn, bv in builtins.items():
        url = url.replace("{" + bn + "}", bv)

    # ---- append unused params as query-string ----
    leftover = {k: v for k, v in resolved.items() if k not in used}
    if leftover:
        parsed = urlparse(url)
        sep = "&" if parsed.query else ""
        new_query = (parsed.query + sep if parsed.query else "") + urlencode(leftover)
        url = urlunparse(parsed._replace(query=new_query))

    return url


class ApiExecutor:
    async def call(self, method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
        safe_url = egress_policy_service.validate_url(url)
        client = http_client_service.get()
        async with asyncio.timeout(30):
            response = await client.request(method=method.upper(), url=safe_url, headers=headers, json=body, timeout=30)
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }


api_executor = ApiExecutor()
