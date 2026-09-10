// Applied immediately (this script is a blocking <head> include, not deferred)
// so the right theme is set before first paint - no saved choice falls back to
// the OS preference.
(function () {
  var STORAGE_KEY = "games-portal-theme";

  // Only "light" is a real override in style.css - the bare :root tokens
  // already are the dark theme, so "dark" is just the attribute's absence.
  function apply(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  var saved = null;
  try {
    saved = localStorage.getItem(STORAGE_KEY);
  } catch (e) {
    // Storage can throw in a private window or with site data blocked - fall
    // back to the OS preference below rather than breaking the page.
  }
  apply(saved);

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var isLight = document.documentElement.getAttribute("data-theme") === "light";
      var next = isLight ? "dark" : "light";
      apply(next);
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch (e) {
        // Nothing to do - the toggle still works for this page view.
      }
    });
  });
})();
