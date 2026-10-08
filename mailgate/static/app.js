/* mailgate web UI: plain JavaScript, no build step, no external requests. */
"use strict";

const T = {
  en: {
    loginIntro: "Enter the UI passphrase you set with mg ui.", passphrase: "Passphrase", login: "Log in",
    wrongPass: "Wrong passphrase.", search: "Search", searchPh: "Search mail  ( / )", compose: "Write",
    folders: "Folders", syncNow: "Sync now", unreadOnly: "Unread only", nothingSelected: "No message selected",
    newMessage: "New message", reply: "Reply", replyAll: "Reply all", forward: "Forward", from: "From", to: "To",
    subject: "Subject", message: "Message", addSignature: "Add signature", fwdAtts: "Include original attachments",
    attach: "Attach files", send: "Send", discard: "Discard", cancel: "Cancel", close: "Close",
    sendHint: "Sending here goes out directly, without the approval queue, because you are the one writing it.",
    moveTo: "Move to folder", folder: "Folder", shortcuts: "Keyboard shortcuts", unified: "All inboxes",
    approvals: "Approvals", archive: "Archive", del: "Move to Trash", markUnread: "Mark unread", markRead: "Mark read",
    flag: "Flag", unflag: "Remove flag", move: "Move…", thread: "Conversation", text: "Text", original: "Original",
    loadImages: "Load images", imagesBlocked: n => `${n} remote image${n === 1 ? "" : "s"} blocked.`,
    attachments: "Attachments", cc: "Cc", date: "Date", noMail: "No mail here.", loading: "Loading…",
    readonly: "Read-only: no UI passphrase is set. Run mg ui --set-passphrase in a terminal to enable changes and sending.",
    synced: t => `Synced ${t}`, newMail: n => `${n} new message${n === 1 ? "" : "s"}`, sent: "Sent.",
    noDrafts: "No drafts waiting for approval.", draftBy: "Queued by an agent", expires: "expires",
    autoAt: "sends automatically at", sendDraft: "Send", discardDraft: "Discard", stop: "Stop",
    confirmSendDraft: "Send this draft now?", confirmDiscard: "Discard this message?", needTo: "Add a recipient.",
    notify: "Desktop notifications", notifyOn: "Notifications on", logout: "Log out", wrote: (d, n) => `On ${d}, ${n} wrote:`,
    fwdHead: "---------- Forwarded message ----------", results: q => `Search: ${q}`, back: "Back",
    keys: [["j / k", "next / previous message"], ["Enter", "open message"], ["r", "reply"], ["a", "reply all"],
      ["f", "forward"], ["c", "write new"], ["/", "search"], ["e", "archive"], ["#", "move to Trash"],
      ["u", "toggle read / unread"], ["s", "toggle flag"], ["v", "move to folder"], ["t", "text / original"],
      ["Esc", "back / close"], ["?", "this help"]],
    error: "Error", sending: "Sending…", agoNow: "just now", pair: "Transfer to another device",
    pairHint: "On the other device run the command below, or scan the code. It includes your passwords (encrypted), works once and expires in 10 minutes.",
    pairNoCrypto: "Transfer needs the optional cryptography package: pip install 'mailgate[crypto]'.",
    pairConfirm: "Start a one-time transfer over your local network for 10 minutes?",
    duplicates: n => `${n} copies`, selectAll: "Select all", plugins: "Plugins",
    auth_pass: "verified", auth_fail: "authentication failed", auth_none: "unverified",
  },
  de: {
    loginIntro: "Gib die UI-Passphrase ein, die du mit mg ui festgelegt hast.", passphrase: "Passphrase",
    login: "Anmelden", wrongPass: "Falsche Passphrase.", search: "Suche", searchPh: "Mails durchsuchen  ( / )",
    compose: "Schreiben", folders: "Ordner", syncNow: "Jetzt abrufen", unreadOnly: "Nur ungelesene",
    nothingSelected: "Keine Nachricht ausgewählt", newMessage: "Neue Nachricht", reply: "Antworten",
    replyAll: "Allen antworten", forward: "Weiterleiten", from: "Von", to: "An", subject: "Betreff",
    message: "Nachricht", addSignature: "Signatur anhängen", fwdAtts: "Originalanhänge mitsenden",
    attach: "Dateien anhängen", send: "Senden", discard: "Verwerfen", cancel: "Abbrechen", close: "Schließen",
    sendHint: "Was du hier schreibst, geht direkt raus, ohne Freigabe-Warteschlange, weil du selbst schreibst.",
    moveTo: "In Ordner verschieben", folder: "Ordner", shortcuts: "Tastenkürzel", unified: "Alle Posteingänge",
    approvals: "Freigaben", archive: "Archivieren", del: "In den Papierkorb", markUnread: "Als ungelesen markieren",
    markRead: "Als gelesen markieren", flag: "Markieren", unflag: "Markierung entfernen", move: "Verschieben…",
    thread: "Unterhaltung", text: "Text", original: "Original", loadImages: "Bilder laden",
    imagesBlocked: n => `${n} externe${n === 1 ? "s Bild" : " Bilder"} blockiert.`, attachments: "Anhänge", cc: "Cc",
    date: "Datum", noMail: "Hier ist keine Post.", loading: "Lädt…",
    readonly: "Nur lesen: Es ist keine UI-Passphrase gesetzt. Mit mg ui --set-passphrase im Terminal lassen sich Änderungen und Senden freischalten.",
    synced: t => `Abgerufen ${t}`, newMail: n => `${n} neue Nachricht${n === 1 ? "" : "en"}`, sent: "Gesendet.",
    noDrafts: "Keine Entwürfe warten auf Freigabe.", draftBy: "Von einem Agenten angelegt", expires: "läuft ab",
    autoAt: "wird automatisch gesendet um", sendDraft: "Senden", discardDraft: "Verwerfen", stop: "Stoppen",
    confirmSendDraft: "Diesen Entwurf jetzt senden?", confirmDiscard: "Diese Nachricht verwerfen?",
    needTo: "Bitte einen Empfänger eintragen.", notify: "Desktop-Benachrichtigungen",
    notifyOn: "Benachrichtigungen an", logout: "Abmelden", wrote: (d, n) => `Am ${d} schrieb ${n}:`,
    fwdHead: "---------- Weitergeleitete Nachricht ----------", results: q => `Suche: ${q}`, back: "Zurück",
    keys: [["j / k", "nächste / vorherige Nachricht"], ["Enter", "Nachricht öffnen"], ["r", "antworten"],
      ["a", "allen antworten"], ["f", "weiterleiten"], ["c", "neue Nachricht"], ["/", "suchen"], ["e", "archivieren"],
      ["#", "in den Papierkorb"], ["u", "gelesen / ungelesen"], ["s", "Markierung an / aus"],
      ["v", "in Ordner verschieben"], ["t", "Text / Original"], ["Esc", "zurück / schließen"], ["?", "diese Hilfe"]],
    error: "Fehler", sending: "Wird gesendet…", agoNow: "gerade eben", pair: "Auf anderes Gerät übertragen",
    pairHint: "Auf dem anderen Gerät den Befehl unten ausführen oder den Code scannen. Enthält deine Passwörter (verschlüsselt), funktioniert einmal und läuft nach 10 Minuten ab.",
    pairNoCrypto: "Für die Übertragung wird das optionale Paket cryptography gebraucht: pip install 'mailgate[crypto]'.",
    pairConfirm: "Einmalige Übertragung im lokalen Netz für 10 Minuten starten?",
    duplicates: n => `${n} Kopien`, selectAll: "Alle auswählen", plugins: "Plugins",
    auth_pass: "geprüft", auth_fail: "Prüfung fehlgeschlagen", auth_none: "ungeprüft",
  },
};

const ICONS = {
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
  pen: '<path d="M12 20h8"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  back: '<path d="M19 12H5"/><path d="m11 18-6-6 6-6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  reply: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v5"/>',
  replyAll: '<path d="M8 14 3 9l5-5"/><path d="M13 14 8 9l5-5"/><path d="M8 9h7a6 6 0 0 1 6 6v5"/>',
  forward: '<path d="m15 14 5-5-5-5"/><path d="M20 9H10a6 6 0 0 0-6 6v5"/>',
  archive: '<rect x="3" y="4" width="18" height="4.5" rx="1"/><path d="M5 8.5V19a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8.5"/><path d="M10 12.5h4"/>',
  trash: '<path d="M4 6.5h16"/><path d="M18 6.5V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V6.5"/><path d="M9 6.5V4.8A1.3 1.3 0 0 1 10.3 3.5h3.4A1.3 1.3 0 0 1 15 4.8v1.7"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/>',
  flag: '<path d="M5 21V4"/><path d="M5 4h12l-2.5 4.5L17 13H5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  move: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M10 13h6M13.5 10.5 16 13l-2.5 2.5"/>',
  clip: '<path d="m20.5 11.5-8.3 8.3a5.5 5.5 0 0 1-7.8-7.8l8.3-8.3a3.7 3.7 0 0 1 5.2 5.2l-8.3 8.3a1.8 1.8 0 0 1-2.6-2.6l7.7-7.7"/>',
  inbox: '<path d="M21 13h-5l-2 3h-4l-2-3H3"/><path d="M5.6 5.9 3 13v5a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5l-2.6-7.1A2 2 0 0 0 16.5 4.5h-9a2 2 0 0 0-1.9 1.4z"/>',
  shield: '<path d="M12 21s7.5-3.6 7.5-9.5V5.5L12 3 4.5 5.5v6C4.5 17.4 12 21 12 21z"/><path d="m9 12 2 2 4-4"/>',
  bell: '<path d="M6 9a6 6 0 0 1 12 0c0 6.5 2.5 8 2.5 8h-17S6 15.5 6 9"/><path d="M10.3 20.5a1.9 1.9 0 0 0 3.4 0"/>',
  text: '<path d="M4 6h16M4 11h16M4 16h10"/>',
  thread: '<path d="M20 14.5a2 2 0 0 1-2 2H8l-4 3.5V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 16l-4-4 4-4"/><path d="M6 12h10"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M8 14h8"/>',
  unsub: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/><path d="M15 15l5 5M20 15l-5 5"/>',
  news: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  snooze: '<path d="M9 4h6l-6 7h6"/><path d="M5 20a8 8 0 0 0 14-6"/>',
  plug: '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>',
  devices: '<rect x="2.5" y="5" width="13" height="10" rx="1.5"/><path d="M6 19h6"/><rect x="17.5" y="8" width="4" height="11" rx="1"/>',
  dl: '<path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 19.5h14"/>',
};

const $ = id => document.getElementById(id);
const S = {
  st: null, csrf: "", lang: "en", view: { kind: "unified" }, items: [], next: "", loading: false, sel: null,
  msg: null, images: new Set(), textMode: false, threadMode: false, drafts: [], compose: null, files: [],
  notify: false,
};
const t = (k, ...a) => { const v = (T[S.lang] || T.en)[k] ?? T.en[k] ?? k; return typeof v === "function" ? v(...a) : v; };

function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "i");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICONS[name] || "";
  return s;
}

/* h("div", {class: "x", onclick: fn}, child, "text") - never uses innerHTML for data */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : String(c));
  return el;
}

function iconBtn(name, label, onclick, extra = {}) {
  return h("button", { type: "button", class: "icon", title: label, "aria-label": label, onclick, ...extra }, icon(name));
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": S.csrf },
  };
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401) { showLogin(); throw new Error(j.error || "login required"); }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function status(msg, err = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("err", err);
}

function fail(e) { status(`${t("error")}: ${e.message || e}`, true); }

/* ---- dates ---- */
function fmtDate(sec, long = false) {
  if (!sec) return "";
  const d = new Date(sec * 1000), now = new Date(), loc = S.lang === "de" ? "de-DE" : "en-GB";
  if (long) return d.toLocaleString(loc, { dateStyle: "medium", timeStyle: "short" });
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(loc, { day: "numeric", month: "short" });
  return d.toLocaleDateString(loc, { day: "2-digit", month: "2-digit", year: "2-digit" });
}

function size(n) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`; }

/* ---- startup / login ---- */
async function boot() {
  S.st = await api("/api/state");
  S.lang = S.st.lang !== "auto" ? S.st.lang : ((navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en");
  document.documentElement.lang = S.lang;
  translate();
  if (S.st.need_login) return showLogin();
  S.csrf = S.st.csrf;
  $("login").hidden = true;
  $("app").hidden = false;
  $("banner").hidden = !S.st.readonly;
  $("banner").textContent = S.st.readonly ? t("readonly") : "";
  $("composeBtn").disabled = S.st.readonly;
  renderNav();
  await loadList(true);
  syncStatus();
  listen();
  const deep = location.hash.slice(1);
  if (/^[0-9a-z]+$/.test(deep)) select(deep, true);
}

function translate() {
  document.querySelectorAll("[data-t]").forEach(el => { el.textContent = t(el.dataset.t); });
  document.querySelectorAll("[data-ph]").forEach(el => { el.placeholder = t(el.dataset.ph); });
  document.querySelectorAll("[data-label]").forEach(el => {
    el.title = t(el.dataset.label); el.setAttribute("aria-label", t(el.dataset.label));
  });
  document.querySelectorAll("[data-label-aria]").forEach(el => el.setAttribute("aria-label", t(el.dataset.labelAria)));
  document.querySelectorAll("[data-icon]").forEach(el => el.replaceChildren(icon(el.dataset.icon)));
  help();
}

function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
  $("pass").focus();
}

$("loginForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  $("loginErr").textContent = "";
  try {
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase: $("pass").value }) });
    if (!r.ok) throw new Error(t("wrongPass"));
    $("pass").value = "";
    await boot();
  } catch (e) { $("loginErr").textContent = e.message; }
});

/* ---- navigation pane ---- */
/* plugin labels are "text" or {"en": ..., "de": ...} */
function L(x) { return typeof x === "string" ? x : (x && (x[S.lang] || x.en || Object.values(x)[0])) || ""; }

function navItem(label, ic, view, count, hot) {
  const cur = JSON.stringify(view) === JSON.stringify(S.view);
  return h("li", {}, h("button", { type: "button", class: "f", "aria-current": cur ? "true" : "false",
    onclick: () => openView(view) }, icon(ic), h("span", { class: "name", text: label }),
    count ? h("span", { class: "count" + (hot ? " hot" : ""), text: count }) : null));
}

function renderNav() {
  const st = S.st, nav = $("nav");
  const inboxUnread = st.accounts.reduce((n, a) => n + (a.folders.find(f => f.name === "INBOX") || { u: 0 }).u, 0);
  const top = h("ul", {}, navItem(t("unified"), "inbox", { kind: "unified" }, inboxUnread, true),
    navItem(t("approvals"), "shield", { kind: "drafts" }, st.drafts, st.drafts > 0),
    st.plugins.views.map(v => navItem(L(v.label), v.icon || "plug", { kind: "plugin", plugin: v.plugin, id: v.id },
      v.badge, v.badge > 0)));
  const accts = st.accounts.map(a => [h("h3", { title: a.email, text: a.display ? `${a.name} · ${a.email}` : a.email }),
    h("ul", {}, a.folders.map(f => navItem(f.name === "INBOX" ? "Inbox" : f.name, f.name === "INBOX" ? "inbox" : "folder",
      { kind: "folder", acct: a.name, folder: f.name }, f.u, true)))]);
  const foot = h("div", { class: "foot" },
    "Notification" in window ? h("button", { type: "button", onclick: enableNotify },
      icon("bell"), " ", S.notify ? t("notifyOn") : t("notify")) : null,
    h("button", { type: "button", onclick: () => $("helpDlg").showModal() }, icon("keyboard"), " ", t("shortcuts")),
    st.auth && st.pairing ? h("button", { type: "button", onclick: startPair }, icon("devices"), " ", t("pair")) : null,
    st.auth ? h("button", { type: "button", onclick: logout }, icon("logout"), " ", t("logout")) : null,
    h("span", { text: `mailgate ${st.version}` }));
  nav.replaceChildren(top, ...accts.flat(), foot);
}

async function refreshState() {
  try {
    S.st = await api("/api/state");
    if (S.st.need_login) return showLogin();
    renderNav();
    syncStatus();
  } catch (e) { fail(e); }
}

function syncStatus() {
  if (S.st.error) return status(S.st.error, true);
  if (S.st.last_sync) status(t("synced", fmtDate(S.st.last_sync)));
}

function setView(v) { $("app").dataset.view = v; }

function openView(view) {
  S.view = view;
  S.sel = null;
  S.msg = null;
  renderNav();
  $("reader").replaceChildren(h("div", { class: "empty", text: t("nothingSelected") }));
  setView("list");
  loadList(true);
}

/* ---- message list ---- */
function viewTitle() {
  const v = S.view;
  if (v.kind === "unified") return t("unified");
  if (v.kind === "drafts") return t("approvals");
  if (v.kind === "plugin") return L((pluginView(v) || {}).label);
  if (v.kind === "search") return t("results", v.q);
  return `${v.folder === "INBOX" ? "Inbox" : v.folder} · ${v.acct}`;
}

function listQuery(before) {
  const v = S.view, p = new URLSearchParams({ n: "50" });
  if (v.kind === "unified") p.set("folder", "INBOX");
  if (v.kind === "folder") { p.set("acct", v.acct); p.set("folder", v.folder); }
  if (v.kind === "search") p.set("q", v.q);
  if ($("unreadOnly").checked) p.set("unread", "1");
  if (before) p.set("before", before);
  return "/api/list?" + p;
}

async function loadList(reset) {
  $("listTitle").textContent = viewTitle();
  $("unreadOnly").parentElement.hidden = S.view.kind === "drafts" || S.view.kind === "plugin";
  if (S.view.kind === "drafts") return loadDrafts();
  if (S.view.kind === "plugin") return loadPluginView();
  if (S.loading || (!reset && !S.next)) return;
  S.loading = true;
  if (reset) { S.items = []; S.next = ""; $("list").replaceChildren(h("li", { class: "listmsg", text: t("loading") })); }
  try {
    const r = await api(listQuery(reset ? "" : S.next));
    const have = new Set(reset ? [] : S.items.map(m => m.mi).filter(Boolean));  // dedupe across pages
    S.items = (reset ? [] : S.items).concat(r.items.filter(m => !m.mi || !have.has(m.mi)));
    S.next = r.next;
    renderList();
  } catch (e) { fail(e); } finally { S.loading = false; }
}

function rowFor(m) {
  const meta = h("span", { class: "meta" }, m.dup ? h("span", { class: "dup", title: t("duplicates", m.dup), text: `×${m.dup}` }) : null,
    m.fl ? h("span", { class: "flag" }, icon("flag")) : null,
    m.at ? icon("clip") : null, fmtDate(m.d));
  return h("li", {}, h("button", {
    type: "button", class: "row" + (m.u ? " unread" : ""), id: "m-" + m.id, "aria-selected": S.sel === m.id ? "true" : "false",
    onclick: () => select(m.id, true),
  }, h("span", { class: "dot", "aria-label": m.u ? "unread" : null }),
  h("span", { class: "who" }, S.view.kind !== "folder" && S.st.accounts.length > 1 ? [h("span", { class: "acct", text: m.a }), " "] : null,
    m.fr || m.e), meta, h("span", { class: "subj", text: m.s || "—" }), h("span", { class: "pv", text: m.p })));
}

function renderList() {
  const ul = $("list");
  ul.replaceChildren(...S.items.map(rowFor));
  if (!S.items.length) ul.append(h("li", { class: "listmsg", text: t("noMail") }));
}

function updateRow(id, patch) {
  const m = S.items.find(x => x.id === id);
  if (!m) return;
  Object.assign(m, patch);
  const old = $("m-" + id);
  if (old) old.parentElement.replaceWith(rowFor(m));
}

new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) loadList(false); }).observe($("more"));

async function mergeTop() {
  if (S.view.kind === "drafts") return loadDrafts();
  if (S.view.kind === "plugin") return loadPluginView();
  try {
    const r = await api(listQuery(""));
    const have = new Map(S.items.map(m => [m.id, m]));
    const fresh = r.items.filter(m => !have.has(m.id));
    r.items.forEach(m => { const o = have.get(m.id); if (o && (o.u !== m.u || o.fl !== m.fl)) Object.assign(o, m); });
    const ids = new Set(r.items.map(m => m.id));
    if (r.items.length) {  // drop rows that vanished from the first page range (moved elsewhere)
      const oldest = r.items[r.items.length - 1].d;
      S.items = S.items.filter(m => ids.has(m.id) || m.d < oldest);
    }
    S.items = fresh.concat(S.items).sort((a, b) => b.d - a.d);
    renderList();
  } catch (e) { fail(e); }
}

/* ---- reader ---- */
async function select(id, open) {
  S.sel = id;
  S.textMode = false;
  S.threadMode = false;
  document.querySelectorAll(".row[aria-selected=true]").forEach(r => r.setAttribute("aria-selected", "false"));
  const row = $("m-" + id);
  if (row) { row.setAttribute("aria-selected", "true"); row.scrollIntoView({ block: "nearest" }); }
  if (open) setView("reader");
  history.replaceState(null, "", "#" + id);
  try {
    S.msg = await api("/api/msg/" + id);
    if (S.sel !== id) return;
    renderReader();
    if (S.msg.u && S.st.mark_read && !S.st.readonly) {
      act("read", [id], true);
    }
  } catch (e) { fail(e); }
}

function hdrLine(k, v) { return v ? [h("span", { class: "k", text: k }), h("span", { class: "v", text: v })] : []; }

function renderReader() {
  const m = S.msg, ro = S.st.readonly;
  const tools = h("div", { class: "tools", role: "toolbar", "aria-label": "Message actions" },
    h("button", { type: "button", class: "icon only-narrow", title: t("back"), "aria-label": t("back"), onclick: () => setView("list") }, icon("back")),
    iconBtn("reply", t("reply") + " (r)", () => startCompose("reply"), { disabled: ro }),
    iconBtn("replyAll", t("replyAll") + " (a)", () => startCompose("all"), { disabled: ro }),
    iconBtn("forward", t("forward") + " (f)", () => startCompose("forward"), { disabled: ro }),
    h("span", { class: "sep" }),
    iconBtn("archive", t("archive") + " (e)", () => act("archive"), { disabled: ro }),
    iconBtn("trash", t("del") + " (#)", () => act("trash"), { disabled: ro }),
    iconBtn("move", t("move") + " (v)", openMove, { disabled: ro }),
    iconBtn("mail", (m.u ? t("markRead") : t("markUnread")) + " (u)", () => act(m.u ? "read" : "unread"), { disabled: ro }),
    iconBtn("flag", (m.fl ? t("unflag") : t("flag")) + " (s)", () => act(m.fl ? "unflag" : "flag"),
      { disabled: ro, class: "icon" + (m.fl ? " on" : ""), "aria-pressed": m.fl ? "true" : "false" }),
    m.html || m.thread > 1 || m.text !== m.full ? h("span", { class: "sep" }) : null,
    m.html || m.text !== m.full ? iconBtn("text", (S.textMode ? t("original") : t("text")) + " (t)", toggleText,
      { class: "icon" + (S.textMode ? " on" : ""), "aria-pressed": S.textMode ? "true" : "false" }) : null,
    m.thread > 1 ? h("button", { type: "button", class: "icon" + (S.threadMode ? " on" : ""), title: t("thread"),
      "aria-pressed": S.threadMode ? "true" : "false", onclick: toggleThread }, icon("thread"), ` ${m.thread}`) : null,
    pluginButtons(m));
  const hdr = h("div", { class: "hdr" },
    h("span", { class: "k", text: t("from") }), h("span", { class: "v", text: m.from && m.from !== m.addr ? `${m.from} <${m.addr}>` : m.addr }),
    h("span", { class: "date", text: fmtDate(m.d, true) }),
    hdrLine(t("to"), m.to), hdrLine(t("cc"), m.cc));
  const atts = m.atts.length ? h("div", { class: "atts", "aria-label": t("attachments") }, m.atts.map(a =>
    h("a", { href: `/api/msg/${m.id}/att/${a.n}`, download: a.name }, icon("dl"), a.name, h("span", { class: "size", text: size(a.size) })))) : null;
  const note = m.html && m.remote && !S.images.has(m.id) && !S.textMode && !S.threadMode ? h("div", { class: "note" },
    t("imagesBlocked", m.remote), h("button", { type: "button", onclick: () => { S.images.add(m.id); renderReader(); } }, t("loadImages"))) : null;
  const rd = m.render || { badges: [], banners: [] };
  const badges = rd.badges.length ? h("span", { class: "badges" }, rd.badges.map(b =>
    h("span", { class: `badge ${b.tone || "neutral"}`, title: b.title || "", text: b.key && t(b.key) !== b.key ? t(b.key) : b.text }))) : null;
  const banners = rd.banners.map(b => h("div", { class: `note ${b.tone || "warn"}`, role: "note" }, "⚠ ", b.text));
  const head = h("div", { class: "rhead" }, tools, h("h1", {}, m.s || "—", badges), hdr, atts, banners, note);
  let body;
  if (S.threadMode) body = h("div", { class: "thread", id: "threadBox", text: t("loading") });
  else if (m.html && !S.textMode) {
    body = h("iframe", { class: "mailframe", title: m.s || "mail", sandbox: "allow-popups allow-popups-to-escape-sandbox",
      referrerpolicy: "no-referrer", src: `/api/msg/${m.id}/html${S.images.has(m.id) ? "?images=1" : ""}` });
  } else body = plain(S.textMode ? m.text : (m.full || m.text), m.links);
  $("reader").replaceChildren(head, body);
  if (S.threadMode) loadThread();
}

const URL_RE = /\bhttps?:\/\/[^\s<>"')\]]+/g;
function plain(text, links) {
  const div = h("div", { class: "plain" });
  let last = 0;
  for (const mt of (text || "").matchAll(URL_RE)) {
    const l = (links || {})[mt[0]] || { href: mt[0], warn: [] };  // cleaned by plugins (tracking params)
    div.append(text.slice(last, mt.index), h("a", { href: l.href, title: l.href, target: "_blank", rel: "noopener noreferrer", text: l.href }));
    for (const w of l.warn || []) div.append(h("span", { class: "linkwarn", text: `⚠ ${w}` }));
    last = mt.index + mt[0].length;
  }
  div.append((text || "").slice(last));
  return div;
}

function toggleText() { if (S.msg && (S.msg.html || S.msg.text !== S.msg.full)) { S.textMode = !S.textMode; S.threadMode = false; renderReader(); } }
function toggleThread() { S.threadMode = !S.threadMode; renderReader(); }

async function loadThread() {
  try {
    const msgs = await api("/api/thread/" + S.msg.id);
    const box = $("threadBox");
    if (!box) return;
    box.replaceChildren(...msgs.map(x => h("article", { class: "tmsg" + (x.id === S.msg.id ? " cur" : "") },
      h("header", {}, h("strong", { text: x.from || x.addr }), h("span", { class: "d", text: fmtDate(x.d, true) })),
      plain(x.text))));
  } catch (e) { fail(e); }
}

/* ---- actions ---- */
async function act(op, ids, quiet) {
  if (S.st.readonly || !S.sel && !ids) return;
  ids = ids || [S.sel];
  const idx = S.items.findIndex(m => m.id === ids[0]);
  try {
    const r = await api("/api/act", { op, ids, folder: S.moveTarget });
    if (!quiet) status(r.ok);
    if (op === "read" || op === "unread") { updateRow(ids[0], { u: op === "unread" ? 1 : 0 }); if (S.msg && S.msg.id === ids[0]) S.msg.u = op === "unread" ? 1 : 0; }
    if (op === "flag" || op === "unflag") { updateRow(ids[0], { fl: op === "flag" ? 1 : 0 }); if (S.msg) S.msg.fl = op === "flag" ? 1 : 0; }
    if (["archive", "trash", "move"].includes(op)) {
      S.items = S.items.filter(m => !ids.includes(m.id));
      renderList();
      const nxt = S.items[Math.min(idx, S.items.length - 1)];
      if (nxt) select(nxt.id, false);
      else { S.sel = null; S.msg = null; $("reader").replaceChildren(h("div", { class: "empty", text: t("nothingSelected") })); setView("list"); }
    } else if (S.msg && !quiet) renderReader();
    if (!quiet || op === "read") refreshState();
  } catch (e) { fail(e); }
}

async function openMove() {
  if (!S.msg || S.st.readonly) return;
  const dlg = $("moveDlg"), list = $("moveList"), filter = $("moveFilter");
  filter.value = "";
  list.replaceChildren(h("li", { class: "listmsg", text: t("loading") }));
  dlg.showModal();
  try {
    const folders = (await api("/api/folders?acct=" + encodeURIComponent(S.msg.acct))).filter(f => f !== S.msg.folder);
    const draw = () => list.replaceChildren(...folders.filter(f => f.toLowerCase().includes(filter.value.toLowerCase()))
      .map(f => h("li", {}, h("button", { type: "button", onclick: () => { dlg.close(); S.moveTarget = f; act("move").finally(() => { S.moveTarget = undefined; }); } }, icon("folder"), " ", f))));
    filter.oninput = draw;
    filter.onkeydown = ev => { if (ev.key === "Enter") { ev.preventDefault(); const b = list.querySelector("button"); if (b) b.click(); } };
    draw();
    filter.focus();
  } catch (e) { dlg.close(); fail(e); }
}

/* ---- approvals ---- */
async function loadDrafts() {
  try {
    S.drafts = await api("/api/drafts");
    const ul = $("list");
    ul.replaceChildren(...S.drafts.map(d => h("li", {}, h("button", { type: "button", class: "row", id: "m-" + d.id,
      "aria-selected": S.sel === d.id ? "true" : "false", onclick: () => showDraft(d.id) },
    h("span", { class: "dot" }), h("span", { class: "who", text: d.to }), h("span", { class: "meta", text: fmtDate(d.created) }),
    h("span", { class: "subj", text: d.s || "—" }), h("span", { class: "pv", text: `${d.id} · ${d.acct}` })))));
    if (!S.drafts.length) ul.append(h("li", { class: "listmsg", text: t("noDrafts") }));
    if (S.sel && !S.drafts.some(d => d.id === S.sel)) $("reader").replaceChildren(h("div", { class: "empty", text: t("nothingSelected") }));
  } catch (e) { fail(e); }
}

function showDraft(id) {
  const d = S.drafts.find(x => x.id === id);
  if (!d) return;
  S.sel = id;
  document.querySelectorAll(".row").forEach(r => r.setAttribute("aria-selected", r.id === "m-" + id ? "true" : "false"));
  setView("reader");
  const sched = d.status === "scheduled";
  const when = sched ? `${t("autoAt")} ${fmtDate(d.send_at, true)}` : `${t("expires")} ${fmtDate(d.expires, true)}`;
  const ro = S.st.readonly;
  $("reader").replaceChildren(h("div", { class: "draftbox" },
    h("button", { type: "button", class: "icon only-narrow", "aria-label": t("back"), onclick: () => setView("list") }, icon("back")),
    h("h1", { class: "", text: `${d.id} · ${d.s || "—"}` }),
    h("p", { class: "hint", text: `${t("draftBy")} · ${d.acct} · ${when}` }),
    h("pre", { text: d.preview }),
    h("div", { class: "btns" },
      sched ? null : h("button", { type: "button", class: "primary", disabled: ro, onclick: () => draftDo(d.id, "send") }, t("sendDraft")),
      h("button", { type: "button", class: "danger", disabled: ro, onclick: () => draftDo(d.id, "discard") }, sched ? t("stop") : t("discardDraft")))));
}

async function draftDo(id, what) {
  if (what === "send" && !confirm(t("confirmSendDraft"))) return;
  try {
    const r = await api("/api/drafts/" + id, { do: what });
    status(r.ok);
    $("reader").replaceChildren(h("div", { class: "empty", text: r.ok }));
    S.sel = null;
    await loadDrafts();
    refreshState();
  } catch (e) { fail(e); }
}

/* ---- compose ---- */
function quoteText(text) { return (text || "").split("\n").map(l => "> " + l).join("\n"); }

async function startCompose(mode) {
  if (S.st.readonly) return;
  const dlg = $("composeDlg");
  S.files = [];
  S.compose = { mode, ref: null };
  const sel = $("cFrom");
  sel.replaceChildren(...S.st.accounts.map(a => h("option", { value: a.name, text: a.display ? `${a.display} <${a.email}>` : a.email })));
  sel.value = S.st.default;
  for (const id of ["cTo", "cCc", "cBcc", "cSubject", "cBody"]) $(id).value = "";
  $("cFwdWrap").hidden = true;
  $("composeErr").textContent = "";
  $("composeTitle").textContent = { reply: t("reply"), all: t("replyAll"), forward: t("forward") }[mode] || t("newMessage");
  if (mode !== "new") {
    if (!S.msg) return;
    try {
      const f = await api(`/api/compose/${S.msg.id}?mode=${mode}`);
      S.compose.ref = S.msg.id;
      sel.value = f.acct;
      $("cTo").value = f.to;
      $("cCc").value = f.cc;
      $("cSubject").value = f.subject;
      const who = f.from && f.from !== f.addr ? `${f.from} <${f.addr}>` : f.addr;
      if (mode === "forward") {
        $("cBody").value = `\n\n${t("fwdHead")}\n${t("from")}: ${who}\n${t("date")}: ${fmtDate(f.d, true)}\n` +
          `${t("subject")}: ${f.orig_subject}\n${t("to")}: ${f.orig_to}\n${f.orig_cc ? "Cc: " + f.orig_cc + "\n" : ""}\n${f.text}`;
        $("cFwdWrap").hidden = !f.atts;
      } else {
        $("cBody").value = `\n\n${t("wrote", fmtDate(f.d, true), who)}\n${quoteText(f.text)}`;
      }
    } catch (e) { return fail(e); }
  }
  renderFiles();
  dlg.showModal();
  const first = mode === "new" || mode === "forward" ? $("cTo") : $("cBody");
  first.focus();
  if (first === $("cBody")) first.setSelectionRange(0, 0);
}

function renderFiles() {
  $("cAtts").replaceChildren(...S.files.map((f, i) => h("li", {}, `${f.name} (${size(f.size)})`,
    h("button", { type: "button", "aria-label": `${t("discard")} ${f.name}`, onclick: () => { S.files.splice(i, 1); renderFiles(); } }, "×"))));
}

$("cFiles").addEventListener("change", async ev => {
  for (const file of ev.target.files) {
    const data = await new Promise((ok, bad) => {
      const r = new FileReader();
      r.onload = () => ok(String(r.result).split(",", 2)[1] || "");
      r.onerror = bad;
      r.readAsDataURL(file);
    });
    S.files.push({ name: file.name, size: file.size, data });
  }
  ev.target.value = "";
  renderFiles();
});

function composeDirty() { return $("cBody").value.trim() || $("cTo").value.trim() || S.files.length; }
function closeCompose(ask) {
  if (ask && composeDirty() && !confirm(t("confirmDiscard"))) return;
  $("composeDlg").close();
}
$("composeClose").addEventListener("click", () => closeCompose(true));
$("composeCancel").addEventListener("click", () => closeCompose(true));
$("composeDlg").addEventListener("cancel", ev => { ev.preventDefault(); closeCompose(true); });
$("composeSend").addEventListener("click", sendCompose);
$("composeForm").addEventListener("keydown", ev => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); sendCompose(); } });

async function sendCompose() {
  if (!$("cTo").value.trim()) { $("composeErr").textContent = t("needTo"); return $("cTo").focus(); }
  const btn = $("composeSend");
  btn.disabled = true;
  btn.textContent = t("sending");
  try {
    const r = await api("/api/send", {
      acct: $("cFrom").value, to: $("cTo").value, cc: $("cCc").value, bcc: $("cBcc").value,
      subject: $("cSubject").value, body: $("cBody").value, signature: $("cSig").checked,
      mode: S.compose.mode === "all" ? "reply" : S.compose.mode, ref: S.compose.ref, fwd_atts: $("cFwd").checked,
      atts: S.files.map(f => ({ name: f.name, data: f.data })),
    });
    $("composeDlg").close();
    status(r.ok || t("sent"));
  } catch (e) { $("composeErr").textContent = e.message; } finally { btn.disabled = false; btn.textContent = t("send"); }
}

/* ---- search, sync, events, notifications ---- */
$("searchForm").addEventListener("submit", ev => {
  ev.preventDefault();
  const q = $("q").value.trim();
  openView(q ? { kind: "search", q } : { kind: "unified" });
  $("q").blur();
});
$("unreadOnly").addEventListener("change", () => loadList(true));
$("syncBtn").addEventListener("click", async () => {
  if (S.st.readonly) return mergeTop();
  try { await api("/api/sync", {}); status(t("loading")); } catch (e) { fail(e); }
});
$("composeBtn").addEventListener("click", () => startCompose("new"));
$("navBtn").addEventListener("click", () => setView("nav"));
$("listBack").addEventListener("click", () => setView("nav"));

let es = null;
function listen() {
  if (es) return;
  es = new EventSource("/api/events");
  es.onmessage = async ev => {
    const d = JSON.parse(ev.data);
    if (d.t === "sync") {
      await refreshState();
      mergeTop();
      if (d.new > 0) notifyNew(d.new);
    }
    if (d.t === "notify") {
      status(d.text);
      refreshState();
      if (S.notify && document.hidden) new Notification("mailgate", { body: d.text, icon: "/static/icon.svg" });
    }
    if (d.t === "drafts") {
      await refreshState();
      if (S.view.kind === "drafts") loadDrafts();
      if (d.n > 0 && document.hidden && S.notify) new Notification("mailgate", { body: `${t("approvals")}: ${d.n}` });
    }
  };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) { es = null; setTimeout(listen, 5000); } };
}

async function enableNotify() {
  if (Notification.permission !== "granted") await Notification.requestPermission();
  S.notify = Notification.permission === "granted";
  try { localStorage.setItem("mg-notify", S.notify ? "1" : ""); } catch (e) { /* private mode */ }
  renderNav();
}

function notifyNew(n) {
  status(t("newMail", n));
  if (S.notify && document.hidden) new Notification("mailgate", { body: t("newMail", n), icon: "/static/icon.svg" });
}

async function logout() {
  try { await api("/api/logout", {}); } catch (e) { /* ignore */ }
  location.reload();
}

/* ---- plugins: message actions, sidebar views, device transfer ---- */
function pluginView(v) { return S.st.plugins.views.find(x => x.plugin === v.plugin && x.id === v.id); }

function choiceMenu(anchor, choices, onpick) {
  const old = document.querySelector(".menu");
  if (old) old.remove();
  const menu = h("div", { class: "menu", role: "menu" }, choices.map(([value, label]) =>
    h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); onpick(value); } }, L(label))));
  anchor.after(menu);
  const first = menu.querySelector("button");
  if (first) first.focus();
  menu.addEventListener("keydown", ev => { if (ev.key === "Escape") { ev.stopPropagation(); menu.remove(); anchor.focus(); } });
}

document.addEventListener("click", ev => {
  const menu = document.querySelector(".menu");
  if (menu && !menu.contains(ev.target) && !(ev.target.closest && ev.target.closest(".menuwrap"))) menu.remove();
});

function pluginButtons(m) {
  const acts = S.st.plugins.actions.filter(a => (m.actions || []).includes(`${a.plugin}/${a.id}`));
  if (!acts.length) return null;
  return [h("span", { class: "sep" }), ...acts.map(a => {
    const run = async choice => {
      if (a.confirm && !confirm(L(a.confirm))) return;
      try { const r = await api(`/api/plugin/${a.plugin}/action/${a.id}`, { id: m.id, choice }); status(r.ok); refreshState(); } catch (e) { fail(e); }
    };
    const btn = h("button", { type: "button", class: "icon", title: L(a.label), "aria-label": L(a.label), disabled: S.st.readonly,
      "aria-haspopup": a.choices ? "menu" : null, onclick: ev => a.choices ? choiceMenu(ev.currentTarget, a.choices, run) : run(null) },
    icon(a.icon || "plug"));
    return h("span", { class: "menuwrap" }, btn);
  })];
}

async function loadPluginView() {
  const v = S.view, def = pluginView(v);
  if (!def) return;
  try {
    const r = await api(`/api/plugin/${v.plugin}/view/${v.id}`);
    S.pv = r;
    const ul = $("list");
    const picked = new Set();
    const bar = h("li", { class: "pvbar" },
      r.multi ? h("label", { class: "toggle" }, h("input", { type: "checkbox", onchange: ev => {
        ul.querySelectorAll("input[data-key]").forEach(c => { c.checked = ev.target.checked; c.dispatchEvent(new Event("change")); });
      } }), " ", t("selectAll")) : null,
      def.actions.map(a => h("button", { type: "button", disabled: S.st.readonly, onclick: ev => {
        const keys = [...picked];
        if (!keys.length) return;
        const go = async choice => {
          if (a.confirm && !confirm(L(a.confirm))) return;
          try { const res = await api(`/api/plugin/${v.plugin}/view/${v.id}/${a.id}`, { keys, choice }); status(res.ok); loadPluginView(); refreshState(); } catch (e) { fail(e); }
        };
        a.choices ? choiceMenu(ev.currentTarget, a.choices, go) : go(null);
      } }, L(a.label))));
    ul.replaceChildren(bar, ...r.items.map(it => h("li", { class: "pvrow" + (it.hot ? " hot" : "") },
      r.multi ? h("input", { type: "checkbox", "data-key": it.key, "aria-label": it.title,
        onchange: ev => ev.target.checked ? picked.add(it.key) : picked.delete(it.key) }) : null,
      h("button", { type: "button", class: "row", onclick: () => it.msg && select(it.msg, true) },
        h("span", { class: "dot" }), h("span", { class: "who", text: it.title }), h("span", { class: "meta", text: "" }),
        h("span", { class: "subj", text: it.sub || "" }), h("span", { class: "pv", text: it.meta || "" })))));
    if (!r.items.length) ul.append(h("li", { class: "listmsg", text: L(r.empty) || t("noMail") }));
  } catch (e) { fail(e); }
}

async function startPair() {
  if (!confirm(t("pairConfirm"))) return;
  const dlg = $("pairDlg");
  $("pairBody").replaceChildren(h("p", { text: t("loading") }));
  dlg.showModal();
  try {
    const r = await api("/api/pair", {});
    const n = r.qr.length, pad = 3, svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${n + 2 * pad} ${n + 2 * pad}`);
    svg.setAttribute("class", "qr");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", r.url);
    let d = "";
    r.qr.forEach((row, y) => row.forEach((on, x) => { if (on) d += `M${x + pad} ${y + pad}h1v1h-1z`; }));
    svg.innerHTML = `<rect width="100%" height="100%" fill="#fff"/><path fill="#000" d="${d}"/>`;
    $("pairBody").replaceChildren(h("p", { text: t("pairHint") }), svg,
      h("pre", { class: "paircmd", text: `mg import ${r.code}@${r.host}:${r.port}` }), h("p", { class: "hint", text: r.url }));
  } catch (e) { $("pairBody").replaceChildren(h("p", { class: "error", text: e.message })); }
}

/* ---- keyboard ---- */
function move(delta) {
  if (!S.items.length || S.view.kind === "drafts") return;
  let i = S.items.findIndex(m => m.id === S.sel);
  i = i < 0 ? 0 : Math.max(0, Math.min(S.items.length - 1, i + delta));
  select(S.items[i].id, false);
  const row = $("m-" + S.items[i].id);
  if (row) row.focus({ preventScroll: true });
  if (i > S.items.length - 10) loadList(false);
}

function help() {
  $("helpList").replaceChildren(...t("keys").flatMap(([k, d]) => [h("dt", {}, h("kbd", { text: k })), h("dd", { text: d })]));
}

document.addEventListener("keydown", ev => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || document.querySelector("dialog[open]")) return;
  const tag = (ev.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") {
    if (ev.key === "Escape") ev.target.blur();
    return;
  }
  const k = ev.key, has = !!S.msg && S.view.kind !== "drafts";
  const map = {
    j: () => move(1), k: () => move(-1), ArrowDown: null, ArrowUp: null,
    Enter: () => { if (S.sel) { setView("reader"); $("reader").focus(); } },
    r: () => has && startCompose("reply"), a: () => has && startCompose("all"), f: () => has && startCompose("forward"),
    c: () => startCompose("new"), "/": () => $("q").focus(), e: () => has && act("archive"), "#": () => has && act("trash"),
    u: () => has && act(S.msg.u ? "read" : "unread"), s: () => has && act(S.msg.fl ? "unflag" : "flag"),
    v: () => has && openMove(), t: () => has && toggleText(), "?": () => $("helpDlg").showModal(),
    Escape: () => setView($("app").dataset.view === "reader" ? "list" : "nav"),
  };
  const fn = map[k];
  if (!fn) return;
  if (k === "Enter" && tag === "button" && !ev.target.classList.contains("row")) return;
  if (k === "Enter" && tag === "button") { ev.preventDefault(); return ev.target.click(); }
  ev.preventDefault();
  fn();
});

try { S.notify = localStorage.getItem("mg-notify") === "1" && "Notification" in window && Notification.permission === "granted"; } catch (e) { /* ignore */ }
boot().catch(fail);
