/* multiselect.js — EA.MultiSelect.create(buttonEl, opts)

   Self-contained pill-style multi-select popover. Behaviourally and
   visually identical to the dashboard widget's private createMultiSelect
   (static/dashboard_widget.js) — deliberately a parallel, standalone copy
   so the just-verified dashboard is not touched. CONSOLIDATION DEBT: if a
   third surface needs a multi-select, promote this into the single shared
   impl and have the dashboard delegate to it too.

   opts:
     allLabel   "All Locations"        label in the cleared/all state
     onChange   fn(snapshot)           snapshot = { all:bool, ids:[..] }
   API: setItems([{id,name}]), setSelection([ids]), reset(), get(),
        refreshList(), destroy()

   ids are coerced with parseInt; use integer ids (a sentinel like -1 is
   fine for pseudo-rows e.g. the "Name Not Detected" bucket). */
(function () {
  "use strict";
  window.EA = window.EA || {};
  if (EA.MultiSelect) return;

  var STYLE = `
    .ea-ms-btn {
      background: var(--surface-2,#F4EFE7); border: 1px solid var(--border,#D8CFC2);
      color: var(--text,#2A2521); font-size: 0.875rem; padding: 8px 10px;
      border-radius: 8px; font-family: inherit; cursor: pointer;
      display: inline-flex; align-items: center; gap: 6px; width: 100%;
      min-width: 0;
    }
    .ea-ms-btn:hover { border-color: var(--accent,#4A8076); }
    .ea-ms-label {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
      text-align: left;
    }
    .ea-ms-caret { color: var(--muted,#7A6F62); font-size: 0.75rem; }
    .ea-ms-popover {
      position: absolute; z-index: 1200; min-width: 220px; max-width: 340px;
      background: var(--surface,#FFFDF9); border: 1px solid var(--border,#D8CFC2);
      border-radius: 8px; box-shadow: 0 6px 18px rgba(16,24,40,0.12);
      padding: 8px; max-height: 340px; display: flex; flex-direction: column;
    }
    .ea-ms-popover input.ea-ms-search {
      background: var(--surface-2,#F4EFE7); border: 1px solid var(--border,#D8CFC2);
      color: var(--text,#2A2521); font-size: 0.85rem; padding: 6px 8px;
      border-radius: 6px; font-family: inherit; margin-bottom: 6px;
    }
    .ea-ms-list { overflow-y: auto; flex: 1; min-height: 0; }
    .ea-ms-row {
      display: flex; align-items: center; gap: 8px;
      padding: 6px 6px; border-radius: 4px; cursor: pointer;
      font-size: 0.875rem; color: var(--text,#2A2521);
    }
    .ea-ms-row:hover { background: var(--surface-2,#F4EFE7); }
    .ea-ms-row input { margin: 0; }
    .ea-ms-empty {
      padding: 10px; color: var(--muted,#7A6F62); font-size: 0.82rem;
      text-align: center;
    }
    .ea-ms-actions {
      display: flex; gap: 8px; padding-top: 6px;
      border-top: 1px solid var(--border,#D8CFC2); margin-top: 6px;
    }
    .ea-ms-actions button {
      background: transparent; color: var(--muted,#7A6F62); border: none;
      font-size: 0.78rem; cursor: pointer; padding: 4px 6px;
    }
    .ea-ms-actions button:hover { color: var(--text,#2A2521); }
  `;

  function injectStyleOnce() {
    if (document.getElementById("ea-ms-style")) return;
    var s = document.createElement("style");
    s.id = "ea-ms-style";
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function create(button, options) {
    injectStyleOnce();
    options = options || {};
    var allLabel = options.allLabel || "All";
    var onChange = options.onChange || function () {};
    var categoryLabel = /^All\s+(.+)$/i.test(allLabel)
      ? allLabel.replace(/^All\s+/i, "") : allLabel;

    var labelEl = button.querySelector(".ea-ms-label");
    var items = [];                       // [{id,name}]
    var state = { all: true, ids: new Set() };
    var popover = null;
    var searchTerm = "";

    function syncLabel() {
      if (state.all || state.ids.size === 0) { labelEl.textContent = allLabel; return; }
      if (state.ids.size === 1) {
        var only = items.find(function (it) { return it.id === [...state.ids][0]; });
        labelEl.textContent = categoryLabel + ": " + (only ? only.name : "1 selected");
        return;
      }
      labelEl.textContent = categoryLabel + ": " + state.ids.size + " selected";
    }

    function close() {
      if (popover) { popover.remove(); popover = null; }
      document.removeEventListener("click", onDocClick, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", onOuterScroll, true);
    }
    function onOuterScroll(ev) {
      if (popover && ev.target && ev.target.nodeType === 1 &&
          (ev.target === popover || popover.contains(ev.target))) return;
      close();
    }
    function onDocClick(ev) {
      if (popover && !popover.contains(ev.target) && ev.target !== button &&
          !button.contains(ev.target)) close();
    }

    function visibleItems() {
      var term = searchTerm.trim().toLowerCase();
      var list = items;
      if (term) list = list.filter(function (it) {
        return (it.name || "").toLowerCase().indexOf(term) !== -1;
      });
      return list;
    }

    function renderList() {
      if (!popover) return;
      var list = popover.querySelector(".ea-ms-list");
      var visible = visibleItems();
      if (visible.length === 0) {
        list.innerHTML = '<div class="ea-ms-empty">No options.</div>';
        return;
      }
      list.innerHTML = visible.map(function (it) {
        var checked = !state.all && state.ids.has(it.id);
        return '<label class="ea-ms-row">' +
            '<input type="checkbox" data-id="' + it.id + '"' +
              (checked ? " checked" : "") + '>' +
            '<span>' + EA.esc(it.name || ("#" + it.id)) + '</span>' +
          '</label>';
      }).join("");
      list.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
        cb.addEventListener("change", function () {
          var id = parseInt(cb.dataset.id, 10);
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
      popover.innerHTML =
        '<input type="text" class="ea-ms-search" placeholder="Search…">' +
        '<div class="ea-ms-list"></div>' +
        '<div class="ea-ms-actions">' +
          '<button type="button" data-act="none">Clear</button>' +
        '</div>';
      document.body.appendChild(popover);
      var rect = button.getBoundingClientRect();
      popover.style.top  = (rect.bottom + window.scrollY + 4) + "px";
      popover.style.left = (rect.left + window.scrollX) + "px";

      var search = popover.querySelector(".ea-ms-search");
      search.addEventListener("input", function () {
        searchTerm = search.value; renderList();
      });
      popover.querySelector('[data-act="none"]').addEventListener("click", function () {
        state = { all: true, ids: new Set() };
        syncLabel(); renderList(); onChange(snapshot());
      });

      renderList();
      setTimeout(function () {
        document.addEventListener("click", onDocClick, true);
      }, 0);
      window.addEventListener("resize", close);
      window.addEventListener("scroll", onOuterScroll, true);
    }

    function onButtonClick(ev) {
      ev.stopPropagation();
      if (popover) close(); else open();
    }
    button.addEventListener("click", onButtonClick);

    function snapshot() {
      return { all: state.all, ids: state.all ? [] : [...state.ids] };
    }

    return {
      setItems: function (newItems) {
        items = newItems || [];
        var valid = new Set(items.map(function (it) { return it.id; }));
        if (!state.all) {
          [...state.ids].forEach(function (id) { if (!valid.has(id)) state.ids.delete(id); });
          if (state.ids.size === 0) state = { all: true, ids: new Set() };
        }
        syncLabel();
        if (popover) renderList();
      },
      setSelection: function (ids) {
        var list = Array.isArray(ids) ? ids.filter(function (n) { return Number.isFinite(n); }) : [];
        state = list.length === 0
          ? { all: true, ids: new Set() }
          : { all: false, ids: new Set(list) };
        syncLabel();
        if (popover) renderList();
      },
      reset: function () {
        state = { all: true, ids: new Set() };
        syncLabel();
        if (popover) renderList();
      },
      get: function () { return snapshot(); },
      refreshList: function () { if (popover) renderList(); },
      destroy: function () {
        close();
        button.removeEventListener("click", onButtonClick);
      },
    };
  }

  EA.MultiSelect = { create: create };
})();
