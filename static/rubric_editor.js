// EA.RubricEditor — thin orchestrator over EA.RubricListView for the items
// of a single rubric_group. Mounted by the Project Hub's Rubric tab.
//
// 1:1 contract
// ────────────
// This editor assumes the rubric_group it edits is owned by exactly one
// project, so any mutation here is safe for that project alone. The schema
// technically allows N:1 sharing (a single rg can be referenced by multiple
// projects), but as of Phase 3 of the project-centric restructure no UI
// surface exposes shared assignment — every project gets its own group.
// Callers must preserve that invariant; if shared rubrics ever return,
// this module needs an "X projects use this rubric" warning before edits.
//
// Snapshot semantics
// ──────────────────
// `interaction_rubric_scores` freezes the rubric item's name, weight, and
// score type at grade time. Editing or deleting items here only affects
// future grades — past grades remain scored against their snapshot. The UI
// surfaces this in the panel header (when `isAdmin`) and in the delete
// confirmation copy.
(function () {
  "use strict";
  const EA = (window.EA = window.EA || {});

  // Canonical view fields → API column names. The view emits onItemChange
  // with one field at a time; backend's PUT allowlist + dict-comp on body
  // keys gives us a true partial update with no extra adapter logic.
  const FIELD_TO_API = {
    name:             "ri_name",
    score_type:       "ri_score_type",
    weight:           "ri_weight",
    scoring_guidance: "ri_scoring_guidance",
  };

  function fromApi(it) {
    return {
      id:               it.rubric_item_id,
      name:             it.ri_name,
      score_type:       it.ri_score_type,
      weight:           it.ri_weight,
      scoring_guidance: it.ri_scoring_guidance,
    };
  }

  function mount(opts) {
    opts = opts || {};
    const container     = opts.container;
    const rubricGroupId = opts.rubricGroupId;
    const gradeTarget   = opts.gradeTarget || "respondent";
    const isAdmin       = !!opts.isAdmin;
    const onChange      = typeof opts.onChange === "function" ? opts.onChange : null;

    if (!container)     throw new Error("EA.RubricEditor.mount: container is required");
    if (!rubricGroupId) throw new Error("EA.RubricEditor.mount: rubricGroupId is required");

    let destroyed = false;
    let view      = null;
    let listInner = null;
    let emptyHint = null;
    let observer  = null;
    const listeners = [];

    function on(el, ev, fn) {
      if (!el) return;
      el.addEventListener(ev, fn);
      listeners.push(function () { el.removeEventListener(ev, fn); });
    }

    function shellHtml() {
      const emptyHintHtml = isAdmin
        ? '<div class="empty-state" data-role="empty-hint" hidden>' +
            'No rubric items yet. Click \u201CAdd item\u201D above to get started.' +
          '</div>'
        : '<div class="empty-state" data-role="empty-hint" hidden>' +
            'This project has no rubric items yet.' +
          '</div>';
      return (
        '<div class="rubric-editor-header">' +
          '<div class="panel-title">Rubric items</div>' +
          (isAdmin
            ? '<button type="button" class="btn btn-ghost btn-sm" data-act="add-item">Add item</button>'
            : '') +
        '</div>' +
        (isAdmin
          ? '<div class="rubric-snapshot-note">' +
              'Past grades use a snapshot of the rubric at the time of grading. ' +
              'Edits affect future grades only.' +
            '</div>'
          : '') +
        emptyHintHtml +
        '<div data-role="rubric-list-inner">' +
          '<div class="skeleton" style="height:52px;margin-bottom:8px;"></div>' +
          '<div class="skeleton" style="height:52px;margin-bottom:8px;"></div>' +
          '<div class="skeleton" style="height:52px;"></div>' +
        '</div>'
      );
    }

    function updateEmptyHint() {
      if (!emptyHint || !view) return;
      emptyHint.hidden = view.getItems().length > 0;
    }

    // ── view callbacks ───────────────────────────────────────────────
    async function handleItemChange(args) {
      const apiField = FIELD_TO_API[args.field];
      if (!apiField) return args.item;  // unknown field — drop, don't touch API
      const body = {};
      body[apiField] = args.value;
      const updated = await EA.fetchJSON(
        "/api/rubric-groups/" + rubricGroupId + "/items/" + args.id,
        { method: "PUT", body: body }
      );
      if (onChange) onChange();
      return fromApi(updated);
    }

    async function handleItemAdd(args) {
      const it = args.item;
      const created = await EA.fetchJSON(
        "/api/rubric-groups/" + rubricGroupId + "/items",
        {
          method: "POST",
          body: {
            ri_name:             it.name,
            ri_score_type:       it.score_type || "out_of_10",
            ri_weight:           it.weight != null ? it.weight : 1.0,
            ri_scoring_guidance: it.scoring_guidance || null,
          },
        }
      );
      if (onChange) onChange();
      return fromApi(created);
    }

    async function handleItemDelete(args) {
      const name = (args.item && args.item.name) || "this item";
      const ok = await EA.confirmDialog({
        title:   "Delete rubric item?",
        body:    'Past grades stay scored against the snapshot of "' + name +
                 '" — only future grades will be affected.',
        okLabel: "Delete",
        variant: "danger",
      });
      if (!ok) return false;  // silent cancel — relies on rubric_list_view.js Phase 4a
      await EA.fetchJSON(
        "/api/rubric-groups/" + rubricGroupId + "/items/" + args.id,
        { method: "DELETE" }
      );
      EA.toast("Item deleted.", "success");
      if (onChange) onChange();
    }

    async function handleReorder(orderedIds) {
      const body = orderedIds.map(function (id, idx) {
        return { rubric_item_id: id, ri_order: idx };
      });
      try {
        await EA.fetchJSON(
          "/api/rubric-groups/" + rubricGroupId + "/items/reorder",
          { method: "POST", body: body }
        );
        EA.toast("Order updated.", "success");
        if (onChange) onChange();
      } catch (err) {
        EA.toast(err.message || "Reorder failed", "error");
        // Server is the source of truth — re-sync so the view reflects
        // what's actually persisted, not the failed reorder attempt.
        await loadItems();
        throw err;
      }
    }

    async function handleGuidanceGenerate(args) {
      const resp = await EA.fetchJSON(
        "/api/rubric-groups/" + rubricGroupId + "/items/" + args.id + "/generate-guidance",
        {
          method: "POST",
          body:   { category_name: args.item.name, grade_target: gradeTarget },
        }
      );
      return resp.guidance || "";
    }

    // ── load + mount ─────────────────────────────────────────────────
    function mountView(initialItems) {
      view = EA.RubricListView.mount({
        container:          listInner,
        items:              initialItems,
        isAdmin:            isAdmin,
        enableInlineEdit:   isAdmin,
        enableReorder:      isAdmin,
        onItemChange:       handleItemChange,
        onItemAdd:          handleItemAdd,
        onItemDelete:       handleItemDelete,
        onReorder:          handleReorder,
        onGuidanceGenerate: handleGuidanceGenerate,
      });

      // The view emits no count-change event, but it mutates `listInner`'s
      // children on every add/delete/setItems and updates its internal
      // items[] before (or with) the DOM mutation. Observing childList
      // keeps the empty-state hint in sync without callback plumbing.
      observer = new MutationObserver(updateEmptyHint);
      observer.observe(listInner, { childList: true });
    }

    async function loadItems() {
      try {
        const res = await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items");
        if (destroyed) return;
        const canonical = (res || []).map(fromApi);
        if (view) view.setItems(canonical);
        else      mountView(canonical);
        updateEmptyHint();
      } catch (err) {
        if (destroyed) return;
        if (listInner) {
          listInner.innerHTML = '<div class="section-error">' +
            EA.esc(err.message || "Failed to load rubric items.") + '</div>';
        }
      }
    }

    // ── kick off ─────────────────────────────────────────────────────
    container.innerHTML = shellHtml();
    listInner = container.querySelector('[data-role="rubric-list-inner"]');
    emptyHint = container.querySelector('[data-role="empty-hint"]');

    if (isAdmin) {
      const addBtn = container.querySelector('[data-act="add-item"]');
      on(addBtn, "click", function () { if (view) view.addBlankRow(); });
    }

    loadItems();

    return {
      refresh: loadItems,
      destroy: function () {
        destroyed = true;
        listeners.forEach(function (off) { try { off(); } catch (_) {} });
        listeners.length = 0;
        if (observer) { try { observer.disconnect(); } catch (_) {} observer = null; }
        // Fire-and-forget: view.destroy() flushes pending edits internally
        // (debounced timers + inflight callbacks up to 1s).
        if (view) { try { view.destroy(); } catch (_) {} view = null; }
        container.innerHTML = "";
      },
    };
  }

  EA.RubricEditor = { mount: mount };
})();
