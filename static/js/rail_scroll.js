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
  }

  document.querySelectorAll("[data-rail]").forEach(initRail);
})();
