// Instant client-side filter for /collections - the whole list is already on
// the page (it's local DB data, not a Steam call), so there's no reason to
// round-trip to the server on every keystroke the way the main search page
// deliberately does (see CLAUDE.md).
(function () {
  var input = document.getElementById("collections-filter");
  var grid = document.getElementById("collections-grid");
  var noMatch = document.getElementById("collections-no-match");
  if (!input || !grid) return;

  var cards = grid.querySelectorAll(".result-card");

  input.addEventListener("input", function () {
    var term = input.value.trim().toLowerCase();
    var visible = 0;
    cards.forEach(function (card) {
      var matches = !term || (card.dataset.name || "").indexOf(term) !== -1;
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    if (noMatch) noMatch.hidden = visible !== 0;
  });
})();
