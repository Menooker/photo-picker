// Photo Picker 前端（无框架，原生 JS）

const PER_PAGE = 15;

const LABELS = [
  { value: "KEEP_PHONE", key: "保留手机", cls: "keep-phone" },
  { value: "KEEP_PC", key: "移到PC", cls: "keep-pc" },
  { value: "DELETE", key: "删除", cls: "del" },
  { value: "UNDECIDED", key: "待定", cls: "undecided" },
];

const state = {
  dir: "",
  items: [],            // [{id, taken_date, width, height, location, action, confidence, reason}]
  labels: new Map(),    // id -> 当前标签（可修改）
  page: 0,
  dest: localStorage.getItem("picker.dest") || "",  // 输出目录（持久化）
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function labelInfo(value) {
  return LABELS.find((l) => l.value === value) || LABELS[LABELS.length - 1];
}

function show(view) {
  ["view-setup", "view-review", "view-confirm"].forEach((v) => ($(v).hidden = v !== view));
}

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ---------- API token 消耗统计 ----------
const fmtK = (n) => (n / 1000).toFixed(2) + "k";

function renderUsage(usage) {
  if (!usage) return;
  $("usage-stats").textContent =
    `缓存 ${fmtK(usage.input_cached)} · 输入 ${fmtK(usage.input_uncached)} · 输出 ${fmtK(usage.output)}`;
}

async function loadUsage() {
  try { renderUsage(await fetchJSON("/api/usage")); } catch (_) { /* 服务器未就绪时忽略 */ }
}

// ---------- 初始化：加载目录列表 ----------
async function loadDirs() {
  const sel = $("dir-select");
  try {
    const data = await fetchJSON("/api/dirs");
    sel.innerHTML = "";
    for (const d of data.dirs) {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      sel.appendChild(opt);
    }
  } catch (err) {
    sel.innerHTML = '<option value="">加载失败</option>';
    $("setup-error").textContent = `无法连接服务器或读取目录：${err.message}`;
  }
}

// ---------- 分类（后台任务 + 轮询进度） ----------
async function startClassify(e) {
  e.preventDefault();
  const dir = $("dir-select").value;
  if (!dir) return;
  const startFrom = $("start-from").value.trim() || null;
  const countRaw = $("count").value.trim();
  const count = countRaw ? parseInt(countRaw, 10) : null;

  const btn = $("btn-start");
  btn.disabled = true;
  btn.textContent = "分类中…";
  $("setup-error").textContent = "";
  $("progress-wrap").hidden = false;
  $("progress-fill").style.width = "0%";
  $("progress-text").textContent = "等待 LLM 批次…";
  try {
    await fetchJSON("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir, start_from: startFrom, count }),
    });
    await pollStatus();
  } catch (err) {
    $("setup-error").textContent = `分类失败：${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "开始分类";
  }
}

function pollStatus() {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      let st;
      try {
        st = await fetchJSON("/api/status");
      } catch (err) {
        clearInterval(timer);
        reject(err);
        return;
      }
      renderUsage(st.usage);

      const total = st.total || 0;
      if (!st.running) {
        clearInterval(timer);
        $("progress-wrap").hidden = true;
        if (st.error) {
          reject(new Error("服务器端分类失败（详见终端日志）"));
          return;
        }
        if (!st.items.length) {
          $("setup-error").textContent = "该目录没有照片。";
          resolve();
          return;
        }
        $("progress-fill").style.width = "100%";
        state.dir = st.dir;
        state.items = st.items;
        state.labels = new Map(st.items.map((it) => [it.id, it.action]));
        state.page = 0;
        show("view-review");
        renderReview();
        resolve();
        return;
      }
      // 进行中：更新 LLM 批次进度
      if (total > 0) {
        const pct = Math.round((st.done / total) * 100);
        $("progress-fill").style.width = pct + "%";
        $("progress-text").textContent = `LLM 批次：${st.done} / ${total}`;
      }
    }, 500);
  });
}

// ---------- 缩略图加载 ----------
async function fetchThumbs(ids) {
  const unique = [...new Set(ids)];
  const qs = unique.map((id) => "ids=" + encodeURIComponent(id)).join("&");
  return (await fetchJSON("/api/thumbs?" + qs)).thumbs;
}

// ---------- 审查视图 ----------
function pageItems() {
  const start = state.page * PER_PAGE;
  return state.items.slice(start, start + PER_PAGE);
}

async function renderReview() {
  const total = state.items.length;
  const pages = Math.max(1, Math.ceil(total / PER_PAGE));
  if (state.page >= pages) state.page = pages - 1;

  $("review-title").textContent = state.dir;
  $("review-sub").textContent = `共 ${total} 张照片 · 第 ${state.page + 1} / ${pages} 页`;

  const chunk = pageItems();
  $("review-grid").innerHTML = "";
  if (!chunk.length) {
    $("review-grid").innerHTML = '<p class="muted">没有照片。</p>';
    return;
  }

  const thumbs = await fetchThumbs(chunk.map((it) => it.id));
  for (const it of chunk) {
    const cell = document.createElement("figure");
    cell.className = "cell";
    cell.dataset.id = it.id;

    const img = document.createElement("img");
    img.alt = it.id;
    img.loading = "lazy";
    if (thumbs[it.id]) img.src = "data:image/jpeg;base64," + thumbs[it.id];

    const info = labelInfo(state.labels.get(it.id));

    const fig = document.createElement("figcaption");
    fig.innerHTML =
      `<div class="cap-top">
         <span class="badge ${info.cls}">${info.key}</span>
         <span class="cap-id" title="${escapeHtml(it.id)}">${escapeHtml(it.id)}</span>
       </div>
       <div class="cap-sub">${escapeHtml(it.taken_date)}</div>
       <select class="label-select" data-id="${escapeHtml(it.id)}">
         ${LABELS.map((l) =>
           `<option value="${l.value}" ${l.value === state.labels.get(it.id) ? "selected" : ""}>${l.key}</option>`
         ).join("")}
       </select>`;
    if (it.reason) {
      fig.title = `置信度 ${(it.confidence * 100).toFixed(0)}%\n${it.reason}`;
    }
    cell.appendChild(img);
    cell.appendChild(fig);

    img.addEventListener("click", () => openLightbox(it.id));
    cell.querySelector(".label-select").addEventListener("change", (ev) => {
      state.labels.set(ev.target.dataset.id, ev.target.value);
      const info2 = labelInfo(ev.target.value);
      fig.querySelector(".badge").className = "badge " + info2.cls;
      fig.querySelector(".badge").textContent = info2.key;
    });
    $("review-grid").appendChild(cell);
  }

  $("page-info").textContent = `${state.page + 1} / ${pages}`;
  $("prev-page").disabled = state.page === 0;
  $("next-page").disabled = state.page >= pages - 1;
}

function goPage(delta) {
  const pages = Math.max(1, Math.ceil(state.items.length / PER_PAGE));
  state.page = Math.min(pages - 1, Math.max(0, state.page + delta));
  renderReview();
}

// ---------- 确认视图 ----------
function goConfirm() {
  $("dest-path").value = state.dest;
  updateDestState();
  $("transfer-wrap").hidden = true;
  $("confirm-error").textContent = "";
  const del = state.items.filter((it) => state.labels.get(it.id) === "DELETE");
  const move = state.items.filter((it) => state.labels.get(it.id) === "KEEP_PC");
  if (!del.length && !move.length) {
    $("confirm-title").textContent = "确认执行";
    $("del-count").textContent = "0";
    $("move-count").textContent = "0";
    $("confirm-del").innerHTML = "";
    $("confirm-move").innerHTML = "";
    $("del-empty").textContent = "无";
    $("move-empty").textContent = "无";
    $("btn-confirm-exec").disabled = true;
  }
  $("confirm-title").textContent = `确认执行 · ${state.dir}`;
  show("view-confirm");
  renderConfirm();
}

function renderConfirm() {
  const groups = {
    del: state.items.filter((it) => state.labels.get(it.id) === "DELETE"),
    move: state.items.filter((it) => state.labels.get(it.id) === "KEEP_PC"),
  };

  $("del-count").textContent = groups.del.length;
  $("move-count").textContent = groups.move.length;
  $("del-empty").hidden = groups.del.length > 0;
  $("move-empty").hidden = groups.move.length > 0;
  $("btn-confirm-exec").disabled =
    !(groups.del.length || groups.move.length) || !state.dest;

  const bothIds = [...groups.del.map((i) => i.id), ...groups.move.map((i) => i.id)];
  fetchThumbs(bothIds).then((thumbs) => {
    renderConfirmGroup($("confirm-del"), groups.del, thumbs);
    renderConfirmGroup($("confirm-move"), groups.move, thumbs);
  });
}

function renderConfirmGroup(container, list, thumbs) {
  container.innerHTML = "";
  const indexById = new Map(state.items.map((it, i) => [it.id, i]));
  const fragment = document.createDocumentFragment();
  for (const it of list) {
    const figure = document.createElement("figure");
    figure.className = "cell small";
    const img = document.createElement("img");
    img.alt = it.id;
    img.title = it.id;
    if (thumbs[it.id]) img.src = "data:image/jpeg;base64," + thumbs[it.id];
    const cap = document.createElement("figcaption");
    cap.textContent = it.id;
    figure.appendChild(img);
    figure.appendChild(cap);
    figure.addEventListener("click", () => {
      // 回到该照片所在的审查页，并回到审查状态
      const idx = indexById.get(it.id) ?? 0;
      state.page = Math.floor(idx / PER_PAGE);
      show("view-review");
      renderReview();
    });
    fragment.appendChild(figure);
  }
  container.appendChild(fragment);
}

// ---------- 输出目录：路径输入 + 目录浏览 ----------
function updateDestState() {
  if (state.dest) {
    $("confirm-error").textContent = "";
  }
}

function onDestInput() {
  state.dest = $("dest-path").value.trim();
  localStorage.setItem("picker.dest", state.dest);
  updateDestState();
  if (!$("view-confirm").hidden) renderConfirm();
}

let browseParent = "";

async function loadBrowse(path) {
  $("browse-error").textContent = "";
  $("browse-list").innerHTML = "";
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  let d;
  try {
    d = await fetchJSON("/api/browse" + q);
  } catch (err) {
    $("browse-error").textContent = `无法浏览：${err.message}`;
    return;
  }
  browseParent = d.parent;
  $("browse-path").textContent = d.path;
  $("browse-up").disabled = !d.parent;
  const list = $("browse-list");
  for (const name of d.dirs) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "browse-item";
    row.textContent = "📁 " + name;
    row.addEventListener("click", () => {
      const sep = d.path.endsWith(d.sep) ? "" : d.sep;
      loadBrowse(d.path + sep + name);
    });
    list.appendChild(row);
  }
  if (!d.dirs.length) {
    list.innerHTML = '<p class="muted">（没有子文件夹）</p>';
  }
}

function openDirBrowser() {
  $("dir-browser").hidden = false;
  $("browse-error").textContent = "";
  loadBrowse(state.dest);
}

function confirmBrowsePath() {
  const path = $("browse-path").textContent;
  if (!path) return;
  state.dest = path;
  localStorage.setItem("picker.dest", path);
  $("dest-path").value = path;
  $("dir-browser").hidden = true;
  updateDestState();
  renderConfirm();
}

// ---------- 确认执行（复制→删除，后台任务 + 轮询） ----------
function openConfirmModal() {
  const delIds = state.items.filter((it) => state.labels.get(it.id) === "DELETE").map((i) => i.id);
  const moveIds = state.items.filter((it) => state.labels.get(it.id) === "KEEP_PC").map((i) => i.id);
  $("modal-text").textContent =
    `确定删除 ${delIds.length} 张（复制到 recycle 后删除原片）、` +
    `移到 PC ${moveIds.length} 张（复制到 moved）吗？\n输出目录：${state.dest || "（未填写）"}`;
  $("modal").hidden = false;
  $("modal-ok").dataset.del = JSON.stringify(delIds);
  $("modal-ok").dataset.move = JSON.stringify(moveIds);
}

async function execConfirm() {
  const delIds = JSON.parse($("modal-ok").dataset.del);
  const moveIds = JSON.parse($("modal-ok").dataset.move);
  if (!state.dest) {
    $("confirm-error").textContent = "请先填写输出目录。";
    return;
  }
  const btn = $("btn-confirm-exec");
  btn.disabled = true;
  $("confirm-error").textContent = "";
  $("transfer-wrap").hidden = false;
  $("transfer-fill").style.width = "0%";
  $("transfer-text").textContent = "连接服务器…";
  try {
    const data = await fetchJSON("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        iphone_dir: state.dir,
        delete_ids: delIds,
        move_ids: moveIds,
        dest: state.dest,
      }),
    });
    await pollTransfer(data.to_delete + data.to_move);
    $("modal").hidden = true;
    window.alert(
      `完成：删除 ${data.to_delete} 张（写入 recycle）、转移到 PC ${data.to_move} 张` +
      `（写入 moved）。\n输出目录：${state.dest}`
    );
    resetToSetup();
  } catch (err) {
    $("transfer-wrap").hidden = true;
    btn.disabled = false;
    $("confirm-error").textContent = `执行失败：${err.message}`;
  }
}

function pollTransfer(total) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      let t;
      try {
        t = await fetchJSON("/api/transfer");
      } catch (err) {
        clearInterval(timer);
        reject(err);
        return;
      }
      const pct = t.total ? Math.round((t.done / t.total) * 100) : 100;
      $("transfer-fill").style.width = pct + "%";
      const phase = t.phase === "recycle" ? "删除" :
        t.phase === "moved" ? "转移到 PC" : "处理";
      $("transfer-text").textContent =
        `${phase}… ${t.done} / ${t.total}${t.current ? "（" + t.current + "）" : ""}`;
      if (!t.running) {
        clearInterval(timer);
        if (t.error) {
          reject(new Error("转移过程中出错（详见服务器日志）"));
          return;
        }
        resolve();
      }
    }, 500);
  });
}

function resetToSetup() {
  state.items = [];
  state.labels = new Map();
  state.page = 0;
  state.dir = "";
  $("start-from").value = "";
  $("count").value = "";
  $("confirm-error").textContent = "";
  show("view-setup");
  loadDirs();
}

// ---------- 点击放大 ----------
async function openLightbox(id) {
  const item = state.items.find((it) => it.id === id);
  if (item) {
    const info = labelInfo(state.labels.get(id));
    $("lightbox-title").textContent = item.id;
    $("lb-date").textContent = item.taken_date || "未知";
    $("lb-loc").textContent = item.location || "未知";
    const badge = $("lb-badge");
    badge.className = "badge " + info.cls;
    badge.textContent = info.key;
    $("lb-conf").textContent =
      item.confidence ? `置信度 ${(item.confidence * 100).toFixed(0)}%` : "";
    $("lb-reason").textContent = item.reason || "（无）";
  }
  const lbox = $("lightbox");
  lbox.hidden = false;
  $("lightbox-img").alt = id;
  $("lightbox-img").src = "";
  try {
    const res = await fetch(
      `/api/photo/${encodeURIComponent(id)}?iphone_dir=${encodeURIComponent(state.dir)}`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    $("lightbox-img").src = URL.createObjectURL(await res.blob());
  } catch (err) {
    $("lightbox-img").alt = `加载失败：${err.message}`;
  }
}

function closeLightbox() {
  const img = $("lightbox-img");
  if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
  img.src = "";
  $("lightbox").hidden = true;
}

// ---------- 事件绑定 ----------
$("setup-form").addEventListener("submit", startClassify);
$("prev-page").addEventListener("click", () => goPage(-1));
$("next-page").addEventListener("click", () => goPage(1));
$("btn-restart").addEventListener("click", () => show("view-setup"));
$("btn-review-done").addEventListener("click", goConfirm);
$("btn-back-review").addEventListener("click", () => {
  show("view-review");
  renderReview();
});
$("btn-confirm-exec").addEventListener("click", openConfirmModal);
$("dest-path").addEventListener("input", onDestInput);
$("btn-browse").addEventListener("click", openDirBrowser);
$("browse-up").addEventListener("click", () => browseParent && loadBrowse(browseParent));
$("browse-home").addEventListener("click", () => loadBrowse(""));
$("browse-cancel").addEventListener("click", () => ($("dir-browser").hidden = true));
$("browse-confirm").addEventListener("click", confirmBrowsePath);
$("dir-browser").addEventListener("click", (e) => {
  if (e.target === $("dir-browser")) $("dir-browser").hidden = true;
});
$("modal-ok").addEventListener("click", execConfirm);
$("modal-cancel").addEventListener("click", () => ($("modal").hidden = true));
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) $("modal").hidden = true;
});
$("lightbox-close").addEventListener("click", closeLightbox);
$("lightbox").addEventListener("click", (e) => {
  if (e.target === $("lightbox")) closeLightbox();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeLightbox();
    $("modal").hidden = true;
    $("dir-browser").hidden = true;
  }
});

show("view-setup");
$("dest-path").value = state.dest;
loadDirs();
loadUsage();
