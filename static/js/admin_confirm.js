// Guards any form containing a submit button with a `data-confirm` message -
// used everywhere a destructive admin action (deleting a request, installing
// an update, restoring the database) must not fire from a single stray click.
// A generic external script rather than an inline onsubmit handler because
// this app's own CSP (script-src 'self') blocks inline event handlers.
(function () {
  document.querySelectorAll("form").forEach(function (form) {
    var button = form.querySelector("button[data-confirm]");
    if (!button) return;
    form.addEventListener("submit", function (e) {
      if (!window.confirm(button.dataset.confirm)) {
        e.preventDefault();
      }
    });
  });
})();
