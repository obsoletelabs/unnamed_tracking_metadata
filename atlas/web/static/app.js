// Progressive enhancement only; every page works without this file.
document.addEventListener("click", function (e) {
  var el = e.target.closest("[data-confirm]");
  if (el && !window.confirm(el.getAttribute("data-confirm"))) { e.preventDefault(); }
});

// In-page image viewer: loads the same panel the stand-alone page shows into a modal <dialog>.
(function () {
  var dlg = document.getElementById("viewer");
  if (!dlg || typeof dlg.showModal !== "function") { return; }
  var body = document.getElementById("viewer-body");
  var opener = null;
  function panelUrl(url) { return url + (url.indexOf("?") < 0 ? "?panel=1" : "&panel=1"); }
  function load(url, focusSel) {
    return fetch(panelUrl(url), {credentials: "same-origin"}).then(function (r) {
      if (!r.ok) { throw new Error("failed"); }
      return r.text();
    }).then(function (html) {
      body.innerHTML = html;
      var close = document.createElement("button");
      close.type = "button"; close.className = "secondary small viewer-close"; close.textContent = "Close (Esc)";
      close.addEventListener("click", function () { dlg.close(); });
      body.insertBefore(close, body.firstChild);
      var target = (focusSel && body.querySelector(focusSel)) || close;
      target.focus();
    });
  }
  function step(dir) {
    var a = body.querySelector('[data-nav="' + dir + '"]');
    if (a) { load(a.getAttribute("href"), '[data-nav="' + dir + '"]'); }
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("a[data-viewer]");
    if (t && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault(); opener = t;
      dlg.showModal();
      load(t.getAttribute("href")).catch(function () { window.location.href = t.getAttribute("href"); });
      return;
    }
    var nav = e.target.closest("#viewer [data-nav]");
    if (nav) { e.preventDefault(); load(nav.getAttribute("href"), '[data-nav="' + nav.getAttribute("data-nav") + '"]'); return; }
    if (e.target === dlg) { dlg.close(); }
  });
  document.addEventListener("submit", function (e) {
    var f = e.target.closest("#viewer form[data-panel-form]");
    if (!f) { return; }
    e.preventDefault();
    var panelPath = f.getAttribute("action").replace(/\/(vote|default)$/, "");
    var data = new FormData(f);
    if (e.submitter && e.submitter.name) { data.append(e.submitter.name, e.submitter.value); }
    fetch(f.getAttribute("action"), {method: "POST", body: data, credentials: "same-origin"}).then(function (r) {
      if (!r.ok) { throw new Error("rejected"); }
      var m = /[?&]m=([a-z]+)/.exec(r.url);
      return load(panelPath + (m ? "?m=" + m[1] : ""));
    }).catch(function () {
      var p = document.createElement("p"); p.className = "flash"; p.setAttribute("role", "alert");
      p.textContent = "That did not work. Open the image on its own page to see why.";
      body.insertBefore(p, body.firstChild.nextSibling);
    });
  });
  document.addEventListener("keydown", function (e) {
    if (!dlg.open) { return; }
    if (e.key === "ArrowLeft") { step("prev"); } else if (e.key === "ArrowRight") { step("next"); }
  });
  dlg.addEventListener("close", function () {
    body.innerHTML = "";
    if (opener) { opener.focus(); }
    if (location.hash.indexOf("#view-") === 0) { history.replaceState(null, "", location.pathname + location.search); }
  });
  // A link such as /game/<id>#view-<image id> opens that image straight away.
  var m = /^#view-([0-9A-Za-z]+)$/.exec(location.hash);
  if (m) {
    var tile = document.querySelector('a[data-viewer][href$="/artwork/' + m[1] + '"]');
    if (tile) { tile.click(); }
  }
})();
