/* ========================================================================
   audio_player.js — Shared playback-speed + 15s-skip wrapper for <audio>.

   Exposes window.EA.AudioPlayer with three entry points:
     - html({src, preload, audioClass})  → HTML string for inline insertion
     - attachAll(rootEl)                  → wire any unwired .ea-audio-player
                                            blocks inside the given root
     - enhance(audioEl)                   → wrap an EXISTING <audio> element
                                            (used when the audio element's
                                            src is reassigned over its life,
                                            e.g. the record-preview surface)

   The selected playback speed is persisted to localStorage so it sticks
   across surfaces and page reloads.
   ======================================================================== */

(function () {
  "use strict";

  const SPEED_KEY = "ea.audio.speed";
  const SPEED_OPTIONS = [0.75, 1, 1.25, 1.5, 1.75, 2];

  function readSpeed() {
    try {
      const v = parseFloat(localStorage.getItem(SPEED_KEY) || "1");
      if (!isFinite(v) || v <= 0) return 1;
      // Snap to the nearest supported option so a stale or out-of-range
      // value (e.g. an old build wrote 1.1x) doesn't reach the audio element.
      let best = SPEED_OPTIONS[0], bestD = Math.abs(v - best);
      for (let i = 1; i < SPEED_OPTIONS.length; i++) {
        const d = Math.abs(v - SPEED_OPTIONS[i]);
        if (d < bestD) { best = SPEED_OPTIONS[i]; bestD = d; }
      }
      return best;
    } catch (_) { return 1; }
  }
  function writeSpeed(v) {
    try { localStorage.setItem(SPEED_KEY, String(v)); } catch (_) {}
  }

  function controlsHtml() {
    const speed = readSpeed();
    const opts = SPEED_OPTIONS.map(function (v) {
      const sel = (Math.abs(v - speed) < 0.001) ? " selected" : "";
      return '<option value="' + v + '"' + sel + '>' + v + 'x</option>';
    }).join("");
    return (
      '<div class="ea-audio-controls">' +
        '<button type="button" class="ea-audio-btn" data-ea-audio-action="rewind"' +
                ' aria-label="Skip back 15 seconds" title="Back 15s">⟲ 15s</button>' +
        '<button type="button" class="ea-audio-btn" data-ea-audio-action="forward"' +
                ' aria-label="Skip forward 15 seconds" title="Forward 15s">15s ⟳</button>' +
        '<label class="ea-audio-speed">' +
          '<span class="muted text-small">Speed</span>' +
          '<select class="ea-audio-speed-select" data-ea-audio-action="speed"' +
                  ' aria-label="Playback speed">' + opts + '</select>' +
        '</label>' +
      '</div>'
    );
  }

  function html(opts) {
    opts = opts || {};
    const src = String(opts.src || "");
    const preload = opts.preload || "none";
    const audioCls = opts.audioClass || "app-audio";
    return (
      '<div class="ea-audio-player">' +
        '<audio class="' + audioCls + '" controls preload="' + preload + '"' +
              ' src="' + src + '"></audio>' +
        controlsHtml() +
      '</div>'
    );
  }

  function attachAll(root) {
    root = root || document;
    const wrappers = root.querySelectorAll(".ea-audio-player");
    wrappers.forEach(wireWrapper);
  }

  function enhance(audioEl) {
    if (!audioEl) return;
    let wrapper = audioEl.parentElement;
    if (!wrapper || !wrapper.classList.contains("ea-audio-player")) {
      wrapper = document.createElement("div");
      wrapper.className = "ea-audio-player";
      audioEl.parentNode.insertBefore(wrapper, audioEl);
      wrapper.appendChild(audioEl);
    }
    if (!wrapper.querySelector(".ea-audio-controls")) {
      wrapper.insertAdjacentHTML("beforeend", controlsHtml());
    }
    wireWrapper(wrapper);
  }

  function wireWrapper(wrapper) {
    if (wrapper.dataset.eaAudioAttached === "1") return;
    const audio = wrapper.querySelector("audio");
    if (!audio) return;
    wrapper.dataset.eaAudioAttached = "1";

    const initialSpeed = readSpeed();
    audio.playbackRate = initialSpeed;
    const speedSel = wrapper.querySelector('[data-ea-audio-action="speed"]');
    if (speedSel) speedSel.value = String(initialSpeed);

    wrapper.addEventListener("click", function (e) {
      const t = e.target.closest("[data-ea-audio-action]");
      if (!t) return;
      const action = t.getAttribute("data-ea-audio-action");
      if (action === "rewind") {
        const cur = audio.currentTime || 0;
        audio.currentTime = Math.max(0, cur - 15);
      } else if (action === "forward") {
        const cur = audio.currentTime || 0;
        const dur = isFinite(audio.duration) ? audio.duration : Infinity;
        audio.currentTime = Math.min(dur, cur + 15);
      }
    });

    if (speedSel) {
      speedSel.addEventListener("change", function () {
        const v = parseFloat(speedSel.value);
        if (!isFinite(v) || v <= 0) return;
        audio.playbackRate = v;
        writeSpeed(v);
      });
    }

    // Browsers reset playbackRate when a new src loads metadata. Reapply.
    audio.addEventListener("loadedmetadata", function () {
      audio.playbackRate = readSpeed();
    });
  }

  window.EA = window.EA || {};
  window.EA.AudioPlayer = {
    html:      html,
    attachAll: attachAll,
    enhance:   enhance,
  };
})();
