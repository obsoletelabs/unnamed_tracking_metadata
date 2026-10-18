// Progressive enhancement only; every page works without this file.
document.addEventListener("click", function (e) {
  var el = e.target.closest("[data-confirm]");
  if (el && !window.confirm(el.getAttribute("data-confirm"))) { e.preventDefault(); }
});
