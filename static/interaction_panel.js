/* ========================================================================
   interaction_panel.js — Right-slide-in side panel for interaction detail.

   Exposes:
     EA.InteractionPanel.open(interactionId)
     EA.InteractionPanel.close()

   Behavior:
     - Fetches GET /api/interactions/<id> and renders via EA.InteractionView
       with opts.readOnly=true (no regrade/delete affordances inside the
       panel — "Open as full page" in the header is the escape hatch).
     - URL state: pushes ?panel=interaction:<id> so the panel deep-links.
       Namespaced to leave room for future respondent/location panels
       (e.g. ?panel=respondent:42).
     - Close via X button, Escape, backdrop click, popstate (back button).
     - On page load, auto-opens if the URL already carries ?panel=interaction:<id>.
     - Body scroll is locked only on narrow viewports (≤960px — the panel
       covers the full screen there); desktop leaves the underlying page
       scrollable because the panel is a sidecar, not a modal.
     - Concurrent opens re-fetch and re-render in place; URL updates via
       replaceState so history doesn't grow a stack of panel openings.

   Depends on:
     - EA.fetchJSON, EA.esc, EA.formatScore, EA.scoreClass, EA.showError
     - EA.InteractionView.render
   ======================================================================== */

(function () {
  "use strict";
  const EA = window.EA;

  const PARAM_KEY = "panel";
  const PARAM_PREFIX = "interaction:";
  const MOBILE_BREAKPOINT = 960;

  // Single live panel instance (null when closed).
  let state = null;

  async function open(interactionId) {
    const iid = Number(interactionId);
    if (!iid || isNaN(iid)) return;

    // Already open — re-fetch into the existing panel rather than stacking.
    if (state) {
      state.currentId = iid;
      updateUrl(iid, /*replace=*/true);
      setHeaderLoading(state, iid);
      await loadInto(state);
      return;
    }

    state = buildPanel(iid);
    document.body.appendChild(state.backdrop);

    updateUrl(iid, /*replace=*/false);
    lockBodyScrollIfMobile(true);

    // Trigger slide-in on next frame so the CSS transition fires.
    requestAnimationFrame(function () {
      state.backdrop.classList.add("show");
    });

    document.addEventListener("keydown", state.onKey);
    window.addEventListener("popstate", state.onPopstate);

    // Focus the close button for keyboard users.
    state.closeBtn.focus();

    await loadInto(state);
  }

  function close(opts) {
    if (!state) return;
    opts = opts || {};

    document.removeEventListener("keydown", state.onKey);
    window.removeEventListener("popstate", state.onPopstate);

    const backdrop = state.backdrop;
    backdrop.classList.remove("show");
    // 220 must match .side-panel transition duration in app.css. If you bump
    // one, bump the other or the panel will yank out mid-slide.
    setTimeout(function () {
      if (backdrop.parentNode) backdrop.remove();
    }, 220);

    lockBodyScrollIfMobile(false);

    // Strip the panel param unless the caller says otherwise (e.g. popstate
    // is reacting to a URL change we didn't initiate — don't re-push).
    if (!opts.skipUrlUpdate) clearUrl();

    state = null;
  }

  function buildPanel(iid) {
    const backdrop = document.createElement("div");
    backdrop.className = "side-panel-backdrop";
    backdrop.innerHTML =
      '<aside class="side-panel" role="dialog" aria-modal="false" aria-label="Interaction detail">' +
        '<header class="side-panel-header">' +
          '<div class="side-panel-title">' +
            '<span class="side-panel-heading" data-role="heading">Interaction #' + iid + '</span>' +
            '<span class="side-panel-score" data-role="score"></span>' +
          '</div>' +
          '<div class="side-panel-actions">' +
            '<a class="btn btn-ghost btn-sm" data-role="open-full" href="/app/history/' + iid + '">' +
              'Open as full page →' +
            '</a>' +
            '<button type="button" class="side-panel-close" data-role="close" aria-label="Close">×</button>' +
          '</div>' +
        '</header>' +
        '<div class="side-panel-body" data-role="body">' +
          '<div class="skeleton" style="height:26px;margin:10px 0;"></div>' +
          '<div class="skeleton" style="height:160px;margin:10px 0;"></div>' +
          '<div class="skeleton" style="height:120px;margin:10px 0;"></div>' +
        '</div>' +
      '</aside>';

    const panel   = backdrop.querySelector(".side-panel");
    const body    = backdrop.querySelector('[data-role="body"]');
    const closeBtn = backdrop.querySelector('[data-role="close"]');
    const heading = backdrop.querySelector('[data-role="heading"]');
    const scoreEl = backdrop.querySelector('[data-role="score"]');
    const openFull = backdrop.querySelector('[data-role="open-full"]');

    const self = {
      backdrop: backdrop,
      panel: panel,
      body: body,
      closeBtn: closeBtn,
      heading: heading,
      scoreEl: scoreEl,
      openFull: openFull,
      currentId: iid,
      onKey: null,
      onPopstate: null,
    };

    self.onKey = function (e) {
      if (e.key === "Escape") close();
    };
    self.onPopstate = function () {
      // Browser navigated history. If the new URL has no panel param, close.
      // If it has a different interaction id, navigate the panel to it instead
      // of closing — supports back/forward across multiple panel openings.
      // In both cases the browser already updated the URL, so don't re-edit it.
      const urlId = readPanelIdFromUrl();
      if (urlId === null) {
        close({ skipUrlUpdate: true });
      } else if (urlId !== self.currentId) {
        self.currentId = urlId;
        setHeaderLoading(self, urlId);
        loadInto(self);
      }
    };

    closeBtn.addEventListener("click", function () { close(); });
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) close();
    });

    return self;
  }

  function setHeaderLoading(self, iid) {
    self.heading.textContent = "Interaction #" + iid;
    self.scoreEl.textContent = "";
    self.scoreEl.className = "side-panel-score";
    self.openFull.setAttribute("href", "/app/history/" + iid);
    self.body.innerHTML =
      '<div class="skeleton" style="height:26px;margin:10px 0;"></div>' +
      '<div class="skeleton" style="height:160px;margin:10px 0;"></div>' +
      '<div class="skeleton" style="height:120px;margin:10px 0;"></div>';
  }

  async function loadInto(self) {
    const iid = self.currentId;
    try {
      const data = await EA.fetchJSON("/api/interactions/" + iid);
      // Bail out if user already closed or opened a different interaction
      // before the fetch returned.
      if (!state || state !== self || self.currentId !== iid) return;

      // Header score pill
      const cls = EA.scoreClass(data.interaction_overall_score);
      self.scoreEl.className = "side-panel-score score-pill " + cls;
      self.scoreEl.textContent =
        (data.status_id === 44) ? "N/A" : EA.formatScore(data.interaction_overall_score);

      EA.InteractionView.render(self.body, data, { readOnly: true });
    } catch (err) {
      if (!state || state !== self || self.currentId !== iid) return;
      EA.showError(self.body, err.message || "Failed to load interaction.");
    }
  }

  // ── URL state ───────────────────────────────────────────────
  function updateUrl(iid, replace) {
    const url = new URL(window.location.href);
    url.searchParams.set(PARAM_KEY, PARAM_PREFIX + iid);
    const method = replace ? "replaceState" : "pushState";
    window.history[method](null, "", url.toString());
  }

  function clearUrl() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has(PARAM_KEY)) return;
    url.searchParams.delete(PARAM_KEY);
    // Push so back button after close returns to the pre-panel URL state
    // for the history trigger flow; but we use replaceState on close so
    // closing a panel doesn't pollute history with a no-op step.
    window.history.replaceState(null, "", url.toString());
  }

  function readPanelIdFromUrl() {
    const raw = new URLSearchParams(window.location.search).get(PARAM_KEY);
    if (!raw) return null;
    if (raw.indexOf(PARAM_PREFIX) !== 0) return null;
    const id = Number(raw.slice(PARAM_PREFIX.length));
    return (id && !isNaN(id)) ? id : null;
  }

  // ── Body scroll lock (mobile only) ─────────────────────────
  let _savedOverflow = null;
  function lockBodyScrollIfMobile(lock) {
    if (window.innerWidth > MOBILE_BREAKPOINT) return;
    if (lock) {
      _savedOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    } else if (_savedOverflow !== null) {
      document.body.style.overflow = _savedOverflow;
      _savedOverflow = null;
    }
  }

  // ── Auto-open on page load if URL carries ?panel=interaction:<id> ──
  document.addEventListener("DOMContentLoaded", function () {
    const urlId = readPanelIdFromUrl();
    if (urlId) open(urlId);
  });

  window.EA = window.EA || {};
  window.EA.InteractionPanel = {
    open: open,
    close: close,
  };
})();
