// Instant client-side filter for /collections - the whole list is already on
// the page (it's local DB data, not a Steam call), so there's no reason to
// round-trip to the server on every keystroke the way the main search page
// deliberately does (see CLAUDE.md). A game with more than one Steam genre
// appears in more than one row's rail, so this also hides a whole row once
// every card in it is filtered out, rather than leaving an empty heading.
(function () {
  var input = document.getElementById("collections-filter");
  var grid = document.getElementById("collections-grid");
  var noMatch = document.getElementById("collections-no-match");
  if (!input || !grid) return;

  var cards = grid.querySelectorAll(".rail-card");
  var rows = grid.querySelectorAll("[data-collection-row]");

  input.addEventListener("input", function () {
    var term = input.value.trim().toLowerCase();
    var visible = 0;
    cards.forEach(function (card) {
      var matches = !term || (card.dataset.name || "").indexOf(term) !== -1;
      // The card's rail__item wrapper is what actually needs hiding - hiding
      // the card alone would leave an empty gap where it sat in the track.
      card.closest(".rail__item").hidden = !matches;
      if (matches) visible += 1;
    });
    rows.forEach(function (row) {
      row.hidden = row.querySelector(".rail__item:not([hidden])") === null;
    });
    if (noMatch) noMatch.hidden = visible !== 0;
  });
})();
