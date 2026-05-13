/* ========================================================================
   explore_chart.js — Chart renderers for /app/explore.

   Exposes EA.ExploreChart with:
     - renderSiteHistory(canvas, data, opts) — Report #1 (dot plot)
     - renderRegional(canvas, data, opts)    — Phase 2 stub

   Chart.js v4 is loaded by the page (Same library every dashboard uses).
   Palette hex values mirror app.css design tokens:
     success #A8E847  warning #FF8C2A  danger #FF5C4A  muted #9C9183
     accent  #4A8076
   ======================================================================== */

(function () {
  "use strict";

  if (!window.EA) {
    console.error("explore_chart.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;

  // Hex resolutions of the score-class color buckets. Kept in sync with
  // EA.scoreClass (app.js) — same green/amber/red/gray thresholds.
  const COLOR_GOOD = "#A8E847";
  const COLOR_WARN = "#FF8C2A";
  const COLOR_BAD  = "#FF5C4A";
  const COLOR_NULL = "#9C9183";
  const COLOR_AVG  = "#4A8076";

  function colorForScore(v) {
    if (v === null || v === undefined) return COLOR_NULL;
    const n = Number(v);
    if (isNaN(n)) return COLOR_NULL;
    if (n > 7)  return COLOR_GOOD;
    if (n >= 5) return COLOR_WARN;
    return COLOR_BAD;
  }

  // Convert "YYYY-MM-DD" → unix epoch ms (UTC midnight) for the Y axis.
  // Chart.js scatter doesn't need real Date objects; numbers + a tick
  // formatter are enough. Returns null when the input doesn't parse.
  function parseISODate(s) {
    if (!s) return null;
    // Force UTC interpretation to avoid timezone-driven off-by-one days.
    const t = Date.parse(s + "T00:00:00Z");
    return isNaN(t) ? null : t;
  }

  // Reverse: epoch ms → "MMM d, yyyy" for axis tick labels + tooltips.
  // Uses UTC components so dates display the same regardless of viewer TZ
  // (interaction_date is a calendar-date column, not a timestamp).
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"];
  function formatEpoch(ms) {
    if (ms == null) return "";
    const d = new Date(ms);
    return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate() + ", " + d.getUTCFullYear();
  }


  function renderSiteHistory(canvas, data, opts) {
    opts = opts || {};
    const onPointClick = opts.onPointClick || function () {};
    const points = Array.isArray(data && data.points) ? data.points : [];

    // Tear down any prior chart on this canvas.
    const existing = canvas.__chart;
    if (existing) existing.destroy();

    // Build the unique X-axis category list. Sorted by location name so the
    // axis is stable across renders and ASC-readable.
    const seen = new Set();
    const labels = [];
    points.forEach((p) => {
      const name = p.location_name || "(unknown)";
      if (!seen.has(name)) { seen.add(name); labels.push(name); }
    });
    labels.sort((a, b) => a.localeCompare(b));

    // Scatter data: x = category (location name), y = epoch ms (date).
    // Chart.js scatter accepts {x,y} pairs against a category x-axis when
    // we declare the scale type as 'category' below.
    const scatterData = points.map((p) => ({
      x: p.location_name || "(unknown)",
      y: parseISODate(p.interaction_date),
      // Stash the source row so the tooltip + click handler can find it
      // without a parallel lookup.
      _meta: p,
    })).filter((row) => row.y !== null);

    const pointColors = scatterData.map((row) => colorForScore(row._meta.score));

    // Y-axis range with a small breathing margin around the date span.
    let yMin = null, yMax = null;
    scatterData.forEach((row) => {
      if (yMin === null || row.y < yMin) yMin = row.y;
      if (yMax === null || row.y > yMax) yMax = row.y;
    });
    if (yMin !== null && yMax !== null) {
      const pad = Math.max(86400000, (yMax - yMin) * 0.05);  // ≥1 day pad
      yMin = yMin - pad;
      yMax = yMax + pad;
    }

    const cfg = {
      type: "scatter",
      data: {
        datasets: [{
          label: "Calls",
          data: scatterData,
          backgroundColor: pointColors,
          borderColor: pointColors,
          pointRadius: 5,
          pointHoverRadius: 7,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(37, 43, 43, 0.96)",
            titleColor: "#ECE6D9",
            bodyColor: "#ECE6D9",
            borderColor: "rgba(236,230,217,0.22)",
            borderWidth: 1,
            padding: 10,
            displayColors: false,
            callbacks: {
              title: (items) => {
                if (!items.length) return "";
                const m = items[0].raw._meta;
                return (m.location_name || "(unknown)")
                  + "  ·  " + formatEpoch(items[0].raw.y);
              },
              label: (item) => {
                const m = item.raw._meta;
                const scoreStr = (m.score === null || m.score === undefined)
                  ? "—" : Number(m.score).toFixed(1);
                const lines = [
                  "Score: " + scoreStr,
                  "Caller: " + (m.caller_name || "—"),
                  "Respondent: " + (m.respondent_name || "—"),
                ];
                if (m.summary) {
                  // Chart.js tooltip doesn't wrap long lines well; split on
                  // word boundaries to give it line breaks it can render.
                  const summary = m.summary;
                  const max = 60;
                  if (summary.length <= max) {
                    lines.push("");
                    lines.push(summary);
                  } else {
                    lines.push("");
                    let line = "";
                    summary.split(/\s+/).forEach((w) => {
                      if ((line + " " + w).trim().length > max) {
                        lines.push(line);
                        line = w;
                      } else {
                        line = (line ? line + " " : "") + w;
                      }
                    });
                    if (line) lines.push(line);
                  }
                }
                return lines;
              },
            },
          },
        },
        scales: {
          x: {
            type: "category",
            labels: labels,
            offset: true,
            ticks: {
              color: "#9C9183",
              autoSkip: false,
              maxRotation: 60,
              minRotation: 30,
            },
            grid: { color: "rgba(236,230,217,0.06)" },
          },
          y: {
            type: "linear",
            min: yMin,
            max: yMax,
            ticks: {
              color: "#9C9183",
              callback: function (val) { return formatEpoch(val); },
            },
            grid: { color: "rgba(236,230,217,0.06)" },
          },
        },
        onClick: function (evt, elements) {
          if (!elements.length) return;
          const el = elements[0];
          const row = scatterData[el.index];
          if (row && row._meta && row._meta.interaction_id) {
            onPointClick(row._meta.interaction_id);
          }
        },
      },
    };

    const chart = new window.Chart(canvas.getContext("2d"), cfg);
    canvas.__chart = chart;
    return chart;
  }


  // Phase 2 — line graph across a campaign's locations. Stub here so the
  // page can import the namespace without conditional checks.
  function renderRegional(canvas, data, opts) {
    console.warn("EA.ExploreChart.renderRegional: not yet implemented (Phase 2).");
  }

  EA.ExploreChart = { renderSiteHistory, renderRegional, colorForScore };
})();
