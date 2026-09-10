// Generic confirm() guard: any <form> submitted via a button carrying
// data-confirm shows that text first. Nothing to wire up per-page - just add
// the attribute to a submit button.
document.addEventListener("submit", function (e) {
  var submitter = e.submitter;
  if (submitter && submitter.dataset.confirm && !window.confirm(submitter.dataset.confirm)) {
    e.preventDefault();
  }
});
