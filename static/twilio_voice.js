/* ========================================================================
   twilio_voice.js — thin wrapper around the Twilio Voice JS SDK.

   Exposes a tiny EA.TwilioVoice API so the grade page doesn't need to
   touch the SDK directly:

     await EA.TwilioVoice.init({ token, identity })
       — registers a Twilio.Device for outbound calls; token comes from
         POST /api/twilio/access-token. Returns when the device is ready.

     await EA.TwilioVoice.dial({ targetPhone, pendingCallId, onEvent })
       — places the call. onEvent fires with one of:
           {type:"connecting"} | {type:"connected"} | {type:"disconnected"}
           | {type:"error", message}
         Returns the active Call object. Only one call at a time.

     EA.TwilioVoice.mute(bool)
     EA.TwilioVoice.hangup()
     EA.TwilioVoice.isInCall()  → bool
     EA.TwilioVoice.teardown()  — drop the device, free mic + WebRTC

   Assumes the Twilio Voice SDK has been loaded via <script>. We pin to the
   v2 line via jsDelivr — Twilio's own CDN URL pattern shifts over time.
   ======================================================================== */

(function () {
  "use strict";

  if (!window.EA) {
    console.error("twilio_voice.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;

  let device = null;       // Twilio.Device instance (singleton per page)
  let activeCall = null;   // currently-connected Call
  let isMuted = false;

  function _sdk() {
    // Twilio's UMD bundle exposes window.Twilio with a Device class.
    return window.Twilio && window.Twilio.Device ? window.Twilio.Device : null;
  }

  // ── Friendly error translation ──────────────────────────────────
  // Twilio Voice surfaces a numeric .code (the 3xxxx range) on its error
  // objects. Map the ones a property caller can actually hit to plain,
  // actionable sentences so we never dump a raw "ConnectionError (31005)" at
  // the user again. Anything unmapped still gets a calm default that keeps the
  // code for support.
  const _CALL_ERROR_MESSAGES = {
    31000: "Something went wrong with the call. Please try again.",
    31003: "The call timed out before it could connect. Please try again.",
    31005: "We couldn't connect to that number — it may be disconnected, " +
           "wrong, or rejecting the call. Double-check the property's phone " +
           "number, then try again or log it as unanswered.",
    31008: "The call dropped before it connected. Please try again.",
    31009: "Network trouble interrupted the call. Check your connection and try again.",
    31201: "We couldn't access your microphone. Allow mic access in your browser, then try again.",
    31208: "Microphone access was blocked. Allow it in your browser settings, then try again.",
    31480: "The line was temporarily unavailable. Try again in a moment.",
    31486: "The line was busy. Try again in a moment.",
  };

  function _errCode(e) {
    if (!e) return null;
    if (typeof e.code === "number") return e.code;
    if (e.code != null) { const n = Number(e.code); return isNaN(n) ? e.code : n; }
    return null;
  }

  function friendlyError(e) {
    const code = _errCode(e);
    const mapped = (code != null) ? _CALL_ERROR_MESSAGES[code] : null;
    if (mapped) return mapped;
    const tail = " — try again, or log it as unanswered.";
    return (code != null)
      ? "The call couldn't be completed (error " + code + ")" + tail
      : "The call couldn't be completed" + tail;
  }

  async function init(opts) {
    opts = opts || {};
    if (!opts.token) throw new Error("init: token required");
    if (!_sdk()) throw new Error("Twilio Voice SDK not loaded");

    if (device) {
      try { device.destroy(); } catch (_) { /* ignore */ }
      device = null;
    }
    const Device = _sdk();
    device = new Device(opts.token, {
      // Disable Twilio's verbose info logs in prod-like envs; keep warnings.
      logLevel: "warn",
      // Pre-warm the codec so first call latency is lower.
      codecPreferences: ["opus", "pcmu"],
    });
    // Wait for the device to be ready before resolving — caller awaits us
    // before showing the dial UI as ready.
    return new Promise((resolve, reject) => {
      const ok  = () => { device.removeListener("error", err); resolve(); };
      const err = (e) => { device.removeListener("registered", ok); reject(e); };
      device.once("registered", ok);
      device.once("error", err);
      device.register();
    });
  }

  async function dial(opts) {
    opts = opts || {};
    if (!device) throw new Error("dial: call init() first");
    if (activeCall) throw new Error("dial: already in a call");
    if (!opts.targetPhone)   throw new Error("dial: targetPhone required");
    if (!opts.pendingCallId) throw new Error("dial: pendingCallId required");
    const onEvent = opts.onEvent || function () {};

    isMuted = false;
    onEvent({ type: "connecting" });

    // Custom params land in Twilio's POST to our /api/twilio/voice TwiML
    // endpoint as form fields. Backend reads pending_call_id to look up the
    // call context (project, location, target number).
    activeCall = await device.connect({
      params: {
        pending_call_id: String(opts.pendingCallId),
        // target_phone is informational; backend reads from the pending row,
        // not from this param. Included for log clarity.
        target_phone: opts.targetPhone,
      },
    });

    activeCall.on("accept", () => onEvent({ type: "connected" }));
    activeCall.on("disconnect", () => {
      activeCall = null; isMuted = false;
      onEvent({ type: "disconnected" });
    });
    activeCall.on("cancel", () => {
      activeCall = null; isMuted = false;
      onEvent({ type: "disconnected" });
    });
    activeCall.on("error", (e) => {
      activeCall = null; isMuted = false;
      onEvent({
        type: "error",
        code: _errCode(e),
        message: (e && e.message) || "Call error",
        friendly: friendlyError(e),
      });
    });

    return activeCall;
  }

  function mute(shouldMute) {
    if (!activeCall) return;
    isMuted = !!shouldMute;
    try { activeCall.mute(isMuted); } catch (e) {
      console.warn("[TwilioVoice] mute failed:", e);
    }
  }

  function hangup() {
    if (activeCall) {
      try { activeCall.disconnect(); } catch (e) {
        console.warn("[TwilioVoice] disconnect failed:", e);
      }
    }
  }

  function sendDigits(digits) {
    if (!activeCall) return false;
    try { activeCall.sendDigits(String(digits)); return true; }
    catch (e) {
      console.warn("[TwilioVoice] sendDigits failed:", e);
      return false;
    }
  }

  function isInCall() { return !!activeCall; }
  function muted()    { return isMuted; }

  function teardown() {
    hangup();
    if (device) {
      try { device.destroy(); } catch (_) { /* ignore */ }
      device = null;
    }
  }

  EA.TwilioVoice = {
    init, dial, mute, hangup, sendDigits, isInCall, muted, teardown,
    friendlyError,
  };
})();
