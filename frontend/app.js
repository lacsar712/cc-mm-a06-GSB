const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const viewReadings = document.querySelector("#view-readings");
const viewHeat = document.querySelector("#view-heat");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");

const isWriter = () => role === "writer";

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
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

function showApp() {
  loginBox.hidden = true;
  document.querySelector("#nav").hidden = false;
  document.querySelector("#who").textContent = isWriter() ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = !isWriter();
  applyRoleToHeat();
  switchTab("readings");
  connect();
  load();
}

/* ---------- 顶栏切换 ---------- */

function switchTab(name) {
  const onHeat = name === "heat";
  viewReadings.hidden = onHeat;
  viewHeat.hidden = !onHeat;
  document.querySelector("#tab-readings").classList.toggle("active", !onHeat);
  document.querySelector("#tab-heat").classList.toggle("active", onHeat);
  if (onHeat) {
    showLiveHeat();
    loadHeat();
    loadSnapshots();
  }
}

document.querySelector("#tab-readings").onclick = () => switchTab("readings");
document.querySelector("#tab-heat").onclick = () => switchTab("heat");

/* ---------- 巷道热力 ---------- */

const prefixBox = document.querySelector("#prefix-box");
const prefixInput = document.querySelector("#prefix-len");
const heatMsg = document.querySelector("#heat-msg");
const heatRows = document.querySelector("#heat-rows");
const heatLive = document.querySelector("#heat-live");
const heatSnapshot = document.querySelector("#heat-snapshot");

function heatRowClass(alarm) {
  if (alarm >= 2) return "heat-high";
  if (alarm === 1) return "heat-mid";
  return "heat-zero";
}

function paintHeatGroups(tbody, groups) {
  if (!groups.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">暂无记录</td></tr>`;
    return;
  }
  tbody.innerHTML = groups
    .map(
      (g) =>
        `<tr class="${heatRowClass(g.alarm)}"><td>${g.prefix}</td><td class="alarm">${g.alarm}</td><td class="ok">${g.ok}</td><td>${g.row_ids.join("、")}</td></tr>`,
    )
    .join("");
}

function applyRoleToHeat() {
  // 旁观账号：不能改前缀、不能签发，只能看
  prefixBox.querySelectorAll("input, button").forEach((el) => {
    el.disabled = !isWriter();
  });
}

async function loadHeat() {
  const data = await api("/api/heat/table");
  prefixInput.value = data.prefix_len;
  heatMsg.textContent = `当前前缀长度 ${data.prefix_len} 字`;
  paintHeatGroups(heatRows, data.groups);
}

document.querySelector("#save-prefix").onclick = async () => {
  try {
    const value = Number(prefixInput.value);
    await api("/api/heat/prefix-len", {
      method: "PUT",
      body: JSON.stringify({ prefix_len: value }),
    });
    heatMsg.textContent = `前缀长度已改为 ${value} 字，只影响现场表，旧快照不动`;
    await loadHeat();
  } catch (err) {
    heatMsg.textContent = err.message;
  }
};

document.querySelector("#issue-snapshot").onclick = async () => {
  try {
    const snap = await api("/api/heat/snapshots", { method: "POST" });
    heatMsg.textContent = `已签发快照 #${snap.id}，冻结前缀长度 ${snap.prefix_len} 字`;
    await loadSnapshots();
    openSnapshot(snap.id);
  } catch (err) {
    heatMsg.textContent = err.message;
  }
};

function showLiveHeat() {
  heatLive.hidden = false;
  heatSnapshot.hidden = true;
}

document.querySelector("#snap-back").onclick = () => {
  showLiveHeat();
  loadHeat();
};

async function openSnapshot(id) {
  const snap = await api(`/api/heat/snapshots/${id}`);
  heatLive.hidden = true;
  heatSnapshot.hidden = false;
  document.querySelector("#snap-title").textContent = `#${snap.id}`;
  document.querySelector("#snap-meta").textContent =
    `冻结前缀长度 ${snap.prefix_len} 字 · 签发人 ${snap.created_by} · ${snap.created_at}`;
  paintHeatGroups(document.querySelector("#snap-rows"), snap.groups);
}

async function loadSnapshots() {
  const list = await api("/api/heat/snapshots");
  const tbody = document.querySelector("#snap-list");
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">还没有快照</td></tr>`;
    return;
  }
  tbody.innerHTML = list
    .map(
      (s) =>
        `<tr><td>${s.id}</td><td>${s.prefix_len}</td><td>${s.group_count}</td><td>${s.created_by}</td><td>${s.created_at}</td><td><span class="snaplink" data-id="${s.id}">打开</span></td></tr>`,
    )
    .join("");
  tbody.querySelectorAll(".snaplink").forEach((el) => {
    el.onclick = () => openSnapshot(Number(el.dataset.id));
  });
}

/* ---------- 班测记录 ---------- */

async function load() {
  paint(await api("/api/readings"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
    if (!viewHeat.hidden && heatLive.hidden === false) loadHeat();
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

applyRoleToHeat();
if (token) showApp();
