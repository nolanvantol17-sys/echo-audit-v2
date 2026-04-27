/* ========================================================================
   bulk_export_modal.js — Per-property bulk export modal.

   Exposes:
     EA.BulkExportModal.open({...})
       Single-property export: project picker, campaigns, toggles, preflight stats,
       Download → window.location to per-property /export endpoint.

     EA.BulkExportModal.openMulti({locations: [{locationId, locationName}, ...]})
       Multi-property export (≤10 properties): aggregated preflight via
       Promise.allSettled, shared project/campaigns/toggles, Download All →
       progress modal with 3s-paced sequential <a download>.click().

   Concurrency: a single openMulti progress flow can be in flight at a time;
   subsequent calls toast and bail.
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

  // Single-flight gate for the multi-export progress flow. A second click
  // while the previous progress modal is open gets a toast and bails.
  let _multiInFlight = false;

  // First 3 names + "+N more" for compact display in the multi-modal subtitle.
  function _summarizeNames(names) {
    if (names.length <= 3) return names.join(", ");
    return names.slice(0, 3).join(", ") + " +" + (names.length - 3) + " more";
  }

  function _renderStats(host, res) {
    if (!res || res.count === 0) {
      host.innerHTML =
        '<div class="muted text-small">No interactions match these filters.</div>';
      return false;
    }
    const noAnsCt  = Number(res.no_answer_count || 0);
    const gradedCt = Number(res.graded_count || 0);
    const breakdown = (noAnsCt > 0)
      ? (' (' + gradedCt + ' graded + ' + noAnsCt + ' no-answer)')
      : '';
    const parts = [
      '<strong>' + res.count + '</strong> interaction' + (res.count === 1 ? '' : 's') + breakdown,
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
      includeNoAns:  true,
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
          '<input type="checkbox" data-role="toggle-noans" checked>' +
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

  // ── Multi-property export ─────────────────────────────────
  async function openMulti(opts) {
    if (_multiInFlight) {
      EA.toast("An export is already in progress.", "error");
      return;
    }

    opts = opts || {};
    const locations = opts.locations || [];
    if (!locations.length) {
      EA.toast("No properties selected.", "error");
      return;
    }
    if (locations.length > 10) {
      EA.toast("Maximum 10 properties at a time.", "error");
      return;
    }

    const state = {
      projectId:            null,
      includeNoAns:         true,
      includeFailed:        false,
      hasResults:           false,
      allCampaigns:         true,
      campaignIds:          [],
      includeUncategorized: false,
      perLocResults:        [],   // [{location, preflight, error}, ...]
    };

    const body = document.createElement("div");
    body.innerHTML =
      '<div class="muted text-small" style="margin-bottom:14px;">' +
        'Exporting calls from <strong>' + locations.length + ' propert' +
        (locations.length === 1 ? 'y' : 'ies') + '</strong>: ' +
        EA.esc(_summarizeNames(locations.map(function (l) { return l.locationName; }))) +
      '</div>' +
      '<div class="field" style="margin-bottom:14px;">' +
        '<label class="field-label" for="bem-multi-project">Project (applies to all)</label>' +
        '<select id="bem-multi-project" class="field-select" data-role="project-picker">' +
          '<option value="">Loading projects…</option>' +
        '</select>' +
      '</div>' +
      '<div class="field" style="margin-bottom:14px;" data-role="campaigns-block" hidden>' +
        '<label class="field-label">Campaigns</label>' +
        '<div data-role="campaigns-list">' +
          '<div class="muted text-small">Loading campaigns…</div>' +
        '</div>' +
      '</div>' +
      '<div class="field" style="margin-bottom:8px;">' +
        '<label class="field-label" style="display:flex;align-items:center;gap:8px;font-weight:normal;">' +
          '<input type="checkbox" data-role="toggle-noans" checked>' +
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

    function buildParams() {
      const params = new URLSearchParams({ project_id: String(state.projectId) });
      if (state.includeNoAns)  params.set("include_no_answer", "1");
      if (state.includeFailed) params.set("include_failed",    "1");
      if (!state.allCampaigns) {
        if (state.campaignIds.length)   params.set("campaign_ids", state.campaignIds.join(","));
        if (state.includeUncategorized) params.set("include_uncategorized", "1");
      }
      return params;
    }

    async function refresh() {
      const stats = body.querySelector('[data-role="stats"]');
      if (!state.projectId) {
        stats.innerHTML = '<div class="muted text-small">Pick a project to see counts.</div>';
        state.hasResults = false;
        _setDownloadEnabled(body, false);
        return;
      }
      if (!state.allCampaigns && !state.campaignIds.length && !state.includeUncategorized) {
        stats.innerHTML = '<div class="muted text-small">' +
          'Pick at least one campaign or check "All campaigns".</div>';
        state.hasResults = false;
        _setDownloadEnabled(body, false);
        return;
      }
      stats.innerHTML = '<div class="muted text-small">Checking interactions across ' +
        locations.length + ' propert' + (locations.length === 1 ? 'y' : 'ies') + '…</div>';
      _setDownloadEnabled(body, false);

      const params = buildParams();
      // Promise.allSettled — one property's preflight failing doesn't block others.
      const results = await Promise.allSettled(locations.map(function (l) {
        return EA.fetchJSON(
          "/api/locations/" + l.locationId + "/export/preflight?" + params.toString()
        );
      }));
      state.perLocResults = results.map(function (r, i) {
        return {
          location:  locations[i],
          preflight: r.status === "fulfilled" ? r.value : null,
          error:     r.status === "rejected"  ? ((r.reason && r.reason.message) || "Preflight failed") : null,
        };
      });

      // Aggregate counts across non-error, non-zero properties.
      let totalCount = 0, totalAudio = 0, totalSize = 0;
      let oldest = null, newest = null;
      let okCount = 0, errCount = 0, zeroCount = 0;
      state.perLocResults.forEach(function (r) {
        if (r.error)                  { errCount++;  return; }
        if (r.preflight.count === 0)  { zeroCount++; return; }
        okCount++;
        totalCount += r.preflight.count;
        totalAudio += r.preflight.audio_count;
        totalSize  += r.preflight.est_zip_mb;
        if (r.preflight.oldest_date && (!oldest || r.preflight.oldest_date < oldest)) oldest = r.preflight.oldest_date;
        if (r.preflight.newest_date && (!newest || r.preflight.newest_date > newest)) newest = r.preflight.newest_date;
      });

      const lines = [];
      if (okCount > 0) {
        const parts = [
          '<strong>' + totalCount + '</strong> interaction' + (totalCount === 1 ? '' : 's'),
          totalAudio + ' with audio',
          '~' + totalSize.toFixed(1) + ' MB estimated',
        ];
        if (oldest && newest) {
          const range = (oldest === newest)
            ? EA.formatDate(oldest)
            : EA.formatDate(oldest) + ' → ' + EA.formatDate(newest);
          parts.push(range);
        }
        lines.push('<div class="text-small">' + parts.join(' &middot; ') + '</div>');
        const subParts = [okCount + ' of ' + locations.length + ' properties have matching calls'];
        if (zeroCount) subParts.push(zeroCount + ' will be skipped');
        if (errCount)  subParts.push(errCount  + ' had preflight errors');
        lines.push('<div class="muted text-small" style="margin-top:6px;">' +
          subParts.join(' &middot; ') + '</div>');
      } else if (errCount === locations.length) {
        lines.push('<div class="text-small" style="color:#b91c1c;">' +
          'Preflight failed for all properties. Try again or pick a different project.</div>');
      } else {
        lines.push('<div class="muted text-small">No matching calls in any selected property.</div>');
      }
      stats.innerHTML = lines.join("");

      state.hasResults = okCount > 0;
      _setDownloadEnabled(body, state.hasResults);
    }

    body.querySelector('[data-role="toggle-noans"]').addEventListener("change", function (e) {
      state.includeNoAns = e.target.checked;
      refresh();
    });
    body.querySelector('[data-role="toggle-failed"]').addEventListener("change", function (e) {
      state.includeFailed = e.target.checked;
      refresh();
    });

    _multiInFlight = true;
    try {
      const dialogPromise = EA.confirmDialog({
        title:       "Export Calls — " + locations.length + " Properties",
        body:        body,
        okLabel:     "Download All",
        cancelLabel: "Cancel",
      });

      _setDownloadEnabled(body, false);

      try {
        const projects = await _loadProjects();
        const sel = body.querySelector('[data-role="project-picker"]');
        if (!projects.length) {
          sel.innerHTML = '<option value="">No projects available</option>';
        } else if (projects.length === 1) {
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

      const ok = await dialogPromise;
      if (!ok) { _multiInFlight = false; return; }
      if (!state.hasResults) {
        EA.toast("Nothing to export with these filters.", "error");
        _multiInFlight = false;
        return;
      }

      const params = buildParams();
      const rows = state.perLocResults.map(function (r) {
        let initialState;
        if (r.error)                       initialState = "error";
        else if (r.preflight.count === 0)  initialState = "skipped";
        else                               initialState = "queued";
        return {
          location: r.location,
          url:      "/api/locations/" + r.location.locationId + "/export?" + params.toString(),
          state:    initialState,
          errorMsg: r.error,
        };
      });

      _openProgressModal(rows, function () { _multiInFlight = false; });
    } catch (err) {
      _multiInFlight = false;
      throw err;
    }
  }

  function _openProgressModal(rows, onClose) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop loc-progress-backdrop";
    backdrop.innerHTML =
      '<div class="modal" style="max-width:520px;">' +
        '<h3 class="modal-title">Downloading exports</h3>' +
        '<div class="modal-body" style="margin-bottom:14px;">' +
          '<div class="muted text-small" style="margin-bottom:10px;">' +
            'Downloads continue in your browser. Some browsers may prompt for ' +
            'permission on the first file.' +
          '</div>' +
          '<div class="loc-progress-list" data-role="rows"></div>' +
        '</div>' +
        '<div class="modal-actions">' +
          '<button type="button" class="btn btn-primary" data-act="close" disabled>Working…</button>' +
        '</div>' +
      '</div>';

    const rowsHost = backdrop.querySelector('[data-role="rows"]');
    rows.forEach(function (row) {
      const div = document.createElement("div");
      div.className = "loc-progress-row";
      // Generic rows use {name}; locations rows nest {location:{locationName,locationId}}.
      const fullName = row.name
        || (row.location && (row.location.locationName || ("Location #" + row.location.locationId)))
        || "Item";
      div.innerHTML =
        '<span class="loc-progress-name" title="' + EA.esc(fullName) + '">' +
          EA.esc(fullName) +
        '</span>' +
        '<span class="loc-progress-status" data-state="' + row.state + '">' +
          _statusLabel(row.state, row.errorMsg) +
        '</span>';
      row.el = div;
      rowsHost.appendChild(div);
    });

    document.body.appendChild(backdrop);

    const closeBtn = backdrop.querySelector('[data-act="close"]');
    let alreadyClosed = false;
    function doClose() {
      if (alreadyClosed) return;
      alreadyClosed = true;
      backdrop.remove();
      if (onClose) onClose();
    }
    closeBtn.addEventListener("click", doClose);

    (async function () {
      for (const row of rows) {
        if (row.state === "skipped" || row.state === "error") continue;
        _setRowState(row, "downloading");
        _triggerDownload(row.url);
        _setRowState(row, "done");
        await _sleep(3000);
      }
      closeBtn.disabled = false;
      closeBtn.textContent = "Close";
      // Auto-close after 5s buffer; user can click Close earlier.
      setTimeout(doClose, 5000);
    })();
  }

  function _statusLabel(stateName, errorMsg) {
    switch (stateName) {
      case "queued":      return "○ Queued";
      case "downloading": return "↓ Downloading…";
      case "done":        return "✓ Downloaded";
      case "skipped":     return "○ Skipped — no matching calls";
      case "error":       return "✕ Failed — " + (errorMsg || "preflight error");
      default:            return stateName;
    }
  }

  function _setRowState(row, newState, errorMsg) {
    row.state = newState;
    const status = row.el.querySelector(".loc-progress-status");
    status.dataset.state = newState;
    status.textContent = _statusLabel(newState, errorMsg);
  }

  function _triggerDownload(url) {
    // <a download>.click() is more compatible than window.location for
    // back-to-back downloads (avoids popup-blocker false positives in
    // Chrome/Safari). The download attribute is a hint; server's
    // Content-Disposition controls the actual filename.
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { a.remove(); }, 100);
  }

  function _sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // ── Generic multi-download progress flow ──────────────────
  // Used by callers that already know the URLs to hit (no preflight, no
  // project picker). Each row maps 1:1 to a download. Shares the
  // _multiInFlight gate with openMulti — only one progress flow at a time.
  async function openProgress(opts) {
    if (_multiInFlight) {
      EA.toast("An export is already in progress.", "error");
      return;
    }
    opts = opts || {};
    const inputRows = opts.rows || [];
    if (!inputRows.length) {
      EA.toast("Nothing to export.", "error");
      return;
    }
    _multiInFlight = true;
    const rows = inputRows.map(function (r) {
      return { name: r.name, url: r.url, state: "queued" };
    });
    _openProgressModal(rows, function () { _multiInFlight = false; });
  }

  window.EA = window.EA || {};
  window.EA.BulkExportModal = {
    open:         open,
    openMulti:    openMulti,
    openProgress: openProgress,
  };
})();
