// Powers the horizontally-scrolling rails ("Recently added" on the search
// page, each genre row on /collections) - a Jellyfin-style left/right arrow
// pair that smooth-scrolls the track, on top of plain touch/trackpad
// scrolling that already works without any JS at all.
(function () {
  function initRail(rail) {
    var track = rail.querySelector("[data-rail-track]");
    var prev = rail.querySelector("[data-rail-prev]");
    var next = rail.querySelector("[data-rail-next]");
    if (!track) return;

    function scrollAmount() {
      return Math.max(track.clientWidth * 0.8, 200);
    }

    // A row that already fits entirely doesn't need arrows at all - showing
    // a pair that can't actually scroll anything just looks broken.
    function updateArrows() {
      var scrollable = track.scrollWidth > track.clientWidth + 1;
      if (prev) prev.hidden = !scrollable;
      if (next) next.hidden = !scrollable;
    }

    if (prev) {
      prev.addEventListener("click", function () {
        track.scrollBy({ left: -scrollAmount(), behavior: "smooth" });
      });
    }
    if (next) {
      next.addEventListener("click", function () {
        track.scrollBy({ left: scrollAmount(), behavior: "smooth" });
      });
    }

    updateArrows();
    window.addEventListener("resize", updateArrows);
  }

  document.querySelectorAll("[data-rail]").forEach(initRail);
})();
