// EA.RubricListView — shared presentational module for rendering a rubric's
// items with inline editing. Mounted by both the Project wizard (step 2) and
// the Project Hub's Rubric tab. The consumer owns orchestration (REST ops,
// confirm dialogs, snapshot warnings, AI-panel layout); this module owns the
// row DOM and local field state.
//
// Canonical item shape
// ────────────────────
//   { id, name, score_type, weight, scoring_guidance }
//
// `id` is null for unpersisted rows the user just added; it becomes a real
// primary key once the consumer's `onItemAdd` callback resolves. Incoming
// items in either legacy shape (`rubric_item_id`/`ri_name`/…) are normalized
// on the way in.
//
// Debounce + save-failure UX
// ──────────────────────────
// Field edits fire `onItemChange` after a 600ms debounce (per-field), with
// two guarantees:
//
//   1. destroy() flushes all pending timers and awaits inflight callbacks up
//      to 1s, so a user who types and immediately navigates away does not
//      lose their work.
//   2. If a callback rejects, the input value is NOT reverted — the user's
//      typed text stays on screen and the row is flagged with `.row-error`;
//      the next successful save clears the flag. The consumer is expected to
//      surface a toast separately (this module only owns the inline marker).
//
// Blur-gated add
// ──────────────
// Clicking "Add" via handle.addBlankRow() inserts an empty row locally; the
// consumer's `onItemAdd` only fires when the user's first non-empty name
// blurs. Empty rows abandon silently (row ×, destroy, or refocus elsewhere).
// Backend never sees empty-name rows.
//
// Delete — cancel vs. failure
// ───────────────────────────
// onItemDelete resolving to `false` (or `Promise<false>`) is "user declined" —
// row stays, no `.row-error`. Rejecting is failure — row stays, `.row-error`
// applied. Any other resolution removes the row. This lets consumers wire
// a confirm-dialog-then-DELETE flow without a row flashing red on cancel.
(function () {
  "use strict";
  const EA = (window.EA = window.EA || {});

  const SCORE_TYPES = [
    { value: "out_of_10",      label: "0.0–9.9 scale" },
    { value: "yes_no",         label: "Yes / No" },
    { value: "yes_no_pending", label: "Yes / No / Pending" },
  ];

  const DEFAULT_DEBOUNCE_MS = 600;
  const DESTROY_FLUSH_TIMEOUT_MS = 1000;
  const FADE_OUT_MS = 200;

  function mount(opts) {
    opts = opts || {};
    const container          = opts.container;
    const isAdmin            = opts.isAdmin !== false;
    const enableReorder      = !!opts.enableReorder;
    const enableInlineEdit   = opts.enableInlineEdit !== false;
    const showGuidanceToggle = opts.showGuidanceToggle !== false;
    const changeDebounceMs   = opts.changeDebounceMs != null
                                 ? opts.changeDebounceMs
                                 : DEFAULT_DEBOUNCE_MS;

    const onItemChange       = fn(opts.onItemChange);
    const onItemDelete       = fn(opts.onItemDelete);
    const onItemAdd          = fn(opts.onItemAdd);
    const onReorder          = fn(opts.onReorder);
    const onGuidanceGenerate = fn(opts.onGuidanceGenerate);

    if (!container) throw new Error("EA.RubricListView.mount: container is required");

    let items     = (opts.items || []).map(normalizeItem);
    let destroyed = false;
    let listeners = [];

    const rowToItem      = new WeakMap();   // DOM row → item ref
    const itemToRow      = new Map();       // item ref → DOM row
    const pendingTimers  = new Map();       // "itemId:field" → { timer, fire }
    const inflight       = new Set();       // in-flight consumer promises

    // ── DOM shell ────────────────────────────────────────────────────
    container.classList.add("rubric-list");
    container.innerHTML = "";
    renderAll();

    // ── rendering ────────────────────────────────────────────────────
    function renderAll() {
      // Detach existing row listeners first (they re-register as rows rebuild).
      listeners.forEach(function (off) { try { off(); } catch (_) {} });
      listeners = [];
      itemToRow.clear();

      container.innerHTML = "";
      items.forEach(function (item) { container.appendChild(buildRow(item)); });
    }

    function buildRow(item) {
      const row = document.createElement("div");
      row.className = "rubric-item";
      if (enableReorder) row.classList.add("with-reorder");
      row.innerHTML = rowInnerHtml(item);
      rowToItem.set(row, item);
      itemToRow.set(item, row);
      wireRow(row, item);
      return row;
    }

    function rowInnerHtml(item) {
      const hasActions = showGuidanceToggle || onGuidanceGenerate;
      const guidance   = item.scoring_guidance || "";
      const readonly   = (!isAdmin || !enableInlineEdit) ? " readonly" : "";

      return (
        (enableReorder
          ? '<span class="ri-drag drag-handle" title="Drag to reorder">\u22EE\u22EE</span>'
          : "") +
        '<input class="ri-name field-input" placeholder="Criterion name"' +
          readonly + ' value="' + EA.esc(item.name || "") + '">' +
        '<select class="ri-type field-select"' + readonly + ">" +
          typeOptions(item.score_type) +
        "</select>" +
        '<input class="ri-weight field-input" type="number" min="0.1" step="0.1"' +
          readonly + ' value="' + (item.weight != null ? item.weight : 1.0) + '">' +
        (isAdmin
          ? '<button type="button" class="ri-delete" title="Delete">\u00D7</button>'
          : "") +
        (hasActions
          ? '<div class="ri-actions">' +
              (showGuidanceToggle
                ? '<button type="button" class="ri-guidance-toggle btn-linklike">' +
                    (guidance ? "Hide guidance" : "+ Guidance") +
                  "</button>"
                : "") +
              (onGuidanceGenerate
                ? '<button type="button" class="ri-ai btn-linklike" ' +
                    'title="Generate guidance with AI">\u2728 AI</button>'
                : "") +
            "</div>"
          : "") +
        '<div class="ri-guidance"' + (guidance ? "" : " hidden") + ">" +
          '<textarea rows="2" placeholder="Scoring guidance — describe what high vs low looks like"' +
            (readonly ? " readonly" : "") + ">" +
            EA.esc(guidance) +
          "</textarea>" +
        "</div>"
      );
    }

    function typeOptions(current) {
      return SCORE_TYPES.map(function (t) {
        return '<option value="' + t.value + '"' +
               (current === t.value ? " selected" : "") + ">" + t.label + "</option>";
      }).join("");
    }

    // ── per-row wiring ───────────────────────────────────────────────
    function wireRow(row, item) {
      const nameEl   = row.querySelector(".ri-name");
      const typeEl   = row.querySelector(".ri-type");
      const weightEl = row.querySelector(".ri-weight");
      const delEl    = row.querySelector(".ri-delete");
      const togEl    = row.querySelector(".ri-guidance-toggle");
      const aiEl     = row.querySelector(".ri-ai");
      const guideBox = row.querySelector(".ri-guidance");
      const guideTA  = row.querySelector(".ri-guidance textarea");
      const dragEl   = row.querySelector(".ri-drag");

      if (nameEl && enableInlineEdit) {
        on(nameEl, "input", function (e) {
          item.name = e.target.value;
          if (item.id != null) scheduleChange(item, "name");
        });
        // Blur-gated persist for unpersisted rows.
        on(nameEl, "blur", function () {
          if (destroyed) return;
          if (item.id == null && item.name && item.name.trim() && !item._adding && !item._pendingDelete) {
            fireAdd(item);
          }
        });
      }
      if (typeEl && enableInlineEdit) {
        on(typeEl, "change", function (e) {
          item.score_type = e.target.value;
          if (item.id != null) scheduleChange(item, "score_type");
        });
      }
      if (weightEl && enableInlineEdit) {
        on(weightEl, "input", function (e) {
          const v = parseFloat(e.target.value);
          item.weight = isFinite(v) && v > 0 ? v : 1.0;
          if (item.id != null) scheduleChange(item, "weight");
        });
      }

      if (delEl) {
        // mousedown fires before the name input's blur, letting us short-circuit
        // the blur-gated add path for unpersisted rows the user is bailing on.
        on(delEl, "mousedown", function () { item._pendingDelete = true; });
        on(delEl, "click", function () { deleteRow(item); });
      }

      if (togEl) {
        on(togEl, "click", function () {
          const hidden = guideBox.hidden;
          guideBox.hidden = !hidden;
          togEl.textContent = hidden ? "Hide guidance" : "+ Guidance";
          if (!hidden) return;
          // Defer focus until after the hidden attribute flip paints.
          setTimeout(function () { if (guideTA) guideTA.focus(); }, 0);
        });
      }

      if (guideTA && enableInlineEdit) {
        on(guideTA, "input", function (e) {
          item.scoring_guidance = e.target.value;
          if (item.id != null) scheduleChange(item, "scoring_guidance");
        });
      }

      if (aiEl) {
        on(aiEl, "click", function () { triggerGuidanceAI(item); });
        // AI requires a persisted id (endpoint is /items/:id/generate-guidance).
        if (item.id == null) aiEl.disabled = true;
      }

      if (enableReorder && dragEl) wireDragRow(row, dragEl);
    }

    // ── debounced change ─────────────────────────────────────────────
    function scheduleChange(item, field) {
      if (!onItemChange || item.id == null) return;
      const key = item.id + ":" + field;
      const existing = pendingTimers.get(key);
      if (existing) clearTimeout(existing.timer);

      const entry = { timer: null, fire: null };
      entry.fire = function () {
        pendingTimers.delete(key);
        if (destroyed && !_flushing) return;
        const p = Promise.resolve()
          .then(function () {
            return onItemChange({ id: item.id, field: field, value: item[field], item: item });
          })
          .then(function (updated) {
            if (destroyed) return;
            if (updated) Object.assign(item, normalizeItem(updated));
            clearRowError(item);
          })
          .catch(function () {
            if (destroyed) return;
            setRowError(item);
          })
          .then(function () { inflight.delete(p); });
        inflight.add(p);
      };
      entry.timer = setTimeout(entry.fire, changeDebounceMs);
      pendingTimers.set(key, entry);
    }

    // ── add (blur-gated) ─────────────────────────────────────────────
    function fireAdd(item) {
      if (!onItemAdd) return;
      item._adding = true;
      const p = Promise.resolve()
        .then(function () { return onItemAdd({ item: item }); })
        .then(function (persisted) {
          delete item._adding;
          if (destroyed) return;
          if (persisted) {
            Object.assign(item, normalizeItem(persisted));
            // Enable the per-row AI button now that we have an id.
            const row = itemToRow.get(item);
            const ai  = row && row.querySelector(".ri-ai");
            if (ai) ai.disabled = false;
            clearRowError(item);
            // Any field the user changed during the add round-trip couldn't
            // schedule because item.id was null; flush them now.
            flushItemFields(item);
          }
        })
        .catch(function () {
          delete item._adding;
          if (destroyed) return;
          setRowError(item);
        })
        .then(function () { inflight.delete(p); });
      inflight.add(p);
    }

    function flushItemFields(item) {
      ["name", "score_type", "weight", "scoring_guidance"].forEach(function (f) {
        scheduleChange(item, f);
      });
    }

    // ── delete ───────────────────────────────────────────────────────
    function deleteRow(item) {
      const row = itemToRow.get(item);
      // Unpersisted row: wipe locally, skip consumer callback.
      if (item.id == null) {
        removeItemLocally(item);
        return;
      }
      if (!onItemDelete) { removeItemLocally(item); return; }
      const p = Promise.resolve()
        .then(function () { return onItemDelete({ id: item.id, item: item }); })
        .then(function (result) {
          if (destroyed) return;
          // Consumer returned `false` — silent cancel (e.g. dismissed confirm).
          // Keep the row, no error state.
          if (result === false) {
            delete item._pendingDelete;
            return;
          }
          removeItemLocally(item);
        })
        .catch(function () {
          // Consumer rejected — API error or other failure. Keep row, flag.
          delete item._pendingDelete;
          if (destroyed) return;
          setRowError(item);
        })
        .then(function () { inflight.delete(p); });
      inflight.add(p);
    }

    function removeItemLocally(item) {
      const row = itemToRow.get(item);
      const idx = items.indexOf(item);
      if (idx >= 0) items.splice(idx, 1);
      itemToRow.delete(item);
      if (row) {
        rowToItem.delete(row);
        if (row.parentNode) row.parentNode.removeChild(row);
      }
    }

    // ── guidance AI ──────────────────────────────────────────────────
    function triggerGuidanceAI(item) {
      if (!onGuidanceGenerate || item.id == null) return;
      const overlay = EA.showOverlay ? EA.showOverlay("Generating scoring guidance\u2026") : null;
      const p = Promise.resolve()
        .then(function () { return onGuidanceGenerate({ id: item.id, item: item }); })
        .then(function (text) {
          if (overlay) overlay.close();
          if (destroyed) return;
          if (typeof text !== "string") return;
          item.scoring_guidance = text;
          const row = itemToRow.get(item);
          if (!row) return;
          const ta  = row.querySelector(".ri-guidance textarea");
          const box = row.querySelector(".ri-guidance");
          const tog = row.querySelector(".ri-guidance-toggle");
          if (ta)  ta.value = text;
          if (box) box.hidden = false;
          if (tog) tog.textContent = "Hide guidance";
          // Persist immediately — bypass debounce for this discrete event.
          if (onItemChange) {
            const fp = Promise.resolve()
              .then(function () {
                return onItemChange({
                  id: item.id, field: "scoring_guidance",
                  value: text, item: item,
                });
              })
              .catch(function () { if (!destroyed) setRowError(item); })
              .then(function () { inflight.delete(fp); });
            inflight.add(fp);
          }
        })
        .catch(function () {
          if (overlay) overlay.close();
          // Consumer is expected to surface a toast.
        });
      // Not tracked in inflight — overlay + toast handle user feedback and we
      // don't want destroy() to block for a generation round-trip.
      return p;
    }

    // ── reorder (drag-and-drop) ──────────────────────────────────────
    let dragged = null;

    function wireDragRow(row, handle) {
      row.draggable = false;
      on(handle, "mousedown", function () { row.draggable = true; });
      on(row, "mouseup", function () { row.draggable = false; });

      on(row, "dragstart", function (e) {
        dragged = row;
        row.classList.add("dragging");
        if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
      });
      on(row, "dragend", function () {
        if (dragged) dragged.classList.remove("dragging");
        container.querySelectorAll(".drag-over").forEach(function (n) {
          n.classList.remove("drag-over");
        });
        dragged = null;
        row.draggable = false;
      });
      on(row, "dragover", function (e) {
        e.preventDefault();
        if (!dragged || row === dragged) return;
        row.classList.add("drag-over");
      });
      on(row, "dragleave", function () { row.classList.remove("drag-over"); });
      on(row, "drop", function (e) {
        e.preventDefault();
        row.classList.remove("drag-over");
        if (!dragged || row === dragged) return;
        const rect  = row.getBoundingClientRect();
        const after = (e.clientY - rect.top) > rect.height / 2;
        row.parentNode.insertBefore(dragged, after ? row.nextSibling : row);
        commitReorderFromDom();
      });
    }

    function commitReorderFromDom() {
      const rows     = Array.prototype.slice.call(container.querySelectorAll(".rubric-item"));
      const newItems = rows.map(function (r) { return rowToItem.get(r); }).filter(Boolean);
      items = newItems;
      if (!onReorder) return;
      const orderedIds = newItems.map(function (it) { return it.id; }).filter(function (id) { return id != null; });
      const p = Promise.resolve()
        .then(function () { return onReorder(orderedIds); })
        .catch(function () { /* consumer toasts; leave DOM as-is */ })
        .then(function () { inflight.delete(p); });
      inflight.add(p);
    }

    // ── error state on rows ──────────────────────────────────────────
    function setRowError(item) {
      const row = itemToRow.get(item);
      if (row) row.classList.add("row-error");
    }
    function clearRowError(item) {
      const row = itemToRow.get(item);
      if (row) row.classList.remove("row-error");
    }

    // ── utilities ────────────────────────────────────────────────────
    function on(el, ev, handler) {
      if (!el) return;
      el.addEventListener(ev, handler);
      listeners.push(function () { el.removeEventListener(ev, handler); });
    }

    function fn(x) { return typeof x === "function" ? x : null; }

    function normalizeItem(raw) {
      if (!raw) return { id: null, name: "", score_type: "out_of_10", weight: 1.0, scoring_guidance: "" };
      return {
        id:               raw.id != null ? raw.id :
                          (raw.rubric_item_id != null ? raw.rubric_item_id : null),
        name:             raw.name != null ? raw.name : (raw.ri_name || ""),
        score_type:       raw.score_type || raw.ri_score_type || "out_of_10",
        weight:           raw.weight != null ? raw.weight :
                          (raw.ri_weight != null ? raw.ri_weight : 1.0),
        scoring_guidance: raw.scoring_guidance != null ? raw.scoring_guidance :
                          (raw.ri_scoring_guidance || ""),
      };
    }

    // ── handle API ───────────────────────────────────────────────────
    function setItems(newItems) {
      items = (newItems || []).map(normalizeItem);
      renderAll();
    }

    function appendItem(raw) {
      const item = normalizeItem(raw);
      items.push(item);
      container.appendChild(buildRow(item));
      return item;
    }

    function addBlankRow() {
      const item = normalizeItem({});
      items.push(item);
      const row = buildRow(item);
      container.appendChild(row);
      const nameEl = row.querySelector(".ri-name");
      if (nameEl) nameEl.focus();
      return item;
    }

    function fadeOutAll() {
      const rows = container.querySelectorAll(".rubric-item");
      rows.forEach(function (r) { r.classList.add("fading-out"); });
      return new Promise(function (resolve) { setTimeout(resolve, FADE_OUT_MS); });
    }

    function getItems() {
      return items.map(function (it) {
        return {
          id: it.id,
          name: it.name,
          score_type: it.score_type,
          weight: it.weight,
          scoring_guidance: it.scoring_guidance,
        };
      });
    }

    function setBusy(busy) {
      container.classList.toggle("is-busy", !!busy);
      container.querySelectorAll("input, select, textarea, button").forEach(function (el) {
        el.disabled = !!busy;
      });
    }

    function showThinking(text) {
      hideThinking();
      const el = document.createElement("div");
      el.className = "rubric-thinking";
      el.dataset.role = "thinking";
      el.textContent = text || "Claude is thinking\u2026";
      container.appendChild(el);
    }
    function hideThinking() {
      const el = container.querySelector('[data-role="thinking"]');
      if (el) el.remove();
    }

    function setStatus(text) {
      let el = container.querySelector('[data-role="status"]');
      if (!text) { if (el) el.remove(); return; }
      if (!el) {
        el = document.createElement("div");
        el.className = "rubric-status";
        el.dataset.role = "status";
        container.appendChild(el);
      }
      el.textContent = text;
    }

    function refresh() { renderAll(); }

    // ── destroy w/ debounce flush ────────────────────────────────────
    let _flushing = false;
    async function destroy() {
      if (destroyed) return;
      destroyed  = true;
      _flushing  = true;

      // Fire pending debounced timers immediately so the consumer's saves run.
      pendingTimers.forEach(function (entry) {
        try { clearTimeout(entry.timer); } catch (_) {}
        try { entry.fire(); } catch (_) {}
      });
      pendingTimers.clear();

      // Await inflight (including freshly-fired ones) with a hard ceiling.
      const pending = Array.from(inflight);
      if (pending.length) {
        await Promise.race([
          Promise.allSettled(pending),
          new Promise(function (r) { setTimeout(r, DESTROY_FLUSH_TIMEOUT_MS); }),
        ]);
      }

      _flushing = false;

      listeners.forEach(function (off) { try { off(); } catch (_) {} });
      listeners = [];
      itemToRow.clear();
      container.innerHTML = "";
      container.classList.remove("rubric-list", "is-busy");
    }

    return {
      setItems:     setItems,
      appendItem:   appendItem,
      addBlankRow:  addBlankRow,
      fadeOutAll:   fadeOutAll,
      getItems:     getItems,
      setBusy:      setBusy,
      showThinking: showThinking,
      hideThinking: hideThinking,
      setStatus:    setStatus,
      refresh:      refresh,
      destroy:      destroy,
    };
  }

  EA.RubricListView = { mount: mount, SCORE_TYPES: SCORE_TYPES };
})();
