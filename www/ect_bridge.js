// =====================================================================
// ect_bridge.js — Bridge between WHO ECT/EB widget and Shiny for Python
//
// Strategy:
//   In production mode, ECT uses apiSecured=false and routes ALL API
//   calls through a server-side proxy (/who-api-proxy/...) that adds
//   the OAuth2 Bearer token.  This eliminates all client-side token
//   management timing issues that break ECT in Python Shiny.
//
//   In dev mode, ECT connects directly to the WHO test API (no auth).
//
//   The proxy URL is computed dynamically from window.location so it
//   works both locally (http://localhost:PORT/who-api-proxy) and on
//   shinyapps.io (https://account.shinyapps.io/appname/who-api-proxy).
//
//   ECT DOM elements appear AFTER login (server-side conditional UI),
//   so a MutationObserver detects and binds them when they appear.
// =====================================================================

var ECTBridgeConfig = window.ECTBridgeConfig || {
  apiServerUrl: "",
  apiSecured: false,
  useProxy: false,
  language: "fr",
  iNo: "1"
};

var _ectInitialized = false;
var _ectCdnFailed = false;
var _ectSearchStart = null;
var _ectBoundInstances = {};

// ---- Diagnostic logger -----------------------------------------------
// Logs to console AND forwards to Shiny server via setInputValue so that
// ECT events appear in shinyapps.io server logs for remote debugging.

function _ectLog(level, msg) {
  var ts = new Date().toISOString().slice(11, 19);
  var full = "[ECT Bridge " + ts + "] " + msg;
  if (level === "error") { console.error(full); }
  else if (level === "warn") { console.warn(full); }
  else { console.log(full); }
  // Forward to server (best-effort, non-blocking)
  if (window.Shiny && window.Shiny.setInputValue) {
    try {
      Shiny.setInputValue("ect_bridge_log",
        { level: level, msg: msg },
        { priority: "event" });
    } catch(e) {}
  }
}

// ---- Helpers --------------------------------------------------------

/**
 * Compute the absolute proxy URL based on the current page location.
 * Works both locally and on shinyapps.io (where the app sits under a
 * sub-path like /appname/).
 */
function _computeProxyUrl() {
  var basePath = window.location.pathname.replace(/\/+$/, "");
  return window.location.origin + basePath + "/who-api-proxy";
}

/**
 * Sync data-ctw-* attributes on an ECT DOM element with the current
 * ECTBridgeConfig values.  Must be called BEFORE ECT.Handler.bind()
 * so the widget reads the correct server URL.
 */
function _syncElementAttrs(el) {
  if (!el) return;
  if (ECTBridgeConfig.apiServerUrl) {
    el.setAttribute("data-ctw-api-server-url", ECTBridgeConfig.apiServerUrl);
  }
  var secured = (ECTBridgeConfig.apiSecured === true || ECTBridgeConfig.apiSecured === "true");
  el.setAttribute("data-ctw-api-secured", secured ? "true" : "false");
}

// ---- ECT Callbacks --------------------------------------------------

function ectSelectedEntity(selectedEntity) {
  console.log("[ECT Bridge] Entity selected:", JSON.stringify(selectedEntity));

  var searchDuration = _ectSearchStart
    ? Math.round((Date.now() - _ectSearchStart) / 1000)
    : null;
  _ectSearchStart = null;

  // Normalize iNo: ECT sometimes reports "InnerCodingTool N" internally
  var rawINo = String(selectedEntity.iNo || ECTBridgeConfig.iNo || "1");
  var normalizedINo = rawINo;
  var innerMatch = rawINo.match(/(\d+)\s*$/);
  if (innerMatch) normalizedINo = innerMatch[1];

  var data = {
    iNo:              normalizedINo,
    code:             selectedEntity.code || "",
    title:            selectedEntity.title || "",
    selectedText:     selectedEntity.selectedText || "",
    foundationUri:    selectedEntity.foundationUri || "",
    linearizationUri: selectedEntity.linearizationUri || "",
    searchQuery:      selectedEntity.searchQuery || "",
    searchDuration:   searchDuration,
    timestamp:        Date.now()
  };

  console.log("[ECT Bridge] Selection iNo=" + data.iNo + " (raw=" + rawINo + "): " + data.code + " — " + data.title);

  if (!data.code) {
    console.warn("[ECT Bridge] Empty code in selection, ignoring");
    return;
  }

  // PRIMARY: Update hidden textInput (reliable Shiny input binding)
  var jsonInputId = ECTBridgeConfig.ectJsonInputId;
  if (jsonInputId) {
    var el = document.getElementById(jsonInputId);
    if (el) {
      el.value = JSON.stringify(data);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  // FALLBACK: Shiny.setInputValue
  if (window.Shiny && window.Shiny.setInputValue) {
    Shiny.setInputValue("ect_selected", JSON.stringify(data), { priority: "event" });
  }

  // Auto-close EB modal after selection from browser (iNo=2)
  if (normalizedINo === "2") {
    var modalEl = document.getElementById("eb_browser_modal");
    if (modalEl) {
      var modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) {
        console.log("[ECT Bridge] Auto-closing EB modal after selection");
        modal.hide();
      }
    }
  }

  // Clear search bar after selection from search tool (iNo=1)
  if (normalizedINo === "1") {
    setTimeout(function() { clearECT("1"); }, 200);
  }
}

function ectSearchStarted() {
  _ectSearchStart = Date.now();
}

// Not used in proxy mode (apiSecured=false) — stub for safety.
function ectGetNewToken(callback) {
  callback("");
}

// ---- Initialization -------------------------------------------------

function initECT() {
  if (_ectInitialized) return true;
  if (typeof ECT === "undefined" || !ECT.Handler) {
    _ectLog("warn", "ECT library not loaded yet");
    return false;
  }

  var cfg = ECTBridgeConfig;

  // In proxy mode: compute the dynamic proxy URL
  if (cfg.useProxy) {
    cfg.apiServerUrl = _computeProxyUrl();
    cfg.apiSecured = false;
    _ectLog("info", "Proxy mode — URL: " + cfg.apiServerUrl);
  }

  var settings = {
    apiServerUrl:              cfg.apiServerUrl || "https://icd11restapi-developer-test.azurewebsites.net",
    apiSecured:                cfg.apiSecured === true || cfg.apiSecured === "true",
    autoBind:                  false,
    language:                  cfg.language || "fr",
    popupMode:                 false,
    wordsAvailable:            true,
    chaptersFilter:            "",
    searchByCodeOrURI:         true,
    flexisearchAvailable:      true,
    enableSelectButton:        "categories",
    browserSearchAvailable:    true,
    browserHierarchyAvailable: true
  };

  var callbacks = {
    selectedEntityFunction: ectSelectedEntity,
    searchStartedFunction:  ectSearchStarted
  };

  if (settings.apiSecured) {
    callbacks.getNewTokenFunction = ectGetNewToken;
  }

  try {
    ECT.Handler.configure(settings, callbacks);
    _ectInitialized = true;
    _ectLog("info", "ECT configured OK. lang=" + settings.language +
                    " url=" + settings.apiServerUrl);
  } catch (e) {
    _ectLog("error", "ECT configure error: " + (e.message || e));
    return false;
  }

  // Try binding (elements may not exist yet — MutationObserver handles late arrival)
  bindECT("1");
  bindECTBrowser("2");
  return true;
}

function bindECT(iNo) {
  iNo = iNo || "1";
  if (typeof ECT === "undefined" || !ECT.Handler) return;

  var attempts = 0;
  var maxAttempts = 50;
  var interval = setInterval(function() {
    var input = document.querySelector('.ctw-input[data-ctw-ino="' + iNo + '"]');
    if (input) {
      clearInterval(interval);
      _syncElementAttrs(input);
      try {
        ECT.Handler.bind(iNo);
        _ectBoundInstances[iNo] = input;
        _ectLog("info", "Search bar bound (iNo=" + iNo + ")");
      } catch (e) {
        _ectLog("error", "Bind error iNo=" + iNo + ": " + (e.message || e));
      }
    } else if (++attempts >= maxAttempts) {
      clearInterval(interval);
      _ectLog("warn", "Search element not found after " + maxAttempts + " attempts (iNo=" + iNo + ")");
    }
  }, 200);
}

function bindECTBrowser(iNo) {
  iNo = iNo || "2";
  if (typeof ECT === "undefined" || !ECT.Handler) return;

  var attempts = 0;
  var maxAttempts = 50;
  var interval = setInterval(function() {
    var el = document.querySelector('.ctw-eb-window[data-ctw-ino="' + iNo + '"]');
    if (el) {
      clearInterval(interval);
      _syncElementAttrs(el);
      try {
        ECT.Handler.bind(iNo);
        _ectBoundInstances[iNo] = el;
        console.log("[ECT Bridge] Bound browser iNo=" + iNo);
      } catch (e) {
        console.error("[ECT Bridge] Bind browser error iNo=" + iNo + ":", e);
      }
    } else if (++attempts >= maxAttempts) {
      clearInterval(interval);
    }
  }, 200);
}

function safeBrowserCode(iNo, code) {
  if (!code || !code.match(/^[A-Z0-9]/)) return;
  if (typeof ECT === "undefined" || !ECT.Handler || !ECT.Handler.setBrowserCode) return;
  try {
    ECT.Handler.setBrowserCode(iNo, code);
    console.log("[ECT Bridge] setBrowserCode iNo=" + iNo + " code=" + code);
  } catch (e) {
    console.warn("[ECT Bridge] setBrowserCode error:", e.message || e);
  }
}

function clearECT(iNo) {
  iNo = iNo || "1";
  if (typeof ECT !== "undefined" && ECT.Handler) {
    try { ECT.Handler.clear(iNo); } catch(e) {}
  }
}

// ---- Shiny Custom Message Handlers ----------------------------------

function registerShinyHandlers() {
  if (!window.Shiny) return;

  // Open EB modal and load code
  Shiny.addCustomMessageHandler("open_eb_modal", function(msg) {
    console.log("[ECT Bridge] open_eb_modal:", msg);
    var modalEl = document.getElementById("eb_browser_modal");
    if (!modalEl) {
      console.error("[ECT Bridge] Modal eb_browser_modal not found");
      return;
    }

    // Ensure EB is bound (in case element was replaced by Shiny re-render)
    var ebEl = modalEl.querySelector('.ctw-eb-window[data-ctw-ino="2"]');
    if (ebEl && typeof ECT !== "undefined" && ECT.Handler) {
      _syncElementAttrs(ebEl);
      try { ECT.Handler.bind("2"); } catch(e) { /* already bound */ }
    }

    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();

    if (msg && msg.code) {
      setTimeout(function() { safeBrowserCode("2", msg.code); }, 500);
    }

    // When modal closes, notify server
    var onHidden = function() {
      modalEl.removeEventListener("hidden.bs.modal", onHidden);
      if (window.Shiny && window.Shiny.setInputValue) {
        Shiny.setInputValue("coding-eb_closed", Date.now(), { priority: "event" });
      }
    };
    modalEl.removeEventListener("hidden.bs.modal", onHidden);
    modalEl.addEventListener("hidden.bs.modal", onHidden);
  });

  // Focus ECT search bar (iNo=1)
  Shiny.addCustomMessageHandler("focus_ect_search", function(msg) {
    var input = document.querySelector('.ctw-input[data-ctw-ino="1"]');
    if (input) {
      if (!_ectBoundInstances["1"] || _ectBoundInstances["1"] !== input) {
        _syncElementAttrs(input);
        try {
          ECT.Handler.bind("1");
          _ectBoundInstances["1"] = input;
        } catch(e) {}
      }
      input.focus();
      input.scrollIntoView({ behavior: "smooth", block: "center" });
      input.style.boxShadow = "0 0 8px 2px rgba(13,110,253,.5)";
      setTimeout(function() { input.style.boxShadow = ""; }, 2000);
    }
  });

  Shiny.addCustomMessageHandler("ect_clear", function(iNo) { clearECT(iNo); });
  Shiny.addCustomMessageHandler("ect_bind", function(iNo) { bindECT(iNo); });

  Shiny.addCustomMessageHandler("ect_configure", function(config) {
    if (config.apiServerUrl) ECTBridgeConfig.apiServerUrl = config.apiServerUrl;
    if (config.apiSecured !== undefined) ECTBridgeConfig.apiSecured = config.apiSecured;
    if (config.language) ECTBridgeConfig.language = config.language;
    _ectInitialized = false;
    initECT();
  });
}

// ---- Auto-init with MutationObserver --------------------------------

(function() {
  function waitForShiny() {
    if (window.Shiny && window.Shiny.setInputValue) {
      _ectLog("info", "Shiny connected — starting ECT init");
      registerShinyHandlers();

      // Retry until ECT library loads from CDN — extended window to 60s
      var retries = 0;
      var retryInit = setInterval(function() {
        if (typeof ECT !== "undefined" && ECT.Handler) {
          clearInterval(retryInit);
          _ectLog("info", "ECT CDN loaded (attempt " + (retries + 1) + ") — initializing");
          initECT();
        } else if (++retries >= 120) {   // 120 × 500ms = 60 seconds
          clearInterval(retryInit);
          _ectLog("error", "ECT CDN failed to load after 60s — check icdcdn.who.int");
          _ectCdnFailed = true;
        }
      }, 500);

      // MutationObserver: detect ECT elements appearing after login
      observeForECTElements();

      // Periodic healing: every 30s re-check if ECT is bound and functional
      startHealLoop();
    } else {
      setTimeout(waitForShiny, 300);
    }
  }

  function observeForECTElements() {
    var observer = new MutationObserver(function() {
      // If not initialized yet but library is loaded, try init
      if (!_ectInitialized) {
        if (typeof ECT !== "undefined" && ECT.Handler) {
          _ectLog("info", "DOM changed — ECT loaded but not init, calling initECT()");
          initECT();
        }
        return;
      }

      // Already initialized — detect new/replaced elements and (re)bind
      var currentSearch = document.querySelector('.ctw-input[data-ctw-ino="1"]');
      if (currentSearch && _ectBoundInstances["1"] !== currentSearch) {
        _ectLog("info", "New search element detected, binding iNo=1");
        _syncElementAttrs(currentSearch);
        try {
          ECT.Handler.bind("1");
          _ectBoundInstances["1"] = currentSearch;
        } catch(e) {
          _ectLog("error", "Rebind error iNo=1: " + (e.message || e));
        }
      }

      var currentBrowser = document.querySelector('.ctw-eb-window[data-ctw-ino="2"]');
      if (currentBrowser && _ectBoundInstances["2"] !== currentBrowser) {
        _ectLog("info", "New browser element detected, binding iNo=2");
        _syncElementAttrs(currentBrowser);
        try {
          ECT.Handler.bind("2");
          _ectBoundInstances["2"] = currentBrowser;
        } catch(e) {}
      }
    });

    observer.observe(document.body, { childList: true, subtree: true });
    _ectLog("info", "MutationObserver active (30min lifetime)");
    // Keep observer alive for the full session (5 minutes not enough for long sessions)
    // Use a weaker timeout: disconnect after 30 minutes of inactivity
    setTimeout(function() {
      observer.disconnect();
      _ectLog("warn", "MutationObserver disconnected after 30 minutes");
    }, 1800000);
  }

  /**
   * Periodic heal loop: every 30 seconds, ensure ECT is properly bound.
   * Catches cases where:
   *   - ECT CDN loaded late (after retryInit gave up)
   *   - ECT element was replaced by Shiny re-render but MutationObserver missed it
   *   - ECT library briefly unavailable and now recovered
   */
  function startHealLoop() {
    setInterval(function() {
      var searchEl = document.querySelector('.ctw-input[data-ctw-ino="1"]');
      if (!searchEl) return;  // not logged in or tab not active

      if (!_ectInitialized) {
        // ECT element is there but ECT not initialized — try again
        if (typeof ECT !== "undefined" && ECT.Handler) {
          _ectLog("info", "Heal: ECT element present but not initialized, retrying");
          initECT();
        }
        return;
      }

      // ECT initialized but might not be bound to this specific element
      if (_ectBoundInstances["1"] !== searchEl) {
        _ectLog("info", "Heal: rebinding search iNo=1");
        _syncElementAttrs(searchEl);
        try {
          ECT.Handler.bind("1");
          _ectBoundInstances["1"] = searchEl;
        } catch(e) {
          _ectLog("warn", "Heal rebind failed: " + (e.message || e));
        }
      }
    }, 30000);
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    waitForShiny();
  } else {
    document.addEventListener("DOMContentLoaded", waitForShiny);
  }
})();

// ---- Dynamic config update (language / release) ---------------------
// Called by Shiny when admin saves new parameters.
// Updates ECTBridgeConfig, then reconfigures and rebinds the ECT widget
// so searches use the new language immediately (no page reload needed).

function _updateECTConfig(cfg) {
  if (!cfg) return;
  if (cfg.language) {
    ECTBridgeConfig.language = cfg.language;
    // Update data-ctw-language on the search input element
    var searchInput = document.getElementById("ect_search_input");
    if (searchInput) {
      searchInput.setAttribute("data-ctw-language", cfg.language);
    }
    // Reconfigure ECT and rebind with new language
    if (typeof ECT !== "undefined" && ECT.Handler) {
      try { ECT.Handler.clear("1"); } catch(e) {}
      var settings = {
        apiServerUrl: ECTBridgeConfig.apiServerUrl || "",
        apiSecured:   ECTBridgeConfig.apiSecured === true || ECTBridgeConfig.apiSecured === "true",
        useProxy:     ECTBridgeConfig.useProxy === true || ECTBridgeConfig.useProxy === "true",
        iNo:          "1",
        language:     cfg.language,
        linearizationName: "mms",
        releaseId:    cfg.release || "2025-01"
      };
      try {
        ECT.Handler.configure(settings, {
          selectedEntityFunction: ectSelectedEntity,
          searchStartedFunction:  function() { _ectSearchStart = Date.now(); }
        });
        bindECT("1");
        console.log("[ECT Bridge] Reconfigured with language=" + cfg.language);
      } catch(e) {
        console.warn("[ECT Bridge] Reconfigure error:", e);
        // Fallback: just rebind
        try { bindECT("1"); } catch(e2) {}
      }
    }
  }
}

// Register Shiny message handler (fires when admin saves params)
if (typeof Shiny !== "undefined") {
  Shiny.addCustomMessageHandler("update_ect_config", function(data) {
    console.log("[ECT Bridge] update_ect_config received:", JSON.stringify(data));
    _updateECTConfig(data);
  });
} else {
  // Shiny not yet loaded — register after DOM ready
  document.addEventListener("DOMContentLoaded", function() {
    if (typeof Shiny !== "undefined") {
      Shiny.addCustomMessageHandler("update_ect_config", function(data) {
        console.log("[ECT Bridge] update_ect_config received:", JSON.stringify(data));
        _updateECTConfig(data);
      });
    }
  });
}

// =====================================================================
// EB (Embedded Browser) — prefill + reset hooks
// Per plan §16.9 + §16.12 :
//   - On modal open (show.bs.modal) : prefill the browser with the
//     `data-prefill-code` attribute of the modal (set by Python before
//     opening), so the user sees the current target code instead of a
//     blank/last-search state.
//   - On modal hide (hidden.bs.modal) : clear the EB window content,
//     reset the iframe so the next opening starts fresh.
// =====================================================================

/**
 * Prefill the EB browser with the given MMS code.
 * Strategy: the WHO EB widget reads `data-ctw-browser-hierarchy-selected-entity`
 * to determine which node to highlight. We also push to its search input.
 */
function ectPrefillEbBrowser(code) {
  if (!code) return;
  var ebWin = document.querySelector('.ctw-eb-window[data-ctw-ino="2"]');
  if (!ebWin) {
    console.warn("[EB] Window element not found for prefill");
    return;
  }
  // Reset previous state first
  ectResetEbBrowser();
  // Re-init with the new code
  ebWin.setAttribute("data-ctw-search-text", code);
  console.log("[EB] Prefilled with code: " + code);
  // Re-bind ECT for iNo=2 if available
  if (typeof ECT !== "undefined" && ECT.Handler) {
    try {
      var settings = {
        apiServerUrl: ECTBridgeConfig.apiServerUrl || "",
        apiSecured:   ECTBridgeConfig.apiSecured === true || ECTBridgeConfig.apiSecured === "true",
        useProxy:     ECTBridgeConfig.useProxy === true || ECTBridgeConfig.useProxy === "true",
        iNo:          "2",
        language:     ECTBridgeConfig.language || "fr",
        linearizationName: "mms",
        searchText:   code,
        releaseId:    ebWin.getAttribute("data-ctw-release") || "2024-01"
      };
      ECT.Handler.configure(settings, {
        selectedEntityFunction: function(entity) {
          // When user picks a code in the browser, write it into the
          // hidden input for Shiny + into the edit form's MMS field.
          var picked = entity && entity.theCode ? entity.theCode : "";
          if (picked) {
            console.log("[EB] User picked: " + picked);
            // Update the edit form input (if present)
            var formInput = document.getElementById("edit_panel-edit_target_mms");
            if (formInput) {
              formInput.value = picked;
              formInput.dispatchEvent(new Event("input", { bubbles: true }));
              formInput.dispatchEvent(new Event("change", { bubbles: true }));
            }
            // Also push via Shiny channel
            if (window.Shiny && window.Shiny.setInputValue) {
              Shiny.setInputValue("eb_picked_code",
                                   { code: picked, ts: Date.now() },
                                   { priority: "event" });
            }
          }
        }
      });
      bindECT("2");
    } catch (e) {
      console.warn("[EB] Configure error during prefill:", e);
    }
  }
}

/**
 * Reset the EB browser window content : remove search state so the next
 * opening starts fresh.
 */
function ectResetEbBrowser() {
  var ebWin = document.querySelector('.ctw-eb-window[data-ctw-ino="2"]');
  if (ebWin) {
    // Clear inner content
    ebWin.innerHTML = "";
    // Remove search-text attribute
    ebWin.removeAttribute("data-ctw-search-text");
    console.log("[EB] Reset");
  }
}

// Expose for external triggers
window.ectPrefillEbBrowser = ectPrefillEbBrowser;
window.ectResetEbBrowser   = ectResetEbBrowser;

// Wire Bootstrap modal events
document.addEventListener("DOMContentLoaded", function() {
  var modal = document.getElementById("eb_browser_modal");
  if (!modal) {
    console.warn("[EB] eb_browser_modal not found (login screen probably)");
    return;
  }
  modal.addEventListener("show.bs.modal", function(ev) {
    // Read the code to prefill from the modal's data attribute (set by Python
    // via the click handler that opens the modal).
    var code = modal.getAttribute("data-prefill-code");
    if (code) {
      ectPrefillEbBrowser(code);
    }
  });
  modal.addEventListener("hidden.bs.modal", function(ev) {
    ectResetEbBrowser();
    // Clear the prefill attr so next open without setting it = blank
    modal.removeAttribute("data-prefill-code");
  });
});

// Shiny custom message handler : Python can call session.send_custom_message(
// "eb_open_with_code", {code: "BA00"}) to open the modal pre-filled.
if (typeof Shiny !== "undefined" && Shiny.addCustomMessageHandler) {
  Shiny.addCustomMessageHandler("eb_open_with_code", function(data) {
    var modal = document.getElementById("eb_browser_modal");
    if (!modal) return;
    if (data && data.code) {
      modal.setAttribute("data-prefill-code", data.code);
    }
    // Use Bootstrap's API to show the modal
    if (window.bootstrap && window.bootstrap.Modal) {
      var instance = window.bootstrap.Modal.getOrCreateInstance(modal);
      instance.show();
    } else {
      // Fallback : click a hidden button with data-bs-toggle
      var btn = document.querySelector('[data-bs-target="#eb_browser_modal"]');
      if (btn) btn.click();
    }
  });
}
