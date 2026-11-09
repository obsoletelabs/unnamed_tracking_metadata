// Progressive enhancement only; every page works without this file.
document.addEventListener("click", function (e) {
  var toggle = e.target.closest("[data-password-toggle]");
  if (toggle) {
    var input = document.getElementById(toggle.getAttribute("data-password-toggle"));
    if (input) {
      var visible = input.type === "password";
      input.type = visible ? "text" : "password";
      toggle.setAttribute("aria-pressed", visible ? "true" : "false");
      toggle.setAttribute("aria-label", visible ? "Hide password" : "Show password");
    }
    return;
  }
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

// Hidden achievements: the real text is fetched only when a signed-in member asks for it.
document.addEventListener("click", function (e) {
  var b = e.target.closest("a[data-reveal]");
  if (!b) { return; }
  e.preventDefault();
  fetch(b.getAttribute("data-reveal"), {credentials: "same-origin"}).then(function (r) {
    if (!r.ok) { throw new Error("failed"); }
    return r.json();
  }).then(function (d) {
    var row = b.closest("tr");
    var name = row.querySelector("[data-hidden-name]");
    if (name) { name.textContent = d.name; }
    var cell = b.parentNode;
    cell.textContent = d.description || "";
  }).catch(function () { window.location.href = b.getAttribute("href"); });
});

// Achievement editor: add, remove and reorder rows in the browser (the page also works without this).
(function () {
  var rows = document.getElementById("ach-rows");
  var tpl = document.getElementById("ach-row-template");
  if (!rows || !tpl) { return; }
  var counter = 0;
  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add-row]");
    if (add) {
      e.preventDefault();
      counter += 1;
      var key = "j" + Date.now().toString(36) + counter;
      var holder = document.createElement("div");
      holder.innerHTML = tpl.innerHTML.split("NEWKEY").join(key);
      var row = holder.firstElementChild;
      rows.appendChild(row);
      var first = row.querySelector("input[type=text], input:not([type])");
      if (first) { first.focus(); }
      return;
    }
    var mv = e.target.closest("[data-move]");
    if (mv) {
      var fs = mv.closest("[data-row]");
      var other = mv.getAttribute("data-move") === "up" ? fs.previousElementSibling : fs.nextElementSibling;
      if (other) { rows.insertBefore(mv.getAttribute("data-move") === "up" ? fs : other, mv.getAttribute("data-move") === "up" ? other : fs); mv.focus(); }
    }
  });
  rows.addEventListener("change", function (e) {
    var rm = e.target.closest("[data-remove]");
    if (rm) { rm.closest("[data-row]").classList.toggle("removed", rm.checked); }
  });
  rows.addEventListener("input", function (e) {
    if (e.target.name && e.target.name.indexOf("name.") === 0) {
      var t = e.target.closest("[data-row]").querySelector(".ach-title");
      if (t) { t.textContent = e.target.value || "New achievement"; }
    }
  });
})();

// Owner-group link flow: the hand-over form to the other node submits itself (its button stays for no-JS).
document.addEventListener("DOMContentLoaded", function () {
  var f = document.querySelector("form[data-autosubmit]");
  if (f) { f.submit(); }
});

// Phone header (U1): the Menu button opens and closes the main navigation. Without scripts the navigation is
// simply always shown (the collapse needs html.js, set by early.js). Escape closes it and returns focus.
(function () {
  var toggle = document.querySelector("[data-nav-toggle]");
  var nav = document.getElementById("site-nav");
  if (!toggle || !nav) { return; }
  function set(open) {
    nav.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  toggle.addEventListener("click", function () { set(toggle.getAttribute("aria-expanded") !== "true"); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && nav.classList.contains("open")) { set(false); toggle.focus(); }
  });
})();

// A tab bar that scrolls sideways on a phone starts with its current tab in view (no vertical page jump).
(function () {
  var bars = document.querySelectorAll("[data-scroll-current]");
  for (var i = 0; i < bars.length; i += 1) {
    var bar = bars[i], cur = bar.querySelector("[aria-current=page]");
    if (cur && bar.scrollWidth > bar.clientWidth) {
      bar.scrollLeft = Math.max(0, cur.offsetLeft - bar.offsetLeft - 110);  // keeps the group label in view too
    }
  }
})();

// One-time date-format offer (SPEC-UI-POLISH-PASS §7.4). The server renders it hidden only while no format was
// chosen; it is shown to an en-US browser in a US time zone (both, H-UI3). Dismissing hides it in this browser.
var US_ZONES = ["America/New_York", "America/Detroit", "America/Chicago", "America/Menominee", "America/Denver",
  "America/Boise", "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "America/Juneau", "America/Sitka",
  "America/Metlakatla", "America/Yakutat", "America/Nome", "America/Adak", "Pacific/Honolulu", "America/Puerto_Rico"];
var US_ZONE_PREFIXES = ["America/Kentucky/", "America/Indiana/", "America/North_Dakota/"];
function atlasLooksUS(lang, tz) {
  if (lang !== "en-US" || !tz) { return false; }
  if (US_ZONES.indexOf(tz) >= 0) { return true; }
  for (var i = 0; i < US_ZONE_PREFIXES.length; i += 1) { if (tz.indexOf(US_ZONE_PREFIXES[i]) === 0) { return true; } }
  return false;
}
(function () {
  var offer = document.querySelector("[data-date-offer]");
  if (!offer) { return; }
  var dismissed = false;
  try { dismissed = window.localStorage.getItem("atlas.dateOfferDismissed") === "1"; } catch (e) { dismissed = false; }
  var lang = (navigator.languages && navigator.languages[0]) || navigator.language || "";
  var tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { tz = ""; }
  if (!dismissed && atlasLooksUS(lang, tz)) { offer.hidden = false; }
  var close = offer.querySelector("[data-date-offer-dismiss]");
  if (close) {
    close.addEventListener("click", function () {
      offer.hidden = true;
      try { window.localStorage.setItem("atlas.dateOfferDismissed", "1"); } catch (e) { /* private mode */ }
    });
  }
})();

// Copy buttons (§5.4): [data-copy="<id>"] copies that element's text (or value) and announces "Copied".
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]");
  if (!b) { return; }
  e.preventDefault();
  var el = document.getElementById(b.getAttribute("data-copy"));
  if (!el) { return; }
  var text = el.value !== undefined && el.tagName !== "CODE" ? el.value : el.textContent;
  var live = document.getElementById("live-status");
  function done(ok) {
    if (live) { live.textContent = ok ? "Copied" : "Select the text and copy it"; }
    b.setAttribute("data-copied", ok ? "yes" : "no");
  }
  function fallback() {
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    done(ok);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text.trim()).then(function () { done(true); }, fallback);
  } else { fallback(); }
});

// Join form (§5.3): a pasted invite payload {"v":1,"type":"atlas-group-invite","url","code"} fills both fields.
document.addEventListener("paste", function (e) {
  var input = e.target;
  if (!input || input.id !== "j-url") { return; }
  var text = (e.clipboardData || window.clipboardData).getData("text");
  var data = null;
  try { data = JSON.parse(text); } catch (err) { return; }
  if (!data || data.type !== "atlas-group-invite" || data.v !== 1 || !data.url || !data.code) { return; }
  e.preventDefault();
  input.value = data.url;
  var code = document.getElementById("j-code");
  if (code) { code.value = data.code; }
  var live = document.getElementById("live-status");
  if (live) { live.textContent = "Address and code filled from the invite"; }
});

// Achievement editor toolbar (§8.1 AE3): filter rows by name, expand or collapse every row, count changes.
(function () {
  var rows = document.getElementById("ach-rows");
  var bar = document.querySelector("[data-ach-toolbar]");
  if (!rows || !bar) { return; }
  bar.hidden = false;
  var filter = bar.querySelector("[data-ach-filter]");
  var counter = document.querySelector("[data-ach-count]");
  var start = {};
  function each(fn) { var list = rows.querySelectorAll("[data-row]"); for (var i = 0; i < list.length; i += 1) { fn(list[i]); } }
  function snapshot(row) {
    var out = [];
    var inputs = row.querySelectorAll("input, select, textarea");
    for (var i = 0; i < inputs.length; i += 1) {
      var el = inputs[i];
      out.push(el.type === "checkbox" ? String(el.checked) : (el.type === "file" ? String(el.files && el.files.length) : el.value));
    }
    return out.join("\u0001");
  }
  each(function (row) { start[row.getAttribute("data-row-key")] = snapshot(row); });
  function count() {
    if (!counter) { return; }
    var n = 0, changed = 0;
    each(function (row) { n += 1; if (start[row.getAttribute("data-row-key")] !== snapshot(row)) { changed += 1; } });
    counter.textContent = n + " row" + (n === 1 ? "" : "s") + " · " + changed + " changed";
  }
  if (filter) {
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      each(function (row) {
        var name = row.querySelector("input[name^='name.']");
        row.hidden = q !== "" && (!name || name.value.toLowerCase().indexOf(q) < 0);
      });
    });
  }
  bar.addEventListener("click", function (e) {
    var t = e.target.closest("[data-ach-expand]");
    if (!t) { return; }
    var open = t.getAttribute("data-ach-expand") === "open";
    each(function (row) { row.open = open; });
  });
  rows.addEventListener("input", count);
  rows.addEventListener("change", count);
  new MutationObserver(count).observe(rows, {childList: true});
  count();
})();
