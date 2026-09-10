// Guards the "Update now" button with a plain confirm() - this is the most
// powerful action in the admin panel (it installs and executes new code), so a
// stray click must not be enough on its own.
(function () {
  var form = document.getElementById("update-form");
  if (!form) return;
  form.addEventListener("submit", function (e) {
    var button = document.getElementById("update-trigger");
    var message = (button && button.dataset.confirm) || "Update this portal now?";
    if (!window.confirm(message)) {
      e.preventDefault();
    }
  });
})();
