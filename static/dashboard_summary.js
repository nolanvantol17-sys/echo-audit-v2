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

  function renderLeaderboard(rows) {
    const box = document.getElementById("leaderboard");
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="empty-state">No graded calls this month.</div>';
      return;
    }
    box.innerHTML = rows.slice(0, 3).map((r, i) => {
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

  function renderRecent(rows) {
    const box = document.getElementById("recent-wrap");
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="empty-state">No grades yet.</div>';
      return;
    }
    box.innerHTML = rows.slice(0, 5).map((r) => {
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

  window.EA.DashboardSummary = {
    renderStats:       renderStats,
    renderLeaderboard: renderLeaderboard,
    renderRecent:      renderRecent,
  };
})();
