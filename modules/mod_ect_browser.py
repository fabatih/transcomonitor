"""
modules/mod_ect_browser.py — WHO ECT (Embedded Coding Tool) & EB (Embedded Browser)
integration for transcomonitor.

Provides :
  - ECT search input (iNo=1) with autocomplete + ctw-* attributes
  - EB embedded browser modal (iNo=2) with release selector
  - Hidden textInput for reliable ECT → Python data transfer
  - WHO OAuth2 token cache (thread-safe with anticipated refresh)
  - Head tags loading the ECT JS/CSS + bridge config

Adapté d'icd11pycode/modules/mod_ect_browser.py. La logique de proxy ASGI
elle-même est isolée dans services/who_proxy.py pour clarté.

Plan §8 : extension future « foundation mode » dans le browser EB sera
ajoutée dans le module mod-mapping-edit-foundation (toggle entre
linéarisation MMS et entité fondation directe).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import requests as sync_requests
from shiny import ui


# ─────────────────────────────────────────────────────────────────────────
# OAuth2 token cache (thread-safe, anticipated refresh)
# ─────────────────────────────────────────────────────────────────────────

_token_cache: dict = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()

WHO_TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"


def _get_who_token() -> str:
    """Return a valid WHO OAuth2 token (cached, refreshed 2 minutes before
    expiry). Returns empty string if credentials are missing or all retries
    fail."""
    now = time.time()
    with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

    client_id = os.environ.get("WHO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("WHO_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return ""

    for attempt in range(3):
        try:
            resp = sync_requests.post(
                WHO_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "scope": "icdapi_access",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            with _token_lock:
                _token_cache["token"] = data["access_token"]
                # Refresh 2 minutes before expiry
                _token_cache["expires_at"] = now + data.get("expires_in", 3600) - 120
                return _token_cache["token"]
        except Exception as e:
            if attempt == 2:
                print(f"[ERROR] WHO token fetch failed after 3 attempts: {e}")
            else:
                time.sleep(2 * (attempt + 1))
    with _token_lock:
        return _token_cache.get("token", "")


def force_token_refresh() -> None:
    """Invalidate the cached token so the next call refetches."""
    with _token_lock:
        _token_cache["expires_at"] = 0.0


# ─────────────────────────────────────────────────────────────────────────
# UI components
# ─────────────────────────────────────────────────────────────────────────

def ect_head_tags(config: Optional[dict] = None) -> ui.TagList:
    """Return HTML head tags for ECT CSS/JS + bridge config.

    In prod mode : ECT routes through /who-api-proxy/* (server-side OAuth2).
    In dev mode  : ECT connects directly to the WHO test API (no auth)."""
    cfg = config or {}
    mode = cfg.get("mode", "prod")
    language = cfg.get("language", "fr")

    if mode == "prod":
        api_url = ""        # computed dynamically in JS via _computeProxyUrl()
        api_secured = "false"
        use_proxy = "true"
    else:
        api_url = cfg.get(
            "dev_url",
            "https://icd11restapi-developer-test.azurewebsites.net",
        )
        api_secured = "false"
        use_proxy = "false"

    return ui.TagList(
        ui.tags.link(
            rel="stylesheet",
            href="https://icdcdn.who.int/embeddedct/icd11ect-1.7.1.css",
        ),
        ui.tags.script(ui.HTML(f"""
            window.ECTBridgeConfig = {{
                apiServerUrl: "{api_url}",
                apiSecured: {api_secured},
                useProxy: {use_proxy},
                language: "{language}",
                iNo: "1",
                ectJsonInputId: "ect_json"
            }};
        """)),
        ui.tags.script(src="https://icdcdn.who.int/embeddedct/icd11ect-1.7.1.js"),
        ui.tags.script(src=f"ect_bridge.js?v={int(time.time())}"),
    )


def ect_search_ui(config: Optional[dict] = None,
                  input_id: str = "ect_search_input",
                  label: str = "Rechercher un code CIM-11 :") -> ui.Tag:
    """ECT search input widget (iNo=1) — inline autocomplete."""
    cfg = config or {}
    mode = cfg.get("mode", "prod")
    language = cfg.get("language", "fr")

    if mode == "prod":
        api_url = ""
        api_secured = "false"
    else:
        api_url = cfg.get(
            "dev_url",
            "https://icd11restapi-developer-test.azurewebsites.net",
        )
        api_secured = "false"

    return ui.div(
        ui.tags.label(
            label, **{"for": input_id},
            class_="form-label fw-bold small",
        ),
        ui.tags.input(
            id=input_id,
            class_="ctw-input form-control",
            type="text",
            placeholder="Tapez un diagnostic, un code ou un terme…",
            autocomplete="one-time-code",
            **{
                "data-ctw-ino": "1",
                "data-ctw-api-server-url": api_url,
                "data-ctw-api-secured": api_secured,
                "data-ctw-language": language,
            },
        ),
        ui.tags.div(class_="ctw-window", **{"data-ctw-ino": "1"}),
        class_="ect-container mb-3",
    )


def ect_hidden_input_ui() -> ui.Tag:
    """Hidden textInput for reliable ECT → Python data transfer.
    Place once at the top of the app body (outside any conditional UI)."""
    return ui.div(
        ui.input_text("ect_json", label=None, value=""),
        style="display: none !important; height: 0; overflow: hidden;",
    )


def eb_browser_modal_ui(
    modal_id: str = "eb_browser_modal",
    title: str = "Navigateur CIM-11",
    releases: Optional[list[str]] = None,
    default_release: str = "2026-01",
) -> ui.Tag:
    """EB embedded browser modal (iNo=2) — full-width modal with release selector.

    `releases` : list of release labels to expose in the dropdown
                 (default ['2026-01']). When the user picks a release, the JS
                 bridge updates the EB widget's `data-ctw-release` attribute.
    """
    releases = releases or [default_release]
    release_options = [
        ui.tags.option(r, value=r, selected="selected" if r == default_release else None)
        for r in releases
    ]

    return ui.div(
        ui.div(
            ui.div(
                ui.div(
                    ui.h5(title, class_="modal-title"),
                    # Release selector (calls JS bridge to update EB)
                    ui.div(
                        ui.tags.label("Release :", class_="form-label small me-2 mb-0"),
                        ui.tags.select(
                            *release_options,
                            id="eb_release_select",
                            class_="form-select form-select-sm",
                            style="width: auto; display: inline-block;",
                            onchange="if (window.ectSetRelease) window.ectSetRelease(this.value);",
                        ),
                        class_="d-flex align-items-center ms-auto me-3",
                    ),
                    ui.tags.button(
                        type="button", class_="btn-close",
                        **{"data-bs-dismiss": "modal", "aria-label": "Fermer"},
                    ),
                    class_="modal-header",
                ),
                ui.div(
                    ui.tags.div(
                        class_="ctw-eb-window",
                        style="width: 100%; min-height: 550px;",
                        **{
                            "data-ctw-ino": "2",
                            "data-ctw-release": default_release,
                        },
                    ),
                    class_="modal-body",
                    style="min-height: 600px; padding: 8px;",
                ),
                ui.div(
                    ui.tags.button(
                        "Fermer", type="button",
                        class_="btn btn-secondary",
                        **{"data-bs-dismiss": "modal"},
                    ),
                    class_="modal-footer",
                ),
                class_="modal-content",
            ),
            class_="modal-dialog modal-dialog-scrollable",
            style="max-width: 95vw; width: 95vw;",
        ),
        id=modal_id,
        class_="modal fade",
        tabindex="-1",
        **{"aria-hidden": "true"},
    )
