/* ========================================================================
   interaction_view.js — Shared render module for the interaction detail body.

   Exposes EA.InteractionView.render(host, data, opts):
     - host: DOM element whose innerHTML will be replaced
     - data: payload from GET /api/interactions/<id>
     - opts:
         readOnly   — when true, hide the reviewer-context regrade collapsible
                      (panel mode; the standalone page is the escape hatch).
                      Selects panel-mode-vs-page-mode UI.
         canRegrade — when true (and !readOnly), render the reviewer-context
                      regrade affordance. This is a UI gate only; the submit
                      endpoint (/api/interactions/<id>/regrade-with-context)
                      is independently role-gated server-side. Pass the
                      result of a Jinja-time role check on the page that
                      hosts the render call. Default false.
         canHardDelete — when true, render the danger-zone "Delete
                      permanently" affordance. UI gate only; the DELETE
                      /api/interactions/<id>/hard endpoint is independently
                      role-gated (admin + super_admin) server-side. Default
                      false. Caller queries `[data-role="hard-delete-interaction"]`
                      post-render and wires it to EA.hardDeleteInteractionFlow.

   Pure rendering — no event wiring. Callers attach handlers by querying the
   host after render (e.g. document.getElementById('btn-context-regrade')).

   Also exposes EA.hardDeleteInteractionFlow(interactionId, opts):
     Pre-fetches deletion impact, shows two-step strong-confirm dialog,
     issues DELETE on success, dispatches `ea:interaction-deleted` event,
     and invokes opts.onSuccess. Use to wire the danger-zone button.
   ======================================================================== */

(function () {
  "use strict";
  const EA = window.EA;

  function render(host, data, opts) {
    opts = opts || {};
    const readOnly = !!opts.readOnly;
    const canRegrade = !!opts.canRegrade;
    const canHardDelete = !!opts.canHardDelete;
    const canEditTestFlag = !!opts.canEditTestFlag;
    const d = data;
    const interactionId = d.interaction_id;

    // Status=44 (no_answer) renders a slim variant — no rubric/strengths/
    // weaknesses/overall/regrade panels. Those would otherwise show empty
    // dashes ("—") that read as a broken graded call rather than the
    // correct "no human ever spoke" state.
    const STATUS_NO_ANSWER = 44;
    const isNoAnswer = (d.status_id === STATUS_NO_ANSWER);

    const regradeBadge = (d.interaction_regrade_count > 0)
      ? ('<span class="regrade-badge" title="Original score: ' +
           EA.formatScore(d.interaction_original_score) + '">' +
           '↻ Regraded ' + d.interaction_regrade_count + 'x · was ' +
           EA.formatScore(d.interaction_original_score) +
         '</span>')
      : "";

    const statusName = EA.statusIdToName(d.status_id);
    const statusCls  = "status-" + EA.statusIdToSlug(d.status_id);

    const flagsHtml = (d.interaction_flags || "")
      ? '<div class="flag-banner">' + EA.esc(d.interaction_flags) + '</div>' : "";

    const regradedBanner = d.interaction_regraded_with_context
      ? ('<div class="score-diff-banner" style="background:var(--accent-soft);border-color:rgba(37,99,235,0.25);color:#93c5fd;">' +
           '<strong>Regraded with reviewer context.</strong>' +
           (d.interaction_reviewer_context
             ? '<div class="text-small" style="margin-top:6px;white-space:pre-wrap;">' +
                 EA.esc(d.interaction_reviewer_context) +
               '</div>' : '') +
         '</div>')
      : "";

    const callTime = EA.formatCallTime(
      d.interaction_call_start_time,
      d.interaction_uploaded_at,
      null,
    );
    const durSecs = d.interaction_call_duration_seconds;
    const durLabel = (durSecs && Number(durSecs) > 0)
      ? (Math.floor(Number(durSecs) / 60) + "m " + (Number(durSecs) % 60) + "s")
      : "—";

    const hero =
      '<div class="score-hero">' +
        '<div class="score-hero-value" style="color:' +
          EA.scoreColor(d.interaction_overall_score) + ';">' +
          EA.formatScore(d.interaction_overall_score) +
        '</div>' +
        '<div class="score-hero-meta">' +
          '<span class="score-hero-label">Total score</span>' +
          '<span class="text-small muted">' +
            [d.project_name, d.phone_routing_name, d.location_name]
              .filter(Boolean).map(EA.esc).join(" · ") +
          '</span>' +
          '<span class="text-small muted">' +
            EA.esc(EA.formatDate(d.interaction_date)) +
            ' · Call time: ' + EA.esc(callTime) +
            ' · Duration: ' + EA.esc(durLabel) +
          '</span>' +
          '<span class="text-small muted">' +
            'Respondent: ' + EA.esc(d.respondent_name || "—") +
            ' · Caller: ' + EA.esc(d.caller_name || "—") +
          '</span>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end;">' +
          '<span class="status-pill ' + statusCls + '">' + EA.esc(statusName) + '</span>' +
          (d.interaction_is_test
            ? '<span class="status-pill status-test" title="Test call — excluded from dashboards & reports">🧪 TEST</span>'
            : '') +
          regradeBadge +
          '<a class="btn btn-ghost btn-sm" ' +
             'href="/api/interactions/' + interactionId + '/export" download>' +
            '↓ Export ZIP' +
          '</a>' +
          (canEditTestFlag
            ? '<button type="button" class="btn btn-ghost btn-sm" ' +
                 'data-role="toggle-test-flag" ' +
                 'data-interaction-id="' + EA.esc(String(interactionId)) + '" ' +
                 'data-is-test="' + (d.interaction_is_test ? 'true' : 'false') + '">' +
                 (d.interaction_is_test ? 'Unmark as test' : '🧪 Mark as test') +
               '</button>'
            : '') +
        '</div>' +
      '</div>';

    // No-answer variant: same .score-hero structure (no layout shift) but
    // with literal "No Answer" in muted color in the value slot, no
    // "Total score" label, no regrade badge.
    const noAnswerHero =
      '<div class="score-hero">' +
        '<div class="score-hero-value" style="color:var(--muted);font-size:1.4rem;">' +
          'No Answer' +
        '</div>' +
        '<div class="score-hero-meta">' +
          '<span class="text-small muted">' +
            [d.project_name, d.phone_routing_name, d.location_name]
              .filter(Boolean).map(EA.esc).join(" · ") +
          '</span>' +
          '<span class="text-small muted">' +
            EA.esc(EA.formatDate(d.interaction_date)) +
            ' · Call time: ' + EA.esc(callTime) +
            ' · Duration: ' + EA.esc(durLabel) +
          '</span>' +
          '<span class="text-small muted">' +
            'Respondent: ' + EA.esc(d.respondent_name || "—") +
            ' · Caller: ' + EA.esc(d.caller_name || "—") +
          '</span>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end;">' +
          '<span class="status-pill ' + statusCls + '">' + EA.esc(statusName) + '</span>' +
          (d.interaction_is_test
            ? '<span class="status-pill status-test" title="Test call — excluded from dashboards & reports">🧪 TEST</span>'
            : '') +
          '<a class="btn btn-ghost btn-sm" ' +
             'href="/api/interactions/' + interactionId + '/export" download>' +
            '↓ Export ZIP' +
          '</a>' +
          (canEditTestFlag
            ? '<button type="button" class="btn btn-ghost btn-sm" ' +
                 'data-role="toggle-test-flag" ' +
                 'data-interaction-id="' + EA.esc(String(interactionId)) + '" ' +
                 'data-is-test="' + (d.interaction_is_test ? 'true' : 'false') + '">' +
                 (d.interaction_is_test ? 'Unmark as test' : '🧪 Mark as test') +
               '</button>'
            : '') +
        '</div>' +
      '</div>';

    const rubricScores = d.rubric_scores || [];
    const rubricHtml = rubricScores.length
      ? rubricScores.map(renderRubricRow).join("")
      : '<div class="empty-state">No rubric scores recorded.</div>';

    const strengthsHtml = bulletize(d.interaction_strengths);
    const weaknessesHtml = bulletize(d.interaction_weaknesses);
    const overallHtml = d.interaction_overall_assessment
      ? '<p style="line-height:1.55;">' + EA.esc(d.interaction_overall_assessment) + '</p>'
      : '<div class="muted text-small">—</div>';

    const cqHtml = renderClarifyingQuestions(d.clarifying_questions || []);

    const transcriptHtml = d.interaction_transcript
      ? '<details class="collapsible">' +
          '<summary>Transcript</summary>' +
          '<div class="collapsible-body"><div class="transcript-body">' +
            EA.esc(d.interaction_transcript) +
          '</div></div>' +
        '</details>' : '';

    // Audio rendering — three states:
    //  1. audio_url present → render player immediately (existing behavior)
    //  2. audio_url missing AND status=graded → loading placeholder + start
    //     a 30s poll for the audio to arrive (handles AI-shop audio_fetcher
    //     race where the user clicks the dock row before the daemon thread
    //     finishes pulling the recording from ElevenLabs)
    //  3. audio_url missing AND status=no_answer → omit (correct: no audio)
    const STATUS_GRADED = 43;
    let audioHtml = "";
    if (d.interaction_audio_url) {
      audioHtml = '<div style="margin-top:14px;">' +
          EA.AudioPlayer.html({
            src: '/api/interactions/' + interactionId + '/audio',
            preload: 'none',
          }) +
        '</div>';
    } else if (d.status_id === STATUS_GRADED) {
      audioHtml = '<div id="audio-pending-' + interactionId + '" ' +
                  'style="margin-top:14px;padding:12px 14px;background:var(--surface-2);' +
                  'border:1px solid var(--border);border-radius:6px;">' +
                  '<span class="muted text-small">' +
                    '⏳ Audio is being fetched — this can take a few seconds for new calls.' +
                  '</span></div>';
    }

    const contextPanelHtml = (!readOnly && canRegrade) ? renderContextPanel(d) : '';

    const hardDeleteHtml = canHardDelete
      ? '<div class="panel" style="margin-top:18px;border-color:rgba(220,38,38,0.35);">' +
          '<div class="panel-title" style="color:#ef4444;">Danger zone</div>' +
          '<div class="muted text-small" style="margin-bottom:10px;">' +
            'Permanently delete this interaction and everything attached to it ' +
            '(rubric scores, audio file, audit-log entries). ' +
            'A deletion receipt is preserved for accountability — the content itself is not.' +
          '</div>' +
          '<button type="button" class="btn btn-danger btn-sm" ' +
            'data-role="hard-delete-interaction" ' +
            'data-interaction-id="' + EA.esc(String(interactionId)) + '">' +
            'Delete permanently…' +
          '</button>' +
        '</div>'
      : '';

    // No-answer rows skip the rubric / strengths / weaknesses / overall /
    // context panels — those would otherwise render as empty dashes that
    // read like a broken graded call. Transcript + audio + hard-delete
    // still show when applicable; the slim hero replaces the score hero.
    const gradedPanelsHtml = isNoAnswer ? '' : (
      '<div class="panel" style="margin-bottom:14px;">' +
        '<div class="panel-title">Rubric Scores</div>' +
        rubricHtml +
      '</div>' +
      '<div class="dash-grid">' +
        '<div class="panel">' +
          '<div class="panel-title">Strengths</div>' + strengthsHtml +
        '</div>' +
        '<div class="panel">' +
          '<div class="panel-title">Weaknesses</div>' + weaknessesHtml +
        '</div>' +
      '</div>' +
      '<div class="panel" style="margin-top:14px;">' +
        '<div class="panel-title">Overall Assessment</div>' + overallHtml +
      '</div>'
    );

    host.innerHTML =
      regradedBanner +
      (isNoAnswer ? noAnswerHero : hero) +
      flagsHtml +
      gradedPanelsHtml +
      cqHtml +
      (isNoAnswer ? '' : contextPanelHtml) +
      (transcriptHtml ? '<div style="margin-top:14px;">' + transcriptHtml + '</div>' : '') +
      audioHtml +
      hardDeleteHtml;

    EA.AudioPlayer.attachAll(host);
    pollForAudio(host, interactionId);
  }

  // Poll for audio that's still being fetched async (AI shop race).
  // No-op when no #audio-pending-<id> placeholder exists in the host (i.e.
  // audio is already present, or the call is no_answer with no audio
  // expected). 10 attempts × 3s = 30s upper bound, then settles on
  // "Audio unavailable" so the loading state can't linger forever. Auto-
  // stops if the placeholder is removed from the DOM (PageRouter swap, etc).
  function pollForAudio(host, interactionId) {
    const placeholder = host.querySelector("#audio-pending-" + interactionId);
    if (!placeholder) return;

    const MAX_ATTEMPTS = 10;
    const INTERVAL_MS  = 3000;
    let attempts = 0;

    const tick = async function () {
      if (!document.body.contains(placeholder)) return;   // user navigated away
      attempts += 1;
      try {
        const fresh = await EA.fetchJSON("/api/interactions/" + interactionId);
        if (fresh && fresh.interaction_audio_url) {
          placeholder.outerHTML =
            '<div style="margin-top:14px;">' +
              EA.AudioPlayer.html({
                src: '/api/interactions/' + interactionId + '/audio',
                preload: 'none',
              }) +
            '</div>';
          EA.AudioPlayer.attachAll(host);
          return;
        }
      } catch (_) { /* swallow; retry next tick */ }

      if (attempts >= MAX_ATTEMPTS) {
        placeholder.innerHTML =
          '<span class="muted text-small">' +
            '✕ Audio unavailable — recording was not captured for this call.' +
          '</span>';
        return;
      }
      setTimeout(tick, INTERVAL_MS);
    };
    setTimeout(tick, INTERVAL_MS);
  }

  function renderRubricRow(r) {
    const snapType = r.irs_snapshot_score_type || "out_of_10";
    let scoreText, scoreCls = "";
    if (snapType === "yes_no" || snapType === "yes_no_pending") {
      const n = Number(r.irs_score_value);
      if (n >= 9.5)      { scoreText = "Yes";     scoreCls = "yes"; }
      else if (n <= 0.5) { scoreText = "No";      scoreCls = "no"; }
      else               { scoreText = "Pending"; }
    } else {
      scoreText = EA.formatScore(r.irs_score_value);
    }
    return (
      '<div class="rubric-row">' +
        '<div class="rubric-name">' + EA.esc(r.irs_snapshot_name) + '</div>' +
        '<div class="rubric-score ' + scoreCls + '">' + EA.esc(scoreText) + '</div>' +
        (r.irs_score_ai_explanation
          ? '<div class="rubric-explanation">' + EA.esc(r.irs_score_ai_explanation) + '</div>'
          : '') +
      '</div>'
    );
  }

  function bulletize(text) {
    if (!text) return '<div class="muted text-small">—</div>';
    const items = String(text).split(/\n+/)
      .map(function (s) { return s.replace(/^\s*[•\-*]\s*/, "").trim(); })
      .filter(Boolean);
    if (!items.length) return '<div class="muted text-small">—</div>';
    return '<ul class="bullet-list">' +
      items.map(function (s) { return '<li>' + EA.esc(s) + '</li>'; }).join("") +
      '</ul>';
  }

  function renderClarifyingQuestions(cqs) {
    if (!cqs.length) return '';
    const rows = cqs.map(function (q) {
      const answer = (q.cq_answer_value !== null && q.cq_answer_value !== undefined && q.cq_answer_value !== "")
        ? '<span class="cq-given-answer">' + EA.esc(q.cq_answer_value) + '</span>'
        : '<span class="muted text-small">Not answered</span>';
      return (
        '<div class="cq-row">' +
          '<div class="cq-question">' + EA.esc(q.cq_text) + '</div>' +
          (q.cq_ai_reason ? '<div class="cq-reason">' + EA.esc(q.cq_ai_reason) + '</div>' : '') +
          '<div class="cq-answer">' +
            '<span class="muted text-small">Answer:</span> ' + answer +
            ' <span class="muted text-small">(' + EA.esc(q.cq_response_format) + ')</span>' +
          '</div>' +
        '</div>'
      );
    }).join("");
    return (
      '<div class="panel" style="margin-top:14px;">' +
        '<div class="panel-title">Clarifying Questions</div>' +
        rows +
      '</div>'
    );
  }

  function renderContextPanel(d) {
    return (
      '<details class="collapsible" id="context-panel" style="margin-top:14px;">' +
        '<summary>Add reviewer context &amp; regrade</summary>' +
        '<div class="collapsible-body">' +
          '<div class="muted text-small" style="margin-bottom:8px;">' +
            'Share what the reviewer noticed that the transcript might have missed. The AI will be asked to regrade using this context.' +
          '</div>' +
          '<textarea class="field-textarea" id="context-text" ' +
            'placeholder="e.g. The caller was a long-time customer; prior complaints on file; background noise obscured part of the middle."></textarea>' +
          '<div style="margin-top:10px;">' +
            '<button type="button" class="btn btn-primary btn-sm" id="btn-context-regrade">Regrade with context</button>' +
          '</div>' +
        '</div>' +
      '</details>'
    );
  }

  // ── Hard-delete flow ─────────────────────────────────────────
  // Pre-fetches deletion impact, builds the structured "what's destroyed /
  // what survives" intro as a DOM node, runs strongConfirmDialog with the
  // typed phrase "DELETE", then issues the DELETE. On success: dispatches
  // a window-level "ea:interaction-deleted" CustomEvent so other open views
  // (history page) can drop the row, then invokes opts.onSuccess.
  //
  // Errors during the impact fetch surface via EA.toast and abort the flow
  // (no destructive call is issued without a valid impact preview).

  function pluralize(n, singular, plural) {
    return (n === 1) ? singular : (plural || (singular + "s"));
  }

  function formatBytes(bytes) {
    if (!bytes || bytes < 1024) return (bytes || 0) + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function audioLabel(impact) {
    if (!impact.audio_present) return "no audio file";
    if (impact.audio_bytes > 0) return "audio file (" + formatBytes(impact.audio_bytes) + ")";
    return "audio file";
  }

  function buildHardDeleteIntro(interactionId, impact) {
    const root = document.createElement("div");

    const lead = document.createElement("div");
    lead.className = "muted";
    lead.style.marginBottom = "10px";
    lead.textContent = "You are about to permanently delete interaction #" +
      interactionId + ".";
    root.appendChild(lead);

    // What's destroyed
    const destroyedHdr = document.createElement("div");
    destroyedHdr.style.fontWeight = "600";
    destroyedHdr.style.marginBottom = "4px";
    destroyedHdr.textContent = "What will be destroyed:";
    root.appendChild(destroyedHdr);

    const destroyedList = document.createElement("ul");
    destroyedList.className = "bullet-list";
    destroyedList.style.marginBottom = "12px";
    [
      "The interaction record itself",
      impact.rubric_scores + " " +
        pluralize(impact.rubric_scores, "rubric score", "rubric scores"),
      impact.audit_entries + " " +
        pluralize(impact.audit_entries, "audit-log entry", "audit-log entries"),
      "The " + audioLabel(impact),
    ].forEach(function (text) {
      const li = document.createElement("li");
      li.textContent = text;
      destroyedList.appendChild(li);
    });
    root.appendChild(destroyedList);

    // What survives
    const survivesHdr = document.createElement("div");
    survivesHdr.style.fontWeight = "600";
    survivesHdr.style.marginBottom = "4px";
    survivesHdr.textContent = "What survives for accountability:";
    root.appendChild(survivesHdr);

    const survivesList = document.createElement("ul");
    survivesList.className = "bullet-list";
    survivesList.style.marginBottom = "12px";
    [
      "A deletion record: who deleted it, when, and which interaction id",
      "No content from the interaction (transcript, scores, audio) is kept",
    ].forEach(function (text) {
      const li = document.createElement("li");
      li.textContent = text;
      survivesList.appendChild(li);
    });
    root.appendChild(survivesList);

    const warning = document.createElement("div");
    warning.style.color = "#ef4444";
    warning.style.fontWeight = "600";
    warning.textContent = "This action cannot be undone.";
    root.appendChild(warning);

    return root;
  }

  async function hardDeleteInteractionFlow(interactionId, opts) {
    opts = opts || {};
    const iid = Number(interactionId);
    if (!iid || isNaN(iid)) return;

    let impact;
    try {
      impact = await EA.fetchJSON(
        "/api/interactions/" + iid + "/hard-delete-impact"
      );
    } catch (err) {
      if (EA.toast) EA.toast("Could not load deletion preview: " + (err.message || err), "error");
      return;
    }

    const intro = buildHardDeleteIntro(iid, impact);

    const confirmed = await EA.strongConfirmDialog({
      title:          "Delete interaction permanently",
      intro:          intro,
      requiredPhrase: "DELETE",
      promptLabel:    'Type DELETE to confirm:',
      okLabel:        "Delete permanently",
      cancelLabel:    "Cancel",
      variant:        "danger",
    });
    if (!confirmed) return;

    try {
      await EA.fetchJSON("/api/interactions/" + iid + "/hard", { method: "DELETE" });
    } catch (err) {
      if (EA.toast) EA.toast("Delete failed: " + (err.message || err), "error");
      return;
    }

    if (EA.toast) EA.toast("Interaction deleted.", "success");
    window.dispatchEvent(new CustomEvent("ea:interaction-deleted", {
      detail: { interaction_id: iid },
    }));
    if (typeof opts.onSuccess === "function") {
      try { opts.onSuccess(); } catch (e) {
        if (window.console) console.error("[hardDeleteInteractionFlow] onSuccess threw:", e);
      }
    }
  }

  window.EA = window.EA || {};
  window.EA.InteractionView = {
    render: render,
  };
  window.EA.hardDeleteInteractionFlow = hardDeleteInteractionFlow;
})();
