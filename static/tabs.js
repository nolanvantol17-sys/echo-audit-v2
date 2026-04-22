// EA.Tabs — tiny tab-strip primitive.
//
// Reuses the existing `.tabs` / `.tab-btn` / `.tab-btn.active` styles in
// app.css (the same look-and-feel used in Settings). Pairs each
// `<button class="tab-btn" data-tab="X">` with a sibling
// `<section class="tab-panel" data-tab-panel="X">` inside the same container,
// flipping the `active` class on the button and the `hidden` attribute on the
// panel. We re-scan panels on every activate() call so panels added after
// init() (e.g. lazy-mounted editors) are picked up automatically.
(function () {
  "use strict";
  const EA = (window.EA = window.EA || {});

  function init(opts) {
    opts = opts || {};
    const container = opts.container;
    if (!container) throw new Error("EA.Tabs.init: container is required");
    const onActivate = typeof opts.onActivate === "function" ? opts.onActivate : null;

    const buttons = Array.prototype.slice.call(
      container.querySelectorAll(".tab-btn[data-tab]")
    );
    let current = null;

    function panels() {
      return Array.prototype.slice.call(
        container.querySelectorAll(".tab-panel[data-tab-panel]")
      );
    }

    function activate(name) {
      if (!name || name === current) return;
      let matched = false;
      buttons.forEach(function (b) {
        const on = b.dataset.tab === name;
        b.classList.toggle("active", on);
        if (on) matched = true;
      });
      if (!matched) return;
      panels().forEach(function (p) {
        p.hidden = p.dataset.tabPanel !== name;
      });
      current = name;
      if (onActivate) {
        try { onActivate(name); } catch (_) {}
      }
    }

    function onClick(e) {
      const btn = e.target.closest(".tab-btn[data-tab]");
      if (!btn || !container.contains(btn)) return;
      activate(btn.dataset.tab);
    }
    container.addEventListener("click", onClick);

    const initial = opts.defaultTab || (buttons[0] && buttons[0].dataset.tab);
    if (initial) activate(initial);

    return {
      activate: activate,
      getActive: function () { return current; },
      destroy: function () { container.removeEventListener("click", onClick); },
    };
  }

  EA.Tabs = { init: init };
})();
