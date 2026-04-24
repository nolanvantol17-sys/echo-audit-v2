/* ========================================================================
   app.js — Echo Audit V2 shared frontend helpers.

   Loaded by base.html on every authenticated page. Exposes a small global
   surface via window.EA — fetchJSON, formatDate, formatScore, and the
   flash-dismiss + org-switcher handlers wired up in base.html.
   ======================================================================== */

(function () {
  "use strict";

  // ── JSON fetch wrapper ─────────────────────────────────────
  // - Always sends credentials so Flask-Login cookies come along
  // - Parses JSON on success; throws Error on !ok so callers can `catch`
  // - Preserves the server-side `error` message on the thrown Error
  async function fetchJSON(url, options) {
    const opts = Object.assign({
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    }, options || {});

    // If a plain object body is passed, JSON-encode it.
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      opts.body = JSON.stringify(opts.body);
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    }

    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (networkErr) {
      const err = new Error("Network error — is the server reachable?");
      err.cause = networkErr;
      throw err;
    }

    const ct = resp.headers.get("content-type") || "";
    let body = null;
    if (ct.indexOf("application/json") !== -1) {
      try { body = await resp.json(); } catch (_) { /* malformed JSON */ }
    }

    if (!resp.ok) {
      const msg = (body && body.error) || resp.statusText || ("HTTP " + resp.status);
      const err = new Error(msg);
      err.status = resp.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  // ── Date formatter ─────────────────────────────────────────
  // Accepts an ISO string or Date. Returns "MMM D, YYYY" — the same style
  // V1 used in its recent-calls list. Falls back to the raw string on
  // parse failure so we never render "Invalid Date" to the UI.
  const _MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function formatDate(value) {
    if (!value) return "";
    // Date-only ISO strings ("YYYY-MM-DD") get parsed as UTC midnight, then
    // .getMonth()/.getDate() read in local time → previous day west of UTC.
    // Hand-parse the components so DATE columns render the day the server stored.
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
      const parts = value.split("-");
      return _MONTHS[parseInt(parts[1], 10) - 1] + " "
           + parseInt(parts[2], 10) + ", " + parts[0];
    }
    const d = (value instanceof Date) ? value : new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return _MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
  }

  // ── Relative-time formatter ────────────────────────────────
  // "just now" / "Nm ago" / "Nh ago" / "today" / "yesterday" / "N days ago"
  // / "1 week ago" / "N weeks ago" / "N months ago", and falls back to
  // formatDate for >1 year. Accepts ISO string or Date. Used for at-a-glance
  // recency on dashboard panels.
  function formatRelativeTime(value) {
    if (!value) return "";
    const d = (value instanceof Date) ? value : new Date(value);
    if (isNaN(d.getTime())) return String(value);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1)  return "just now";
    if (diffMin < 60) return diffMin + "m ago";
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24)   return diffH + "h ago";
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const day   = new Date(d.getFullYear(),   d.getMonth(),   d.getDate());
    const diffDays = Math.round((today.getTime() - day.getTime()) / 86400000);
    if (diffDays === 0)  return "today";
    if (diffDays === 1)  return "yesterday";
    if (diffDays < 7)    return diffDays + " days ago";
    if (diffDays < 14)   return "1 week ago";
    if (diffDays < 30)   return Math.floor(diffDays / 7) + " weeks ago";
    if (diffDays < 365)  return Math.floor(diffDays / 30) + " months ago";
    return formatDate(value);
  }

  // ── Score formatter ────────────────────────────────────────
  // One decimal, strips trailing zero so "7.0" renders as "7.0" intentionally
  // (dashboard expects fixed-width scores). Returns em-dash for null/NaN.
  function formatScore(value) {
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (isNaN(n)) return "—";
    return n.toFixed(1);
  }

  // Format a call's clock time. Prefer the live-call start time; fall back
  // to the server-side upload time if there's no recorded start. Appends a
  // duration like "· 2m 14s" when one was captured. Returns "—" when neither
  // timestamp is present (e.g. older rows backfilled before timestamps shipped).
  function formatCallTime(startTime, uploadedAt, durationSeconds) {
    const ts = startTime || uploadedAt;
    if (!ts) return "—";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "—";
    const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (durationSeconds && Number(durationSeconds) > 0) {
      const secs = Math.round(Number(durationSeconds));
      return timeStr + " · " + Math.floor(secs / 60) + "m " + (secs % 60) + "s";
    }
    return timeStr;
  }

  // Classify a 0–10 score for the score-pill color.
  // Shared convention across the app: green >7, amber 5-7, red <5, gray null.
  function scoreClass(value) {
    if (value === null || value === undefined || value === "") return "gray";
    const n = Number(value);
    if (isNaN(n)) return "gray";
    if (n > 7)  return "good";
    if (n >= 5) return "warn";
    return "bad";
  }

  // Return the CSS color variable name that matches scoreClass. Useful for
  // badge-fill decisions in inline SVGs.
  function scoreColor(value) {
    const c = scoreClass(value);
    return c === "good" ? "var(--success)"
         : c === "warn" ? "var(--warning)"
         : c === "bad"  ? "var(--danger)"
         : "var(--muted)";
  }

  // ── Flash dismiss ──────────────────────────────────────────
  function initFlashDismiss() {
    document.querySelectorAll(".flash-dismiss").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const msg = btn.closest(".flash-msg");
        if (msg) msg.remove();
      });
    });
  }

  // ── Org switcher (super_admin only) ────────────────────────
  function initOrgSwitcher() {
    const sel = document.getElementById("org-switcher");
    if (!sel) return;

    sel.addEventListener("change", async function () {
      const companyId = sel.value;
      if (!companyId) return;
      const prior = sel.dataset.prior || "";
      try {
        await fetchJSON("/api/platform/switch-org", {
          method: "POST",
          body: { company_id: parseInt(companyId, 10) },
        });
        // Reload so every dashboard fetch runs against the new org.
        window.location.reload();
      } catch (err) {
        // Restore previous selection + surface the error inline.
        sel.value = prior;
        const msg = (err && err.message) || "Failed to switch org";
        alert(msg);
      }
    });
    // Remember the initial value so we can restore on failure.
    sel.dataset.prior = sel.value;
  }

  // ── Section helpers (loading + error states) ──────────────
  // Caller passes an element reference or selector. These swap the whole
  // child content — use on panels whose skeleton shape is known.
  function showLoading(el, rows = 3) {
    const target = (typeof el === "string") ? document.querySelector(el) : el;
    if (!target) return;
    const skel = [];
    for (let i = 0; i < rows; i++) {
      skel.push('<div class="skeleton" style="height:14px;margin:8px 0;"></div>');
    }
    target.innerHTML = skel.join("");
  }
  function showError(el, message) {
    const target = (typeof el === "string") ? document.querySelector(el) : el;
    if (!target) return;
    const safe = String(message || "Failed to load.")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    target.innerHTML = '<div class="section-error">' + safe + "</div>";
  }

  // HTML-escape small pieces of text for use in innerHTML concatenation.
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Wire up once DOM is ready.
  document.addEventListener("DOMContentLoaded", function () {
    initFlashDismiss();
    initOrgSwitcher();
  });

  // ── Toast ─────────────────────────────────────────────────
  // Brief, auto-dismissing notification. Variants: success (default), error,
  // info. One toast visible at a time — a new call replaces the previous.
  // Errors linger 5s so users can read them; success/info dismiss at 3s.
  let _toastTimer = null;
  function toast(message, variant) {
    variant = variant || "success";
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    // Replace any current toast
    host.innerHTML = "";
    if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }

    const el = document.createElement("div");
    el.className = "toast-msg toast-" + variant;
    el.textContent = message;
    host.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("show"); });

    const lingerMs = variant === "error" ? 5000 : 3000;
    _toastTimer = setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { if (el.parentNode) el.remove(); }, 250);
      _toastTimer = null;
    }, lingerMs);
  }

  // ── Modal / confirm dialog ────────────────────────────────
  // Promise-based. Resolves to true on confirm, false on cancel / backdrop.
  // Single active modal at a time — new calls close any existing modal.
  function confirmDialog(opts) {
    opts = opts || {};
    const title = opts.title || "Confirm";
    const body  = opts.body  || "";
    const okLabel  = opts.okLabel  || "Confirm";
    const cancelLabel = opts.cancelLabel || "Cancel";
    const variant = opts.variant || "primary";  // primary | danger

    return new Promise(function (resolve) {
      // Clean up any existing modal first
      document.querySelectorAll(".modal-backdrop").forEach(function (n) { n.remove(); });

      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      const titleSafe = esc(title);
      const bodyHtml  = typeof body === "string" ? esc(body) : "";
      backdrop.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
          '<h3 class="modal-title">' + titleSafe + '</h3>' +
          '<div class="modal-body">' + bodyHtml + '</div>' +
          '<div class="modal-actions">' +
            '<button type="button" class="btn btn-ghost" data-act="cancel">' + esc(cancelLabel) + '</button>' +
            '<button type="button" class="btn btn-' + variant + '" data-act="ok">' + esc(okLabel) + '</button>' +
          '</div>' +
        '</div>';
      // If caller passed a DOM element as body, replace the text with it.
      if (body && typeof body !== "string") {
        const bodyHost = backdrop.querySelector(".modal-body");
        bodyHost.innerHTML = "";
        bodyHost.appendChild(body);
      }
      document.body.appendChild(backdrop);

      const finish = function (result) {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
        resolve(result);
      };
      const onKey = function (e) {
        if (e.key === "Escape") finish(false);
        if (e.key === "Enter")  finish(true);
      };
      backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) finish(false);
      });
      backdrop.querySelector('[data-act="cancel"]').addEventListener("click", function () { finish(false); });
      backdrop.querySelector('[data-act="ok"]')    .addEventListener("click", function () { finish(true); });
      document.addEventListener("keydown", onKey);
      // Autofocus the OK button
      backdrop.querySelector('[data-act="ok"]').focus();
    });
  }

  // Full-screen overlay with a status message. Returns a controller object
  // so callers can update the message mid-flight.
  function showOverlay(initialMessage) {
    let host = document.getElementById("overlay-host");
    if (host) host.remove();
    host = document.createElement("div");
    host.id = "overlay-host";
    host.className = "overlay-backdrop";
    host.innerHTML =
      '<div class="overlay-card">' +
        '<div class="overlay-spinner"></div>' +
        '<div class="overlay-msg"></div>' +
      '</div>';
    document.body.appendChild(host);
    const msgEl = host.querySelector(".overlay-msg");
    msgEl.textContent = initialMessage || "Working...";
    return {
      set: function (m) { msgEl.textContent = m; },
      close: function () { host.remove(); },
    };
  }

  // ── Strong-confirm dialog ────────────────────────────────
  // Like confirmDialog, but requires the user to type a phrase that matches
  // `requiredPhrase` before the destructive action button is enabled. Used
  // for irreversible operations (e.g. deleting an organization).
  //
  // Comparison normalization (applied to BOTH typed input and required phrase
  // before compare): trim, collapse internal whitespace runs to single spaces,
  // lowercase. This avoids "you typed 'acme  corp' with two spaces"-style
  // rejections for what feels like the right answer.
  function strongConfirmDialog(opts) {
    opts = opts || {};
    const title          = opts.title || "Confirm";
    const intro          = (opts.intro != null) ? opts.intro : "This cannot be undone.";
    const requiredPhrase = String(opts.requiredPhrase || "");
    const promptLabel    = opts.promptLabel || "Type the name to confirm:";
    const okLabel        = opts.okLabel  || "Delete";
    const cancelLabel    = opts.cancelLabel || "Cancel";
    const variant        = opts.variant || "danger";

    const norm = function (s) {
      return String(s == null ? "" : s).trim().replace(/\s+/g, " ").toLowerCase();
    };
    const target = norm(requiredPhrase);

    // intro can be a plain string (escaped) or a DOM node (appended verbatim).
    // Callers pass nodes when they need structured markup like counts/lists.
    const introIsNode = (intro && typeof intro !== "string");
    const introHtml = introIsNode
      ? '<div data-role="strong-confirm-intro" style="margin-bottom:10px;"></div>'
      : '<div class="muted" style="margin-bottom:10px;">' + esc(intro) + '</div>';

    return new Promise(function (resolve) {
      document.querySelectorAll(".modal-backdrop").forEach(function (n) { n.remove(); });

      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
          '<h3 class="modal-title">' + esc(title) + '</h3>' +
          '<div class="modal-body">' +
            introHtml +
            '<div class="muted text-small" style="margin-bottom:6px;">' + esc(promptLabel) + '</div>' +
            '<div style="font-weight:600;margin-bottom:8px;">' + esc(requiredPhrase) + '</div>' +
            '<input type="text" id="strong-confirm-in" class="field-input" autocomplete="off" autocapitalize="off" spellcheck="false">' +
          '</div>' +
          '<div class="modal-actions">' +
            '<button type="button" class="btn btn-ghost" data-act="cancel">' + esc(cancelLabel) + '</button>' +
            '<button type="button" class="btn btn-' + variant + '" data-act="ok" disabled>' + esc(okLabel) + '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(backdrop);

      if (introIsNode) {
        backdrop.querySelector('[data-role="strong-confirm-intro"]').appendChild(intro);
      }

      const input  = backdrop.querySelector("#strong-confirm-in");
      const okBtn  = backdrop.querySelector('[data-act="ok"]');
      const matches = function () { return norm(input.value) === target && target !== ""; };
      input.addEventListener("input", function () { okBtn.disabled = !matches(); });

      const finish = function (result) {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
        resolve(result);
      };
      const onKey = function (e) {
        if (e.key === "Escape") finish(false);
        if (e.key === "Enter" && matches()) finish(true);
      };
      backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) finish(false);
      });
      backdrop.querySelector('[data-act="cancel"]').addEventListener("click", function () { finish(false); });
      okBtn.addEventListener("click", function () { if (matches()) finish(true); });
      document.addEventListener("keydown", onKey);
      input.focus();
    });
  }

  // ── Form modal ────────────────────────────────────────────
  // Renders a form inside a modal. Resolves to a dict of form values on
  // save, or null if the user cancels. Callers pass `fields` as an array of
  // {name, label, type, required, value, options, help, autocomplete}.
  // type ∈ {text, email, password, date, number, textarea, select, hidden}.
  // For `select`, `options` is an array of {value, label}.
  function formDialog(opts) {
    opts = opts || {};
    const title  = opts.title || "";
    const fields = opts.fields || [];
    const okLabel     = opts.okLabel     || "Save";
    const cancelLabel = opts.cancelLabel || "Cancel";
    const variant     = opts.variant     || "primary";
    const before = opts.before || "";  // optional HTML to prepend inside body

    const host = document.createElement("form");
    host.autocomplete = "off";
    host.addEventListener("submit", function (e) { e.preventDefault(); });

    let html = typeof before === "string" ? before : "";
    fields.forEach(function (f) {
      if (f.type === "hidden") {
        html += '<input type="hidden" name="' + esc(f.name) + '" value="' +
                esc(f.value || "") + '">';
        return;
      }
      const labelBit =
        '<label class="field-label' + (f.required ? ' field-required' : '') +
          '" for="fd-' + esc(f.name) + '">' + esc(f.label || f.name) + '</label>';
      let input = "";
      if (f.type === "textarea") {
        input = '<textarea id="fd-' + esc(f.name) + '" name="' + esc(f.name) +
                '" class="field-textarea"' +
                (f.required ? " required" : "") +
                '>' + esc(f.value || "") + '</textarea>';
      } else if (f.type === "select") {
        const opts = (f.options || []).map(function (o) {
          const selected = (String(o.value) === String(f.value)) ? ' selected' : '';
          return '<option value="' + esc(o.value) + '"' + selected + '>' +
                   esc(o.label) + '</option>';
        }).join("");
        input = '<select id="fd-' + esc(f.name) + '" name="' + esc(f.name) +
                '" class="field-select"' +
                (f.required ? " required" : "") + '>' + opts + '</select>';
      } else {
        const attrs = [
          'id="fd-' + esc(f.name) + '"',
          'name="' + esc(f.name) + '"',
          'type="' + esc(f.type || "text") + '"',
          'class="field-input"',
          'value="' + esc(f.value == null ? "" : f.value) + '"',
        ];
        if (f.required)     attrs.push("required");
        if (f.autocomplete) attrs.push('autocomplete="' + esc(f.autocomplete) + '"');
        if (f.readOnly)     attrs.push("readonly");
        if (f.min  != null) attrs.push('min="'  + esc(f.min)  + '"');
        if (f.max  != null) attrs.push('max="'  + esc(f.max)  + '"');
        if (f.step != null) attrs.push('step="' + esc(f.step) + '"');
        input = "<input " + attrs.join(" ") + ">";
      }
      const help = f.help ? '<div class="muted text-small">' + esc(f.help) + '</div>' : '';
      html += '<div class="field" style="margin-bottom:12px;">' +
                labelBit + input + help +
              '</div>';
    });
    host.innerHTML = html;

    return new Promise(function (resolve) {
      confirmDialog({
        title:       title,
        body:        host,
        okLabel:     okLabel,
        cancelLabel: cancelLabel,
        variant:     variant,
      }).then(function (ok) {
        if (!ok) return resolve(null);
        // Validate required fields natively
        for (const el of host.querySelectorAll("[required]")) {
          const val = (el.value || "").trim();
          if (!val) {
            toast("Please fill in " + (el.name || "all required fields") + ".", "error");
            return resolve(null);
          }
        }
        const data = {};
        host.querySelectorAll("[name]").forEach(function (el) {
          const v = el.value;
          data[el.name] = (v === "" ? null : v);
        });
        resolve(data);
      });
    });
  }

  // ── Clipboard + reveal-once password widget ──────────────────
  // copyToClipboard(text, opts):
  //   Writes `text` to the system clipboard. Returns a Promise<bool>.
  //   opts.successToast (string|null): toast text to show on success
  //                                    (pass null for no toast).
  //   opts.errorToast   (string|null): toast text to show on failure
  //                                    (pass null for no toast).
  function copyToClipboard(text, opts) {
    opts = opts || {};
    const successToast = (opts.successToast === undefined) ? "Copied." : opts.successToast;
    const errorToast   = (opts.errorToast   === undefined) ? "Copy failed" : opts.errorToast;
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      if (errorToast) toast(errorToast, "error");
      return Promise.resolve(false);
    }
    return navigator.clipboard.writeText(text).then(
      function () { if (successToast) toast(successToast, "success"); return true; },
      function () { if (errorToast)   toast(errorToast,   "error");   return false; }
    );
  }

  // revealOncePassword(pw):
  //   Returns a DOM element that displays a masked password with a
  //   "Reveal & copy" button. On click: starts a 10s countdown,
  //   reveals the password, attempts clipboard copy in parallel.
  //   When the countdown ends the password re-masks and the reveal
  //   button is permanently disabled (one-shot per widget instance).
  //   Countdown pauses while the tab/window loses focus so an admin
  //   alt-tabbing to paste the value doesn't lose visibility.
  function revealOncePassword(pw) {
    const REVEAL_MS = 10000;
    const MASK = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";

    const wrap = document.createElement("span");
    wrap.className = "temp-pw-reveal";

    const value = document.createElement("code");
    value.className = "temp-pw-value temp-pw-masked";
    value.textContent = MASK;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "inline-copy-btn";
    btn.textContent = "Reveal & copy";

    const countdown = document.createElement("span");
    countdown.className = "temp-pw-countdown";
    countdown.hidden = true;

    const warn = document.createElement("span");
    warn.className = "temp-pw-warn";
    warn.textContent = "Won't be shown again.";
    warn.hidden = true;

    wrap.appendChild(value);
    wrap.appendChild(btn);
    wrap.appendChild(countdown);
    wrap.appendChild(warn);

    let state = "masked";  // masked → revealed → spent
    let expiresAt = 0;
    let remainingWhenHidden = 0;
    let tickHandle = null;

    function isHidden() {
      return document.hidden || !document.hasFocus();
    }

    function tick() {
      const remaining = Math.max(0, expiresAt - Date.now());
      countdown.textContent = "(" + Math.ceil(remaining / 1000) + "s)";
      if (remaining <= 0) finish();
    }

    function onVisibility() {
      if (state !== "revealed") return;
      if (isHidden()) {
        // Pause: snapshot remaining time, stop ticking.
        if (tickHandle !== null) {
          clearInterval(tickHandle);
          tickHandle = null;
        }
        remainingWhenHidden = Math.max(0, expiresAt - Date.now());
      } else {
        // Resume: shift the deadline forward by however long we paused.
        if (remainingWhenHidden > 0) {
          expiresAt = Date.now() + remainingWhenHidden;
          remainingWhenHidden = 0;
        }
        if (tickHandle === null) {
          tickHandle = setInterval(tick, 250);
          tick();
        }
      }
    }

    function finish() {
      state = "spent";
      if (tickHandle !== null) {
        clearInterval(tickHandle);
        tickHandle = null;
      }
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur",  onVisibility);
      window.removeEventListener("focus", onVisibility);
      value.textContent = MASK;
      value.classList.remove("temp-pw-revealed");
      value.classList.add("temp-pw-masked");
      countdown.hidden = true;
      btn.disabled = true;
      btn.textContent = "Hidden";
    }

    btn.addEventListener("click", function () {
      if (state !== "masked") return;
      state = "revealed";
      value.textContent = pw;
      value.classList.remove("temp-pw-masked");
      value.classList.add("temp-pw-revealed");
      btn.disabled = true;
      countdown.hidden = false;
      warn.hidden = false;
      expiresAt = Date.now() + REVEAL_MS;
      tick();
      tickHandle = setInterval(tick, 250);
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("blur",  onVisibility);
      window.addEventListener("focus", onVisibility);
      // Clipboard write runs in parallel; failure leaves the user with
      // the visible value to copy manually before the countdown ends.
      copyToClipboard(pw, { successToast: "Copied.", errorToast: null });
    });

    return wrap;
  }

  // Shared status-id helpers. Name is user-facing copy (with spaces);
  // slug is the CSS-selector form used in `.status-<slug>` class names.
  // Kept as separate functions so copy changes don't break CSS selectors.
  function statusIdToName(id) {
    switch (Number(id)) {
      case 40: return "Transcribing";
      case 41: return "Awaiting clarification";
      case 42: return "Grading";
      case 43: return "Graded";
      case 44: return "No answer";
      case 45: return "Pending";
      default: return "—";
    }
  }
  function statusIdToSlug(id) {
    switch (Number(id)) {
      case 40: return "transcribing";
      case 41: return "awaiting-clarification";
      case 42: return "grading";
      case 43: return "graded";
      case 44: return "no-answer";
      case 45: return "pending";
      default: return "none";
    }
  }

  // Expose as a single namespace so template scripts can use them.
  window.EA = {
    fetchJSON:       fetchJSON,
    formatDate:      formatDate,
    formatRelativeTime: formatRelativeTime,
    formatCallTime:  formatCallTime,
    formatScore:     formatScore,
    scoreClass:      scoreClass,
    scoreColor:      scoreColor,
    showLoading:     showLoading,
    showError:       showError,
    esc:             esc,
    toast:           toast,
    showToast:       toast,
    confirmDialog:   confirmDialog,
    strongConfirmDialog: strongConfirmDialog,
    formDialog:      formDialog,
    showOverlay:     showOverlay,
    copyToClipboard: copyToClipboard,
    revealOncePassword: revealOncePassword,
    statusIdToName:  statusIdToName,
    statusIdToSlug:  statusIdToSlug,
  };
})();
