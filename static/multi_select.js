/* ========================================================================
   multi_select.js — Reusable multi-select dropdown.

   Lifted from dashboard_widget.js (the in-widget createMultiSelect that's
   shipped reliably on /app dashboards). Same behavior, same state shape;
   just promoted to EA.MultiSelect so /app/explore (and future pages) can
   reuse it without redeclaring 200 lines of popover logic.

   Followup: dashboard_widget.js still has its own private copy of this
   function (with the old .daw-popover-* class names) — migration to
   EA.MultiSelect.create is queued for a follow-up commit once the new
   Explore page is stable. See memory: followup_dashboard_widget_use_ea_multiselect.

   Usage:
     const ms = EA.MultiSelect.create(buttonEl, {
       allLabel: "All Locations",
       onChange: (state) => { ... },        // {all: bool, ids: number[]}
     });
     ms.setItems([{id: 1, name: "Briarwood"}, ...]);
     ms.setSelection([1, 5]);    // URL-hydration entry point
     ms.get();                   // {all: false, ids: [1, 5]}
     ms.reset();                 // back to "all"

   Contract for the caller:
     - buttonEl must contain a child with class .ea-ms-label whose text we
       update as the user picks items.
   ======================================================================== */

(function () {
  "use strict";

  if (!window.EA) {
    console.error("multi_select.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;
  if (EA.MultiSelect) return;  // idempotent — page reload safety

  // ── Scoped CSS injected once on first create() call ──
  const STYLE_ID = "ea-ms-style";
  const STYLE = `
    .ea-ms-popover {
      position: absolute; z-index: 50; min-width: 220px; max-width: 320px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; box-shadow: 0 6px 18px rgba(0,0,0,0.30);
      padding: 8px; max-height: 320px; display: flex; flex-direction: column;
    }
    .ea-ms-popover input.ea-ms-search {
      background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); font-size: 0.82rem; padding: 5px 8px;
      border-radius: 6px; font-family: inherit; margin-bottom: 6px;
    }
    .ea-ms-list { overflow-y: auto; flex: 1; min-height: 0; }
    .ea-ms-row {
      display: flex; align-items: center; gap: 8px;
      padding: 5px 6px; border-radius: 4px; cursor: pointer;
      font-size: 0.85rem; color: var(--text);
    }
    .ea-ms-row:hover { background: var(--surface-2); }
    .ea-ms-row input { margin: 0; }
    .ea-ms-empty {
      padding: 10px; color: var(--muted); font-size: 0.82rem; text-align: center;
    }
    .ea-ms-actions {
      display: flex; gap: 8px; padding-top: 6px;
      border-top: 1px solid var(--border); margin-top: 6px;
    }
    .ea-ms-actions button {
      background: transparent; color: var(--muted); border: none;
      font-size: 0.76rem; cursor: pointer; padding: 4px 6px;
    }
    .ea-ms-actions button:hover { color: var(--text); }
  `;

  function ensureStyleInjected() {
    if (document.getElementById(STYLE_ID)) return;
    const el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = STYLE;
    document.head.appendChild(el);
  }

  // ── create() — returns a controller for one multi-select button ──
  // State shape: { all: bool, ids: Set<number> }. all=true means "All X".
  function create(button, options) {
    ensureStyleInjected();
    options = options || {};
    const allLabel = options.allLabel || "All";
    const onChange = options.onChange || function () {};
    const filterRows = options.filterRows || null;  // optional fn(item, state)

    const labelEl = button.querySelector(".ea-ms-label");
    if (!labelEl) {
      console.warn("EA.MultiSelect.create: button has no .ea-ms-label child");
    }
    // Strip leading "All " from the all-state label to derive the category
    // word used as a prefix when something IS selected: "All Locations" →
    // "Locations". Falls back to the full allLabel if the pattern doesn't match.
    const categoryLabel = /^All\s+(.+)$/i.test(allLabel)
      ? allLabel.replace(/^All\s+/i, "")
      : allLabel;
    let items = [];
    let state = { all: true, ids: new Set() };
    let popover = null;
    let searchTerm = "";

    function syncLabel() {
      if (!labelEl) return;
      if (state.all || state.ids.size === 0) {
        labelEl.textContent = allLabel;
        return;
      }
      if (state.ids.size === 1) {
        const only = items.find((it) => it.id === [...state.ids][0]);
        labelEl.textContent = categoryLabel + ": " +
          (only ? only.name : "1 selected");
        return;
      }
      labelEl.textContent = categoryLabel + ": " + state.ids.size + " selected";
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
      const list = popover.querySelector(".ea-ms-list");
      const visible = visibleItems();
      if (visible.length === 0) {
        list.innerHTML = '<div class="ea-ms-empty">No options.</div>';
        return;
      }
      list.innerHTML = visible.map((it) => {
        const checked = !state.all && state.ids.has(it.id);
        return (
          '<label class="ea-ms-row">' +
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
      popover.className = "ea-ms-popover";
      popover.innerHTML = `
        <input type="text" class="ea-ms-search" placeholder="Search…">
        <div class="ea-ms-list"></div>
        <div class="ea-ms-actions">
          <button type="button" data-act="all">Select all</button>
          <button type="button" data-act="none">Clear</button>
        </div>
      `;
      document.body.appendChild(popover);

      const rect = button.getBoundingClientRect();
      popover.style.top  = (rect.bottom + window.scrollY + 4) + "px";
      popover.style.left = (rect.left   + window.scrollX) + "px";

      const search = popover.querySelector(".ea-ms-search");
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
        // "Clear" is identical to "all" here: empty set = no filter applied
        state = { all: true, ids: new Set() };
        syncLabel();
        renderList();
        onChange(snapshot());
      });

      renderList();
      setTimeout(() => {
        document.addEventListener("click", onDocClick, true);
      }, 0);
      window.addEventListener("resize", close);
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
        const validIds = new Set(items.map((it) => it.id));
        if (!state.all) {
          [...state.ids].forEach((id) => { if (!validIds.has(id)) state.ids.delete(id); });
          if (state.ids.size === 0) state = { all: true, ids: new Set() };
        }
        syncLabel();
        if (popover) renderList();
      },
      setSelection(ids) {
        const list = Array.isArray(ids) ? ids.filter((n) => Number.isFinite(n)) : [];
        if (list.length === 0) {
          state = { all: true, ids: new Set() };
        } else {
          state = { all: false, ids: new Set(list) };
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

  EA.MultiSelect = { create };
})();
