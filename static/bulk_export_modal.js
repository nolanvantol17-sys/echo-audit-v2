/* ========================================================================
   bulk_export_modal.js — Per-property bulk export modal.

   Exposes EA.BulkExportModal.open({
     locationId,         // required
     locationName,       // optional, for the dialog title
     lockedProject,      // true → project is fixed (Hub case); false → picker shown
     projectId,          // required when lockedProject=true
     projectName,        // optional display name for locked project
   })

   Flow:
     1. Build body DOM (project picker if !lockedProject, two toggles, stats slot).
     2. Open EA.confirmDialog with body.
     3. On project change OR toggle change → re-fetch preflight, mutate stats
        in place. Disable Download when count=0 or no project chosen.
     4. On Download click (confirmDialog resolves true) → showOverlay + trigger
        browser download via window.location, then setTimeout-close overlay.
   ======================================================================== */
(function () {
  "use strict";
  const EA = window.EA;

  // Cached projects list per page-load; first opening from Locations does
  // the fetch, subsequent openings reuse.
  let _projectsCache = null;
  // Campaigns cached per project (per page-load); rare cross-tab edits
  // require a refresh — see followup memory if this ever becomes an issue.
  const _campaignsCache = {};

  async function _loadProjects() {
    if (_projectsCache) return _projectsCache;
    _projectsCache = await EA.fetchJSON("/api/projects");
    return _projectsCache;
  }

  async function _loadCampaigns(projectId) {
    if (_campaignsCache[projectId]) return _campaignsCache[projectId];
    _campaignsCache[projectId] =
      await EA.fetchJSON("/api/projects/" + projectId + "/campaigns");
    return _campaignsCache[projectId];
  }

  function _renderStats(host, res) {
    if (!res || res.count === 0) {
      host.innerHTML =
        '<div class="muted text-small">No interactions match these filters.</div>';
      return false;
    }
    const parts = [
      '<strong>' + res.count + '</strong> interaction' + (res.count === 1 ? '' : 's'),
      res.audio_count + ' with audio',
      '~' + res.est_zip_mb + ' MB estimated',
    ];
    if (res.oldest_date && res.newest_date) {
      const range = (res.oldest_date === res.newest_date)
        ? EA.formatDate(res.oldest_date)
        : EA.formatDate(res.oldest_date) + ' → ' + EA.formatDate(res.newest_date);
      parts.push(range);
    }
    host.innerHTML =
      '<div class="text-small">' + parts.join(' &middot; ') + '</div>';
    return true;
  }

  function _setDownloadEnabled(body, enabled) {
    // Find the OK button via the modal that confirmDialog mounted.
    const backdrop = body.closest(".modal-backdrop");
    if (!backdrop) return;
    const okBtn = backdrop.querySelector('[data-act="ok"]');
    if (okBtn) okBtn.disabled = !enabled;
  }

  async function open(opts) {
    opts = opts || {};
    const locationId    = opts.locationId;
    const locationName  = opts.locationName || "Location";
    const lockedProject = !!opts.lockedProject;

    if (!locationId) { EA.toast("Missing location.", "error"); return; }
    if (lockedProject && !opts.projectId) {
      EA.toast("Missing project for export.", "error"); return;
    }

    const state = {
      projectId:     lockedProject ? opts.projectId : null,
      includeNoAns:  false,
      includeFailed: false,
      hasResults:    false,
      allCampaigns:         true,    // default: All campaigns checked
      campaignIds:          [],      // selected individual campaign IDs
      includeUncategorized: false,   // selected the "Uncategorized" pseudo-row
    };

    const body = document.createElement("div");
    const pickerHtml = lockedProject
      ? '<div class="muted text-small" style="margin-bottom:14px;">' +
          'Project: <strong>' + EA.esc(opts.projectName || ('#' + opts.projectId)) +
        '</strong></div>'
      : '<div class="field" style="margin-bottom:14px;">' +
          '<label class="field-label" for="bem-project">Project</label>' +
          '<select id="bem-project" class="field-select" data-role="project-picker">' +
            '<option value="">Loading projects…</option>' +
          '</select>' +
        '</div>';
    body.innerHTML =
      pickerHtml +
      '<div class="field" style="margin-bottom:14px;" data-role="campaigns-block" hidden>' +
        '<label class="field-label">Campaigns</label>' +
        '<div data-role="campaigns-list">' +
          '<div class="muted text-small">Loading campaigns…</div>' +
        '</div>' +
      '</div>' +
      '<div class="field" style="margin-bottom:8px;">' +
        '<label class="field-label" style="display:flex;align-items:center;gap:8px;font-weight:normal;">' +
          '<input type="checkbox" data-role="toggle-noans">' +
          'Include no-answer calls' +
        '</label>' +
      '</div>' +
      '<div class="field" style="margin-bottom:14px;">' +
        '<label class="field-label" style="display:flex;align-items:center;gap:8px;font-weight:normal;">' +
          '<input type="checkbox" data-role="toggle-failed">' +
          'Include failed grades' +
        '</label>' +
      '</div>' +
      '<div class="muted text-small" style="margin-bottom:6px;">Toggling filters will update the counts.</div>' +
      '<div data-role="stats" style="padding:10px 12px;background:var(--surface-2,#f1f5f9);border-radius:6px;">' +
        '<div class="muted text-small">Pick a project to see counts.</div>' +
      '</div>';

    function syncCampaignState() {
      state.campaignIds = [];
      state.includeUncategorized = false;
      body.querySelectorAll('.campaign-cb:checked').forEach(function (cb) {
        if (cb.dataset.uncategorized) state.includeUncategorized = true;
        else state.campaignIds.push(parseInt(cb.dataset.id, 10));
      });
    }

    function renderCampaignsList(campaigns) {
      const block = body.querySelector('[data-role="campaigns-block"]');
      const list  = body.querySelector('[data-role="campaigns-list"]');
      if (!campaigns.length) {
        // Project has no campaigns → hide entirely (current behavior preserved).
        block.hidden = true;
        state.allCampaigns = true;
        state.campaignIds = [];
        state.includeUncategorized = false;
        return;
      }
      block.hidden = false;
      let html = '<label style="display:flex;align-items:center;gap:8px;font-weight:normal;margin-bottom:6px;">' +
        '<input type="checkbox" data-role="campaign-all" checked> ' +
        '<strong>All campaigns</strong>' +
      '</label>';
      campaigns.forEach(function (c) {
        html += '<label style="display:flex;align-items:center;gap:8px;font-weight:normal;margin-bottom:4px;padding-left:18px;">' +
          '<input type="checkbox" class="campaign-cb" data-id="' + c.campaign_id + '" disabled> ' +
          EA.esc(c.campaign_name) +
        '</label>';
      });
      // Uncategorized pseudo-row — always shown when project has campaigns.
      html += '<label style="display:flex;align-items:center;gap:8px;font-weight:normal;margin-bottom:4px;padding-left:18px;font-style:italic;color:var(--muted,#64748b);">' +
        '<input type="checkbox" class="campaign-cb" data-uncategorized="1" disabled> ' +
        'Uncategorized (no campaign)' +
      '</label>';
      list.innerHTML = html;

      list.querySelector('[data-role="campaign-all"]').addEventListener("change", function (e) {
        state.allCampaigns = e.target.checked;
        list.querySelectorAll(".campaign-cb").forEach(function (cb) {
          cb.disabled = state.allCampaigns;
          if (state.allCampaigns) cb.checked = false;
        });
        syncCampaignState();
        refresh();
      });
      list.querySelectorAll(".campaign-cb").forEach(function (cb) {
        cb.addEventListener("change", function () { syncCampaignState(); refresh(); });
      });
    }

    async function refreshCampaigns() {
      const block = body.querySelector('[data-role="campaigns-block"]');
      if (!state.projectId) { block.hidden = true; return; }
      block.hidden = false;
      body.querySelector('[data-role="campaigns-list"]').innerHTML =
        '<div class="muted text-small">Loading campaigns…</div>';
      // Reset campaign selection on every project change.
      state.allCampaigns = true;
      state.campaignIds = [];
      state.includeUncategorized = false;
      try {
        const camps = await _loadCampaigns(state.projectId);
        renderCampaignsList(camps);
      } catch (err) {
        body.querySelector('[data-role="campaigns-list"]').innerHTML =
          '<div class="muted text-small">Failed to load campaigns.</div>';
      }
    }

    async function refresh() {
      const stats = body.querySelector('[data-role="stats"]');
      if (!state.projectId) {
        stats.innerHTML = '<div class="muted text-small">Pick a project to see counts.</div>';
        state.hasResults = false;
        _setDownloadEnabled(body, false);
        return;
      }
      // Campaign filter validation: if user unchecked "All campaigns" they
      // must pick at least one (campaign or Uncategorized) to proceed.
      if (!state.allCampaigns && !state.campaignIds.length && !state.includeUncategorized) {
        stats.innerHTML = '<div class="muted text-small">' +
          'Pick at least one campaign or check "All campaigns".</div>';
        state.hasResults = false;
        _setDownloadEnabled(body, false);
        return;
      }
      stats.innerHTML = '<div class="muted text-small">Checking interactions…</div>';
      _setDownloadEnabled(body, false);
      const params = new URLSearchParams({ project_id: String(state.projectId) });
      if (state.includeNoAns)  params.set("include_no_answer", "1");
      if (state.includeFailed) params.set("include_failed",    "1");
      if (!state.allCampaigns) {
        if (state.campaignIds.length)   params.set("campaign_ids", state.campaignIds.join(","));
        if (state.includeUncategorized) params.set("include_uncategorized", "1");
      }
      try {
        const res = await EA.fetchJSON(
          "/api/locations/" + locationId + "/export/preflight?" + params.toString()
        );
        const ok = _renderStats(stats, res);
        state.hasResults = ok;
        _setDownloadEnabled(body, ok);
      } catch (err) {
        stats.innerHTML = '<div class="text-small" style="color:#b91c1c;">' +
          EA.esc(err.message || "Failed to load preflight") + '</div>';
        state.hasResults = false;
        _setDownloadEnabled(body, false);
      }
    }

    body.querySelector('[data-role="toggle-noans"]').addEventListener("change", function (e) {
      state.includeNoAns = e.target.checked;
      refresh();
    });
    body.querySelector('[data-role="toggle-failed"]').addEventListener("change", function (e) {
      state.includeFailed = e.target.checked;
      refresh();
    });

    const dialogPromise = EA.confirmDialog({
      title:       "Export Calls — " + locationName,
      body:        body,
      okLabel:     "Download",
      cancelLabel: "Cancel",
    });

    _setDownloadEnabled(body, false);

    if (lockedProject) {
      refreshCampaigns().then(refresh);
    } else {
      try {
        const projects = await _loadProjects();
        const sel = body.querySelector('[data-role="project-picker"]');
        if (!projects.length) {
          sel.innerHTML = '<option value="">No projects available</option>';
        } else if (projects.length === 1) {
          // Auto-select per Q6 — still show the picker for consistent UX.
          sel.innerHTML = '<option value="' + projects[0].project_id + '">' +
                          EA.esc(projects[0].project_name) + '</option>';
          state.projectId = projects[0].project_id;
          refreshCampaigns().then(refresh);
        } else {
          sel.innerHTML = '<option value="">Select a project…</option>' +
            projects.map(function (p) {
              return '<option value="' + p.project_id + '">' +
                      EA.esc(p.project_name) + '</option>';
            }).join("");
          sel.addEventListener("change", function (e) {
            state.projectId = e.target.value ? parseInt(e.target.value, 10) : null;
            refreshCampaigns().then(refresh);
          });
        }
      } catch (err) {
        const sel = body.querySelector('[data-role="project-picker"]');
        if (sel) sel.innerHTML = '<option value="">Failed to load projects</option>';
      }
    }

    const ok = await dialogPromise;
    if (!ok) return;

    if (!state.projectId || !state.hasResults) {
      EA.toast("Nothing to export with these filters.", "error");
      return;
    }

    const params = new URLSearchParams({ project_id: String(state.projectId) });
    if (state.includeNoAns)  params.set("include_no_answer", "1");
    if (state.includeFailed) params.set("include_failed",    "1");
    if (!state.allCampaigns) {
      if (state.campaignIds.length)   params.set("campaign_ids", state.campaignIds.join(","));
      if (state.includeUncategorized) params.set("include_uncategorized", "1");
    }
    const url = "/api/locations/" + locationId + "/export?" + params.toString();

    const overlay = EA.showOverlay("Building your export — this may take up to a minute…");
    window.location = url;
    // The browser handles the attachment as a download (no navigation),
    // so beforeunload won't fire. Dismiss the overlay after 5s — by then
    // the download has begun and the browser's native UI is showing. The
    // 5s buffer (vs 2s) gives the server room to start streaming on
    // larger exports before the overlay disappears.
    setTimeout(function () { overlay.close(); }, 5000);
  }

  window.EA = window.EA || {};
  window.EA.BulkExportModal = { open: open };
})();
