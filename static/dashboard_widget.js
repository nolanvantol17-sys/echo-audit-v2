/* ========================================================================
   dashboard_widget.js — reusable analytics widget.

   Renders one horizontal filter row (Locations / Group by / Callers /
   Campaigns) + a Chart.js chart below. Hits /api/dashboard/filters for the
   dropdown options and /api/dashboard/chart for the data.

   Usage:
     EA.DashboardWidget.init({
       container: HTMLElement,        // required: empty host node
       projectId: number | null,      // optional: scope to one project
       defaultViewBy: "date" | "caller" | "location" | "campaign",
     });

   Self-contained — relies only on window.EA + Chart.js (already loaded by
   the host page).
   ======================================================================== */

(function () {
  "use strict";

  console.log("[dashboard_widget.js loaded]", { EA: typeof window.EA });

  if (!window.EA) {
    console.error("dashboard_widget.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;

  // ── Static: per-instance template rendered into the container ──
  const TEMPLATE = `
    <div class="daw-filters">
      <span class="daw-bar-title">Analytics</span>

      <button type="button" class="daw-ms-btn" data-key="locations">
        <span class="daw-ms-label">All Locations</span>
        <span class="daw-ms-caret">▾</span>
      </button>

      <select class="daw-view-by">
        <option value="date">By Date</option>
        <option value="caller">By Caller</option>
        <option value="location">By Location</option>
        <option value="campaign">By Campaign</option>
      </select>

      <button type="button" class="daw-ms-btn" data-key="callers">
        <span class="daw-ms-label">All Callers</span>
        <span class="daw-ms-caret">▾</span>
      </button>

      <button type="button" class="daw-ms-btn" data-key="campaigns">
        <span class="daw-ms-label">All Campaigns</span>
        <span class="daw-ms-caret">▾</span>
      </button>

      <div class="daw-date-pills" role="group" aria-label="Date range">
        <button type="button" class="daw-pill" data-range="7">7D</button>
        <button type="button" class="daw-pill" data-range="30">30D</button>
        <button type="button" class="daw-pill" data-range="90">90D</button>
        <button type="button" class="daw-pill" data-range="365">1Y</button>
      </div>
    </div>

    <section class="panel daw-chart-panel">
      <div class="panel-title daw-chart-title">Score Trend</div>
      <div class="daw-chart-wrap">
        <canvas class="daw-chart"></canvas>
        <div class="daw-chart-loading" hidden>
          <div class="daw-spinner" aria-label="Loading chart"></div>
        </div>
        <div class="daw-chart-empty" hidden>No calls match the current filters.</div>
      </div>
    </section>
  `;

  // ── Static: scoped CSS, injected once on first init ──
  const STYLE_ID = "daw-style";
  const STYLE = `
    .daw-filters {
      display: flex; flex-wrap: nowrap; gap: 8px; align-items: center;
      padding: 10px 14px; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius);
      margin-bottom: 12px;
    }
    .daw-bar-title {
      font-size: 0.88rem; font-weight: 600; color: var(--text);
      margin-right: 6px; white-space: nowrap;
    }
    .daw-filters .daw-view-by {
      background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); font-size: 0.82rem; padding: 6px 10px;
      border-radius: 6px; font-family: inherit; flex: 1 1 0; min-width: 0;
    }
    .daw-ms-btn {
      background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); font-size: 0.82rem; padding: 6px 10px;
      border-radius: 6px; font-family: inherit; cursor: pointer;
      display: inline-flex; align-items: center; gap: 6px;
      flex: 1 1 0; min-width: 0;
    }
    .daw-ms-btn:hover { border-color: var(--accent); }
    .daw-ms-label {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
      text-align: left;
    }
    .daw-ms-caret { color: var(--muted); font-size: 0.75rem; }

    .daw-date-pills {
      display: inline-flex; gap: 4px; margin-left: auto;
      padding-left: 10px; flex-shrink: 0;
    }
    .daw-pill {
      background: transparent; border: 1px solid var(--border);
      color: var(--muted); font-size: 0.74rem; font-weight: 500;
      padding: 4px 10px; min-width: 38px;
      border-radius: 6px; font-family: inherit; cursor: pointer;
      transition: all 0.15s ease;
    }
    .daw-pill:hover { color: var(--text); border-color: var(--accent); }
    .daw-pill.is-active {
      background: var(--accent); border-color: var(--accent);
      color: #fff;
    }

    .daw-popover {
      position: absolute; z-index: 50; min-width: 220px; max-width: 320px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; box-shadow: 0 6px 18px rgba(0,0,0,0.35);
      padding: 8px; max-height: 320px; display: flex; flex-direction: column;
    }
    .daw-popover input.daw-popover-search {
      background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); font-size: 0.82rem; padding: 5px 8px;
      border-radius: 6px; font-family: inherit; margin-bottom: 6px;
    }
    .daw-popover-list {
      overflow-y: auto; flex: 1; min-height: 0;
    }
    .daw-popover-row {
      display: flex; align-items: center; gap: 8px;
      padding: 5px 6px; border-radius: 4px; cursor: pointer;
      font-size: 0.85rem; color: var(--text);
    }
    .daw-popover-row:hover { background: var(--surface-2); }
    .daw-popover-row input { margin: 0; }
    .daw-popover-empty {
      padding: 10px; color: var(--muted); font-size: 0.82rem; text-align: center;
    }
    .daw-popover-actions {
      display: flex; gap: 8px; padding-top: 6px;
      border-top: 1px solid var(--border); margin-top: 6px;
    }
    .daw-popover-actions button {
      background: transparent; color: var(--muted); border: none;
      font-size: 0.76rem; cursor: pointer; padding: 4px 6px;
    }
    .daw-popover-actions button:hover { color: var(--text); }

    .daw-chart-panel {
      display: flex; flex-direction: column; position: relative;
      min-height: 420px;
    }
    .daw-chart-wrap {
      flex: 1 1 auto; position: relative; width: 100%; min-height: 380px;
    }
    .daw-chart { display: block; width: 100%; height: 100%; }

    .daw-chart-loading {
      position: absolute; inset: 0; display: flex; align-items: center;
      justify-content: center; background: rgba(13, 26, 46, 0.55);
      border-radius: var(--radius); z-index: 2; backdrop-filter: blur(1px);
    }
    .daw-spinner {
      width: 28px; height: 28px; border: 3px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%;
      animation: daw-spin 0.8s linear infinite;
    }
    @keyframes daw-spin { to { transform: rotate(360deg); } }
    .daw-chart-empty {
      position: absolute; inset: 0; display: flex; align-items: center;
      justify-content: center; color: var(--muted); font-size: 0.92rem;
      z-index: 2; background: var(--surface); border-radius: var(--radius);
    }
    .daw-chart-loading[hidden],
    .daw-chart-empty[hidden] { display: none !important; }

    @media (max-width: 900px) {
      .daw-filters {
        flex-wrap: wrap;
      }
      .daw-bar-title { width: 100%; margin-bottom: 4px; }
      .daw-filters .daw-view-by,
      .daw-ms-btn { flex: 1 1 140px; }
      .daw-date-pills { margin-left: 0; padding-left: 0; width: 100%; }
      .daw-chart-panel { min-height: 340px; }
      .daw-chart-wrap { min-height: 340px; }
    }
  `;

  function ensureStyleInjected() {
    if (document.getElementById(STYLE_ID)) return;
    const el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = STYLE;
    document.head.appendChild(el);
  }

  // ── Multi-select dropdown ──
  // State shape: { all: bool, ids: Set<number> }. all=true means "All X".
  function createMultiSelect(button, options) {
    options = options || {};
    const allLabel = options.allLabel || "All";
    const onChange = options.onChange || function () {};
    const filterRows = options.filterRows || null;  // optional fn(item, state) → bool

    const labelEl = button.querySelector(".daw-ms-label");
    let items = [];           // [{id, name, ...extras}]
    let state = { all: true, ids: new Set() };
    let popover = null;
    let searchTerm = "";

    function syncLabel() {
      if (state.all || state.ids.size === 0) {
        labelEl.textContent = allLabel;
        return;
      }
      if (state.ids.size === 1) {
        const only = items.find((it) => it.id === [...state.ids][0]);
        labelEl.textContent = only ? only.name : "1 selected";
        return;
      }
      labelEl.textContent = state.ids.size + " selected";
    }

    function close() {
      if (popover) {
        popover.remove();
        popover = null;
      }
      document.removeEventListener("click", onDocClick, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", onOuterScroll, true);
    }

    function onOuterScroll(ev) {
      // Scrolling inside the popover (e.g. through the locations list) bubbles
      // up to the capture-phase listener but should NOT close the popover.
      if (popover && ev.target && ev.target.nodeType === 1 &&
          (ev.target === popover || popover.contains(ev.target))) {
        return;
      }
      close();
    }

    function onDocClick(ev) {
      if (popover && !popover.contains(ev.target) && ev.target !== button &&
          !button.contains(ev.target)) {
        close();
      }
    }

    function visibleItems() {
      const term = searchTerm.trim().toLowerCase();
      let list = items;
      if (filterRows) list = list.filter((it) => filterRows(it, state));
      if (term) list = list.filter((it) =>
        (it.name || "").toLowerCase().indexOf(term) !== -1);
      return list;
    }

    function renderList() {
      if (!popover) return;
      const list = popover.querySelector(".daw-popover-list");
      const visible = visibleItems();
      if (visible.length === 0) {
        list.innerHTML = '<div class="daw-popover-empty">No options.</div>';
        return;
      }
      list.innerHTML = visible.map((it) => {
        const checked = !state.all && state.ids.has(it.id);
        return (
          '<label class="daw-popover-row">' +
            '<input type="checkbox" data-id="' + it.id + '"' +
              (checked ? " checked" : "") + '>' +
            '<span>' + EA.esc(it.name || ("#" + it.id)) + '</span>' +
          '</label>'
        );
      }).join("");
      list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
        cb.addEventListener("change", () => {
          const id = parseInt(cb.dataset.id, 10);
          if (state.all) {
            state = { all: false, ids: new Set([id]) };
          } else if (cb.checked) {
            state.ids.add(id);
          } else {
            state.ids.delete(id);
            if (state.ids.size === 0) state = { all: true, ids: new Set() };
          }
          syncLabel();
          onChange(snapshot());
        });
      });
    }

    function open() {
      close();
      popover = document.createElement("div");
      popover.className = "daw-popover";
      popover.innerHTML = `
        <input type="text" class="daw-popover-search" placeholder="Search…">
        <div class="daw-popover-list"></div>
        <div class="daw-popover-actions">
          <button type="button" data-act="all">Select all</button>
          <button type="button" data-act="none">Clear</button>
        </div>
      `;
      document.body.appendChild(popover);

      const rect = button.getBoundingClientRect();
      popover.style.top  = (rect.bottom + window.scrollY + 4) + "px";
      popover.style.left = (rect.left   + window.scrollX) + "px";

      const search = popover.querySelector(".daw-popover-search");
      search.addEventListener("input", () => {
        searchTerm = search.value;
        renderList();
      });

      popover.querySelector('[data-act="all"]').addEventListener("click", () => {
        state = { all: true, ids: new Set() };
        syncLabel();
        renderList();
        onChange(snapshot());
      });
      popover.querySelector('[data-act="none"]').addEventListener("click", () => {
        // "Clear" is identical to "all" here: empty set → no filter applied
        state = { all: true, ids: new Set() };
        syncLabel();
        renderList();
        onChange(snapshot());
      });

      renderList();
      // Defer so the click that opened the popover doesn't immediately close it
      setTimeout(() => {
        document.addEventListener("click", onDocClick, true);
      }, 0);
      window.addEventListener("resize", close);
      // Capture-phase scroll listener fires for the popover's own internal
      // scroll too — which would close the dropdown the moment a user tries
      // to scroll a long location list. Ignore scroll events whose target is
      // inside the popover; only outer-page scrolls should dismiss.
      window.addEventListener("scroll", onOuterScroll, true);
    }

    button.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (popover) close();
      else open();
    });

    function snapshot() {
      return {
        all: state.all,
        ids: state.all ? [] : [...state.ids],
      };
    }

    return {
      setItems(newItems) {
        items = newItems || [];
        // Drop any selected ids that no longer exist in the option set.
        const validIds = new Set(items.map((it) => it.id));
        if (!state.all) {
          [...state.ids].forEach((id) => { if (!validIds.has(id)) state.ids.delete(id); });
          if (state.ids.size === 0) state = { all: true, ids: new Set() };
        }
        syncLabel();
        if (popover) renderList();
      },
      reset() {
        state = { all: true, ids: new Set() };
        syncLabel();
        if (popover) renderList();
      },
      get() { return snapshot(); },
      refreshList() { if (popover) renderList(); },
    };
  }

  // ── Chart helpers ──
  function hasChartPoints(data) {
    if (!data || !Array.isArray(data.labels) || data.labels.length === 0) return false;
    const ds = (data.datasets || [])[0];
    if (!ds || !Array.isArray(ds.data)) return false;
    return ds.data.some((v) => v !== null && v !== undefined);
  }

  function chartTitleFor(viewBy) {
    return viewBy === "date"     ? "Score Trend"
         : viewBy === "caller"   ? "Average Score by Caller"
         : viewBy === "location" ? "Average Score by Location"
         : viewBy === "campaign" ? "Average Score by Campaign"
                                 : "Score";
  }

  // Canvas can't interpret "var(--name)" — resolve once per render via
  // getComputedStyle and cache per-name. Returns the original string if it
  // isn't a CSS var reference (e.g. already a hex/rgba), and a muted gray
  // fallback if the lookup returns empty.
  function makeColorResolver() {
    const cache = new Map();
    const root = document.documentElement;
    return function resolve(val) {
      if (!val) return "#94a3b8";
      const m = /^var\((--[^)]+)\)$/.exec(String(val).trim());
      if (!m) return val;
      const key = m[1];
      if (cache.has(key)) return cache.get(key);
      const resolved = getComputedStyle(root).getPropertyValue(key).trim() || "#94a3b8";
      cache.set(key, resolved);
      return resolved;
    };
  }

  function buildLineCfg(data, resolve) {
    const dataset = (data.datasets && data.datasets[0]) || { data: [] };
    const vals = (data.points && data.points.length)
      ? data.points.map((p) => (p ? p.score : null))
      : (dataset.data || []);
    const pointColors = vals.map((v) => resolve(EA.scoreColor(v)));
    return {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [{
          label: "Score",
          data: dataset.data || [],
          borderColor: "#2563eb",
          backgroundColor: "rgba(37,99,235,0.15)",
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBackgroundColor: pointColors,
          pointBorderColor: pointColors,
          tension: 0.25,
          fill: true,
        }],
      },
      options: chartCommonOptions({ viewBy: "date" }),
      plugins: [thresholdLinePlugin()],
    };
  }

  function buildBarCfg(data, resolve) {
    const dataset = (data.datasets && data.datasets[0]) || { data: [] };
    const vals = dataset.data || [];
    const barColors = vals.map((v) => resolve(EA.scoreColor(v)));
    return {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [{
          label: "Avg Score",
          data: vals,
          backgroundColor: barColors,
          borderColor: barColors,
          borderWidth: 1,
        }],
      },
      options: chartCommonOptions({ viewBy: "aggregate" }),
      plugins: [thresholdLinePlugin()],
    };
  }

  function chartCommonOptions(opts) {
    opts = opts || {};
    const isDate = opts.viewBy === "date";
    const plugins = { legend: { labels: { color: "#f1f5f9" } } };
    if (isDate) {
      // Date-view tooltip reads the matched row from chart.$points (stashed
      // in renderChart). Aggregate views leave defaults untouched.
      plugins.tooltip = {
        callbacks: {
          label(ctx) {
            const pts = ctx.chart && ctx.chart.$points;
            const p = pts && pts[ctx.dataIndex];
            if (!p) return ctx.formattedValue;
            const resp  = p.respondent_name || "—";
            const date  = p.date || ctx.label || "";
            const score = EA.formatScore(p.score);
            return resp + " · " + date + " · " + score;
          },
        },
      };
    }
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: {
          min: 0, max: 10,
          ticks: { color: "#94a3b8", stepSize: 2 },
          grid:  { color: "rgba(255,255,255,0.05)" },
        },
        x: {
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true },
          grid:  { color: "rgba(255,255,255,0.05)" },
        },
      },
      plugins: plugins,
    };
  }

  function thresholdLinePlugin() {
    return {
      id: "daw-threshold-line",
      afterDraw(chartInstance) {
        const { ctx, chartArea, scales } = chartInstance;
        if (!scales.y) return;
        const y = scales.y.getPixelForValue(5);
        ctx.save();
        ctx.strokeStyle = "rgba(239,68,68,0.7)";
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(chartArea.left, y);
        ctx.lineTo(chartArea.right, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(239,68,68,0.85)";
        ctx.font = "11px Inter, sans-serif";
        ctx.textAlign = "right";
        ctx.fillText("threshold 5.0", chartArea.right - 6, y - 4);
        ctx.restore();
      },
    };
  }

  // ── Date range presets ──
  // Returns {from: "YYYY-MM-DD", to: "YYYY-MM-DD"} or {from: null, to: null}.
  function isoDay(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }
  function dateRangeFor(preset) {
    const days = parseInt(preset, 10);
    if (isNaN(days) || days <= 0) return { from: null, to: null };
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const start = new Date(today);
    start.setDate(start.getDate() - (days - 1));
    return { from: isoDay(start), to: isoDay(today) };
  }

  const DEFAULT_DATE_PRESET = "30";

  // ── Main init ──
  function init(opts) {
    opts = opts || {};
    if (!opts.container) {
      console.error("DashboardWidget.init: container required");
      return;
    }
    ensureStyleInjected();
    const root = opts.container;
    const projectId = opts.projectId || null;
    const defaultViewBy = opts.defaultViewBy || "date";

    root.innerHTML = TEMPLATE;

    const titleEl     = root.querySelector(".daw-chart-title");
    const canvasEl    = root.querySelector(".daw-chart");
    const loadingEl   = root.querySelector(".daw-chart-loading");
    const emptyEl     = root.querySelector(".daw-chart-empty");
    const viewBySel   = root.querySelector(".daw-view-by");

    viewBySel.value = defaultViewBy;
    titleEl.textContent = chartTitleFor(defaultViewBy);

    let chart = null;
    let allCampaigns = [];   // raw campaigns list for client-side narrowing
    let datePreset = DEFAULT_DATE_PRESET;

    // Date pill wiring
    const pillBtns = root.querySelectorAll(".daw-pill");
    function syncPillActive() {
      pillBtns.forEach((b) => {
        b.classList.toggle("is-active", b.dataset.range === datePreset);
      });
    }
    pillBtns.forEach((b) => {
      b.addEventListener("click", () => {
        if (b.dataset.range === datePreset) return;
        datePreset = b.dataset.range;
        syncPillActive();
        reload();
      });
    });
    syncPillActive();

    // Chart state machine
    function showLoading() {
      loadingEl.hidden = false; emptyEl.hidden = true;
      canvasEl.style.visibility = "hidden";
    }
    function showEmpty(msg) {
      emptyEl.textContent = msg || "No calls match the current filters.";
      emptyEl.hidden = false; loadingEl.hidden = true;
      canvasEl.style.visibility = "hidden";
    }
    function showCanvas() {
      loadingEl.hidden = true; emptyEl.hidden = true;
      canvasEl.style.visibility = "visible";
    }

    // Multi-selects
    const locMS = createMultiSelect(root.querySelector('[data-key="locations"]'), {
      allLabel: "All Locations",
      onChange: () => { narrowCampaignsByLocation(); reload(); },
    });
    const callerMS = createMultiSelect(root.querySelector('[data-key="callers"]'), {
      allLabel: "All Callers",
      onChange: reload,
    });
    const campMS = createMultiSelect(root.querySelector('[data-key="campaigns"]'), {
      allLabel: "All Campaigns",
      onChange: reload,
    });

    function narrowCampaignsByLocation() {
      const loc = locMS.get();
      let visible = allCampaigns;
      if (!loc.all && loc.ids.length > 0) {
        const allowed = new Set(loc.ids);
        visible = allCampaigns.filter((c) =>
          c.location_id != null && allowed.has(c.location_id));
      }
      campMS.setItems(visible);
    }

    viewBySel.addEventListener("change", () => {
      titleEl.textContent = chartTitleFor(viewBySel.value);
      reload();
    });

    function buildParams() {
      const params = new URLSearchParams();
      params.set("metric",  "interaction_overall_score");
      params.set("view_by", viewBySel.value);
      if (projectId) params.set("project_id", projectId);

      const loc    = locMS.get();
      const caller = callerMS.get();
      const camp   = campMS.get();
      if (!loc.all    && loc.ids.length)    params.set("location_ids",  loc.ids.join(","));
      if (!caller.all && caller.ids.length) params.set("caller_ids",    caller.ids.join(","));
      if (!camp.all   && camp.ids.length)   params.set("campaign_ids",  camp.ids.join(","));

      const range = dateRangeFor(datePreset);
      if (range.from) params.set("date_from", range.from);
      if (range.to)   params.set("date_to",   range.to);
      return params;
    }

    async function reload() {
      const params = buildParams();
      showLoading();
      let data;
      try {
        data = await EA.fetchJSON("/api/dashboard/chart?" + params.toString());
      } catch (err) {
        showEmpty(err.message || "Failed to load chart.");
        return;
      }
      if (!hasChartPoints(data)) {
        showEmpty();
        if (chart) { chart.destroy(); chart = null; }
        return;
      }
      renderChart(data);
    }

    function renderChart(data) {
      if (chart) { chart.destroy(); chart = null; }
      const resolve = makeColorResolver();
      const isDateView = data.type !== "bar";
      const cfg = isDateView ? buildLineCfg(data, resolve) : buildBarCfg(data, resolve);

      if (isDateView) {
        cfg.options.onClick = function (evt, activeEls, ch) {
          if (!activeEls || !activeEls.length) return;
          const p = ch.$points && ch.$points[activeEls[0].index];
          if (p && p.interaction_id && EA.InteractionPanel) {
            EA.InteractionPanel.open(p.interaction_id);
          }
        };
        cfg.options.onHover = function (evt, activeEls, ch) {
          if (!ch || !ch.canvas) return;
          ch.canvas.style.cursor = (activeEls && activeEls.length) ? "pointer" : "default";
        };
      }

      chart = new Chart(canvasEl, cfg);
      chart.$points = data.points || [];
      showCanvas();
    }

    async function loadFilters() {
      const params = new URLSearchParams();
      if (projectId) params.set("project_id", projectId);
      try {
        const data = await EA.fetchJSON("/api/dashboard/filters?" + params.toString());
        const locs = (data.locations || []).map((r) => ({
          id: r.location_id, name: r.location_name,
        }));
        const callers = (data.callers || []).map((r) => ({
          id: r.user_id, name: r.user_name || ("User #" + r.user_id),
        }));
        allCampaigns = (data.campaigns || []).map((r) => ({
          id: r.campaign_id, name: r.campaign_name, location_id: r.location_id,
        }));
        locMS.setItems(locs);
        callerMS.setItems(callers);
        narrowCampaignsByLocation();
      } catch (err) {
        // Filters are non-critical — chart still works with no options.
        console.warn("Failed to load filter options:", err);
      }
    }

    // Boot: fetch filter options + initial chart in parallel.
    showLoading();
    Promise.all([loadFilters(), reload()]).catch(() => {});

    // Teardown handle — called by host page on PageRouter swap-away so the
    // chart instance, its ResizeObserver, and any open multi-select popovers
    // (appended to document.body) release their references instead of
    // orphaning after the widget's container is destroyed.
    return {
      destroy() {
        try { locMS.close(); }    catch (_) {}
        try { callerMS.close(); } catch (_) {}
        try { campMS.close(); }   catch (_) {}
        if (chart) { try { chart.destroy(); } catch (_) {} chart = null; }
      },
    };
  }

  window.EA.DashboardWidget = { init: init };
  console.log("[dashboard_widget.js registered]", { init: typeof window.EA.DashboardWidget.init });
})();
