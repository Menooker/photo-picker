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

// ---------- 分类 ----------
async function startClassify(e) {
  e.preventDefault();
  const dir = $("dir-select").value;
  if (!dir) return;
  const startFrom = $("start-from").value.trim() || null;
  const countRaw = $("count").value.trim();
  const count = countRaw ? parseInt(countRaw, 10) : null;

  const btn = $("btn-start");
  btn.disabled = true;
  btn.textContent = "分类中…（请耐心等待）";
  $("setup-error").textContent = "";
  try {
    const data = await fetchJSON("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir, start_from: startFrom, count }),
    });
    state.dir = data.dir;
    state.items = data.items;
    state.labels = new Map(data.items.map((it) => [it.id, it.action]));
    state.page = 0;
    show("view-review");
    renderReview();
  } catch (err) {
    $("setup-error").textContent = `分类失败：${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "开始分类";
  }
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
  $("btn-confirm-exec").disabled = !(groups.del.length || groups.move.length);

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

// ---------- 确认执行（Dummy） ----------
function openConfirmModal() {
  const delIds = state.items.filter((it) => state.labels.get(it.id) === "DELETE").map((i) => i.id);
  const moveIds = state.items.filter((it) => state.labels.get(it.id) === "KEEP_PC").map((i) => i.id);
  $("modal-text").textContent =
    `确定删除 ${delIds.length} 张、移到 PC ${moveIds.length} 张吗？（演示阶段：服务器仅打印日志，不会真的执行）`;
  $("modal").hidden = false;
  $("modal-ok").dataset.del = JSON.stringify(delIds);
  $("modal-ok").dataset.move = JSON.stringify(moveIds);
}

async function execConfirm() {
  try {
    const delIds = JSON.parse($("modal-ok").dataset.del);
    const moveIds = JSON.parse($("modal-ok").dataset.move);
    const data = await fetchJSON("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_ids: delIds, move_ids: moveIds }),
    });
    window.alert(
      `演示操作已记录：待删除 ${data.to_delete} 张、移至 PC ${data.to_move} 张。` +
      "（服务器终端会打印对应日志）"
    );
    $("modal").hidden = true;
    resetToSetup();
  } catch (err) {
    $("confirm-error").textContent = `执行失败：${err.message}`;
  }
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
    const res = await fetch(`/api/photo/${encodeURIComponent(id)}`);
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
  }
});

show("view-setup");
loadDirs();