"use strict";

const ICONS = { console: "🎮", pc: "🖥", telephone: "📱", tablette: "📲", tv: "📺", autre: "❔" };
const $ = (s) => document.querySelector(s);
const state = { view: "today", day: todayISO(), devices: [], editing: null };

function todayISO() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}
function shiftDay(iso, n) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + n);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}
function dayLabel(iso) {
  if (iso === todayISO()) return "Aujourd’hui";
  if (iso === shiftDay(todayISO(), -1)) return "Hier";
  return new Date(iso + "T12:00:00").toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}
function fmtDur(sec) {
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const linkLabel = (d) => d.link === "wifi" ? "📶 Wi‑Fi · " : d.link === "filaire" ? "🔌 Filaire · " : "";
const displayName = (d) => d.name || d.hostname || d.ip || d.mac;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}

// --- Vue « Temps » ----------------------------------------------------------------
function fmtRate(bps) {
  if (bps == null) return "";
  const m = bps / 1e6;
  return m >= 10 ? `${m.toFixed(0)} Mbit/s` : m >= 0.1 ? `${m.toFixed(1).replace(".", ",")} Mbit/s` : `${Math.round(bps / 1e3)} kbit/s`;
}
const measured = (d) => d.active_seconds != null || d.rate_down != null;

function statusLine(d) {
  if (!d.online) return `<span class="dot"></span> Hors ligne`;
  if (d.active_now) return `<span class="dot live"></span> <b class="live-txt">Utilisé · ${fmtRate(d.rate_down + d.rate_up)}</b>`;
  if (d.rate_down != null) return `<span class="dot on"></span> En ligne · au repos`;
  return `<span class="dot on"></span> En ligne`;
}

function renderLive(devices) {
  // Tous les appareils (suivis ou non) qui consomment du trafic en ce moment
  const now = devices.filter((d) => d.rate_down != null && d.rate_down + d.rate_up >= 50000)
    .sort((a, b) => (b.rate_down + b.rate_up) - (a.rate_down + a.rate_up));
  if (!devices.some((d) => d.rate_down != null)) return "";
  const max = Math.max(1e6, ...now.map((d) => d.rate_down + d.rate_up));
  return `<div class="group-title">⚡ En direct</div><div class="card live-card">${now.length ? now.slice(0, 8).map((d) => `
    <div class="live-row tap-row" data-mac="${esc(d.mac)}">
      <span class="live-name">${ICONS[d.kind] || "❔"} ${esc(displayName(d))}</span>
      <span class="live-rate">${fmtRate(d.rate_down + d.rate_up)}</span>
      <div class="live-bar"><div style="width:${((d.rate_down + d.rate_up) / max) * 100}%"></div></div>
    </div>`).join("") : `<div class="muted">Aucun appareil n’utilise Internet en ce moment.</div>`}
    ${now.length > 8 ? `<div class="muted">… et ${now.length - 8} autres</div>` : ""}</div>
    <p class="live-note">Débit relevé par la Livebox : toutes les 10 s pour le Wi‑Fi direct, toutes les 30 s pour les autres (câble, switch, répéteur).</p>`;
}

function renderToday(usage) {
  const el = $("#view-today");
  const isToday = usage.day === todayISO();
  const live = isToday ? renderLive(usage.devices) : "";
  const tracked = usage.devices.filter((d) => d.tracked);
  if (!tracked.length) {
    el.innerHTML = live + `<div class="empty">Aucun appareil suivi pour l’instant.<br><br>
      Ouvrez l’onglet <b>Appareils</b>, touchez une console ou un PC et cochez
      « Suivre le temps de connexion ».</div>`;
    return;
  }
  const span = usage.end - usage.start;
  el.innerHTML = live + (live ? `<div class="group-title">Temps du jour</div>` : "") + tracked.map((d) => {
    const m = measured(d);
    const main = m ? (d.active_seconds || 0) : d.seconds;  // temps qui compte pour la limite
    const lim = d.daily_limit ? d.daily_limit * 60 : 0;
    const ratio = lim ? main / lim : 0;
    const cls = ratio >= 1 ? "over" : ratio >= 0.8 ? "warn" : "";
    const segs = d.sessions.map((s) =>
      `<i style="left:${((s.start - usage.start) / span) * 100}%;width:${((s.end - s.start) / span) * 100}%"></i>`).join("");
    const list = d.sessions.slice(-4).map((s) =>
      `${fmtTime(s.start)}–${s.open ? "maintenant" : fmtTime(s.end)}`).join(" · ");
    return `<div class="card tap" data-mac="${esc(d.mac)}">
      <div class="dev-head">
        <span class="dev-icon">${ICONS[d.kind] || "❔"}</span>
        <div style="flex:1;min-width:0">
          <div class="dev-name">${esc(displayName(d))}</div>
          <div class="dev-sub">${isToday ? statusLine(d) : ""}</div>
        </div>
        <div class="dev-times">
          <span class="dev-time">${fmtDur(main)}</span>
          <span class="dev-time-label">${m ? `utilisé · connecté ${fmtDur(d.seconds)}` : "connecté"}</span>
        </div>
      </div>
      ${lim ? `<div class="bar ${cls}"><div style="width:${Math.min(100, ratio * 100)}%"></div></div>
        <div class="limit ${ratio >= 1 ? "over" : ""}">${ratio >= 1
          ? `Limite dépassée de ${fmtDur(main - lim)}`
          : `Reste ${fmtDur(lim - main)} sur ${fmtDur(lim)}`}</div>` : ""}
      <div class="timeline">${segs}</div>
      <div class="ticks"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>24 h</span></div>
      ${list ? `<div class="sessions">Connexions : ${d.sessions.length > 4 ? "… " : ""}${list}</div>` : ""}
    </div>`;
  }).join("");
}

// --- Vue « Semaine » --------------------------------------------------------------
function renderWeek(week) {
  const el = $("#view-week");
  const tracked = state.devices.filter((d) => d.tracked);
  if (!tracked.length) { el.innerHTML = `<div class="empty">Aucun appareil suivi.</div>`; return; }
  const dayNames = week.days.map((d) => new Date(d + "T12:00:00").toLocaleDateString("fr-FR", { weekday: "narrow" }));
  el.innerHTML = tracked.map((d) => {
    const conn = week.usage[d.mac] || week.days.map(() => 0);
    const act = (week.active || {})[d.mac];
    const main = act || conn;  // temps utilisé si mesuré, sinon temps connecté
    const max = Math.max(3600, d.daily_limit ? d.daily_limit * 60 : 0, ...conn);
    const total = main.reduce((a, b) => a + b, 0);
    const lim = d.daily_limit ? d.daily_limit * 60 : Infinity;
    return `<div class="card">
      <div class="dev-head">
        <span class="dev-icon">${ICONS[d.kind] || "❔"}</span>
        <div class="dev-name">${esc(displayName(d))}</div>
        <div class="week-total">${fmtDur(total)} · moy. ${fmtDur(total / 7)}/j</div>
      </div>
      <div class="week-chart">${conn.map((c, i) => `<div class="col" title="${fmtDur(main[i])}">
          <div class="b ${act ? "conn" : main[i] > lim ? "over" : ""}" style="height:${(c / max) * 100}%">
            ${act ? `<div class="b act ${main[i] > lim ? "over" : ""}" style="height:${c ? Math.min(100, (act[i] / c) * 100) : 0}%"></div>` : ""}
          </div></div>`).join("")}</div>
      <div class="week-days">${main.map((v, i) => `<span>${dayNames[i]}<br>${v ? fmtDur(v).replace(" min", "′") : "–"}</span>`).join("")}</div>
      ${act ? `<div class="legend"><i class="sw act"></i>utilisé <i class="sw conn"></i>connecté sans utilisation</div>` : ""}
    </div>`;
  }).join("");
}

// --- Vue « Appareils » ------------------------------------------------------------
function renderDevices() {
  const el = $("#view-devices");
  if (!state.devices.length) {
    el.innerHTML = `<div class="empty">Aucun appareil détecté pour l’instant.<br>Le premier scan prend environ une minute.</div>`;
    return;
  }
  const card = (d) => `<div class="card tap" data-mac="${esc(d.mac)}">
    <div class="dev-head">
      <span class="dot ${d.online ? "on" : ""}"></span>
      <span class="dev-icon">${ICONS[d.kind] || "❔"}</span>
      <div style="flex:1;min-width:0">
        <div class="dev-name">${esc(displayName(d))}</div>
        <div class="dev-sub">${linkLabel(d)}${esc(d.ip)} · ${esc(d.mac)}${d.hostname && d.name ? " · " + esc(d.hostname) : ""}</div>
        ${d.active_now ? `<div class="dev-sub live-txt">⚡ ${fmtRate(d.rate_down + d.rate_up)}</div>` : ""}
      </div>
      ${d.tracked ? "⏱" : ""}
    </div></div>`;
  const tracked = state.devices.filter((d) => d.tracked);
  const others = state.devices.filter((d) => !d.tracked);
  el.innerHTML =
    (tracked.length ? `<div class="group-title">Suivis</div>${tracked.map(card).join("")}` : "") +
    `<div class="group-title">Autres appareils (${others.length})</div>` +
    `<p class="muted" style="margin:0 4px 10px">Touchez un appareil pour le nommer et le suivre.
      Astuce : éteignez/allumez une console pour repérer laquelle c’est.</p>` +
    others.map(card).join("");
}

// --- Édition ----------------------------------------------------------------------
function openEdit(mac) {
  const d = state.devices.find((x) => x.mac === mac);
  if (!d) return;
  state.editing = mac;
  const f = $("#edit-form");
  f.name.value = d.name || "";
  f.kind.value = d.kind || "autre";
  f.tracked.checked = !!d.tracked;
  f.daily_limit.value = d.daily_limit || "";
  $("#edit-info").textContent = `${d.ip} · ${d.mac}${d.hostname ? " · " + d.hostname : ""} — vu pour la 1re fois le ${new Date(d.first_seen * 1000).toLocaleDateString("fr-FR")}`;
  $("#edit").showModal();
}

$("#edit").addEventListener("close", async () => {
  const action = $("#edit").returnValue;
  const f = $("#edit-form");
  const mac = encodeURIComponent(state.editing);
  try {
    if (action === "save") {
      await api(`/api/devices/${mac}`, { method: "PATCH", body: JSON.stringify({
        name: f.name.value, kind: f.kind.value, tracked: f.tracked.checked,
        daily_limit: f.daily_limit.value ? Number(f.daily_limit.value) : null }) });
    } else if (action === "delete" && confirm("Oublier cet appareil et son historique ?")) {
      await api(`/api/devices/${mac}`, { method: "DELETE" });
    } else return;
    await refresh();
  } catch (e) { showError(e); }
});

document.addEventListener("click", (e) => {
  const card = e.target.closest(".card.tap, .tap-row");
  if (card) openEdit(card.dataset.mac);
});

// --- Navigation & rafraîchissement --------------------------------------------------
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  state.view = b.dataset.view;
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${state.view}`));
  $("#day-nav").style.visibility = state.view === "devices" ? "hidden" : "visible";
  refresh();
}));
$("#prev").addEventListener("click", () => { state.day = shiftDay(state.day, -1); refresh(); });
$("#next").addEventListener("click", () => { state.day = shiftDay(state.day, 1); refresh(); });
$("#refresh").addEventListener("click", async () => {
  $("#refresh").classList.add("spin");
  try { await api("/api/scan", { method: "POST" }); } catch (e) { showError(e); }
  setTimeout(() => { $("#refresh").classList.remove("spin"); refresh(); }, 6000);
});

function showError(e) {
  const s = $("#status");
  s.textContent = "Erreur : " + e.message;
  s.className = "status err";
}

async function refresh() {
  $("#day-label").textContent = state.view === "week" ? `7 jours jusqu’à ${dayLabel(state.day).toLowerCase()}` : dayLabel(state.day);
  $("#next").disabled = state.day >= todayISO();
  try {
    const [status, devices] = await Promise.all([api("/api/status"), api("/api/devices")]);
    state.devices = devices;
    if (state.view === "today") renderToday(await api(`/api/usage?day=${state.day}`));
    else if (state.view === "week") renderWeek(await api(`/api/week?day=${state.day}`));
    else renderDevices();
    const s = $("#status");
    s.className = "status" + (status.last_error ? " err" : "");
    const src = status.source === "Livebox" ? "Livebox" : `scan du réseau ${status.network || ""}`;
    s.textContent = status.last_error ? `⚠ ${status.last_error}`
      : status.last_scan ? `Source : ${src} · dernier relevé à ${fmtTime(status.last_scan)} · toutes les ${status.interval} s`
      : "Premier relevé en cours…";
  } catch (e) { showError(e); }
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden && !$("#edit").open) refresh(); }, 10000);
refresh();
