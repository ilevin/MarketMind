/* 原生 JS：自选管理页（watchlist）+ 行情首页（index）+ 标签管理页（tags）。
 * 页面通过 body[data-page] 区分；v0.03 增加标签管理与行情页标签筛选（本地过滤）。 */

"use strict";

// ---------- 通用 ----------

async function api(path, options) {
  const resp = await fetch(path, options);
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${resp.status}`;
    throw new Error(detail);
  }
  return data;
}

function showMsg(el, text, kind) {
  el.textContent = text;
  el.className = `message ${kind}`;
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

const MARKET_LABEL = { CN: "A股", HK: "港股" };
const TYPE_LABEL = { STOCK: "股票", ETF: "ETF", INDEX: "指数" };

// ---------- 自选管理页 ----------

function initWatchlistPage() {
  const wlForm = document.getElementById("add-watchlist-form");
  const wlMsg = document.getElementById("wl-message");
  const idxForm = document.getElementById("add-index-form");
  const idxMsg = document.getElementById("idx-message");
  if (!wlForm) return;

  async function loadList(kind) {
    // kind: "wl" | "idx"
    const data = await api(kind === "wl" ? "/api/watchlist" : "/api/index-watchlist");
    const tbody = document.querySelector(`#${kind}-table tbody`);
    const empty = document.getElementById(`${kind}-empty`);
    tbody.innerHTML = "";
    empty.classList.toggle("hidden", data.items.length > 0);
    document.getElementById(`${kind}-table`).classList.toggle("hidden", data.items.length === 0);

    data.items.forEach((item, i) => {
      // 标签列：只读展示已关联标签（编辑入口在操作列「标签」按钮）
      const tagCell =
        kind === "wl"
          ? `<td class="tag-cell">${(item.tags || [])
              .map((t) => `<span class="chip on readonly" data-tid="${t.id}">${esc(t.name)}</span>`)
              .join("") || '<span class="muted">-</span>'}</td>`
          : "";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(item.name)}</td>
        <td>${esc(item.symbol)}</td>
        <td>${MARKET_LABEL[item.market] || item.market}${kind === "wl" && item.market === "HK" && item.delayed ? " · 延时" : ""}</td>
        ${kind === "wl" ? `<td>${TYPE_LABEL[item.asset_type] || item.asset_type}</td>` : ""}
        ${tagCell}
        <td class="right">${item.sort_order}</td>
        <td>
          <button class="link" data-act="up" data-iid="${esc(item.instrument_id)}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button class="link" data-act="down" data-iid="${esc(item.instrument_id)}" ${i === data.items.length - 1 ? "disabled" : ""}>↓</button>
          ${kind === "wl" ? `<button class="link" data-act="tags" data-iid="${esc(item.instrument_id)}">标签</button>` : ""}
          <button class="link danger" data-act="del" data-iid="${esc(item.instrument_id)}">删除</button>
        </td>`;
      tbody.appendChild(tr);
    });
  }

  async function refreshBoth() {
    await Promise.all([loadList("wl"), loadList("idx")]);
  }

  async function reorderFromCurrent(kind) {
    // 用当前渲染顺序重算 sort_order（步长 10）
    const rows = Array.from(document.querySelectorAll(`#${kind}-table tbody tr`));
    const ids = rows.map((r) => r.querySelector("button[data-act=del]").dataset.iid);
    const items = ids.map((iid, i) => ({ instrument_id: iid, sort_order: (i + 1) * 10 }));
    await api(kind === "wl" ? "/api/watchlist/order" : "/api/index-watchlist/order", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
  }

  // 标签编辑弹层：操作列「标签」按钮打开，层内点击标签即添加/取消关联（即时保存）
  const tagModal = document.getElementById("tag-edit-modal");
  let editingIid = null;

  async function openTagEditor(iid, name, currentTagIds) {
    editingIid = iid;
    let selected = [...currentTagIds];
    document.getElementById("tag-edit-title").textContent = `编辑标签：${name}`;

    const box = document.getElementById("tag-edit-chips");
    box.innerHTML = '<span class="muted">加载中…</span>';
    tagModal.classList.remove("hidden");

    let tags = [];
    try {
      tags = (await api("/api/tags")).items;
    } catch (err) {
      box.innerHTML = `<span class="muted">标签加载失败：${esc(err.message)}</span>`;
      return;
    }
    box.innerHTML = "";
    if (tags.length === 0) {
      box.innerHTML = '<span class="muted">还没有标签，请先在标签管理页创建。</span>';
      return;
    }
    tags.forEach((t) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (selected.includes(t.id) ? " on" : "");
      chip.textContent = t.name;
      chip.addEventListener("click", async () => {
        const next = selected.includes(t.id)
          ? selected.filter((x) => x !== t.id)
          : [...selected, t.id];
        try {
          await api(`/api/watchlist/${encodeURIComponent(editingIid)}/tags`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tag_ids: next }),
          });
          selected = next;
          chip.classList.toggle("on");
          showMsg(wlMsg, "标签已更新", "success");
        } catch (err) {
          showMsg(wlMsg, `标签更新失败：${err.message}`, "error");
        }
      });
      box.appendChild(chip);
    });
  }

  function closeTagEditor() {
    tagModal.classList.add("hidden");
    if (editingIid) {
      editingIid = null;
      refreshBoth().catch(() => {}); // 同步行内标签列显示
    }
  }

  document.getElementById("tag-edit-close").addEventListener("click", closeTagEditor);
  tagModal.addEventListener("click", (ev) => {
    if (ev.target === tagModal) closeTagEditor(); // 点击遮罩关闭
  });

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const iid = btn.dataset.iid;
    const kind = iid.includes(":INDEX:") ? "idx" : "wl";
    const act = btn.dataset.act;
    if (act === "tags") {
      const row = btn.closest("tr");
      const name = row.querySelector("td").textContent.trim();
      const tids = Array.from(row.querySelectorAll(".tag-cell .chip[data-tid]")).map((c) =>
        Number(c.dataset.tid)
      );
      openTagEditor(iid, name, tids);
      return;
    }
    try {
      if (act === "del") {
        await api(`/${kind === "wl" ? "api/watchlist" : "api/index-watchlist"}/${encodeURIComponent(iid)}`, { method: "DELETE" });
      } else if (act === "up" || act === "down") {
        const table = document.getElementById(`${kind}-table`);
        const row = btn.closest("tr");
        if (act === "up") table.tBodies[0].insertBefore(row, row.previousSibling);
        else row.nextSibling.insertAdjacentElement("afterend", row);
        await reorderFromCurrent(kind);
      }
      await refreshBoth();
    } catch (err) {
      showMsg(kind === "wl" ? wlMsg : idxMsg, err.message, "error");
    }
  });

  wlForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: document.getElementById("wl-symbol").value.trim(),
          market: document.getElementById("wl-market").value,
          asset_type: document.getElementById("wl-type").value,
        }),
      });
      showMsg(wlMsg, "添加成功", "success");
      document.getElementById("wl-symbol").value = "";
      await refreshBoth();
    } catch (err) {
      showMsg(wlMsg, `添加失败：${err.message}`, "error");
    }
  });

  idxForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/index-watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: document.getElementById("idx-symbol").value.trim(),
          market: document.getElementById("idx-market").value,
          asset_type: "INDEX",
        }),
      });
      showMsg(idxMsg, "添加成功", "success");
      document.getElementById("idx-symbol").value = "";
      await refreshBoth();
    } catch (err) {
      showMsg(idxMsg, `添加失败：${err.message}`, "error");
    }
  });

  refreshBoth().catch((err) => showMsg(wlMsg, err.message, "error"));
}

// ---------- 行情首页 ----------

const STATUS_LABEL = {
  OPEN: "交易中",
  LUNCH_BREAK: "午间休市",
  CLOSED: "已收盘",
  HOLIDAY: "休市",
};
const REFRESH_MS = 60 * 1000;
let pollTimer = null;

function chgClass(value) {
  if (value == null) return "flat";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

function fmtNum(value, digits = 2) {
  return value == null ? "-" : Number(value).toFixed(digits);
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return "-";
  // 统一显示北京时间
  return d.toLocaleTimeString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function renderMarketStatus(status) {
  for (const market of ["CN", "HK"]) {
    const el = document.getElementById(`status-${market}`);
    const st = status[market];
    el.textContent = `${MARKET_LABEL[market]} · ${STATUS_LABEL[st] || st || "--"}`;
    el.className = `market-badge ${st === "OPEN" ? "open" : "closed"}`;
  }
}

function renderIndices(data) {
  const box = document.getElementById("indices");
  const empty = document.getElementById("indices-empty");
  box.innerHTML = "";
  empty.classList.toggle("hidden", data.items.length > 0);
  data.items.forEach((idx) => {
    const card = document.createElement("div");
    card.className = "index-card";
    card.innerHTML = `
      <div class="name">${esc(idx.name)}</div>
      <div class="points">${fmtNum(idx.price, 2)}</div>
      <div class="chg ${chgClass(idx.change_percent)}">${fmtNum(idx.change_percent)}%${idx.is_stale ? '<span class="stale-mark">&#9888;</span>' : ""}</div>`;
    box.appendChild(card);
  });
}

function renderQuotes(data) {
  const tbody = document.querySelector("#quotes-table tbody");
  const empty = document.getElementById("quotes-empty");
  const filterEmpty = document.getElementById("quotes-filter-empty");
  // 标签筛选为前端本地过滤：基于已加载数据渲染，不发起任何行情请求（v0.03 §12.2）
  const items = filterQuotes(data.items);
  tbody.innerHTML = "";
  const hasAny = data.items.length > 0;
  empty.classList.toggle("hidden", hasAny);
  filterEmpty.classList.toggle("hidden", !hasAny || items.length > 0);
  document.getElementById("quotes-table").classList.toggle("hidden", !hasAny);

  items.forEach((q) => {
    const marketText =
      MARKET_LABEL[q.market] || q.market;
    const marketCell =
      q.market === "HK" && q.delayed ? `${marketText} · 延时` : marketText;
    const tagCell = q.tags && q.tags.length ? q.tags.map((t) => esc(t.name)).join("、") : "-";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(q.name)}</td>
      <td class="muted">${esc(q.symbol)}</td>
      <td>${marketCell}</td>
      <td class="muted">${tagCell}</td>
      <td class="right num">${fmtNum(q.price)}</td>
      <td class="right num ${chgClass(q.change_percent)}">${fmtNum(q.change_percent)}%</td>
      <td class="right num">${fmtNum(q.volume_ratio)}</td>
      <td class="right num">${fmtNum(q.pe_ttm)}</td>
      <td class="right num">${fmtNum(q.pb)}</td>
      <td class="right num">${fmtNum(q.dividend_yield_ttm)}</td>
      <td class="right num muted">${fmtTime(q.source_timestamp)}${q.is_stale ? '<span class="stale-mark">&#9888;</span>' : ""}</td>`;
    tbody.appendChild(tr);
  });
}

function showError(text) {
  const el = document.getElementById("error-message");
  el.textContent = text;
  el.classList.remove("hidden");
}

async function fetchAndRender() {
  let quotesData = null;
  let indicesData = null;
  try {
    [quotesData, indicesData] = await Promise.all([
      api("/api/quotes"),
      api("/api/indices"),
    ]);
  } catch (err) {
    showError(`获取行情失败：${err.message}`);
    return null;
  }
  lastQuotesData = quotesData; // 供本地筛选重渲染使用
  document.getElementById("error-message").classList.add("hidden");
  renderMarketStatus(quotesData.market_status);
  renderIndices(indicesData);
  renderQuotes(quotesData);
  return quotesData.market_status;
}

function schedulePolling(marketStatus) {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  // 任一市场 OPEN 时每 60 秒读取一次缓存；全部非 OPEN 停止轮询（保留已显示数据）
  const anyOpen = Object.values(marketStatus || {}).some((s) => s === "OPEN");
  if (anyOpen) {
    pollTimer = setTimeout(async () => {
      const status = await fetchAndRender();
      schedulePolling(status);
    }, REFRESH_MS);
  }
}

// 标签筛选状态："all" | "untagged" | "<tag_id>"；轮询刷新后自动重新应用
let tagFilter = "all";
let lastQuotesData = null;

function filterQuotes(items) {
  if (tagFilter === "all") return items;
  if (tagFilter === "untagged") return items.filter((q) => !q.tags || q.tags.length === 0);
  return items.filter((q) => q.tags && q.tags.some((t) => String(t.id) === tagFilter));
}

async function loadTagFilterOptions() {
  const select = document.getElementById("tag-filter");
  if (!select) return;
  try {
    const data = await api("/api/tags");
    const current = select.value || "all";
    select.innerHTML =
      '<option value="all">全部</option><option value="untagged">无标签</option>' +
      data.items.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    const valid =
      current === "all" ||
      current === "untagged" ||
      data.items.some((t) => String(t.id) === current);
    select.value = valid ? current : "all";
  } catch (err) {
    // 标签选项加载失败不阻塞行情展示
    showError(`标签选项加载失败：${err.message}`);
  }
}

function initIndexPage() {
  const select = document.getElementById("tag-filter");
  if (select) {
    select.addEventListener("change", () => {
      tagFilter = select.value;
      // 本地过滤重渲染：零请求、零延迟，不触发任何行情请求
      if (lastQuotesData) renderQuotes(lastQuotesData);
    });
  }
  loadTagFilterOptions();
  // 首次打开：无论是否交易，立即读取一次后端缓存
  fetchAndRender()
    .then((status) => schedulePolling(status))
    .catch(() => {});
}

// ---------- 标签管理页 ----------

function initTagsPage() {
  const form = document.getElementById("add-tag-form");
  const msg = document.getElementById("tag-message");
  if (!form) return;

  async function loadList() {
    const data = await api("/api/tags");
    const tbody = document.querySelector("#tags-table tbody");
    const empty = document.getElementById("tags-empty");
    tbody.innerHTML = "";
    empty.classList.toggle("hidden", data.items.length > 0);
    document.getElementById("tags-table").classList.toggle("hidden", data.items.length === 0);

    data.items.forEach((tag) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="tag-name">${esc(tag.name)}</td>
        <td class="right">${tag.usage_count}</td>
        <td>
          <button class="link" data-act="edit" data-id="${tag.id}" data-name="${esc(tag.name)}">编辑</button>
          <button class="link danger" data-act="del" data-id="${tag.id}">删除</button>
        </td>`;
      tbody.appendChild(tr);
    });
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/tags", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: document.getElementById("tag-name-input").value }),
      });
      showMsg(msg, "添加成功", "success");
      document.getElementById("tag-name-input").value = "";
      await loadList();
    } catch (err) {
      showMsg(msg, `添加失败：${err.message}`, "error");
    }
  });

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const tr = btn.closest("tr");
    const id = btn.dataset.id;

    if (btn.dataset.act === "edit") {
      // 行内编辑：名称单元格换成输入框 + 保存/取消，禁用原操作按钮
      const nameTd = tr.querySelector(".tag-name");
      nameTd.innerHTML = `
        <input type="text" class="tag-edit-input" value="${esc(btn.dataset.name)}" maxlength="50">
        <button class="link" data-act="save" data-id="${id}">保存</button>
        <button class="link" data-act="cancel">取消</button>`;
      tr.querySelectorAll("td:last-child button").forEach((b) => (b.disabled = true));
      nameTd.querySelector("input").focus();
    } else if (btn.dataset.act === "save") {
      const name = tr.querySelector(".tag-edit-input").value;
      try {
        await api(`/api/tags/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        showMsg(msg, "修改成功", "success");
        await loadList();
      } catch (err) {
        showMsg(msg, `修改失败：${err.message}`, "error");
      }
    } else if (btn.dataset.act === "cancel") {
      await loadList();
    } else if (btn.dataset.act === "del") {
      try {
        await api(`/api/tags/${id}`, { method: "DELETE" });
        showMsg(msg, "删除成功", "success");
        await loadList();
      } catch (err) {
        // 被引用标签删除返回 409，展示后端文案
        showMsg(msg, `删除失败：${err.message}`, "error");
      }
    }
  });

  loadList().catch((err) => showMsg(msg, err.message, "error"));
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "watchlist") initWatchlistPage();
  else if (page === "index") initIndexPage();
  else if (page === "tags") initTagsPage();
});
