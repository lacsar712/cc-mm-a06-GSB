const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let view = "shift";

const loginBox = document.querySelector("#login");
const shiftBox = document.querySelector("#shift");
const heatBox = document.querySelector("#heat");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const heatRows = document.querySelector("#heat-rows");
const snapRows = document.querySelector("#snap-rows");
const heatMsg = document.querySelector("#heat-msg");
const prefixLen = document.querySelector("#prefix-len");
const heatLiveView = document.querySelector("#heat-live-view");
const heatSnapView = document.querySelector("#heat-snap-view");

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.site)}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${esc(r.note)}</td></tr>`,
    )
    .join("");
}

function paintHeat(groups) {
  heatRows.innerHTML = groups
    .map(
      (g) =>
        `<tr><td>${esc(g.prefix)}</td><td class="${g.alarm_count > 0 ? "alarm hot" : ""}">${g.alarm_count}</td><td class="ok">${g.normal_count}</td><td class="hitids">${g.hit_ids.join(", ") || "—"}</td></tr>`,
    )
    .join("");
}

function paintSnaps(list) {
  snapRows.innerHTML = list
    .map(
      (s) =>
        `<tr><td>${s.id}</td><td>${s.prefix_length}</td><td>${esc(s.created_by)}</td><td>${new Date(s.created_at).toLocaleString()}</td><td class="hitids">${s.hit_ids.join(", ") || "—"}</td><td><button data-snap="${s.id}">查看</button></td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function switchView(next) {
  view = next;
  shiftBox.hidden = view !== "shift";
  heatBox.hidden = view !== "heat";
  document.querySelector("#nav-shift").classList.toggle("active", view === "shift");
  document.querySelector("#nav-heat").classList.toggle("active", view === "heat");
  if (view === "heat") {
    heatLiveView.hidden = false;
    heatSnapView.hidden = true;
    loadHeat();
    loadSnaps();
  }
}

function showApp() {
  loginBox.hidden = true;
  document.querySelector("#nav").hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  document.querySelector("#save-prefix").hidden = role !== "writer";
  document.querySelector("#issue-snap").hidden = role !== "writer";
  prefixLen.disabled = role !== "writer";
  connect();
  load();
  switchView("shift");
}

async function load() {
  paint(await api("/api/readings"));
}

async function loadHeat() {
  const data = await api("/api/heat/live");
  prefixLen.value = data.prefix_length;
  paintHeat(data.groups);
}

async function loadSnaps() {
  paintSnaps(await api("/api/heat/snapshots"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
    if (view === "heat" && !heatLiveView.hidden) loadHeat();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

document.querySelector("#nav-shift").onclick = () => switchView("shift");
document.querySelector("#nav-heat").onclick = () => switchView("heat");

document.querySelector("#save-prefix").onclick = async () => {
  heatMsg.textContent = "";
  try {
    const data = await api("/api/heat/prefix", {
      method: "PUT",
      body: JSON.stringify({ prefix_length: Number(prefixLen.value) }),
    });
    heatMsg.textContent = `前缀长度已改为 ${data.prefix_length}`;
    loadHeat();
  } catch (err) {
    heatMsg.textContent = err.message;
  }
};

document.querySelector("#issue-snap").onclick = async () => {
  heatMsg.textContent = "";
  try {
    const snap = await api("/api/heat/snapshots", { method: "POST" });
    heatMsg.textContent = `已签发热力快照 #${snap.id}（前缀长度 ${snap.prefix_length}）`;
    loadSnaps();
  } catch (err) {
    heatMsg.textContent = err.message;
  }
};

snapRows.onclick = async (ev) => {
  const btn = ev.target.closest("button[data-snap]");
  if (!btn) return;
  const snap = await api(`/api/heat/snapshots/${btn.dataset.snap}`);
  document.querySelector("#snap-id").textContent = snap.id;
  document.querySelector("#snap-time").textContent = new Date(snap.created_at).toLocaleString();
  document.querySelector("#snap-by").textContent = snap.created_by;
  document.querySelector("#snap-len").textContent = snap.prefix_length;
  document.querySelector("#snap-hits").textContent = snap.hit_ids.join(", ") || "—";
  document.querySelector("#snap-groups").innerHTML = snap.groups
    .map(
      (g) =>
        `<tr><td>${esc(g.prefix)}</td><td class="${g.alarm_count > 0 ? "alarm hot" : ""}">${g.alarm_count}</td><td class="ok">${g.normal_count}</td><td class="hitids">${g.hit_ids.join(", ") || "—"}</td></tr>`,
    )
    .join("");
  heatLiveView.hidden = true;
  heatSnapView.hidden = false;
};

document.querySelector("#snap-back").onclick = () => {
  heatSnapView.hidden = true;
  heatLiveView.hidden = false;
  loadHeat();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
