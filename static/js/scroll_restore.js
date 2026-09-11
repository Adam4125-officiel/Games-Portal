// Restores scroll position after a full-page navigation (a refresh, a form
// Save redirecting back to the same page, clicking a link) - browsers only
// do this automatically for back/forward navigation, not for a fresh GET, so
// every save/refresh here used to jump straight back to the top of the page.
// Keyed by pathname+search (sessionStorage, so it never leaks between tabs
// or across visitors sharing a machine) - a per-viewer convenience, so a
// failure to read/write it is silently ignored rather than surfaced.
(function () {
  var KEY_PREFIX = "games-portal-scroll:";

  // Without this, the browser's own automatic scroll restoration on a
  // reload can fire after ours and stomp over it with its own idea of the
  // position - this script is the sole authority instead.
  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }

  function storageKey() {
    return KEY_PREFIX + location.pathname + location.search;
  }

  function restore() {
    var saved;
    try {
      saved = sessionStorage.getItem(storageKey());
    } catch (e) {
      return;
    }
    if (saved !== null) {
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  }

  function save() {
    try {
      sessionStorage.setItem(storageKey(), String(window.scrollY));
    } catch (e) {
      // Storage can throw in a private window or with site data blocked -
      // losing the saved position is harmless, so just skip it.
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restore);
  } else {
    restore();
  }
  // Restored again once everything (including lazy images further down the
  // page) has settled - a page tall enough that a saved position exceeds
  // its height-so-far at DOMContentLoaded would otherwise clamp short.
  window.addEventListener("load", restore);
  // Both events, for browser coverage: Safari favors pagehide (and may skip
  // beforeunload on a real navigation), others still fire beforeunload.
  window.addEventListener("pagehide", save);
  window.addEventListener("beforeunload", save);
})();
