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

   Pure rendering — no event wiring. Callers attach handlers by querying the
   host after render (e.g. document.getElementById('btn-context-regrade')).
   ======================================================================== */

(function () {
  "use strict";
  const EA = window.EA;

  function render(host, data, opts) {
    opts = opts || {};
    const readOnly = !!opts.readOnly;
    const canRegrade = !!opts.canRegrade;
    const d = data;
    const interactionId = d.interaction_id;

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
          regradeBadge +
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

    const audioHtml = d.interaction_audio_url
      ? '<div style="margin-top:14px;">' +
          EA.AudioPlayer.html({
            src: '/api/interactions/' + interactionId + '/audio',
            preload: 'none',
          }) +
        '</div>'
      : '';

    const contextPanelHtml = (!readOnly && canRegrade) ? renderContextPanel(d) : '';

    host.innerHTML =
      regradedBanner +
      hero +
      flagsHtml +
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
      '</div>' +
      cqHtml +
      contextPanelHtml +
      (transcriptHtml ? '<div style="margin-top:14px;">' + transcriptHtml + '</div>' : '') +
      audioHtml;

    EA.AudioPlayer.attachAll(host);
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

  window.EA = window.EA || {};
  window.EA.InteractionView = {
    render: render,
  };
})();
