const $ = (id) => document.getElementById(id);

const state = {
  jobs: [],
  library: [],
  settings: { out_dir: "", quality: "best", proxy: "system", proxy_label: "" },
  playing: null,
};

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function fmtDur(sec) {
  sec = Math.round(Number(sec) || 0);
  if (sec <= 0) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSpeed(n) {
  n = Number(n) || 0;
  if (n <= 0) return "";
  return `${(n / 1048576).toFixed(1)} MB/s`;
}

function preview(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}

function applyState(payload) {
  if (payload.settings) state.settings = payload.settings;
  if (payload.jobs) state.jobs = payload.jobs;
  if (payload.library) state.library = payload.library;
  render();
}

function upsertJob(job) {
  const i = state.jobs.findIndex((j) => j.id === job.id);
  if (i >= 0) state.jobs[i] = job;
  else state.jobs.unshift(job);
  renderJobs();
}

function render() {
  $("outDir").textContent = state.settings.out_dir || "—";
  $("outDir").title = state.settings.out_dir || "";
  if (state.settings.quality) $("quality").value = state.settings.quality;
  if (document.activeElement !== $("proxy")) {
    $("proxy").value = state.settings.proxy || "system";
  }
  if (!$("proxyStatus").dataset.locked) {
    $("proxyStatus").textContent = `当前：${state.settings.proxy_label || "系统环境"}`;
    $("proxyStatus").classList.remove("ok", "bad");
  }
  renderJobs();
  renderLibrary();
}

function renderJobs() {
  const box = $("jobs");
  const empty = $("jobsEmpty");
  box.innerHTML = "";
  empty.hidden = state.jobs.length > 0;
  for (const job of state.jobs) {
    const el = document.createElement("article");
    el.className = "job";
    const title = job.author ? `@${job.author}` : job.source;
    const sub = preview(job.text) || job.filename || job.source;
    const canCancel = ["queued", "resolving", "downloading"].includes(job.status);
    const canPlay = ["done", "skipped"].includes(job.status) && job.filename;
    el.innerHTML = `
      <div class="job-top">
        <div>
          <div class="job-title">${escapeHtml(title)}</div>
          <div class="job-sub">${escapeHtml(sub)}</div>
        </div>
        <span class="badge ${job.status}">${escapeHtml(job.stage || job.status)}</span>
      </div>
      <div class="bar"><span style="width:${job.percent || 0}%"></span></div>
      <div class="job-foot">
        <span>${jobFoot(job)}</span>
        <div class="job-actions">
          ${canPlay ? `<button class="ghost" data-play="${escapeAttr(job.filename)}">播放</button>` : ""}
          ${canCancel ? `<button class="ghost" data-cancel="${job.id}">取消</button>` : ""}
        </div>
      </div>
    `;
    box.appendChild(el);
  }
}

function jobFoot(job) {
  if (job.error) return job.error;
  const bits = [];
  if (job.total) bits.push(`${fmtBytes(job.downloaded)} / ${fmtBytes(job.total)}`);
  else if (job.downloaded) bits.push(fmtBytes(job.downloaded));
  if (job.speed) bits.push(fmtSpeed(job.speed));
  if (job.eta && job.status === "downloading") bits.push(`剩余 ${fmtDur(job.eta)}`);
  if (job.quality) bits.push(job.quality);
  return bits.join("  ·  ") || "等待解析";
}

function renderLibrary() {
  const q = ($("search").value || "").trim().toLowerCase();
  const items = state.library.filter((item) => {
    if (!q) return true;
    const hay = `${item.author} ${item.text} ${item.tweet_id} ${item.filename}`.toLowerCase();
    return hay.includes(q);
  });
  const box = $("library");
  const empty = $("libEmpty");
  box.innerHTML = "";
  empty.hidden = items.length > 0;
  empty.textContent = state.library.length
    ? "没有匹配的视频。"
    : "这里会列出下过的视频，点封面就能播放。";
  for (const item of items) {
    const el = document.createElement("article");
    el.className = "card";
    const thumb = item.thumb
      ? `style="background-image:url('${escapeAttr(item.thumb)}')"`
      : "";
    el.innerHTML = `
      <div class="thumb" ${thumb} data-play="${escapeAttr(item.filename)}">
        <span class="play-dot"></span>
        <span class="chip">${escapeHtml(fmtDur(item.duration) || item.quality || "视频")}</span>
      </div>
      <div class="card-body">
        <strong>@${escapeHtml(item.author || "unknown")}</strong>
        <p>${escapeHtml(preview(item.text) || item.filename)}</p>
      </div>
      <div class="card-foot">
        <button class="ghost" data-play="${escapeAttr(item.filename)}">播放</button>
        <button class="ghost" data-open="${escapeAttr(item.filename)}">访达</button>
        <button class="ghost" data-del="${escapeAttr(item.filename)}">删除</button>
      </div>
    `;
    box.appendChild(el);
  }
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function findLibrary(filename) {
  return state.library.find((x) => x.filename === filename) || state.jobs.find((x) => x.filename === filename);
}

function openPlayer(filename) {
  const item = findLibrary(filename) || { filename };
  state.playing = item;
  const player = $("player");
  const video = $("video");
  $("playerTitle").textContent = item.author ? `@${item.author}` : filename;
  $("playerSub").textContent = [fmtDur(item.duration), item.quality, fmtBytes(item.size), preview(item.text)]
    .filter(Boolean)
    .join("  ·  ");
  video.src = `/media/${encodeURIComponent(filename)}`;
  player.hidden = false;
  player.classList.remove("hidden");
  video.play().catch(() => {});
}

function closePlayer() {
  const player = $("player");
  const video = $("video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  player.hidden = true;
  player.classList.add("hidden");
  state.playing = null;
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "hello" && msg.state) applyState(msg.state);
    if (msg.type === "job" && msg.job) upsertJob(msg.job);
    if (msg.type === "library" || msg.type === "jobs-cleared") {
      api("/api/state").then(applyState).catch(() => {});
    }
  };
}

$("download").addEventListener("click", async () => {
  const text = $("urls").value.trim();
  $("composerHint").textContent = "";
  if (!text) {
    $("composerHint").textContent = "先贴一条推文、archives 或 eve568 播放链接。";
    return;
  }
  $("download").disabled = true;
  try {
    await api("/api/download", { text, quality: $("quality").value });
    $("urls").value = "";
  } catch (err) {
    $("composerHint").textContent = err.message;
  } finally {
    $("download").disabled = false;
  }
});

$("urls").addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") $("download").click();
});

$("quality").addEventListener("change", () => {
  api("/api/settings", { quality: $("quality").value }).catch(() => {});
});

async function saveProxy() {
  $("composerHint").textContent = "";
  $("proxyStatus").dataset.locked = "";
  try {
    const data = await api("/api/settings", { proxy: $("proxy").value.trim() || "system" });
    if (data.settings) state.settings = data.settings;
    $("proxy").value = state.settings.proxy || "system";
    $("proxyStatus").textContent = `当前：${state.settings.proxy_label || "系统环境"}`;
    $("proxyStatus").classList.remove("ok", "bad");
  } catch (err) {
    $("composerHint").textContent = err.message;
  }
}

$("proxyApply").addEventListener("click", saveProxy);
$("proxy").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    saveProxy();
  }
});
$("proxyTest").addEventListener("click", async () => {
  $("composerHint").textContent = "";
  $("proxyTest").disabled = true;
  $("proxyStatus").textContent = "正在测试代理…";
  $("proxyStatus").classList.remove("ok", "bad");
  $("proxyStatus").dataset.locked = "1";
  try {
    const data = await api("/api/proxy-test", { proxy: $("proxy").value.trim() || "system" });
    if (data.settings) state.settings = data.settings;
    $("proxy").value = state.settings.proxy || "system";
    const test = data.test || {};
    if (test.ok) {
      const ip = test.ip ? ` · IP ${test.ip}` : "";
      $("proxyStatus").textContent = `连通 ${test.via}${ip} · ${test.elapsed}s`;
      $("proxyStatus").classList.add("ok");
    } else {
      $("proxyStatus").textContent = `失败 ${test.via || ""}：${test.error || "无法连接"}`;
      $("proxyStatus").classList.add("bad");
    }
  } catch (err) {
    $("proxyStatus").textContent = err.message;
    $("proxyStatus").classList.add("bad");
  } finally {
    $("proxyTest").disabled = false;
    setTimeout(() => {
      $("proxyStatus").dataset.locked = "";
    }, 8000);
  }
});

$("search").addEventListener("input", renderLibrary);
$("clearDone").addEventListener("click", () => api("/api/clear-done").catch(() => {}));
$("openDir").addEventListener("click", () => api("/api/open", { folder: true }).catch(() => {}));
$("pickDir").addEventListener("click", async () => {
  const data = await api("/api/pick-dir").catch(() => null);
  if (data && data.out_dir) {
    state.settings.out_dir = data.out_dir;
    const fresh = await api("/api/state");
    applyState(fresh);
  }
});

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-play],[data-cancel],[data-open],[data-del],[data-close]");
  if (!t) return;
  if (t.dataset.close) return closePlayer();
  if (t.dataset.play) return openPlayer(t.dataset.play);
  if (t.dataset.cancel) return api("/api/cancel", { id: t.dataset.cancel }).catch(() => {});
  if (t.dataset.open) return api("/api/open", { filename: t.dataset.open }).catch(() => {});
  if (t.dataset.del) {
    if (!confirm(`删除 ${t.dataset.del}？`)) return;
    await api("/api/delete", { filename: t.dataset.del }).catch((err) => alert(err.message));
    if (state.playing && state.playing.filename === t.dataset.del) closePlayer();
  }
});

$("playerClose").addEventListener("click", closePlayer);
$("playerReveal").addEventListener("click", () => {
  if (state.playing && state.playing.filename) {
    api("/api/open", { filename: state.playing.filename }).catch(() => {});
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closePlayer();
});

api("/api/state").then(applyState).catch((err) => {
  $("composerHint").textContent = err.message;
});
connectEvents();
