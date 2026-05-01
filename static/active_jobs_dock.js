/* ========================================================================
   active_jobs_dock.js — Persistent multi-job dock.

   Bottom-right floating pill that expands upward into a panel. Shows
   in-flight + recently-completed work for the current user across all
   pages of Echo Audit. Polls /api/active-jobs at 10s when the tab is
   visible, 60s when hidden. Hides entirely when the active list is empty.

   First paint reads from window.__INITIAL_DOCK_STATE (server-side
   injected by the inject_client_config context processor) so there's
   no flash of empty before the first poll lands.

   Exposes window.__refreshDock() so other JS (e.g. P3's AI shop initiate
   flow) can ping for an immediate refresh after submitting work.

   Coexists with templates/grade.html's #gj-pane on /app/grade — they
   poll different endpoints and share no DOM/state.
   ======================================================================== */

(function () {
  "use strict";

  const EA = window.EA;
  if (!EA) { console.error("active_jobs_dock.js requires window.EA"); return; }

  const root        = document.getElementById("active-jobs-dock");
  if (!root) return;   // element absent on unauthenticated pages
  const pillBtn     = root.querySelector("#ajd-pill");
  const pillCount   = root.querySelector(".ajd-pill-count");
  const panelEl     = root.querySelector("#ajd-panel");
  const panelList   = root.querySelector("#ajd-panel-list");
  const panelClose  = root.querySelector("#ajd-panel-close");

  const POLL_ACTIVE_MS = 10000;
  const POLL_IDLE_MS   = 60000;

  let jobs        = Array.isArray(window.__INITIAL_DOCK_STATE)
                      ? window.__INITIAL_DOCK_STATE.slice()
                      : [];
  let pollTimer   = null;
  let loadSeq     = 0;
  let panelOpen   = false;

  const TERMINAL_STATUSES = new Set(["graded", "no_answer", "failed", "timeout"]);
  function isTerminal(s) { return TERMINAL_STATUSES.has(s); }

  // ── Render ─────────────────────────────────────────────────

  function render() {
    if (jobs.length === 0) {
      root.hidden = true;
      panelEl.hidden = true;
      panelOpen = false;
      pillBtn.setAttribute("aria-expanded", "false");
      removeClearAllButton();
      return;
    }
    root.hidden = false;
    pillCount.textContent = String(jobs.length);
    syncClearAllButton();
    panelList.innerHTML = jobs.map(renderRow).join("");
    panelList.querySelectorAll(".ajd-row").forEach(function (row) {
      row.addEventListener("click", function () {
        const iid = row.dataset.interactionId;
        if (iid && iid !== "null") {
          window.location.href = "/app/history/" + encodeURIComponent(iid);
        }
      });
    });
    panelList.querySelectorAll(".ajd-row-dismiss").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        dismissOne(btn.dataset.source, parseInt(btn.dataset.rowId, 10));
      });
    });
  }

  function renderRow(job) {
    const status   = job.display_status || "queued";
    const title    = job.display_title  || "Submission";
    const iid      = job.interaction_id;
    const score    = job.interaction_overall_score;
    const meta     = job.meta || {};
    const project  = meta.project_name || "";
    const clickable = (status === "graded" && iid != null);
    const dismissable = isTerminal(status);

    let pillHtml;
    if (status === "graded" && score != null) {
      pillHtml = '<span class="ajd-pill-status graded">' +
                   EA.formatScore(score) + '</span>';
    } else {
      pillHtml = '<span class="ajd-pill-status ' + EA.esc(status) + '">' +
                   EA.esc(statusLabel(status)) + '</span>';
    }

    const dismissHtml = dismissable
      ? '<button type="button" class="ajd-row-dismiss" aria-label="Dismiss" ' +
          'data-source="' + EA.esc(job.source) + '" ' +
          'data-row-id="' + EA.esc(String(job.id)) + '">×</button>'
      : '';

    return (
      '<div class="ajd-row' + (clickable ? ' clickable' : '') +
        '" data-interaction-id="' + (iid != null ? EA.esc(String(iid)) : "null") + '">' +
        pillHtml +
        '<div class="ajd-row-summary">' +
          '<div class="ajd-row-title">' + EA.esc(title) + '</div>' +
          (project ? '<div class="ajd-row-meta">' + EA.esc(project) + '</div>' : '') +
        '</div>' +
        dismissHtml +
      '</div>'
    );
  }

  function statusLabel(s) {
    if (s === "queued")      return "QUEUED";
    if (s === "in_progress") return "RUNNING";
    if (s === "graded")      return "GRADED";
    if (s === "no_answer")   return "NO ANSWER";
    if (s === "failed")      return "FAILED";
    if (s === "timeout")     return "TIMEOUT";
    return String(s).toUpperCase();
  }

  // ── Polling ────────────────────────────────────────────────

  function isHidden() {
    return document.hidden || !document.hasFocus();
  }

  function pollOnce() {
    const seq = ++loadSeq;
    EA.fetchJSON("/api/active-jobs")
      .then(function (data) {
        if (seq !== loadSeq) return;
        jobs = Array.isArray(data) ? data : [];
        render();
      })
      .catch(function (_err) {
        /* silent — try again next tick */
      });
  }

  function setPollRate(ms) {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = setInterval(pollOnce, ms);
  }

  function onVisibility() {
    if (isHidden()) {
      setPollRate(POLL_IDLE_MS);
    } else {
      setPollRate(POLL_ACTIVE_MS);
      pollOnce();   // catch up immediately on focus return
    }
  }

  // ── Dismiss + clear-all ────────────────────────────────────

  function dismissOne(source, rowId) {
    if (!source || !Number.isFinite(rowId)) return;
    const idx = jobs.findIndex(function (j) {
      return j.source === source && j.id === rowId;
    });
    if (idx === -1) return;
    const removed = jobs.splice(idx, 1)[0];
    render();
    EA.fetchJSON("/api/active-jobs/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: source, id: rowId }),
    }).catch(function (err) {
      console.warn("[dock] dismiss failed; restoring row", err);
      jobs.splice(idx, 0, removed);
      render();
    });
  }

  function dismissAll() {
    const removed = [];
    const kept = [];
    jobs.forEach(function (j, i) {
      if (isTerminal(j.display_status)) removed.push({ idx: i, job: j });
      else kept.push(j);
    });
    if (removed.length === 0) return;
    jobs = kept;
    render();
    EA.fetchJSON("/api/active-jobs/dismiss-all", {
      method: "POST",
    }).catch(function (err) {
      console.warn("[dock] dismiss-all failed; restoring rows", err);
      removed.forEach(function (r) {
        jobs.splice(r.idx, 0, r.job);
      });
      render();
    });
  }

  function syncClearAllButton() {
    const hasTerminal = jobs.some(function (j) { return isTerminal(j.display_status); });
    let btn = document.getElementById("ajd-clear-all");
    if (hasTerminal) {
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.id = "ajd-clear-all";
        btn.className = "ajd-clear-all";
        btn.textContent = "Clear all";
        btn.setAttribute("aria-label", "Dismiss all completed rows");
        btn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          dismissAll();
        });
        panelClose.parentNode.insertBefore(btn, panelClose);
      }
    } else {
      removeClearAllButton();
    }
  }

  function removeClearAllButton() {
    const btn = document.getElementById("ajd-clear-all");
    if (btn) btn.remove();
  }

  // ── Pill / panel toggle ────────────────────────────────────

  function openPanel() {
    panelOpen = true;
    panelEl.hidden = false;
    pillBtn.setAttribute("aria-expanded", "true");
  }
  function closePanel() {
    panelOpen = false;
    panelEl.hidden = true;
    pillBtn.setAttribute("aria-expanded", "false");
  }

  pillBtn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    panelOpen ? closePanel() : openPanel();
  });
  panelClose.addEventListener("click", closePanel);
  // Outside-click closes the panel (mirrors project-settings popover pattern).
  document.addEventListener("click", function (ev) {
    if (!panelOpen) return;
    if (root.contains(ev.target)) return;
    closePanel();
  });

  // ── Boot + public refresh hook ─────────────────────────────

  render();   // first paint from server-side initial state

  setPollRate(isHidden() ? POLL_IDLE_MS : POLL_ACTIVE_MS);
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("focus", onVisibility);
  window.addEventListener("blur",  onVisibility);

  // Public hook for ad-hoc refresh from other JS (P3's AI shop initiate
  // flow will call this immediately after a successful submit so the new
  // row appears without waiting for the next poll tick).
  window.__refreshDock = pollOnce;
})();
