"""
services/who_proxy.py — ASGI middleware proxying ECT requests to WHO API.

In production mode, the ECT JS widget runs with `apiSecured=false` and routes
ALL its API calls through this server-side proxy (`/who-api-proxy/...`). The
proxy adds the OAuth2 Bearer token transparently so the WHO secret never
appears in client-side code.

Robustness :
  - aiohttp ClientSession with force_close=True (prevents stale connections
    on shinyapps.io after container suspension)
  - One auto-retry on HTTP 401 (refreshes the token cache)
  - One auto-retry on network errors (recreates the aiohttp session)
  - Full CORS headers on every response
  - Host whitelist (defense-in-depth against URL injection)

Adapté d'icd11pycode/app.py (lignes 650-807).
"""
from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response

from modules.mod_ect_browser import _get_who_token, force_token_refresh


PROXY_PREFIX = "/who-api-proxy/"
ALLOWED_HOSTS = ("id.who.int", "icdcdn.who.int")

_CORS_HEADERS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": (
        "Accept, Accept-Language, API-Version, Authorization, Content-Type"
    ),
    "access-control-max-age": "86400",
}

_aiohttp_session = None  # type: ignore


def _get_aiohttp_session(force_new: bool = False):
    """Lazy-create or recreate the aiohttp session.
    force_close=True is critical on shinyapps.io to prevent stale TCP
    connections after the app is suspended/resumed."""
    global _aiohttp_session
    import aiohttp
    if force_new and _aiohttp_session is not None:
        try:
            asyncio.create_task(_aiohttp_session.close())
        except Exception:
            pass
        _aiohttp_session = None
    if _aiohttp_session is None:
        connector = aiohttp.TCPConnector(
            limit=100, ttl_dns_cache=300, force_close=True
        )
        _aiohttp_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _aiohttp_session


async def _who_proxy(request: Request, language_default: str = "fr") -> Response:
    """Async proxy for ECT requests to WHO API."""
    import aiohttp

    # CORS preflight — answer immediately
    if request.method == "OPTIONS":
        return Response(content=b"", status_code=204, headers=_CORS_HEADERS)

    # Extract the ICD API sub-path after PROXY_PREFIX (handles full-prefix
    # deployments on shinyapps.io where the path is /appname/who-api-proxy/...)
    raw_path = request.url.path
    idx = raw_path.find(PROXY_PREFIX)
    if idx >= 0:
        path = raw_path[idx + len(PROXY_PREFIX):]
    else:
        path = request.path_params.get("path", "")

    # Security : block absolute URL injection
    if path.startswith(("http://", "https://", "//")):
        return Response(
            content=b"Forbidden: invalid proxy path",
            status_code=403, headers=_CORS_HEADERS,
        )

    qs = str(request.query_params)
    target_url = f"https://id.who.int/{path}"
    if qs:
        target_url += f"?{qs}"

    # Defense-in-depth : verify final hostname
    parsed = urlparse(target_url)
    if parsed.hostname not in ALLOWED_HOSTS:
        return Response(
            content=b"Forbidden: unauthorized target host",
            status_code=403, headers=_CORS_HEADERS,
        )

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        if attempt == 1:
            force_token_refresh()
        token = await asyncio.to_thread(_get_who_token)

        req_headers = {
            "accept": request.headers.get("accept", "application/json"),
            "accept-language": request.headers.get("accept-language", language_default),
            "api-version": request.headers.get("api-version", "v2"),
        }
        if (ct := request.headers.get("content-type")):
            req_headers["content-type"] = ct
        if token:
            req_headers["authorization"] = f"Bearer {token}"

        try:
            session = _get_aiohttp_session(force_new=(attempt == 1))
            async with session.request(
                method=request.method,
                url=target_url,
                headers=req_headers,
                data=body,
            ) as resp:
                content = await resp.read()
                if resp.status == 401 and attempt == 0:
                    print(f"[who-proxy] 401 on {path} — refreshing token and retrying")
                    continue
                resp_headers = dict(_CORS_HEADERS)
                resp_headers["content-type"] = resp.headers.get(
                    "content-type", "application/json"
                )
                print(f"[who-proxy] {request.method} {path[:60]} → {resp.status}")
                return Response(
                    content=content,
                    status_code=resp.status,
                    headers=resp_headers,
                )
        except (aiohttp.ClientError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ServerConnectionError) as exc:
            last_exc = exc
            print(f"[who-proxy] Network error (attempt {attempt+1}): {exc}")
            if attempt == 0:
                _get_aiohttp_session(force_new=True)
                continue

    err_headers = dict(_CORS_HEADERS)
    err_headers["content-type"] = "text/plain"
    return Response(
        content=f"Proxy error: {last_exc}".encode(),
        status_code=502,
        headers=err_headers,
    )


def wrap_with_proxy(starlette_app, language_default: str = "fr"):
    """Wrap a Shiny Starlette app with the WHO proxy at the ASGI level.

    Usage in app.py :
        from services.who_proxy import wrap_with_proxy
        app.starlette_app = wrap_with_proxy(app.starlette_app, language_default="fr")
    """
    async def _proxy_asgi(scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if PROXY_PREFIX in path:
                request = Request(scope, receive)
                response = await _who_proxy(request, language_default=language_default)
                await response(scope, receive, send)
                return
        await starlette_app(scope, receive, send)

    return _proxy_asgi
