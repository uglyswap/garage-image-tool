/* Garage Image Tool : frontend (vanilla JS, aucune dépendance) */
"use strict";

var ICONS = {
  eye: '<path d="M1.5 12s3.8-7 10.5-7 10.5 7 10.5 7-3.8 7-10.5 7S1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3"/>',
  download: '<path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 20h16"/>',
  pencil: '<path d="M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1z"/>',
  sliders: '<path d="M4 7h10m4 0h2M4 17h2m4 0h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  save: '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a1 1 0 0 1 1-1z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  prev: '<path d="M15 5l-7 7 7 7"/>',
  next: '<path d="M9 5l7 7-7 7"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M4 18l5-5 4 4 3-3 4 4"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.200 1.800L12 17l-1.800-5.200L5 10l5.200-1.800z"/><path d="M19 16v4m-2-2h4"/>'
};
function icon(name) { return '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">' + ICONS[name] + "</svg>"; }

var PSEUDO = [
  { id: "_original", name: "Conserver le fond d'origine", icon: "image" },
  { id: "_transparent", name: "Fond transparent (PNG)", icon: "sparkle" }
];
var ADJUST_KEYS = ["brightness", "contrast", "saturation", "vibrance"];
var ADJUST_LABELS = { brightness: "Luminosité", contrast: "Contraste", saturation: "Saturation", vibrance: "Vibrance" };
var JOB_KEY = "gi_job";  /* job en cours, retrouvé après un rechargement de la page */

var state = {
  files: [], bg: "_original", bgs: [], jobId: null, health: null, lastJob: null, auth: null, wired: false,
  selected: {},  /* index -> true */
  cards: {}      /* index -> signature du dernier rendu de la carte */
};

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
function plural(n, word) { return n + " " + word + (n > 1 ? "s" : ""); }
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch (e) { /* stockage indisponible (navigation privée) : la reprise après rechargement est simplement absente */ }
  return null;
}

function toast(message, kind) {
  var el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(function () { el.remove(); }, kind === "error" ? 8000 : 4500);
}
function fail(e) { if (!e || !e.auth) toast("Erreur : " + (e && e.message ? e.message : "inconnue"), "error"); }

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

/* Appel JSON : renvoie le corps décodé, lève une erreur lisible sinon */
async function apiJson(url, method, body) {
  var opts = { method: method || "GET" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  var res = await api(url, opts);
  var data = await res.json().catch(function () { return {}; });
  if (!res.ok) throw new Error(detailText(data.detail) || ("HTTP " + res.status));
  return data;
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
      : (detailText(err.detail) || "Connexion impossible");
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
    var badge = $("engine-badge");
    badge.hidden = h.plate_engine === "yolo";  // affiché seulement en cas de repli
    if (!badge.hidden) badge.textContent = "Plaques : cascade Haar (repli)";
    $("max-files").textContent = h.max_files;
    $("max-mb").textContent = h.max_file_mb;
  } catch (e) {
    if (e.auth) return;
    $("engine-badge").textContent = "API indisponible";
    $("engine-badge").hidden = false;
  }
  await refreshBackgrounds();
  if (!state.wired) { wire(); state.wired = true; }
  if (!state.jobId) await restoreJob();
}

/* Après un rechargement de la page : retrouve le dernier lot, encore conservé par le serveur */
async function restoreJob() {
  var saved = store(JOB_KEY);
  if (!saved || !/^[a-f0-9]{6,32}$/.test(saved)) return;
  try {
    var res = await api("/api/jobs/" + saved);
    if (!res.ok) { store(JOB_KEY, null); return; }
    var j = await res.json();
    state.jobId = saved;
    $("progress").hidden = false;
    renderPlaceholder(j.total);
    pollErrors = 0;
    poll();
  } catch (e) { /* hors ligne ou non connecté : rien à restaurer */ }
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
    el.innerHTML = '<span class="bg-icon">' + icon(p.icon) + '</span><span class="bg-name">' + p.name + '</span>';
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
  var refused = 0;
  Array.prototype.forEach.call(fileList, function (f) {
    if (state.files.length >= max || !/^image\/(jpeg|png|webp)$/.test(f.type)) { refused += 1; return; }
    var key = f.name + "|" + f.size + "|" + f.lastModified;
    var dup = state.files.some(function (x) { return x._key === key; });
    if (dup) return;
    f._key = key;
    f._url = URL.createObjectURL(f);
    state.files.push(f);
  });
  if (refused) toast(plural(refused, "fichier") + " ignoré" + (refused > 1 ? "s" : "") + " (format non pris en charge ou limite de " + max + " images).", "error");
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
      '<button class="t-del" title="Retirer" aria-label="Retirer cette photo">✕</button>';
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
/* Envoi avec progression (fetch ne sait pas la mesurer) */
function upload(url, formData, onProgress) {
  return new Promise(function (resolve, reject) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    xhr.onload = function () {
      var body = {};
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* réponse non JSON (proxy) */ }
      resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300, body: body });
    };
    xhr.onerror = function () { reject(new Error("connexion au serveur impossible")); };
    xhr.send(formData);
  });
}

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

  var total = state.files.length;
  $("btn-process").disabled = true;
  $("btn-download-all").hidden = true;
  $("btn-download-all").disabled = true;
  $("results-bar").hidden = true;
  $("progress").hidden = false;
  state.selected = {};
  state.lastJob = null;
  renderPlaceholder(total);
  $("bar-fill").style.width = "0%";
  $("progress-text").textContent = "Envoi 0 %";

  try {
    var res = await upload("/api/jobs", fd, function (ratio) {
      $("bar-fill").style.width = Math.round(ratio * 100) + "%";
      $("progress-text").textContent = ratio < 1 ? "Envoi " + Math.round(ratio * 100) + " %" : "Vérification…";
    });
    if (res.status === 401) { showLogin(); throw Object.assign(new Error("Authentification requise"), { auth: true }); }
    if (!res.ok) throw new Error(detailText(res.body.detail) || ("HTTP " + res.status));
    state.jobId = res.body.job_id;
    store(JOB_KEY, state.jobId);
    setProgress(0, total);
    pollErrors = 0;
    poll();
  } catch (e) {
    fail(e);
    $("progress").hidden = true;
    $("results").innerHTML = "";
    updateButtons();
  }
}

var pollErrors = 0;

async function poll() {
  var jobId = state.jobId;
  if (!jobId) return;
  try {
    var res = await api("/api/jobs/" + jobId);
    if (jobId !== state.jobId) return;  /* lot vidé ou remplacé pendant la requête */
    if (res.status === 404) {
      $("progress-text").textContent = "Résultats expirés";
      store(JOB_KEY, null);
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
      var ready = doneResults().length > 0;
      $("btn-download-all").hidden = !ready;
      $("btn-download-all").disabled = !ready;
      updateButtons();
      return;
    }
  } catch (e) {
    if (e.auth) return;
    pollErrors += 1;
    if (pollErrors >= 5) {
      $("progress-text").textContent = "Connexion perdue";
      toast("Connexion au serveur perdue : " + e.message, "error");
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
  state.cards = {};
  for (var i = 0; i < n; i++) {
    var el = document.createElement("div");
    el.className = "result";
    el.id = "res-" + i;
    el.innerHTML = '<div class="r-ph">En attente</div><div class="r-info"><div class="r-name">…</div>' +
      '<div class="r-meta">En attente</div></div>';
    wrap.appendChild(el);
  }
}

/* --------------------------------- Résultats ------------------------------- */
function doneResults() {
  return (state.lastJob ? state.lastJob.results : []).filter(function (r) { return r.status === "done"; });
}
function resultAt(index) {
  return (state.lastJob ? state.lastJob.results : []).filter(function (r) { return r.index === index; })[0] || null;
}
/* Cibles d'une action en lot : la sélection, ou toutes les images traitées si rien n'est coché */
function targets() {
  var done = doneResults();
  var picked = done.filter(function (r) { return state.selected[r.index]; });
  return picked.length ? picked : done;
}
function fullUrl(r) { return r.url + "?v=" + r.rev; }
function thumbUrl(r) { return r.url + "?thumb=1&v=" + r.rev; }
function isAdjusted(r) { return ADJUST_KEYS.some(function (k) { return r.adjust && r.adjust[k]; }); }

/* Libellé selon les options réellement utilisées par le job (flou, rectangle blanc, désactivé) */
function plateLabel(j, n) {
  if (j.blur_enabled === false) return "Plaques non traitées";
  var s = n > 1 ? "s" : "";
  return n + " plaque" + s + " masquée" + s;
}

function renderResults(j) {
  j.results.forEach(function (r) { renderCard(j, r); });
  updateSelectionBar();
}

/* Une carte n'est reconstruite que si son contenu a changé : pas de scintillement pendant le suivi */
function renderCard(j, r) {
  var el = $("res-" + r.index);
  if (!el) return;
  var signature = [r.status, r.rev, r.plates, r.error, r.name, r.edits, JSON.stringify(r.adjust), j.blur_enabled].join("|");
  if (state.cards[r.index] === signature) return;
  state.cards[r.index] = signature;
  el.className = "result " + r.status + (state.selected[r.index] ? " selected" : "");
  if (r.status !== "done") {
    var text = r.status === "processing" ? "Traitement en cours…" : (r.status === "error" ? "Échec" : "En attente");
    el.innerHTML = '<div class="r-ph">' + text + '</div><div class="r-info"><div class="r-name">' + esc(r.name) +
      '</div><div class="r-meta">' + (r.status === "error" ? '<span class="err">' + esc(r.error || "Erreur") + "</span>" : text) +
      "</div></div>";
    return;
  }
  var meta = '<span class="plates">' + plateLabel(j, r.plates) + "</span>";
  if (r.edits) meta += '<span class="chip">' + plural(r.edits, "retouche") + "</span>";
  if (isAdjusted(r)) meta += '<span class="chip">Réglages</span>';
  el.innerHTML =
    '<label class="r-check" title="Sélectionner"><input type="checkbox" aria-label="Sélectionner ' + esc(r.name) + '"' +
      (state.selected[r.index] ? " checked" : "") + "></label>" +
    '<button class="r-media" aria-label="Voir l’image"><img class="r-thumb" src="' + thumbUrl(r) + '" alt="" loading="lazy"></button>' +
    '<div class="r-info"><div class="r-name" title="' + esc(r.name) + '">' + esc(r.name) + '</div><div class="r-meta">' + meta + "</div></div>" +
    '<div class="r-actions">' +
      '<button class="r-view" title="Voir l’image" aria-label="Voir l’image">' + icon("eye") + "</button>" +
      '<button class="r-dl" title="Télécharger" aria-label="Télécharger">' + icon("download") + "</button>" +
      '<button class="r-rt" title="Retouche manuelle (masquer une zone)" aria-label="Retouche manuelle">' + icon("pencil") + "</button>" +
      '<button class="r-adj" title="Réglages : luminosité, contraste, saturation, vibrance" aria-label="Réglages">' + icon("sliders") + "</button>" +
      '<button class="r-save" title="Enregistrer sous…" aria-label="Enregistrer sous">' + icon("save") + "</button>" +
    "</div>";
  var idx = r.index;
  el.querySelector(".r-check input").addEventListener("change", function () {
    if (this.checked) state.selected[idx] = true; else delete state.selected[idx];
    el.classList.toggle("selected", this.checked);
    updateSelectionBar();
  });
  el.querySelector(".r-media").addEventListener("click", function () { openViewer(resultAt(idx)); });
  el.querySelector(".r-view").addEventListener("click", function () { openViewer(resultAt(idx)); });
  el.querySelector(".r-dl").addEventListener("click", function () { downloadOne(resultAt(idx)); });
  el.querySelector(".r-rt").addEventListener("click", function () { openRetouch(state.jobId, resultAt(idx)); });
  el.querySelector(".r-adj").addEventListener("click", function () { openAdjust([idx], false); });
  el.querySelector(".r-save").addEventListener("click", function () { openSaveAs([idx]); });
}

/* Résultat renvoyé par le serveur après une modification : met à jour l'état et la carte */
function updateResult(result) {
  if (!state.lastJob || !result) return;
  state.lastJob.results = state.lastJob.results.map(function (r) { return r.index === result.index ? result : r; });
  renderCard(state.lastJob, result);
}

function updateSelectionBar() {
  var done = doneResults();
  var n = done.filter(function (r) { return state.selected[r.index]; }).length;
  $("results-bar").hidden = done.length === 0;
  $("select-all").checked = done.length > 0 && n === done.length;
  $("select-all").indeterminate = n > 0 && n < done.length;
  $("select-label").textContent = n ? plural(n, "image") + " sélectionnée" + (n > 1 ? "s" : "") : "Tout sélectionner";
  var scope = n ? " (" + n + ")" : " (toutes)";
  $("btn-bulk-auto").textContent = "Harmoniser" + scope;
  $("btn-bulk-adjust").textContent = "Réglages" + scope;
  $("btn-bulk-save").textContent = "Enregistrer sous…" + scope;
}

function toggleAll(checked) {
  state.selected = {};
  doneResults().forEach(function (r) {
    if (checked) state.selected[r.index] = true;
    var el = $("res-" + r.index);
    if (!el) return;
    el.classList.toggle("selected", checked);
    var box = el.querySelector(".r-check input");
    if (box) box.checked = checked;
  });
  updateSelectionBar();
}

function downloadUrl(url, name) {
  var a = document.createElement("a");
  a.href = url;
  a.download = name || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
function downloadOne(r) { if (r) downloadUrl(r.url + "?download=1", r.name); }
function downloadAll() { if (state.jobId) downloadUrl("/api/jobs/" + state.jobId + "/zip", "photos.zip"); }

function reset() {
  state.files.forEach(function (f) { URL.revokeObjectURL(f._url); });
  state.files = [];
  state.jobId = null;
  state.lastJob = null;
  state.selected = {};
  state.cards = {};
  store(JOB_KEY, null);
  $("results").innerHTML = "";
  $("progress").hidden = true;
  $("btn-download-all").hidden = true;
  $("results-bar").hidden = true;
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
        throw new Error(detailText(err.detail) || ("HTTP " + res.status));
      }
      var created = await res.json();
      await refreshBackgrounds();
      state.bg = created.id;
      renderBackgrounds();
    } catch (e) {
      fail(e);
    } finally { this.value = ""; }
  });

  $("blur-enabled").addEventListener("change", function () {
    $("strength-row").classList.toggle("disabled", !this.checked);
  });
  $("blur-strength").addEventListener("input", function () { $("strength-val").textContent = this.value; });
  $("btn-process").addEventListener("click", process);
  $("btn-download-all").addEventListener("click", downloadAll);
  $("btn-reset").addEventListener("click", reset);
  $("select-all").addEventListener("change", function () { toggleAll(this.checked); });
  $("btn-bulk-auto").addEventListener("click", function () { openAdjust(indicesOf(targets()), true); });
  $("btn-bulk-adjust").addEventListener("click", function () { openAdjust(indicesOf(targets()), false); });
  $("btn-bulk-save").addEventListener("click", function () { openSaveAs(indicesOf(targets())); });
}
function indicesOf(results) { return results.map(function (r) { return r.index; }); }

/* --------------------------------- Fenêtres -------------------------------- */
/* Fenêtre modale : Échap et clic à l'extérieur la ferment, le focus revient où il était */
var modals = {};
function openModal(id, label, html, onKey) {
  closeModal(id);
  var ov = document.createElement("div");
  ov.id = id;
  ov.setAttribute("role", "dialog");
  ov.setAttribute("aria-modal", "true");
  ov.setAttribute("aria-label", label);
  ov.innerHTML = html;
  document.body.appendChild(ov);
  var entry = { opener: document.activeElement, cleanup: [] };
  entry.key = function (ev) {
    if (ev.key === "Escape") closeModal(id);
    else if (onKey) onKey(ev);
  };
  document.addEventListener("keydown", entry.key);
  ov.addEventListener("mousedown", function (ev) { if (ev.target === ov) closeModal(id); });
  var closer = ov.querySelector("[data-close]");
  if (closer) { closer.addEventListener("click", function () { closeModal(id); }); closer.focus(); }
  modals[id] = entry;
  return ov;
}
function closeModal(id) {
  var entry = modals[id];
  if (entry) {
    document.removeEventListener("keydown", entry.key);
    entry.cleanup.forEach(function (fn) { fn(); });
    delete modals[id];
    if (entry.opener && entry.opener.focus && document.contains(entry.opener)) entry.opener.focus();
  }
  var ov = $(id);
  if (ov) ov.remove();
}
function closeButton(id) {
  return '<button id="' + id + '" data-close title="Fermer (Échap)" aria-label="Fermer">' + icon("close") + "</button>";
}

/* ------------------------------ Visionneuse -------------------------------- */
function openViewer(r) {
  if (!r) return;
  var list = doneResults();
  var pos = Math.max(0, indicesOf(list).indexOf(r.index));
  var ov = openModal("view-overlay", "Aperçu de l'image",
    '<div class="rt-box"><div class="rt-head"><span class="view-title"></span>' + closeButton("view-close") + "</div>" +
    '<div class="view-stage">' +
    '<button class="icon-btn" id="view-prev" aria-label="Image précédente">' + icon("prev") + "</button>" +
    '<img id="view-img" alt="">' +
    '<button class="icon-btn" id="view-next" aria-label="Image suivante">' + icon("next") + "</button>" +
    "</div></div>",
    function (ev) {
      if (ev.key === "ArrowLeft") show(pos - 1);
      if (ev.key === "ArrowRight") show(pos + 1);
    });
  function show(next) {
    pos = (next + list.length) % list.length;
    var cur = resultAt(list[pos].index) || list[pos];
    ov.querySelector(".view-title").textContent = (cur.name || "Image") + (list.length > 1 ? "  ·  " + (pos + 1) + " / " + list.length : "");
    $("view-img").src = fullUrl(cur);
  }
  $("view-prev").hidden = $("view-next").hidden = list.length < 2;
  $("view-prev").addEventListener("click", function () { show(pos - 1); });
  $("view-next").addEventListener("click", function () { show(pos + 1); });
  show(pos);
}

function closeViewer() { closeModal("view-overlay"); }

/* ------------------------- Retouche manuelle ------------------------------- */
var rt = { jobId: null, index: null, pts: [], scale: 1, zoom: 1, fitWidth: 0 };

function openRetouch(jobId, r) {
  if (!jobId || !r) { toast("Retouche indisponible sur ce résultat.", "error"); return; }
  rt.jobId = jobId; rt.index = r.index; rt.pts = []; rt.zoom = 1; rt.fitWidth = 0;
  /* Intensité de départ : celle choisie sur la page, modifiable ici pour cette retouche */
  var strength = Math.min(5, Math.max(1, parseInt($("blur-strength").value, 10) || 3));
  var ov = openModal("rt-overlay", "Retouche manuelle", '<div class="rt-box">' +
    '<div class="rt-head"><span>Retouche : cliquez les coins de la zone à masquer, puis Appliquer</span>' + closeButton("rt-close") + "</div>" +
    '<div id="rt-stage"><div id="rt-wrap"></div></div>' +
    '<div class="rt-actions"><button id="rt-apply" class="btn primary small-inline">Appliquer</button>' +
    '<button id="rt-undo-pt" class="btn ghost small-inline">Annuler le dernier point</button>' +
    '<button id="rt-reset" class="btn ghost small-inline">Effacer les points</button>' +
    '<select id="rt-style" class="rt-select" aria-label="Type de masque"><option value="blur">Flou</option><option value="white">Rectangle blanc</option></select>' +
    '<div class="opt strength" id="rt-strength-row"><span>Intensité du flou</span>' +
    '<input type="range" id="rt-strength" min="1" max="5" value="' + strength + '" aria-label="Intensité du flou">' +
    '<span id="rt-strength-val" class="val">' + strength + '</span></div>' +
    '<button id="rt-zoom-out" class="icon-btn" aria-label="Zoom arrière" title="Zoom arrière">−</button>' +
    '<button id="rt-zoom-in" class="icon-btn" aria-label="Zoom avant" title="Zoom avant">+</button>' +
    '<button id="rt-undo" class="btn ghost small-inline" hidden>Annuler la dernière retouche</button></div>' +
    '<div class="rt-actions"><span class="hint" id="rt-n">0 point</span>' +
    '<span class="hint">3 points au minimum, 4 suffisent pour une plaque inclinée. Une retouche appliquée reste annulable.</span></div>' +
    "</div>");
  modals["rt-overlay"].cleanup.push(function () { window.removeEventListener("resize", layoutRt); });
  var im = document.createElement("img");
  im.id = "rt-img";
  im.alt = "";
  im.src = fullUrl(r);
  $("rt-wrap").appendChild(im);
  var cv = document.createElement("canvas");
  cv.id = "rt-canvas";
  $("rt-wrap").appendChild(cv);
  im.onload = layoutRt;
  window.addEventListener("resize", layoutRt);
  cv.addEventListener("click", function (ev) {
    var rect = cv.getBoundingClientRect();
    rt.pts.push([Math.round((ev.clientX - rect.left) * rt.scale), Math.round((ev.clientY - rect.top) * rt.scale)]);
    drawRtPts();
  });
  $("rt-strength").addEventListener("input", function () { $("rt-strength-val").textContent = this.value; });
  $("rt-style").addEventListener("change", function () {
    $("rt-strength-row").classList.toggle("disabled", this.value === "white");  // sans effet sur le rectangle blanc
  });
  $("rt-apply").addEventListener("click", applyRetouch);
  $("rt-undo-pt").addEventListener("click", function () { rt.pts.pop(); drawRtPts(); });
  $("rt-reset").addEventListener("click", function () { rt.pts = []; drawRtPts(); });
  $("rt-zoom-in").addEventListener("click", function () { zoomRt(1.5); });
  $("rt-zoom-out").addEventListener("click", function () { zoomRt(1 / 1.5); });
  $("rt-undo").hidden = !r.edits;
  $("rt-undo").addEventListener("click", undoRetouch);
  return ov;
}

/* Les points sont stockés en pixels de l'image réelle : seule l'échelle d'affichage change */
function layoutRt() {
  var im = $("rt-img"), cv = $("rt-canvas");
  if (!im || !cv || !im.clientWidth) return;
  if (rt.zoom === 1) {
    im.style.maxWidth = ""; im.style.maxHeight = ""; im.style.width = "";
    rt.fitWidth = im.clientWidth;
  }
  cv.width = im.clientWidth; cv.height = im.clientHeight;
  rt.scale = im.naturalWidth / im.clientWidth;
  drawRtPts();
}

/* Zoom : l'image s'agrandit dans sa zone défilante, pratique pour viser une petite plaque au doigt */
function zoomRt(factor) {
  var im = $("rt-img");
  if (!im || !rt.fitWidth) return;
  rt.zoom = Math.min(6, Math.max(1, rt.zoom * factor));
  if (rt.zoom > 1) {
    im.style.maxWidth = "none"; im.style.maxHeight = "none";
    im.style.width = Math.round(rt.fitWidth * rt.zoom) + "px";
  }
  layoutRt();
}

function drawRtPts() {
  var cv = $("rt-canvas");
  if (!cv) return;
  var ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.strokeStyle = "#7fe6fa"; ctx.lineWidth = 2; ctx.fillStyle = "#ffd9b4";
  var i, p, x, y;
  if (rt.pts.length > 1) {
    ctx.beginPath();
    for (i = 0; i < rt.pts.length; i++) {
      p = rt.pts[i]; x = p[0] / rt.scale; y = p[1] / rt.scale;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath(); ctx.stroke();
  }
  for (i = 0; i < rt.pts.length; i++) {
    p = rt.pts[i]; x = p[0] / rt.scale; y = p[1] / rt.scale;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, 6.3); ctx.fill();
  }
  $("rt-n").textContent = plural(rt.pts.length, "point");
}

function closeRetouch() { closeModal("rt-overlay"); }

async function applyRetouch() {
  if (rt.pts.length < 3) { toast("Placez au moins 3 points autour de la zone à masquer.", "error"); return; }
  $("rt-apply").disabled = true;
  try {
    var result = await apiJson("/api/retouch/" + rt.jobId + "/" + rt.index, "POST",
      { polygon: rt.pts, strength: parseInt($("rt-strength").value, 10) || 3, style: $("rt-style").value });
    closeRetouch();
    updateResult(result);
  } catch (e) {
    fail(e);
    if ($("rt-apply")) $("rt-apply").disabled = false;
  }
}

async function undoRetouch() {
  try {
    var result = await apiJson("/api/retouch/" + rt.jobId + "/" + rt.index, "DELETE");
    updateResult(result);
    if ($("rt-img")) $("rt-img").src = fullUrl(result);
    if ($("rt-undo")) $("rt-undo").hidden = !result.edits;
    toast("Dernière retouche annulée.");
  } catch (e) { fail(e); }
}

/* ------------------------------ Réglages ----------------------------------- */
/* Luminosité, contraste, saturation, vibrance : une image ou un lot. En mode automatique,
   chaque image reçoit ses propres valeurs, calculées pour rapprocher tout le lot. */
var adj = { indices: [], pos: 0, auto: null, timer: null };

function openAdjust(indices, autoStart) {
  if (!indices.length) return;
  adj.indices = indices; adj.pos = 0; adj.auto = null;
  var bulk = indices.length > 1;
  var first = resultAt(indices[0]);
  var sliders = ADJUST_KEYS.map(function (k) {
    return '<label class="slider-row"><span>' + ADJUST_LABELS[k] + '</span><span class="val" id="adj-val-' + k + '">0</span>' +
      '<input type="range" id="adj-' + k + '" min="-100" max="100" step="1" value="0" aria-label="' + ADJUST_LABELS[k] + '"></label>';
  }).join("");
  openModal("adj-overlay", "Réglages de l'image", '<div class="rt-box">' +
    '<div class="rt-head"><span>' + (bulk ? "Réglages : " + indices.length + " images" : "Réglages") + "</span>" + closeButton("adj-close") + "</div>" +
    '<div class="adj-body"><div class="adj-preview"><img id="adj-img" alt="Aperçu des réglages">' +
    '<div class="adj-nav"><button class="icon-btn" id="adj-prev" aria-label="Image précédente">' + icon("prev") + "</button>" +
    '<span id="adj-name"></span><button class="icon-btn" id="adj-next" aria-label="Image suivante">' + icon("next") + "</button></div></div>" +
    '<div class="adj-side">' + sliders +
    '<p class="hint" id="adj-scope"></p><p class="hint" id="adj-mode"></p>' +
    '<div class="adj-buttons"><button id="adj-auto" class="btn ghost small-inline" title="Calcule les réglages qui rapprochent cette image du reste du lot">Automatique</button>' +
    '<button id="adj-reset" class="btn ghost small-inline">Réinitialiser</button>' +
    '<button id="adj-apply" class="btn primary small-inline">Appliquer</button></div>' +
    "</div></div></div>",
    function (ev) {
      if (ev.target && ev.target.tagName === "INPUT") return;  /* les flèches règlent le curseur actif */
      if (ev.key === "ArrowLeft") adjShow(adj.pos - 1);
      if (ev.key === "ArrowRight") adjShow(adj.pos + 1);
    });
  modals["adj-overlay"].cleanup.push(function () { clearTimeout(adj.timer); });
  $("adj-prev").hidden = $("adj-next").hidden = !bulk;
  ADJUST_KEYS.forEach(function (k) {
    $("adj-" + k).addEventListener("input", function () {
      adj.auto = null;  /* un réglage à la main remplace le mode automatique */
      $("adj-val-" + k).textContent = this.value;
      adjModeText();
      adjPreviewSoon();
    });
  });
  $("adj-prev").addEventListener("click", function () { adjShow(adj.pos - 1); });
  $("adj-next").addEventListener("click", function () { adjShow(adj.pos + 1); });
  $("adj-reset").addEventListener("click", function () { adj.auto = null; adjSet({}); adjModeText(); adjPreview(); });
  $("adj-auto").addEventListener("click", adjAuto);
  $("adj-apply").addEventListener("click", adjApply);
  $("adj-apply").textContent = bulk ? "Appliquer aux " + indices.length + " images" : "Appliquer";
  adjSet(bulk ? {} : (first ? first.adjust : {}));
  adjShow(0);
  if (autoStart) adjAuto();
}

function adjValues() {
  var out = {};
  ADJUST_KEYS.forEach(function (k) { out[k] = parseInt($("adj-" + k).value, 10) || 0; });
  return out;
}
function adjSet(values) {
  ADJUST_KEYS.forEach(function (k) {
    var v = (values && values[k]) || 0;
    $("adj-" + k).value = v;
    $("adj-val-" + k).textContent = v;
  });
}
/* Valeurs destinées à une image : sa proposition automatique, sinon les curseurs */
function adjFor(index) { return adj.auto && adj.auto[index] ? adj.auto[index] : adjValues(); }

function adjModeText() {
  $("adj-mode").textContent = adj.auto
    ? "Mode automatique : chaque image reçoit ses propres valeurs pour un lot homogène. Bougez un curseur pour reprendre la main."
    : "";
}

function adjShow(next) {
  var n = adj.indices.length;
  adj.pos = (next + n) % n;
  var r = resultAt(adj.indices[adj.pos]);
  if (!r) return;
  $("adj-name").textContent = r.name + (n > 1 ? "  ·  " + (adj.pos + 1) + " / " + n : "");
  $("adj-scope").textContent = r.fg_only
    ? "Seul le véhicule détouré est modifié : le fond reste intact."
    : "Fond d'origine conservé sur cette image : le réglage s'applique à toute la photo.";
  if (adj.auto) adjSet(adj.auto[r.index]);
  adjModeText();
  adjPreview();
}

function adjPreviewSoon() {
  clearTimeout(adj.timer);
  adj.timer = setTimeout(adjPreview, 120);
}

/* L'aperçu est calculé par le serveur avec le même code que le rendu final : ce que l'on voit est ce que l'on obtient */
function adjPreview() {
  var img = $("adj-img");
  var r = resultAt(adj.indices[adj.pos]);
  if (!img || !r) return;
  var v = adjFor(r.index);
  var query = ADJUST_KEYS.map(function (k) { return k + "=" + v[k]; }).join("&");
  img.classList.add("loading");
  img.onload = img.onerror = function () { img.classList.remove("loading"); };
  img.src = r.url + "/preview?" + query + "&v=" + r.rev;
}

async function adjAuto() {
  var btn = $("adj-auto");
  btn.disabled = true;
  try {
    /* Mesuré sur tout le lot, même pour une seule image : c'est ce qui rend l'ensemble cohérent */
    var data = await apiJson("/api/jobs/" + state.jobId + "/auto-adjust", "POST", {});
    adj.auto = {};
    adj.indices.forEach(function (i) { adj.auto[i] = data.adjust[String(i)] || {}; });
    adjShow(adj.pos);
  } catch (e) {
    fail(e);
  } finally {
    if ($("adj-auto")) $("adj-auto").disabled = false;
  }
}

async function adjApply() {
  var jobId = state.jobId;
  var plan = adj.indices.map(function (i) { return { index: i, values: adjFor(i) }; });
  var btn = $("adj-apply");
  btn.disabled = true;
  var done = 0, failed = 0;
  /* Deux rendus à la fois : rapide sans saturer le serveur pendant un traitement en cours */
  async function worker() {
    while (plan.length) {
      var task = plan.shift();
      try {
        updateResult(await apiJson("/api/results/" + jobId + "/" + task.index + "/adjust", "PUT", task.values));
      } catch (e) {
        if (e.auth) { plan.length = 0; return; }
        failed += 1;
      }
      done += 1;
      if ($("adj-apply")) $("adj-apply").textContent = done + " / " + adj.indices.length + "…";
    }
  }
  await Promise.all([worker(), worker()]);
  closeModal("adj-overlay");
  if (failed) toast(plural(failed, "image") + " en échec sur " + adj.indices.length + ".", "error");
  else toast(adj.indices.length > 1 ? "Réglages appliqués aux " + adj.indices.length + " images." : "Réglages appliqués.");
}

/* --------------------------- Enregistrer sous ------------------------------ */
var SAVE_TYPES = {
  jpeg: { ext: ".jpg", mime: "image/jpeg", label: "Image JPEG" },
  png: { ext: ".png", mime: "image/png", label: "Image PNG" },
  webp: { ext: ".webp", mime: "image/webp", label: "Image WebP" },
  zip: { ext: ".zip", mime: "application/zip", label: "Archive ZIP" }
};

function openSaveAs(indices) {
  if (!indices.length) return;
  var bulk = indices.length > 1;
  var r = resultAt(indices[0]);
  var current = /\.png$/i.test(r.name) ? "png" : "jpeg";
  var stem = bulk ? "photos" : r.name.replace(/\.[^.]+$/, "");
  openModal("save-overlay", "Enregistrer sous", '<div class="rt-box narrow">' +
    '<div class="rt-head"><span>' + (bulk ? "Enregistrer " + indices.length + " images sous…" : "Enregistrer sous…") + "</span>" + closeButton("save-close") + "</div>" +
    '<label class="field">' + (bulk ? "Nom de l'archive ZIP" : "Nom du fichier") +
    '<input type="text" id="save-name" maxlength="80" value="' + esc(stem) + '"></label>' +
    '<label class="field">Format<select id="save-format">' +
    '<option value="">' + (bulk ? "Format d'origine de chaque image" : "Format d'origine (" + current.toUpperCase() + ")") + "</option>" +
    '<option value="jpeg">JPEG (léger, sans transparence)</option><option value="png">PNG (sans perte)</option>' +
    '<option value="webp">WebP (léger, transparence conservée)</option></select></label>' +
    '<div class="slider-row" id="save-quality-row"><span>Qualité</span><span class="val" id="save-quality-val">92</span>' +
    '<input type="range" id="save-quality" min="50" max="100" value="92" aria-label="Qualité"></div>' +
    '<p class="hint" id="save-hint"></p>' +
    '<div class="adj-buttons"><button id="save-go" class="btn primary small-inline">Enregistrer</button></div></div>');
  function refresh() {
    var fmt = $("save-format").value;
    $("save-quality-row").classList.toggle("disabled", fmt !== "jpeg" && fmt !== "webp");
  }
  $("save-hint").textContent = window.showSaveFilePicker
    ? "Votre navigateur vous laissera choisir le dossier de destination."
    : "Le fichier ira dans le dossier de téléchargement de votre navigateur (le choix du dossier n'est proposé que par Chrome et Edge sur ordinateur).";
  $("save-format").addEventListener("change", refresh);
  $("save-quality").addEventListener("input", function () { $("save-quality-val").textContent = this.value; });
  $("save-go").addEventListener("click", function () { saveAs(indices); });
  $("save-name").addEventListener("keydown", function (ev) { if (ev.key === "Enter") saveAs(indices); });
  refresh();
  $("save-name").focus();
  $("save-name").select();
}

async function saveAs(indices) {
  var bulk = indices.length > 1;
  var r = resultAt(indices[0]);
  var fmt = $("save-format").value;
  var name = $("save-name").value.trim().replace(/[\\/:*?"<>|]+/g, "_") || (bulk ? "photos" : "image");
  var query = "name=" + encodeURIComponent(name) + "&quality=" + $("save-quality").value + (fmt ? "&format=" + fmt : "");
  var url, kind;
  if (bulk) {
    kind = "zip";
    url = "/api/jobs/" + state.jobId + "/zip?indices=" + indices.join(",") + "&" + query;
  } else {
    kind = fmt || (/\.png$/i.test(r.name) ? "png" : "jpeg");
    url = r.url + "?download=1&" + query;
  }
  var type = SAVE_TYPES[kind];
  var fileName = name + type.ext;
  if (!window.showSaveFilePicker) {
    downloadUrl(url, fileName);
    closeModal("save-overlay");
    return;
  }
  var handle;
  try {
    var accept = {};
    accept[type.mime] = [type.ext];
    handle = await window.showSaveFilePicker({ suggestedName: fileName, types: [{ description: type.label, accept: accept }] });
  } catch (e) {
    if (e && e.name === "AbortError") return;  /* fenêtre du navigateur annulée */
    downloadUrl(url, fileName);                 /* sélecteur refusé (iframe, politique) : téléchargement classique */
    closeModal("save-overlay");
    return;
  }
  $("save-go").disabled = true;
  $("save-go").textContent = "Enregistrement…";
  try {
    var res = await api(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    await res.body.pipeTo(await handle.createWritable());  /* écrit en flux : pas de gros fichier en mémoire */
    closeModal("save-overlay");
    toast("Enregistré : " + handle.name);
  } catch (e) {
    fail(e);
    if ($("save-go")) { $("save-go").disabled = false; $("save-go").textContent = "Enregistrer"; }
  }
}

init();
