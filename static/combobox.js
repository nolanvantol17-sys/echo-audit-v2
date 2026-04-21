/* ========================================================================
   combobox.js — EA.Combobox.mount(inputEl, opts)

   Searchable single-select with optional inline-create affordance. Designed
   for forms where the list is small-to-medium and the user may need to
   create a new entry without leaving the page.

     opts:
       items        — [{id, label}, ...]. id is any scalar; label is display text.
       initialValue — pre-select the item with this id (if present).
       placeholder  — input placeholder when nothing is selected.
       onSelect     — (item) => void. Fires when user picks an existing item.
       onCreate     — async (typedText) => item | null. Fires when user clicks
                      "Create '<typedText>'". Must return the new item (with an
                      id). Component appends it to the list and selects it.
       createLabel  — label template, default: "Create '{q}'".
       emptyLabel   — shown when the list is empty and input is blank.

   Returns an API object:
       {setItems, setValue, getValue, focus, destroy}

   Semantics:
     - Single-select. The input displays the selected item's label; typing
       re-opens the dropdown in filter mode. Selecting an item closes it.
     - Blank input + closed dropdown === "no selection" (getValue returns null).
     - Exactly one item is "active" while the dropdown is open (arrow nav).
     - Enter commits the active row. Escape closes without committing.
     - Click-outside closes without committing.
   ====================================================================== */

(function () {
  "use strict";
  const EA = window.EA = window.EA || {};

  function mount(inputEl, opts) {
    opts = opts || {};
    let items = Array.isArray(opts.items) ? opts.items.slice() : [];
    const onSelect = typeof opts.onSelect === "function" ? opts.onSelect : null;
    const onCreate = typeof opts.onCreate === "function" ? opts.onCreate : null;
    const createLabelTpl = opts.createLabel || "Create '{q}'";
    const emptyLabel = opts.emptyLabel || "No matches.";
    const placeholder = opts.placeholder || "";

    if (!inputEl) throw new Error("EA.Combobox.mount: missing input element");

    // Wrap the existing input so the list can be absolutely positioned
    // relative to it, independent of whatever grid layout the caller uses.
    const wrap = document.createElement("div");
    wrap.className = "typeahead-wrap ea-cb-wrap";
    inputEl.parentNode.insertBefore(wrap, inputEl);
    wrap.appendChild(inputEl);

    const list = document.createElement("div");
    list.className = "typeahead-list ea-cb-list";
    list.hidden = true;
    list.setAttribute("role", "listbox");
    wrap.appendChild(list);

    if (placeholder) inputEl.placeholder = placeholder;
    inputEl.setAttribute("autocomplete", "off");
    inputEl.setAttribute("role", "combobox");
    inputEl.setAttribute("aria-autocomplete", "list");
    inputEl.setAttribute("aria-expanded", "false");

    const state = {
      selected: null,     // the selected item object, or null
      activeIdx: -1,      // index into the currently-rendered options
      rendered: [],       // the options currently shown (filtered + maybe create row)
      open: false,
      typing: false,      // user has typed since last selection — filter active
    };

    function setValue(id) {
      if (id === null || id === undefined || id === "") {
        state.selected = null;
        inputEl.value = "";
        return;
      }
      const hit = items.find((it) => String(it.id) === String(id));
      if (hit) {
        state.selected = hit;
        inputEl.value = hit.label || "";
      }
    }

    function getValue() {
      return state.selected ? state.selected.id : null;
    }

    function setItems(next) {
      items = Array.isArray(next) ? next.slice() : [];
      // If the currently selected item is no longer in the list, drop it.
      if (state.selected) {
        const hit = items.find((it) => String(it.id) === String(state.selected.id));
        if (!hit) {
          state.selected = null;
          inputEl.value = "";
        } else {
          state.selected = hit;
          inputEl.value = hit.label || "";
        }
      }
      if (state.open) renderList();
    }

    function focus() { inputEl.focus(); }

    function openList() {
      state.open = true;
      list.hidden = false;
      inputEl.setAttribute("aria-expanded", "true");
      renderList();
    }

    function closeList() {
      state.open = false;
      state.typing = false;
      list.hidden = true;
      inputEl.setAttribute("aria-expanded", "false");
      // Restore the input text to the selected label — the filter query is
      // thrown away when the user cancels.
      inputEl.value = state.selected ? state.selected.label || "" : "";
      state.activeIdx = -1;
    }

    function currentQuery() {
      return state.typing ? (inputEl.value || "").trim() : "";
    }

    function filterItems(q) {
      if (!q) return items.slice();
      const needle = q.toLowerCase();
      return items.filter((it) => (it.label || "").toLowerCase().indexOf(needle) !== -1);
    }

    function exactMatch(q) {
      if (!q) return null;
      const needle = q.toLowerCase();
      return items.find((it) => (it.label || "").toLowerCase() === needle) || null;
    }

    function renderList() {
      const q = currentQuery();
      const filtered = filterItems(q);
      const rendered = [];

      list.innerHTML = "";
      if (!filtered.length && !q) {
        const empty = document.createElement("div");
        empty.className = "typeahead-option ea-cb-empty muted text-small";
        empty.textContent = emptyLabel;
        empty.setAttribute("aria-disabled", "true");
        list.appendChild(empty);
      } else {
        filtered.forEach(function (it, i) {
          const opt = document.createElement("div");
          opt.className = "typeahead-option ea-cb-option";
          opt.setAttribute("role", "option");
          opt.textContent = it.label || "";
          opt.dataset.idx = String(rendered.length);
          opt.addEventListener("mousedown", function (e) {
            // mousedown (not click) so it fires before the input blur that
            // would otherwise close the list. preventDefault keeps focus
            // on the input.
            e.preventDefault();
            commitSelect(it);
          });
          list.appendChild(opt);
          rendered.push({ type: "item", item: it, el: opt });
        });
      }

      // Optional "Create '<q>'" row when the query is non-empty, has no exact
      // match, and the caller opted in via onCreate. Shown even if filtered
      // has partial matches — users may want to create despite similar names.
      if (onCreate && q && !exactMatch(q)) {
        const createOpt = document.createElement("div");
        createOpt.className = "typeahead-option ea-cb-option ea-cb-create";
        createOpt.setAttribute("role", "option");
        createOpt.textContent = createLabelTpl.replace("{q}", q);
        createOpt.dataset.idx = String(rendered.length);
        createOpt.addEventListener("mousedown", function (e) {
          e.preventDefault();
          commitCreate(q);
        });
        list.appendChild(createOpt);
        rendered.push({ type: "create", query: q, el: createOpt });
      }

      state.rendered = rendered;
      // Default activeIdx to the first actionable row.
      state.activeIdx = rendered.length ? 0 : -1;
      highlight();
    }

    function highlight() {
      state.rendered.forEach(function (r, i) {
        r.el.classList.toggle("active", i === state.activeIdx);
      });
    }

    function commitSelect(item) {
      state.selected = item;
      inputEl.value = item.label || "";
      state.typing = false;
      closeList();
      if (onSelect) {
        try { onSelect(item); } catch (_) {}
      }
    }

    async function commitCreate(q) {
      if (!onCreate) return;
      // Give the caller a chance to handle errors; on failure we leave the
      // list open so the user can retry.
      let created;
      try {
        created = await onCreate(q);
      } catch (_) {
        return;
      }
      if (!created || created.id === undefined || created.id === null) return;
      // Append to items if not already present.
      if (!items.find((it) => String(it.id) === String(created.id))) {
        items.push(created);
      }
      commitSelect(created);
    }

    function moveActive(delta) {
      if (!state.rendered.length) return;
      let next = state.activeIdx + delta;
      if (next < 0) next = state.rendered.length - 1;
      if (next >= state.rendered.length) next = 0;
      state.activeIdx = next;
      highlight();
      // Keep the highlighted row visible in the scroll area.
      const el = state.rendered[next].el;
      if (el && typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ block: "nearest" });
      }
    }

    // ── Input wiring ───────────────────────────────────────
    function onInput() {
      state.typing = true;
      if (!state.open) openList();
      else renderList();
    }

    function onFocus() {
      if (!state.open) openList();
    }

    function onKeyDown(e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!state.open) { openList(); return; }
        moveActive(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!state.open) { openList(); return; }
        moveActive(-1);
      } else if (e.key === "Enter") {
        if (!state.open) return;
        e.preventDefault();
        const row = state.rendered[state.activeIdx];
        if (!row) return;
        if (row.type === "item")         commitSelect(row.item);
        else if (row.type === "create")  commitCreate(row.query);
      } else if (e.key === "Escape") {
        if (state.open) {
          e.preventDefault();
          closeList();
        }
      } else if (e.key === "Tab") {
        // Tab closes without committing — consistent with native select.
        if (state.open) closeList();
      }
    }

    function onDocMouseDown(e) {
      if (!state.open) return;
      if (wrap.contains(e.target)) return;
      closeList();
    }

    inputEl.addEventListener("input", onInput);
    inputEl.addEventListener("focus", onFocus);
    inputEl.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onDocMouseDown, true);

    // Pre-select
    if (opts.initialValue !== undefined && opts.initialValue !== null) {
      setValue(opts.initialValue);
    }

    function destroy() {
      inputEl.removeEventListener("input", onInput);
      inputEl.removeEventListener("focus", onFocus);
      inputEl.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onDocMouseDown, true);
      // Unwrap: put inputEl back where it was, remove the wrapper + list.
      if (wrap.parentNode) {
        wrap.parentNode.insertBefore(inputEl, wrap);
        wrap.parentNode.removeChild(wrap);
      }
    }

    return { setItems: setItems, setValue: setValue, getValue: getValue,
             focus: focus, destroy: destroy };
  }

  EA.Combobox = { mount: mount };
})();
