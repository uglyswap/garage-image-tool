/* Garage Image Tool — frontend (vanilla JS, aucune dépendance) */
"use strict";

var PSEUDO = [
  { id: "_original", name: "Conserver le fond d'origine", icon: "🖼️" },
  { id: "_transparent", name: "Fond transparent (PNG)", icon: "✨" }
];

var state = { files: [], bg: "_original", bgs: [], jobId: null, health: null, lastJob: null, auth: null, wired: false };

function $(id) { return document.getElementById(id); }
function sleep(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
/* Message lisible depuis le champ "detail" de FastAPI (chaîne ou liste d'erreurs de validation) */
function detailText(detail) {
  if (Array.isArray(detail)) return detail.map(function (d) { return d.msg || ""; }).join(", ");
  return detail ? String(detail) : "";
}
function fmtSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + " Mo" : Math.max(1, Math.round(b / 1024)) + " Ko"; }

/* ------------------------------ Authentification --------------------------- */
/* fetch qui renvoie à l'écran de connexion sur 401 (l'erreur porte auth = true) */
async function api(url, opts) {
  var res = await fetch(url, opts);
  if (res.status === 401) {
    showLogin();
    var e = new Error("Authentification requise");
    e.auth = true;
    throw e;
  }
  return res;
}

function showLogin() {
  $("login").hidden = false;
  $("login-error").textContent = "";
  $("login-password").value = "";
  $("login-password").focus();
}

async function submitLogin(ev) {
  ev.preventDefault();
  $("login-error").textContent = "";
  try {
    var res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("login-password").value })
    });
    if (res.status === 204) {
      $("login").hidden = true;
      state.auth = { auth_required: true, authenticated: true };
      await start();
      return;
    }
    var err = await res.json().catch(function () { return {}; });
    $("login-error").textContent = res.status === 429
      ? "Trop d'essais, réessayez dans quelques minutes."
      : (err.detail || "Connexion impossible");
  } catch (e) {
    $("login-error").textContent = "Erreur réseau : " + e.message;
  }
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" }).catch(function () {});
  location.reload();
}

/* ------------------------------ Initialisation ----------------------------- */
async function init() {
  try {
    state.auth = await fetch("/api/auth/status").then(function (r) { return r.json(); });
  } catch (e) {
    state.auth = null;
  }
  $("login-form").addEventListener("submit", submitLogin);
  $("btn-logout").addEventListener("click", logout);
  $("open-banner").hidden = !(state.auth && state.auth.auth_required === false);
  if (state.auth && state.auth.auth_required && !state.auth.authenticated) {
    showLogin();
    return;
  }
  await start();
}

async function start() {
  $("btn-logout").hidden = !(state.auth && state.auth.auth_required);
  try {
    var h = await api("/api/health").then(function (r) { return r.json(); });
    state.health = h;
    $("version").textContent = "v" + h.version;
    $("engine-badge").textContent = h.plate_engine === "yolo"
      ? "🧠 Plaques : YOLOv8 · IA locale"
      : "⚠️ Plaques : cascade Haar (repli)";
    $("max-files").textContent = h.max_files;
    $("max-mb").textContent = h.max_file_mb;
  } catch (e) {
    if (e.auth) return;
    $("engine-badge").textContent = "⚠️ API indisponible";
  }
  await refreshBackgrounds();
  if (!state.wired) { wire(); state.wired = true; }
}

async function refreshBackgrounds() {
  try {
    state.bgs = await api("/api/backgrounds").then(function (r) { return r.json(); });
  } catch (e) { state.bgs = []; }
  renderBackgrounds();
}

/* -------------------------------- Fonds ------------------------------------ */
/* Rend un élément non-bouton activable au clavier (Entrée / Espace). handler(viaKeyboard) */
function makeActivatable(el, handler, pressed) {
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  if (pressed !== undefined) el.setAttribute("aria-pressed", String(pressed));
  el.addEventListener("click", function () { handler(false); });
  el.addEventListener("keydown", function (ev) {
    if (ev.target !== el) return; /* ne pas capter Entrée sur le bouton ✕ interne */
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); handler(true); }
  });
}

function renderBackgrounds(focusId) {
  var list = $("bg-list");
  list.innerHTML = "";
  PSEUDO.forEach(function (p) {
    var el = document.createElement("div");
    el.className = "bg-item pseudo" + (state.bg === p.id ? " selected" : "");
    el.setAttribute("data-id", p.id);
    el.setAttribute("aria-label", p.name);
    el.innerHTML = '<span class="bg-icon">' + p.icon + '</span><span class="bg-name">' + p.name + '</span>';
    makeActivatable(el, function (viaKey) { state.bg = p.id; renderBackgrounds(viaKey ? p.id : null); }, state.bg === p.id);
    list.appendChild(el);
  });
  state.bgs.forEach(function (b) {
    var el = document.createElement("div");
    el.className = "bg-item" + (state.bg === b.id ? " selected" : "");
    el.setAttribute("data-id", b.id);
    el.setAttribute("aria-label", b.name);
    el.innerHTML = '<img src="' + b.url + '" alt="" loading="lazy">' +
      '<span class="bg-name">' + esc(b.name) + '</span>' +
      '<button class="bg-del" title="Supprimer" aria-label="Supprimer ce fond">✕</button>';
    makeActivatable(el, function (viaKey) { state.bg = b.id; renderBackgrounds(viaKey ? b.id : null); }, state.bg === b.id);
    el.querySelector(".bg-del").addEventListener("click", function (ev) { ev.stopPropagation(); deleteBg(b.id); });
    list.appendChild(el);
  });
  if (focusId) {
    var target = list.querySelector('[data-id="' + focusId + '"]');
    if (target) target.focus();
  }
}

async function deleteBg(id) {
  if (!confirm("Supprimer cet arrière-plan ?")) return;
  try { await api("/api/backgrounds/" + id, { method: "DELETE" }); } catch (e) { if (e.auth) return; }
  if (state.bg === id) state.bg = "_original";
  await refreshBackgrounds();
}

/* -------------------------------- Fichiers --------------------------------- */
function addFiles(fileList) {
  var max = state.health ? state.health.max_files : 60;
  Array.prototype.forEach.call(fileList, function (f) {
    if (state.files.length >= max) return;
    if (!/^image\/(jpeg|png|webp)$/.test(f.type)) return;
    var key = f.name + "|" + f.size + "|" + f.lastModified;
    var dup = state.files.some(function (x) { return x._key === key; });
    if (dup) return;
    f._key = key;
    f._url = URL.createObjectURL(f);
    state.files.push(f);
  });
  renderThumbs();
}

function renderThumbs() {
  var wrap = $("thumbs");
  wrap.innerHTML = "";
  state.files.forEach(function (f, i) {
    var el = document.createElement("div");
    el.className = "thumb";
    el.innerHTML = '<img src="' + f._url + '" alt="">' +
      '<div class="t-name">' + esc(f.name) + " · " + fmtSize(f.size) + '</div>' +
      '<button class="t-del" title="Retirer">✕</button>';
    el.querySelector(".t-del").addEventListener("click", function () {
      URL.revokeObjectURL(f._url);
      state.files.splice(i, 1);
      renderThumbs();
    });
    wrap.appendChild(el);
  });
  updateButtons();
}

function updateButtons() {
  $("btn-process").disabled = state.files.length === 0;
  $("btn-reset").disabled = state.files.length === 0 && !state.lastJob;
}

/* ------------------------------- Traitement -------------------------------- */
async function process() {
  if (!state.files.length) return;
  var transparent = state.bg === "_transparent";
  var replace = state.bg !== "_original" && !transparent;

  var fd = new FormData();
  state.files.forEach(function (f) { fd.append("files", f, f.name); });
  fd.append("bg_mode", transparent ? "transparent" : (replace ? "replace" : "original"));
  fd.append("background_id", replace ? state.bg : "");
  fd.append("blur_enabled", String($("blur-enabled").checked));
  fd.append("blur_strength", $("blur-strength").value);
  fd.append("out_format", transparent ? "png" : $("out-format").value);
  fd.append("jpeg_quality", "92");
  fd.append("rembg_model", $("rembg-model").value);
  fd.append("plate_style", $("plate-style").value);

  $("btn-process").disabled = true;
  $("btn-download-all").hidden = true;
  $("btn-download-all").disabled = true;
  $("dl-hint").hidden = true;
  $("progress").hidden = false;
  setProgress(0, state.files.length);
  renderPlaceholder(state.files.length);

  try {
    var res = await api("/api/jobs", { method: "POST", body: fd });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      var detail = detailText(err.detail);
      throw new Error(detail || ("HTTP " + res.status));
    }
    var j = await res.json();
    state.jobId = j.job_id;
    pollErrors = 0;
    poll();
  } catch (e) {
    if (!e.auth) alert("Erreur : " + e.message);
    $("progress").hidden = true;
    updateButtons();
  }
}

var pollErrors = 0;

async function poll() {
  try {
    var res = await api("/api/jobs/" + state.jobId);
    if (res.status === 404) {
      $("progress-text").textContent = "Résultats expirés ou introuvables";
      updateButtons();
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    var j = await res.json();
    pollErrors = 0;
    state.lastJob = j;
    renderResults(j);
    setProgress(j.done, j.total);
    if (j.status === "done") {
      $("btn-download-all").hidden = false;
      $("btn-download-all").disabled = false;
      $("dl-hint").hidden = false;
      updateButtons();
      return;
    }
  } catch (e) {
    if (e.auth) return;
    pollErrors += 1;
    if (pollErrors >= 5) {
      $("progress-text").textContent = "Connexion au serveur perdue : " + e.message;
      updateButtons();
      return;
    }
  }
  setTimeout(poll, 800);
}

function setProgress(done, total) {
  $("bar-fill").style.width = total ? Math.round((done / total) * 100) + "%" : "0%";
  $("progress-text").textContent = done + " / " + total;
}

function renderPlaceholder(n) {
  var wrap = $("results");
  wrap.innerHTML = "";
  for (var i = 0; i < n; i++) {
    var el = document.createElement("div");
    el.className = "result";
    el.id = "res-" + i;
    el.innerHTML = '<div class="r-ph">⏳</div><div class="r-info"><div class="r-name">…</div>' +
      '<div class="r-meta">En attente</div></div>';
    wrap.appendChild(el);
  }
}

/* Libellé selon les options réellement utilisées par le job (flou, rectangle blanc, désactivé) */
function plateLabel(j, n) {
  if (j.blur_enabled === false) return "Plaques non traitées";
  var s = n > 1 ? "s" : "";
  return n + " plaque" + s + (j.plate_style === "white" ? " masquée" : " floutée") + s;
}

function renderResults(j) {
  j.results.forEach(function (r) {
    var el = $("res-" + r.index);
    if (!el) return;
    var meta = "";
    var ph = "⏳";
    if (r.status === "processing") { meta = "Traitement en cours…"; ph = "🔄"; }
    else if (r.status === "done") {
      meta = '<span class="plates">' + plateLabel(j, r.plates) + "</span>";
      ph = "✅";
    } else if (r.status === "error") {
      meta = '<span class="err">❌ ' + esc(r.error || "Erreur") + "</span>";
      ph = "❌";
    }
    el.className = "result " + r.status;
    el.innerHTML =
      (r.status === "done"
        ? '<img class="r-thumb" src="' + r.url + '" alt="">'
        : '<div class="r-ph">' + ph + "</div>") +
      '<div class="r-info"><div class="r-name">' + esc(r.name) + '</div><div class="r-meta">' + meta + "</div></div>" +
      (r.status === "done" ? '<button class="r-dl">⬇️</button><button class="r-rt" title="Retouche manuelle">✏️</button>' : "");
    if (r.status === "done") {
      el.querySelector(".r-dl").addEventListener("click", function () {
        downloadOne(r);
      });
      el.querySelector(".r-rt").addEventListener("click", function () {
        openRetouch(state.jobId, r);
      });
    }
  });
}

function downloadOne(r) {
  var a = document.createElement("a");
  a.href = r.url + "?download=1";
  a.download = r.name || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function downloadAll() {
  var items = (state.lastJob ? state.lastJob.results : []).filter(function (r) { return r.status === "done"; });
  for (var i = 0; i < items.length; i++) {
    downloadOne(items[i]);
    await sleep(450);
  }
}

function reset() {
  state.files.forEach(function (f) { URL.revokeObjectURL(f._url); });
  state.files = [];
  state.jobId = null;
  state.lastJob = null;
  $("results").innerHTML = "";
  $("progress").hidden = true;
  $("btn-download-all").hidden = true;
  $("dl-hint").hidden = true;
  renderThumbs();
}

/* --------------------------------- Wiring ---------------------------------- */
function wire() {
  var dz = $("dropzone");
  dz.addEventListener("click", function () { $("file-input").click(); });
  dz.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); }
  });
  dz.addEventListener("dragover", function (e) { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", function () { dz.classList.remove("drag"); });
  dz.addEventListener("drop", function (e) {
    e.preventDefault();
    dz.classList.remove("drag");
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });
  $("file-input").addEventListener("change", function () { addFiles(this.files); this.value = ""; });

  $("bg-input").addEventListener("change", async function () {
    var f = this.files[0];
    if (!f) return;
    var fd = new FormData();
    fd.append("file", f, f.name);
    fd.append("name", f.name.replace(/\.[^.]+$/, ""));
    try {
      var res = await api("/api/backgrounds", { method: "POST", body: fd });
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        alert("Erreur : " + (detailText(err.detail) || res.status));
      } else {
        var created = await res.json();
        await refreshBackgrounds();
        state.bg = created.id;
        renderBackgrounds();
      }
    } catch (e) {
      if (!e.auth) alert("Erreur : " + e.message);
    } finally { this.value = ""; }
  });

  $("blur-enabled").addEventListener("change", function () {
    $("strength-row").classList.toggle("disabled", !this.checked);
  });
  $("blur-strength").addEventListener("input", function () { $("strength-val").textContent = this.value; });
  $("btn-process").addEventListener("click", process);
  $("btn-download-all").addEventListener("click", downloadAll);
  $("btn-reset").addEventListener("click", reset);
}

/* ------------------------- Retouche manuelle ------------------------------- */
var rt = { jobId: null, index: null, pts: [], scale: 1 };

function openRetouch(jobId, r) {
  if (!jobId) { alert("Retouche indisponible sur ce resultat."); return; }
  rt.jobId = jobId; rt.index = r.index; rt.pts = [];
  var ov = document.createElement("div");
  ov.id = "rt-overlay";
  ov.setAttribute("role", "dialog");
  ov.setAttribute("aria-modal", "true");
  ov.setAttribute("aria-label", "Retouche manuelle");
  ov.innerHTML = '<div class="rt-box">' +
    '<div class="rt-head">✏️ Retouche — cliquez les points autour de la plaque (octogone), puis Appliquer' +
    '<button id="rt-close" title="Fermer (Échap)" aria-label="Fermer">✕</button></div>' +
    '<div id="rt-stage"></div>' +
    '<div class="rt-actions"><button id="rt-apply" class="btn primary">Appliquer le flou</button>' +
    '<button id="rt-reset" class="btn">Effacer les points</button>' +
    '<select id="rt-style" class="rt-select"><option value="blur">Flou</option><option value="white">Rectangle blanc</option></select>' +
    '<span class="hint" id="rt-n">0 point</span>' +
    '<span class="hint">3 à 8 points — 4 points suffisent pour un rectangle incliné.</span></div>' +
    '</div>';
  document.body.appendChild(ov);
  var im = document.createElement("img");
  im.id = "rt-img";
  im.src = r.url + "?t=" + Date.now();
  $("rt-stage").appendChild(im);
  var cv = document.createElement("canvas");
  cv.id = "rt-canvas";
  $("rt-stage").appendChild(cv);
  im.onload = layoutRt;
  window.addEventListener("resize", layoutRt);
  document.addEventListener("keydown", rtKeydown);
  cv.addEventListener("click", function (ev) {
    var rect = cv.getBoundingClientRect();
    rt.pts.push([Math.round((ev.clientX - rect.left) * rt.scale), Math.round((ev.clientY - rect.top) * rt.scale)]);
    drawRtPts();
  });
  $("rt-apply").addEventListener("click", applyRetouch);
  $("rt-reset").addEventListener("click", function () { rt.pts = []; drawRtPts(); });
  $("rt-close").addEventListener("click", closeRetouch);
  $("rt-close").focus();
}

/* Les points sont stockés en pixels de l'image réelle : seule l'échelle d'affichage change */
function layoutRt() {
  var im = $("rt-img"), cv = $("rt-canvas");
  if (!im || !cv || !im.clientWidth) return;
  cv.width = im.clientWidth; cv.height = im.clientHeight;
  rt.scale = im.naturalWidth / im.clientWidth;
  drawRtPts();
}

function rtKeydown(ev) {
  if (ev.key === "Escape") closeRetouch();
}

function drawRtPts() {
  var cv = $("rt-canvas");
  if (!cv) return;
  var ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.strokeStyle = "#f59e0b"; ctx.lineWidth = 2; ctx.fillStyle = "#fbbf24";
  var i, p, x, y;
  for (i = 0; i < rt.pts.length; i++) {
    p = rt.pts[i]; x = p[0] / rt.scale; y = p[1] / rt.scale;
    ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.3); ctx.fill();
  }
  if (rt.pts.length > 1) {
    ctx.beginPath();
    for (i = 0; i < rt.pts.length; i++) {
      p = rt.pts[i]; x = p[0] / rt.scale; y = p[1] / rt.scale;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath(); ctx.stroke();
  }
  $("rt-n").textContent = rt.pts.length + " point(s)";
}

function closeRetouch() {
  window.removeEventListener("resize", layoutRt);
  document.removeEventListener("keydown", rtKeydown);
  var ov = $("rt-overlay");
  if (ov) ov.remove();
}

async function applyRetouch() {
  if (rt.pts.length < 3) { alert("Placez au moins 3 points autour de la plaque."); return; }
  try {
    var res = await api("/api/retouch/" + rt.jobId + "/" + rt.index, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polygon: rt.pts, strength: parseInt($("blur-strength").value, 10) || 3, style: $("rt-style").value })
    });
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      throw new Error(detailText(err.detail) || ("HTTP " + res.status));
    }
    closeRetouch();
    var el = document.querySelector("#res-" + rt.index + " .r-thumb");
    if (el) el.src = el.src.split("?")[0] + "?t=" + Date.now();
  } catch (e) {
    if (!e.auth) alert("Erreur : " + e.message);
  }
}

init();
