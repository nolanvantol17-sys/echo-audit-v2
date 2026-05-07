/* ========================================================================
   dashboard_summary.js — shared renderers for the stat strip + leaderboards
   in _dashboard_stats.html + _dashboard_analytics.html.

   Consumed by both projects.html (tenant-scoped, 5-card strip) and
   project_hub.html (project-scoped, 4-card strip). Renderers tolerate
   missing DOM elements so the same module serves both strip variants.

   Usage:
     EA.DashboardSummary.renderStats(stat_cards);
     EA.DashboardSummary.renderLeaderboard(leaderboard_rows);
     EA.DashboardSummary.renderRecent(recent_rows);
   ======================================================================== */

(function () {
  "use strict";

  if (!window.EA) {
    console.error("dashboard_summary.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;

  function renderStats(s) {
    s = s || {};
    const num = (v, fallback = 0) => (v === null || v === undefined) ? fallback : v;
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    set("stat-total", num(s.total_calls, 0));
    const avgEl = document.getElementById("stat-avg");
    if (avgEl) {
      avgEl.textContent = (s.avg_score === null || s.avg_score === undefined)
        ? "—" : EA.formatScore(s.avg_score);
    }
    set("stat-below", num(s.below_threshold, 0));
    // Unanswered tile: count (rate%) when nonzero with rate. Mirrors the
    // locations table cell treatment. Direct innerHTML is XSS-safe — only
    // numbers flow in.
    const noAnsEl = document.getElementById("stat-noans");
    if (noAnsEl) {
      const count = num(s.no_answer_count, 0);
      const rate  = s.no_answer_rate;
      if (count === 0) {
        noAnsEl.innerHTML = '<span style="color: var(--muted);">0</span>';
      } else if (rate !== null && rate !== undefined) {
        noAnsEl.innerHTML = count + ' <span style="color: var(--muted);">(' +
          (rate * 100).toFixed(1) + '%)</span>';
      } else {
        noAnsEl.textContent = String(count);
      }
    }
    // Landing-only: 5th card is only in the DOM when
    // include_active_projects=True was passed to the partial.
    const projEl = document.getElementById("stat-projects");
    if (projEl) projEl.textContent = num(s.active_projects, 0);
  }

  function renderLeaderboard(rows, opts) {
    const limit = (opts && opts.limit) || 3;
    const box = document.getElementById("leaderboard");
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="empty-state">No graded calls this month.</div>';
      return;
    }
    box.innerHTML = rows.slice(0, limit).map((r, i) => {
      const cls = EA.scoreClass(r.avg_score);
      const fullName = r.respondent_name || "—";
      const nameEsc = EA.esc(fullName);

      const locs = Array.isArray(r.locations) ? r.locations : [];
      let locStr = "";
      if (locs.length === 1) {
        locStr = EA.esc(locs[0]);
      } else if (locs.length === 2) {
        locStr = EA.esc(locs[0]) + ", " + EA.esc(locs[1]);
      } else if (locs.length > 2) {
        locStr = EA.esc(locs[0]) + ", " + EA.esc(locs[1]) +
                 ' <span class="more">+' + (locs.length - 2) + " more</span>";
      }
      const locHtml = locStr
        ? '<span class="leader-locations" title="' + EA.esc(locs.join(", ")) + '">' + locStr + '</span>'
        : '';

      const trend = r.trend || "none";
      const trendArrow = trend === "up" ? "↑"
                       : trend === "down" ? "↓"
                       : trend === "flat" ? "→" : "";
      const trendTitle = trend === "up"   ? "Trending up (last 30 days)"
                      : trend === "down" ? "Trending down (last 30 days)"
                      : trend === "flat" ? "Steady (last 30 days)"
                      : "Not enough data for a 30-day trend";

      const callCount = r.call_count || 0;
      const lastCall = r.last_call ? EA.formatRelativeTime(r.last_call) : "";

      const rowClass = r.report_url ? "leader-row clickable" : "leader-row no-report";
      const dataHref = r.report_url ? ' data-href="' + EA.esc(r.report_url) + '"' : '';

      return (
        '<div class="' + rowClass + '"' + dataHref + '>' +
          '<span class="leader-rank">' + (i + 1) + '</span>' +
          '<div class="leader-name-col">' +
            '<span class="leader-name" title="' + nameEsc + '">' + nameEsc + '</span>' +
            locHtml +
          '</div>' +
          '<span class="score-pill ' + cls + '">' + EA.formatScore(r.avg_score) + '</span>' +
          '<span class="leader-trend ' + trend + '" title="' + EA.esc(trendTitle) + '">' +
            trendArrow +
          '</span>' +
          '<span class="leader-meta">' +
            callCount + ' call' + (callCount === 1 ? '' : 's') +
            (lastCall ? '<span class="leader-last">' + EA.esc(lastCall) + '</span>' : '') +
          '</span>' +
        '</div>'
      );
    }).join("");
    box.querySelectorAll(".leader-row.clickable[data-href]").forEach((el) => {
      el.addEventListener("click", () => {
        window.location.href = el.dataset.href;
      });
    });
  }

  function renderRecent(rows, opts) {
    const limit = (opts && opts.limit) || 5;
    const box = document.getElementById("recent-wrap");
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="empty-state">No grades yet.</div>';
      return;
    }
    box.innerHTML = rows.slice(0, limit).map((r) => {
      const cls = EA.scoreClass(r.interaction_overall_score);
      const full = r.respondent_name || "—";
      const name = full.length > 30 ? (full.slice(0, 29) + "…") : full;

      const ts = r.interaction_call_start_time || r.interaction_uploaded_at || r.interaction_date;
      const rel = ts ? EA.formatRelativeTime(ts) : "—";
      const callTime = EA.formatCallTime(
        r.interaction_call_start_time, r.interaction_uploaded_at, null,
      );
      const dateCell = (callTime && callTime !== "—")
        ? ('<span class="recent-date" style="display:flex;flex-direction:column;line-height:1.15;">' +
             EA.esc(rel) +
             '<span class="muted text-small" style="font-size:0.7rem;">' + EA.esc(callTime) + '</span>' +
           '</span>')
        : '<span class="recent-date">' + EA.esc(rel) + '</span>';

      const locName = r.location_name || "";
      const nameStack = locName
        ? ('<span class="recent-name" style="display:flex;flex-direction:column;line-height:1.15;" title="' + EA.esc(full) + '">' +
             EA.esc(name) +
             '<span class="muted text-small" style="font-size:0.7rem;">' + EA.esc(locName) + '</span>' +
           '</span>')
        : ('<span class="recent-name" title="' + EA.esc(full) + '">' + EA.esc(name) + '</span>');

      const flagsText = r.interaction_flags || "";
      const flagCount = (flagsText.match(/🚩/g) || []).length;
      const flagPill = flagCount
        ? '<span class="flag-pill-mini" title="' + flagCount +
            ' flag' + (flagCount === 1 ? '' : 's') + ' on this call">🚩 ' + flagCount + '</span>'
        : '';

      return (
        '<div class="recent-item" data-iid="' + EA.esc(r.interaction_id) + '">' +
          dateCell + nameStack +
          '<span class="recent-trail">' +
            '<span class="score-pill ' + cls + '">' + EA.formatScore(r.interaction_overall_score) + '</span>' +
            flagPill +
          '</span>' +
        '</div>'
      );
    }).join("");
    box.querySelectorAll(".recent-item[data-iid]").forEach((el) => {
      el.addEventListener("click", () => {
        const iid = el.dataset.iid;
        if (window.EA && EA.InteractionPanel && typeof EA.InteractionPanel.open === "function") {
          EA.InteractionPanel.open(iid);
        } else {
          window.location.href = "/app/history/" + encodeURIComponent(iid);
        }
      });
    });
  }

  // ── Activity strip (rolling 7d vs prior 7d) ──────────────────────
  function renderActivity(activity) {
    const wrap = document.getElementById("dash-activity");
    if (!wrap) return;
    activity = activity || {};
    const thisWeek = activity.this_week == null ? 0 : activity.this_week;
    const lastWeek = activity.last_week == null ? 0 : activity.last_week;
    const delta    = activity.delta_pct;

    const setText = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    setText("activity-this-week", thisWeek);
    setText("activity-last-week", lastWeek);

    const deltaEl = document.getElementById("activity-delta");
    if (deltaEl) {
      let text, cls;
      if (delta === null || delta === undefined) {
        text = "—"; cls = "flat";
      } else if (delta > 0) {
        text = "↑ " + delta + "%"; cls = "up";
      } else if (delta < 0) {
        text = "↓ " + Math.abs(delta) + "%"; cls = "down";
      } else {
        text = "→ 0%"; cls = "flat";
      }
      deltaEl.textContent = text;
      deltaEl.className = "stat-value activity-delta " + cls;
    }
    wrap.hidden = false;
  }

  // ── Recurring Issues panel ────────────────────────────────────────
  // Tiny bullet-list markdown renderer. Haiku is prompted to emit only
  // top-level "- " bullets with optional "  - " sub-bullets, so we don't
  // need a full markdown parser — just a 2-level nested <ul>.
  function _renderInsightsMarkdown(md) {
    const lines = String(md || "").split("\n");
    let html = "";
    let inTop = false;
    let inSub = false;
    const flushSub = () => { if (inSub) { html += "</ul>"; inSub = false; } };
    const flushTop = () => { flushSub(); if (inTop) { html += "</li></ul>"; inTop = false; } };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/g, "");
      if (!line.trim()) continue;
      const subMatch = line.match(/^\s{2,}-\s+(.+)$/);
      const topMatch = line.match(/^-\s+(.+)$/);
      if (subMatch && inTop) {
        if (!inSub) { html += "<ul>"; inSub = true; }
        html += "<li>" + EA.esc(subMatch[1]) + "</li>";
      } else if (topMatch) {
        if (inTop) { flushSub(); html += "</li>"; }
        else { html += "<ul>"; inTop = true; }
        html += "<li>" + EA.esc(topMatch[1]);
      } else {
        // Stray paragraph — render as muted text outside the list.
        flushTop();
        html += "<p>" + EA.esc(line) + "</p>";
      }
    }
    flushTop();
    return html;
  }

  function _formatGeneratedAt(iso) {
    if (!iso) return "";
    if (window.EA && EA.formatRelativeTime) {
      try { return EA.formatRelativeTime(iso); } catch (_) {}
    }
    return iso;
  }

  function renderInsights(payload, opts) {
    const wrap = document.getElementById("insights-panel");
    if (!wrap) return;
    payload = payload || {};
    const body = wrap.querySelector(".insights-body");
    const meta = wrap.querySelector(".insights-meta");
    const refreshBtn = wrap.querySelector(".insights-refresh");

    if (refreshBtn) {
      const isAdmin = !!(opts && opts.isAdmin);
      refreshBtn.hidden = !isAdmin;
    }

    if (!payload.report_markdown) {
      body.innerHTML =
        '<div class="insights-empty">Generating your first report — this can take a moment. ' +
        'Refresh the page in a minute or so.</div>';
      if (meta) meta.textContent = "";
      return;
    }

    body.innerHTML = _renderInsightsMarkdown(payload.report_markdown);
    if (meta) {
      const calls = payload.calls_in_window || 0;
      const when  = _formatGeneratedAt(payload.generated_at);
      const stale = payload.is_generating ? " · refreshing in background" : "";
      meta.textContent =
        "Last 30 days · " + calls + " graded call" + (calls === 1 ? "" : "s") +
        (when ? " · updated " + when : "") + stale;
    }
    wrap.hidden = false;
  }

  window.EA.DashboardSummary = {
    renderStats:       renderStats,
    renderLeaderboard: renderLeaderboard,
    renderRecent:      renderRecent,
    renderActivity:    renderActivity,
    renderInsights:    renderInsights,
  };
})();
