// EA.RubricEditor — self-contained editor for the items of a single
// rubric_group. Mounted into a host container by the caller (the Project Hub's
// Rubric tab today; potentially other surfaces later).
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

  const SCORE_TYPES = [
    { value: "out_of_10",      label: "1–10 scale" },
    { value: "yes_no",         label: "Yes / No" },
    { value: "yes_no_pending", label: "Yes / No / Pending" },
  ];
  const TYPE_LABELS = SCORE_TYPES.reduce(function (acc, t) {
    acc[t.value] = t.label;
    return acc;
  }, {});

  function mount(opts) {
    opts = opts || {};
    const container     = opts.container;
    const rubricGroupId = opts.rubricGroupId;
    const gradeTarget   = opts.gradeTarget || "respondent";
    const isAdmin       = !!opts.isAdmin;
    const onChange      = typeof opts.onChange === "function" ? opts.onChange : null;

    if (!container)     throw new Error("EA.RubricEditor.mount: container is required");
    if (!rubricGroupId) throw new Error("EA.RubricEditor.mount: rubricGroupId is required");

    let items = [];
    let listeners = [];
    let destroyed = false;

    function on(el, ev, fn) {
      if (!el) return;
      el.addEventListener(ev, fn);
      listeners.push(function () { el.removeEventListener(ev, fn); });
    }

    function shellHtml() {
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
        '<div data-rubric-list>' +
          '<div class="skeleton" style="height:52px;margin-bottom:8px;"></div>' +
          '<div class="skeleton" style="height:52px;margin-bottom:8px;"></div>' +
          '<div class="skeleton" style="height:52px;"></div>' +
        '</div>'
      );
    }

    async function loadItems() {
      try {
        const res = await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items");
        if (destroyed) return;
        items = res || [];
        renderList();
      } catch (err) {
        if (destroyed) return;
        const list = container.querySelector("[data-rubric-list]");
        if (list) {
          list.innerHTML = '<div class="section-error">' +
            EA.esc(err.message || "Failed to load rubric items.") + '</div>';
        }
      }
    }

    function renderList() {
      const list = container.querySelector("[data-rubric-list]");
      if (!list) return;

      if (!items.length) {
        list.innerHTML = isAdmin
          ? '<div class="empty-state">No items yet. Click \u201CAdd item\u201D to get started.</div>'
          : '<div class="empty-state">No rubric items yet.</div>';
        return;
      }

      list.innerHTML = items.map(rowHtml).join("");

      if (isAdmin) {
        list.querySelectorAll(".item-edit").forEach(function (b) {
          on(b, "click", function () { onEditItem(b.dataset.iid); });
        });
        list.querySelectorAll(".item-delete").forEach(function (b) {
          on(b, "click", function () { onDeleteItem(b.dataset.iid); });
        });
        list.querySelectorAll(".item-guide").forEach(function (b) {
          on(b, "click", function () { onGenerateGuidance(b.dataset.iid); });
        });
        wireDragDrop(list);
      }
    }

    function rowHtml(it) {
      const guidance = it.ri_scoring_guidance || "";
      const typeLabel = TYPE_LABELS[it.ri_score_type] || it.ri_score_type || "—";
      const weight = it.ri_weight != null ? it.ri_weight : "—";

      if (!isAdmin) {
        return (
          '<div class="rubric-row readonly">' +
            '<div class="rubric-row-main">' +
              '<div class="rubric-row-title">' + EA.esc(it.ri_name || "—") + '</div>' +
              '<div class="rubric-row-meta">' +
                '<span class="meta-chip">' + EA.esc(typeLabel) + '</span>' +
                '<span class="meta-chip">weight ' + EA.esc(String(weight)) + '</span>' +
              '</div>' +
              (guidance
                ? '<div class="rubric-row-guidance">' + EA.esc(guidance) + '</div>'
                : '') +
            '</div>' +
          '</div>'
        );
      }

      return (
        '<div class="rubric-row drag-row" draggable="true" data-iid="' + it.rubric_item_id + '">' +
          '<span class="drag-handle" title="Drag to reorder">\u22EE\u22EE</span>' +
          '<div class="rubric-row-main">' +
            '<div class="rubric-row-title">' + EA.esc(it.ri_name || "—") + '</div>' +
            '<div class="rubric-row-meta">' +
              '<span class="meta-chip">' + EA.esc(typeLabel) + '</span>' +
              '<span class="meta-chip">weight ' + EA.esc(String(weight)) + '</span>' +
            '</div>' +
            (guidance
              ? '<div class="rubric-row-guidance">' + EA.esc(guidance) + '</div>'
              : '') +
          '</div>' +
          '<div class="rubric-row-actions">' +
            '<button type="button" class="btn btn-ghost btn-sm item-guide"  data-iid="' + it.rubric_item_id + '">Guidance</button>' +
            '<button type="button" class="btn btn-ghost btn-sm item-edit"   data-iid="' + it.rubric_item_id + '">Edit</button>' +
            '<button type="button" class="btn btn-ghost btn-sm item-delete" data-iid="' + it.rubric_item_id + '">Delete</button>' +
          '</div>' +
        '</div>'
      );
    }

    function wireDragDrop(listEl) {
      let dragged = null;
      listEl.querySelectorAll(".drag-row").forEach(function (row) {
        on(row, "dragstart", function (e) {
          dragged = row;
          row.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
        });
        on(row, "dragend", function () {
          if (dragged) dragged.classList.remove("dragging");
          listEl.querySelectorAll(".drag-over").forEach(function (n) {
            n.classList.remove("drag-over");
          });
          dragged = null;
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
          const rect = row.getBoundingClientRect();
          const after = (e.clientY - rect.top) > rect.height / 2;
          row.parentNode.insertBefore(dragged, after ? row.nextSibling : row);
          postNewOrder(listEl);
        });
      });
    }

    async function postNewOrder(listEl) {
      const body = Array.prototype.slice.call(listEl.querySelectorAll(".drag-row")).map(
        function (row, idx) {
          return { rubric_item_id: parseInt(row.dataset.iid, 10), ri_order: idx };
        }
      );
      try {
        await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items/reorder", {
          method: "POST", body: body,
        });
        EA.toast("Order updated.", "success");
        // Reload so local `items` array matches the server's persisted order;
        // otherwise subsequent edits would hit a stale index.
        await loadItems();
        if (onChange) onChange();
      } catch (err) {
        EA.toast(err.message || "Reorder failed", "error");
        await loadItems();
      }
    }

    async function onAddItem() {
      const data = await EA.formDialog({
        title: "Add rubric item",
        fields: [
          { name: "ri_name",            label: "Name", required: true },
          { name: "ri_score_type",      label: "Score type", type: "select", required: true, options: SCORE_TYPES, value: "out_of_10" },
          { name: "ri_weight",          label: "Weight", type: "number", value: "1.0", step: "0.1", min: "0.1" },
          { name: "ri_scoring_guidance", label: "Scoring guidance", type: "textarea" },
        ],
        okLabel: "Create",
      });
      if (!data) return;
      try {
        await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items", {
          method: "POST",
          body: {
            ri_name: data.ri_name,
            ri_score_type: data.ri_score_type,
            ri_weight: parseFloat(data.ri_weight || "1.0"),
            ri_scoring_guidance: data.ri_scoring_guidance || null,
          },
        });
        await loadItems();
        EA.toast("Item added.", "success");
        if (onChange) onChange();
      } catch (err) {
        EA.toast(err.message || "Add failed", "error");
      }
    }

    async function onEditItem(itemId) {
      const it = items.find(function (x) {
        return String(x.rubric_item_id) === String(itemId);
      });
      if (!it) return;
      const data = await EA.formDialog({
        title: "Edit rubric item",
        fields: [
          { name: "ri_name",            label: "Name", required: true, value: it.ri_name },
          { name: "ri_score_type",      label: "Score type", type: "select", required: true, options: SCORE_TYPES, value: it.ri_score_type },
          { name: "ri_weight",          label: "Weight", type: "number", value: it.ri_weight, step: "0.1", min: "0.1" },
          { name: "ri_scoring_guidance", label: "Scoring guidance", type: "textarea", value: it.ri_scoring_guidance || "" },
        ],
      });
      if (!data) return;
      try {
        await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items/" + itemId, {
          method: "PUT",
          body: {
            ri_name: data.ri_name,
            ri_score_type: data.ri_score_type,
            ri_weight: parseFloat(data.ri_weight || "1.0"),
            ri_scoring_guidance: data.ri_scoring_guidance || null,
          },
        });
        await loadItems();
        EA.toast("Item saved.", "success");
        if (onChange) onChange();
      } catch (err) {
        EA.toast(err.message || "Save failed", "error");
      }
    }

    async function onDeleteItem(itemId) {
      const it = items.find(function (x) {
        return String(x.rubric_item_id) === String(itemId);
      });
      const name = (it && it.ri_name) || "this item";
      const ok = await EA.confirmDialog({
        title: "Delete rubric item?",
        body:  'Past grades stay scored against the snapshot of "' + name +
               '" — only future grades will be affected.',
        okLabel: "Delete", variant: "danger",
      });
      if (!ok) return;
      try {
        await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items/" + itemId, {
          method: "DELETE",
        });
        await loadItems();
        EA.toast("Item deleted.", "success");
        if (onChange) onChange();
      } catch (err) {
        EA.toast(err.message || "Delete failed", "error");
      }
    }

    async function onGenerateGuidance(itemId) {
      const it = items.find(function (x) {
        return String(x.rubric_item_id) === String(itemId);
      });
      if (!it) return;
      const overlay = EA.showOverlay("Generating scoring guidance…");
      try {
        const resp = await EA.fetchJSON(
          "/api/rubric-groups/" + rubricGroupId + "/items/" + itemId + "/generate-guidance",
          {
            method: "POST",
            body: { category_name: it.ri_name, grade_target: gradeTarget },
          }
        );
        overlay.close();
        const row = container.querySelector('.drag-row[data-iid="' + itemId + '"]');
        if (!row) return;

        // Drop any previous preview for this item before showing a new one.
        const existing = row.parentNode.querySelector(
          '[data-guide-for="' + itemId + '"]'
        );
        if (existing) existing.remove();

        const preview = document.createElement("div");
        preview.dataset.guideFor = itemId;
        preview.className = "rubric-guidance-preview";
        preview.innerHTML =
          '<div class="panel" style="background:var(--surface-2);">' +
            '<div class="panel-title">Generated guidance</div>' +
            '<textarea class="field-textarea" readonly></textarea>' +
            '<div class="hstack-wrap" style="margin-top:8px;">' +
              '<button type="button" class="btn btn-primary btn-sm" data-act="save-guide">Save as scoring guidance</button>' +
              '<button type="button" class="btn btn-ghost btn-sm"   data-act="dismiss-guide">Dismiss</button>' +
            '</div>' +
          '</div>';
        // Set textarea via .value to avoid any HTML interpretation of the
        // generated text — readonly textareas still render entity references
        // literally if injected as innerHTML, which would surprise users.
        preview.querySelector("textarea").value = resp.guidance || "";
        row.insertAdjacentElement("afterend", preview);

        preview.querySelector('[data-act="save-guide"]').addEventListener("click", async function () {
          try {
            await EA.fetchJSON("/api/rubric-groups/" + rubricGroupId + "/items/" + itemId, {
              method: "PUT",
              body: { ri_scoring_guidance: resp.guidance },
            });
            preview.remove();
            await loadItems();
            EA.toast("Guidance saved.", "success");
            if (onChange) onChange();
          } catch (err) {
            EA.toast(err.message || "Save failed", "error");
          }
        });
        preview.querySelector('[data-act="dismiss-guide"]').addEventListener("click", function () {
          preview.remove();
        });
      } catch (err) {
        overlay.close();
        EA.toast(err.message || "Generation failed", "error");
      }
    }

    // ── kick off ─────────────────────────────────────────────────────
    container.innerHTML = shellHtml();
    if (isAdmin) {
      const addBtn = container.querySelector('[data-act="add-item"]');
      if (addBtn) on(addBtn, "click", onAddItem);
    }
    loadItems();

    return {
      refresh: loadItems,
      destroy: function () {
        destroyed = true;
        listeners.forEach(function (off) { try { off(); } catch (_) {} });
        listeners = [];
        container.innerHTML = "";
      },
    };
  }

  EA.RubricEditor = { mount: mount };
})();
