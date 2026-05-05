/* ========================================================================
   page_router.js — HTMX-style content-swap navigation.

   Click on an intra-app sidebar link → fetch the target URL with the
   HX-Request header, swap the <main id="app-main"> content, pushState to
   update the URL. Base shell (sidebar, topbar, impersonation banner) stays
   mounted. Back/forward work via popstate + per-URL scroll restore.

   Pages opt in by calling EA.PageRouter.register({match, init, teardown}):
     - match:    path string | RegExp | fn(URL) -> bool
     - init:     fn(root) -> { teardown?: fn } | void
     - teardown: optional top-level (if not returned from init)

   Unregistered routes fall through to native navigation. Links with the
   data-no-swap attribute are never intercepted (used for /app/grade which
   holds mic streams + MediaRecorder state that can't survive a DOM swap).

   Pairs with a Flask render helper that, on HX-Request, returns only the
   content block (no <html>/<head>/<body>/sidebar shell).
   ======================================================================== */

(function () {
  "use strict";

  if (!window.EA) {
    console.error("page_router.js requires window.EA (app.js).");
    return;
  }
  const EA = window.EA;

  // Registered pages. Newer registrations replace older ones with the same
  // matchKey so re-entering a page after swap doesn't leak closures.
  const pages = [];
  const beforeSwapGuards = [];   // [fn(URL) -> bool | Promise<bool>]
  let currentTeardown = null;

  // ── Match building ──
  function buildMatch(spec) {
    if (typeof spec === "string") {
      return { key: "s:" + spec, fn: (u) => u.pathname === spec };
    }
    if (spec instanceof RegExp) {
      return { key: "r:" + spec.source + ":" + spec.flags, fn: (u) => spec.test(u.pathname) };
    }
    if (typeof spec === "function") {
      return { key: "f:" + spec.toString().slice(0, 80), fn: spec };
    }
    return null;
  }

  function register(spec) {
    const m = buildMatch(spec && spec.match);
    if (!m) { console.error("PageRouter.register: bad match", spec); return; }
    const entry = {
      key:      m.key,
      match:    m.fn,
      init:     spec.init,
      teardown: spec.teardown || null,
    };
    const idx = pages.findIndex((p) => p.key === entry.key);
    if (idx >= 0) pages[idx] = entry;
    else pages.push(entry);
  }

  function findPage(url) {
    // Iterate newest-first so the latest registration wins in case of overlap.
    for (let i = pages.length - 1; i >= 0; i--) {
      if (pages[i].match(url)) return pages[i];
    }
    return null;
  }

  function registerBeforeSwap(fn) {
    beforeSwapGuards.push(fn);
    return function off() {
      const i = beforeSwapGuards.indexOf(fn);
      if (i >= 0) beforeSwapGuards.splice(i, 1);
    };
  }

  async function runGuards(nextUrl) {
    for (const g of beforeSwapGuards) {
      let ok = true;
      try { ok = await g(nextUrl); }
      catch (e) { console.error("Guard error:", e); ok = true; }
      if (!ok) return false;
    }
    return true;
  }

  // ── Scroll restore per URL ──
  function scrollKey() {
    return "pr:scroll:" + window.location.pathname + window.location.search;
  }
  function saveScroll() {
    try { sessionStorage.setItem(scrollKey(), String(window.scrollY)); }
    catch (_) { /* private mode */ }
  }
  function restoreScroll() {
    let y = 0;
    try { y = parseInt(sessionStorage.getItem(scrollKey()) || "0", 10); }
    catch (_) { y = 0; }
    window.scrollTo(0, isNaN(y) ? 0 : y);
  }

  // ── Active sidebar link ──
  // Mirrors base.html's Jinja logic: exact match on /app, prefix match on
  // everything else. Runs after each successful swap.
  function updateSidebarActive() {
    const path = window.location.pathname;
    const links = document.querySelectorAll(".sidebar-nav a[href]");
    links.forEach((a) => {
      const href = a.getAttribute("href");
      if (!href || !href.startsWith("/")) return;
      let linkPath;
      try { linkPath = new URL(href, window.location.origin).pathname; }
      catch (_) { return; }
      let active;
      if (linkPath === "/app") {
        active = (path === "/app");
      } else {
        active = (path === linkPath || path.startsWith(linkPath + "/"));
      }
      a.classList.toggle("active", active);
    });
  }

  // ── Script injection after innerHTML swap ──
  // fetch() + innerHTML parses <script> nodes but doesn't execute them. We
  // clone each one: external srcs dedupe against already-loaded scripts and
  // get promoted to <head> so they persist; inline scripts run in place.
  // Awaits external script loads so the page's inline IIFE runs *after*
  // its defer'd dependencies (Chart.js, dashboard_widget, etc).
  //
  // ⚠️ INVARIANT — IIFE WRAP IS REQUIRED for every inline page script that
  // participates in content-swap. On re-entry the same template's
  // <script> tag is cloned and re-executed: any top-level `const`, `let`,
  // or `class` declaration from the first run is still bound to the global
  // scope, and the second execution throws `Identifier 'X' has already
  // been declared` — breaking navigation entirely.
  //
  // WRAP the body of every inline script in:
  //     (function () { "use strict"; … })();
  // …so re-execution opens a fresh lexical scope and the prior bindings
  // remain safely hidden as harmless closure-captured values. Export any
  // state you actually need to share via `window.*` or the EA namespace.
  async function runInlineScripts(root) {
    const scripts = Array.from(root.querySelectorAll("script"));
    for (const old of scripts) {
      const src = old.getAttribute("src");
      if (src) {
        if (document.querySelector('script[src="' + cssEsc(src) + '"]')) {
          old.remove();
          continue;
        }
        await new Promise((resolve) => {
          const n = document.createElement("script");
          for (const a of old.attributes) n.setAttribute(a.name, a.value);
          n.onload = resolve;
          n.onerror = resolve;   // tolerate load errors, continue navigation
          document.head.appendChild(n);
        });
        old.remove();
      } else {
        const n = document.createElement("script");
        for (const a of old.attributes) n.setAttribute(a.name, a.value);
        n.text = old.textContent;
        old.parentNode.replaceChild(n, old);
      }
    }
  }

  function cssEsc(s) {
    return String(s).replace(/[\\"']/g, "\\$&");
  }

  // ── Core swap ──
  async function swap(url, opts) {
    opts = opts || {};
    const push     = opts.push     !== false;
    const restoreY = opts.restoreY === true;

    const u = new URL(url, window.location.origin);
    const target = findPage(u);
    if (!target) {
      // Unknown route → full nav
      window.location.href = u.href;
      return;
    }

    if (!(await runGuards(u))) return;

    if (push) saveScroll();

    let resp;
    try {
      resp = await fetch(u.href, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { "HX-Request": "true", "Accept": "text/html" },
      });
    } catch (err) {
      if (EA.toast) EA.toast(err.message || "Navigation failed", "error");
      return;
    }

    // 4xx → full nav so the browser follows auth redirects / 404 pages.
    if (resp.status >= 400 && resp.status < 500) {
      window.location.href = u.href;
      return;
    }
    if (!resp.ok) {
      if (EA.toast) EA.toast("Server error (" + resp.status + ")", "error");
      return;
    }

    const html = await resp.text();

    // Tear down the outgoing page before nuking its DOM so teardown code can
    // still see/interact with live elements if it wants to.
    if (typeof currentTeardown === "function") {
      try { currentTeardown(); }
      catch (e) { console.error("Teardown error:", e); }
      currentTeardown = null;
    }

    const main = document.getElementById("app-main");
    if (!main) { window.location.href = u.href; return; }
    main.innerHTML = html;

    await runInlineScripts(main);

    if (push) {
      history.pushState({ url: u.href, ts: Date.now() }, "", u.href);
    }

    updateSidebarActive();
    if (typeof window.__refreshBreadcrumb === "function") {
      try { window.__refreshBreadcrumb(); }
      catch (e) { console.error("Breadcrumb refresh error:", e); }
    }

    if (restoreY) restoreScroll();
    else window.scrollTo(0, 0);

    // Re-look-up in case the page re-registered with a fresh closure during
    // runInlineScripts (expected path for per-fragment inline IIFEs).
    const fresh = findPage(u);
    if (fresh && typeof fresh.init === "function") {
      try {
        const result = fresh.init(main);
        currentTeardown = (result && typeof result.teardown === "function")
          ? result.teardown
          : fresh.teardown;
      } catch (e) {
        console.error("Page init error:", e);
      }
    }
  }

  // ── Click interception ──
  document.addEventListener("click", function (ev) {
    if (ev.defaultPrevented) return;
    if (ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;

    const a = ev.target.closest("a");
    if (!a) return;
    if (a.dataset.noSwap != null) return;          // explicit opt-out
    if (a.target && a.target !== "_self") return;  // _blank etc.
    if (a.hasAttribute("download")) return;

    const href = a.getAttribute("href");
    if (!href || href.startsWith("#")) return;

    let u;
    try { u = new URL(href, window.location.origin); }
    catch (_) { return; }
    if (u.origin !== window.location.origin) return;

    // Only intercept if a page is registered for the target URL.
    if (!findPage(u)) return;

    ev.preventDefault();
    swap(u.href);
  });

  // ── Back/forward ──
  window.addEventListener("popstate", function () {
    swap(window.location.href, { push: false, restoreY: true });
  });

  // ── Initial boot: run the registered init for the starting URL so
  //    full-page loads and post-swap renders converge on the same codepath.
  document.addEventListener("DOMContentLoaded", function () {
    history.replaceState(
      { url: window.location.href, ts: Date.now() },
      "",
      window.location.href
    );

    const u = new URL(window.location.href);
    const target = findPage(u);
    if (target && typeof target.init === "function") {
      const main = document.getElementById("app-main");
      try {
        const result = target.init(main);
        currentTeardown = (result && typeof result.teardown === "function")
          ? result.teardown
          : target.teardown;
      } catch (e) {
        console.error("Initial page init error:", e);
      }
    }
  });

  window.EA.PageRouter = {
    register:          register,
    registerBeforeSwap: registerBeforeSwap,
    navigate:          (url) => swap(url),
  };
})();
