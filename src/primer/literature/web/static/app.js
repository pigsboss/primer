"use strict";

// PRIMER · 文献库前端：项目树｜文献清单｜记录详情＋首启页。
// 纯原生 JS，无外部资源；一切数据来自本地服务的 /api/* 端点。

const NATURES = [
  ["doi-consistent", "正式"],
  ["preprint-substitute", "预印本"],
  ["title-match", "题名匹配"],
  ["manual-upload", "人工"],
  ["other", "其他"],
];

const TYPE_LABELS = {
  "journal-article": "期刊",
  "conference-paper": "会议",
  "book-chapter": "文集论文",
  "standard": "标准",
  "report": "报告",
  "preprint": "预印本",
  "book": "图书",
  "thesis": "学位论文",
  "patent": "专利",
  "other": "其他",
};

// 编辑器：通用字段（全类型显示）＋各类型专属字段（biblatex 规范名）。
const COMMON_FIELDS = new Set(["title", "authors", "year", "type", "doi", "url", "download_url", "keywords", "notes"]);
const TYPE_FIELDS = {
  "journal-article": ["venue", "volume", "number", "pages", "eid", "issn"],
  "conference-paper": ["venue", "eventtitle", "eventdate", "organization", "location", "publisher", "volume", "pages", "editor", "series", "isbn"],
  "book-chapter": ["venue", "editor", "publisher", "location", "edition", "volume", "pages", "series", "isbn"],
  "book": ["publisher", "location", "edition", "series", "isbn", "volume", "editor", "translator"],
  "report": ["institution", "number", "location"],
  "standard": ["number", "publisher", "location"],
  "patent": ["number", "location"],
  "thesis": ["institution", "location"],
  "preprint": ["venue", "eprint"],
};
const TEXT_FIELDS = [
  "volume", "number", "pages", "eid", "publisher", "location", "institution",
  "organization", "series", "edition", "isbn", "issn", "url", "eprint",
  "eventtitle", "eventdate", "keywords", "download_url",
];

const state = {
  loaded: false,
  path: "",
  defaultPath: "",
  loadError: "",
  records: [],
  tree: { roots: [], total: 0, unfiled: 0 },
  scan: null,
  filter: { kind: "all" },
  search: "",
  sortStack: [{ key: "year", dir: "desc" }],
  filterStack: [],
  view: "records",
  fileRecords: [],
  selectedFile: null,
  filePollTimer: null,
  selection: new Set(),
  anchor: null,
  enriching: false,
  lastScanAt: "",
  collapsed: new Set(),
  editingUuid: null,
  bulkEdit: null,
  editorFiles: [],
};

function selectedRecords() {
  return state.records.filter((record) => state.selection.has(record.uuid));
}

function selectSingle(uuid) {
  state.selection = new Set([uuid]);
  state.anchor = uuid;
}

function clearSelection() {
  state.selection = new Set();
  state.anchor = null;
}

function handleRowClick(record, event) {
  if (event.shiftKey && state.anchor) {
    const list = visibleRecords();
    const anchorIndex = list.findIndex((item) => item.uuid === state.anchor);
    const index = list.findIndex((item) => item.uuid === record.uuid);
    if (anchorIndex >= 0 && index >= 0) {
      const [start, end] = anchorIndex <= index ? [anchorIndex, index] : [index, anchorIndex];
      state.selection = new Set(list.slice(start, end + 1).map((item) => item.uuid));
    } else {
      selectSingle(record.uuid);
    }
  } else if (event.metaKey || event.ctrlKey) {
    if (state.selection.has(record.uuid)) state.selection.delete(record.uuid);
    else state.selection.add(record.uuid);
    state.anchor = record.uuid;
  } else {
    selectSingle(record.uuid);
  }
  renderList();
  renderDetail();
}

const byId = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, onClick, disabled) {
  const node = el("button", null, label);
  node.disabled = Boolean(disabled);
  node.addEventListener("click", onClick);
  return node;
}

// ------------------------------------------------------------------ API

async function api(method, path, payload) {
  const options = { method, headers: {} };
  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    let message = data.message || "HTTP " + response.status;
    if (typeof message === "string" && message.startsWith("no route:")) {
      message =
        "服务端进程是旧版本（缺少该接口）。请重启 web 服务后重试：Ctrl-C 停掉，" +
        "并在原目录重新运行 python -m primer.literature web。";
    }
    throw new Error(message);
  }
  return data;
}

// ------------------------------------------------------- 项目路径与过滤

function normalizePath(raw) {
  return String(raw || "")
    .split("/")
    .map((part) => part.trim())
    .filter(Boolean)
    .join("/");
}

function activeProjects(record) {
  const seen = new Set();
  const out = [];
  for (const raw of record.projects || []) {
    const path = normalizePath(raw);
    if (path && !seen.has(path)) {
      seen.add(path);
      out.push(path);
    }
  }
  return out;
}

function isUnfiled(record) {
  return activeProjects(record).length === 0;
}

function inSubtree(record, path) {
  return activeProjects(record).some((p) => p === path || p.startsWith(path + "/"));
}

// 显示菜单：可排序/筛选的记录字段（key 为记录字段名）。
const SORT_KEYS = {
  title: "标题",
  authors: "作者",
  editor: "编者",
  year: "年份",
  type: "类型",
  venue: "来源",
  volume: "卷",
  number: "期号",
  pages: "页码",
  publisher: "出版者",
  location: "出版地",
  isbn: "ISBN",
  issn: "ISSN",
  doi: "DOI",
  keywords: "关键词",
  projects: "项目",
  files: "本地文件数",
  uuid: "UUID",
  created_at: "创建时间",
  updated_at: "更新时间",
};

const FILTER_OPS = {
  eq: "等于",
  gt: "大于",
  lt: "小于",
  between: "区间内",
};

function recordValue(record, key) {
  if (key === "files") return String((record.files || []).length);
  if (key === "authors") return (record.authors || []).join("；");
  if (key === "editor") return (record.editor || []).join("；");
  if (key === "translator") return (record.translator || []).join("；");
  if (key === "projects") return activeProjects(record).join("；");
  if (key === "type") return typeLabel(record.type);
  const value = record[key];
  return value === null || value === undefined ? "" : String(value);
}

function compareLoose(left, right) {
  const leftText = String(left).trim();
  const rightText = String(right).trim();
  const leftNumber = Number(leftText);
  const rightNumber = Number(rightText);
  if (
    leftText !== "" &&
    rightText !== "" &&
    Number.isFinite(leftNumber) &&
    Number.isFinite(rightNumber)
  ) {
    return leftNumber - rightNumber;
  }
  return leftText.toLowerCase().localeCompare(rightText.toLowerCase(), "zh");
}

function activeFilters() {
  return state.filterStack.filter((rule) => String(rule.a || "").trim());
}

function matchesFilters(record) {
  for (const rule of activeFilters()) {
    const raw = recordValue(record, rule.key);
    const first = String(rule.a || "").trim();
    if (rule.op === "eq") {
      if (raw.trim().toLowerCase() !== first.toLowerCase()) return false;
    } else if (rule.op === "between") {
      const second = String(rule.b || "").trim();
      if (!second) continue;
      if (compareLoose(raw, first) < 0 || compareLoose(raw, second) > 0) return false;
    } else if (rule.op === "gt") {
      if (compareLoose(raw, first) <= 0) return false;
    } else if (rule.op === "lt") {
      if (compareLoose(raw, first) >= 0) return false;
    }
  }
  return true;
}

function compareByKey(left, right, key) {
  if (key === "year") return (left.year || 0) - (right.year || 0);
  if (key === "files") return (left.files || []).length - (right.files || []).length;
  return recordValue(left, key).localeCompare(recordValue(right, key), "zh");
}

function sortRecords(list) {
  const stack = state.sortStack;
  if (!stack.length) return list;
  return list.slice().sort((left, right) => {
    for (const rule of stack) {
      const cmp = compareByKey(left, right, rule.key);
      if (cmp !== 0) return rule.dir === "desc" ? -cmp : cmp;
    }
    return 0;
  });
}

function visibleRecords() {
  let list = state.records.slice();
  const filter = state.filter;
  if (filter.kind === "node") list = list.filter((r) => inSubtree(r, filter.path));
  else if (filter.kind === "unfiled") list = list.filter(isUnfiled);

  const query = state.search.trim().toLowerCase();
  if (query) {
    list = list.filter((record) => {
      const haystack = [
        record.title,
        (record.authors || []).join(" "),
        (record.editor || []).join(" "),
        (record.translator || []).join(" "),
        record.doi || "",
        activeProjects(record).join(" "),
        record.venue || "",
        record.notes || "",
        ...TEXT_FIELDS.map((field) => record[field] || ""),
      ]
        .join("\n")
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  list = list.filter(matchesFilters);
  return sortRecords(list);
}

function filterTitle() {
  const filter = state.filter;
  if (filter.kind === "node") return filter.path;
  if (filter.kind === "unfiled") return "未归项目";
  return "全部";
}

function sameFilter(a, b) {
  return a.kind === b.kind && (a.path || "") === (b.path || "");
}

// --------------------------------------------------------------- 渲染

function render() {
  if (!state.loaded) {
    renderTopbar();
    renderUnloaded();
    renderStatus();
    return;
  }
  renderTopbar();
  renderTree();
  renderList();
  renderDetail();
  renderStatus();
}

function renderTopbar() {
  byId("lib-path").textContent = state.loaded ? "库: " + state.path : "库: 未加载";
  byId("lib-path").title = state.loaded ? state.path : state.defaultPath || "";
  byId("btn-new").disabled = !state.loaded;
  byId("btn-scan").disabled = !state.loaded;
}

function renderUnloaded() {
  byId("tree").textContent = "";
  byId("detail").textContent = "";
  byId("list-title").textContent = "文献清单";
  const host = byId("list");
  host.textContent = "";
  const hint = el("div", "empty-hint");
  hint.appendChild(el("p", null, "尚未加载文献库。"));
  hint.appendChild(button("打开设置…", showSetup));
  host.appendChild(hint);
}

function renderTree() {
  const host = byId("tree");
  const scrollTop = host.scrollTop;
  host.textContent = "";
  host.appendChild(specialRow("全部", state.tree.total, { kind: "all" }));
  for (const root of state.tree.roots) host.appendChild(renderTreeNode(root, 1));
  host.appendChild(specialRow("未归项目", state.tree.unfiled, { kind: "unfiled" }));
  host.scrollTop = scrollTop;
}

function specialRow(label, count, filter) {
  const row = el("div", "tree-row");
  row.appendChild(el("span", "caret", label === "全部" ? "▾" : "·"));
  row.appendChild(el("span", "tree-label", label));
  row.appendChild(el("span", "tree-count", String(count)));
  if (sameFilter(state.filter, filter)) row.classList.add("selected");
  row.addEventListener("click", () => selectFilter(filter));
  return row;
}

function renderTreeNode(node, depth) {
  const wrap = document.createDocumentFragment();
  const row = el("div", "tree-row");
  row.style.paddingLeft = 8 + depth * 14 + "px";
  const hasChildren = node.children && node.children.length > 0;
  const collapsed = state.collapsed.has(node.path);
  const caret = el("span", "caret", hasChildren ? (collapsed ? "▸" : "▾") : "·");
  if (hasChildren) {
    caret.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleCollapsed(node.path);
    });
  }
  row.appendChild(caret);
  row.appendChild(el("span", "tree-label", node.name));
  row.appendChild(el("span", "tree-count", String(node.count)));
  if (sameFilter(state.filter, { kind: "node", path: node.path })) row.classList.add("selected");
  row.addEventListener("click", () => selectFilter({ kind: "node", path: node.path }));
  wrap.appendChild(row);
  if (hasChildren && !collapsed) {
    for (const child of node.children) wrap.appendChild(renderTreeNode(child, depth + 1));
  }
  return wrap;
}

function selectFilter(filter) {
  state.filter = filter;
  renderTree();
  renderList();
  renderStatus();
}

function toggleCollapsed(path) {
  if (state.collapsed.has(path)) state.collapsed.delete(path);
  else state.collapsed.add(path);
  renderTree();
}

function linkedFilesOf(record) {
  return (state.fileRecords || []).filter((file) => file.record_uuid === record.uuid);
}

function coverage(record) {
  const legacy = (record.files || []).length;
  const linked = linkedFilesOf(record);
  const total = legacy + linked.length;
  if (!total) return { kind: "none", text: "—" };
  let exists = 0;
  let verified = true;
  if (legacy) {
    const entries = state.scan && state.scan[record.uuid];
    if (!entries || entries.length !== legacy) verified = false;
    else exists += entries.filter((entry) => entry.exists).length;
  }
  if (linked.some((file) => file.exists === undefined)) verified = false;
  else exists += linked.filter((file) => file.exists).length;
  if (!verified) return { kind: "unknown", text: "…" };
  if (exists === total) return { kind: "ok", text: "✓ " + total };
  if (exists === 0) return { kind: "bad", text: "✗ " + total };
  return { kind: "part", text: exists + "/" + total };
}

function typeLabel(type) {
  if (!type) return "—";
  return TYPE_LABELS[type] || type;
}

function natureLabel(nature) {
  const found = NATURES.find(([value]) => value === nature);
  return found ? found[1] : nature || "其他";
}

function authorsShort(authors) {
  const list = (authors || []).filter(Boolean);
  if (!list.length) return "—";
  if (list.length <= 2) return list.join(", ");
  return list.slice(0, 2).join(", ") + " 等";
}

function formatBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + " MB";
  return (value / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fileSuffix(record) {
  const match = String(record.name || record.path || "").match(/\.([^.]+)$/);
  return match ? match[1].toLowerCase() : "—";
}

const FILE_STATUS_LABELS = { pending: "待解析", parsing: "解析中", done: "已完成", failed: "失败" };

function fileStatusLabel(status) {
  return FILE_STATUS_LABELS[status] || status || "—";
}

function renderList() {
  if (state.view === "files") {
    renderFilesList();
    return;
  }
  renderRecordsList();
}

function renderFilesList() {
  const host = byId("list");
  const scrollTop = host.scrollTop;
  const query = state.search.trim().toLowerCase();
  let files = state.fileRecords.slice();
  if (query) {
    files = files.filter((record) =>
      ((record.name || "") + "\n" + (record.path || "")).toLowerCase().includes(query)
    );
  }
  files.sort((a, b) => String(b.added_at || "").localeCompare(String(a.added_at || "")));
  const parsing = files.filter((item) => item.status === "pending" || item.status === "parsing").length;
  byId("list-title").textContent = "文件 · " + files.length + " 条" + (parsing ? " · 待解析 " + parsing : "");
  host.textContent = "";
  if (!files.length) {
    const hint = el("div", "empty-hint");
    hint.appendChild(
      el(
        "p",
        null,
        state.fileRecords.length
          ? "没有匹配的文件。"
          : "还没有本地文件——用「文件」菜单的「扫描文件夹…」或「添加本地文件…」加入。"
      )
    );
    host.appendChild(hint);
    return;
  }
  const table = el("table");
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["名称", "格式", "大小", "状态", "关联文献", "重复", "Markdown"]) {
    headRow.appendChild(el("th", null, label));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const record of files) {
    const row = el("tr");
    if (record.uuid === state.selectedFile) row.classList.add("selected");
    const nameCell = el("td", "title", record.name || "（未命名）");
    nameCell.title = record.path || "";
    row.appendChild(nameCell);
    row.appendChild(el("td", null, fileSuffix(record)));
    row.appendChild(el("td", null, formatBytes(record.size || 0)));
    row.appendChild(el("td", "file-status st-" + (record.status || "other"), fileStatusLabel(record.status)));
    row.appendChild(
      el(
        "td",
        "file-link-cell" + (record.record_uuid ? "" : " none"),
        record.record_uuid ? recordTitle(record.record_uuid) : "—"
      )
    );
    const dup = record.dup && record.dup.kind ? record.dup : null;
    const dupCell = el(
      "td",
      "dup-cell" + (dup ? " dup-yes" : ""),
      dup ? "⚠ " + (dup.kind === "doi" ? "DOI" : "arXiv") : "—"
    );
    if (dup) {
      dupCell.title =
        "重复（" +
        (dup.kind === "doi" ? "DOI" : "arXiv") +
        " " +
        (dup.value || "") +
        "）：" +
        (dup.matches || [])
          .map((item) => (item.kind === "record" ? "文献：" : "文件：") + (item.label || item.uuid))
          .join("；");
    }
    row.appendChild(dupCell);
    row.appendChild(el("td", null, record.md_path ? "✓" : "—"));
    row.addEventListener("click", () => {
      state.selectedFile = record.uuid;
      renderList();
      renderDetail();
    });
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  host.appendChild(table);
  host.scrollTop = scrollTop;
}

function renderRecordsList() {
  const host = byId("list");
  const scrollTop = host.scrollTop;
  const list = visibleRecords();
  const filterCount = activeFilters().length;
  byId("list-title").textContent =
    filterTitle() + " · " + list.length + " 条" + (filterCount ? " · 筛选 " + filterCount + " 项" : "");
  host.textContent = "";
  if (!list.length) {
    const hint = el("div", "empty-hint");
    hint.appendChild(el("p", null, state.records.length ? "没有匹配的记录。" : "库是空的，点右上角「＋ 新建记录」开始。"));
    host.appendChild(hint);
    return;
  }
  const table = el("table");
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["标题", "作者", "年份", "类型", "本地文件", "DOI"]) {
    headRow.appendChild(el("th", null, label));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const record of list) {
    const row = el("tr");
    if (state.selection.has(record.uuid)) row.classList.add("selected");
    row.appendChild(el("td", "title", record.title || "（无标题）"));
    row.appendChild(el("td", null, authorsShort(record.authors)));
    row.appendChild(el("td", null, record.year ? String(record.year) : "—"));
    row.appendChild(el("td", null, typeLabel(record.type)));
    const cov = coverage(record);
    row.appendChild(el("td", "coverage " + cov.kind, cov.text));
    row.appendChild(el("td", null, record.doi ? "✓" : "—"));
    row.addEventListener("click", (event) => handleRowClick(record, event));
    row.addEventListener("dblclick", () => openPrimary(record));
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      selectSingle(record.uuid);
      renderList();
      renderDetail();
      showRowMenu(event.clientX, event.clientY, record);
    });
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  host.appendChild(table);
  host.scrollTop = scrollTop;
}

async function openFileRecord(record, which) {
  try {
    await api("POST", "/api/files/open", { uuid: record.uuid, which });
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function retryParse(record) {
  try {
    const data = await api("POST", "/api/files/parse", { uuids: [record.uuid] });
    if (data.queued) {
      showMessage("已重新排入解析队列", "info");
      await refreshFiles();
      ensureFilePolling();
    } else {
      showMessage("该文件已在队列中", "info");
    }
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function removeFileRecord(record) {
  if (
    !confirm("移除文件记录「" + (record.name || record.uuid) + "」？磁盘上的文件与解析产物不会被删除。")
  ) {
    return;
  }
  try {
    await api("POST", "/api/files/delete", { uuid: record.uuid });
    if (state.selectedFile === record.uuid) state.selectedFile = null;
    await refreshFiles();
    showMessage("已移除", "info");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function renderFileDetail() {
  const host = byId("detail");
  host.textContent = "";
  const record = state.fileRecords.find((item) => item.uuid === state.selectedFile);
  if (!record) {
    host.appendChild(el("p", "hint", "选择清单中的一条文件记录。"));
    return;
  }
  host.appendChild(el("h2", "detail-title", record.name || "（未命名）"));
  host.appendChild(
    el("p", "detail-meta", fileStatusLabel(record.status) + " · " + formatBytes(record.size || 0))
  );

  const linked = state.records.find((item) => item.uuid === record.record_uuid) || null;
  const linkRow = el("div", "detail-row");
  linkRow.appendChild(el("span", "detail-label", "关联文献"));
  if (!record.record_uuid) {
    linkRow.appendChild(el("span", "detail-value", "—（未关联）"));
  } else if (!linked) {
    linkRow.appendChild(el("span", "detail-value", "关联记录不存在"));
  } else {
    const link = el("span", "detail-value link-like", linked.title || "（无标题）");
    link.addEventListener("click", () => {
      selectSingle(linked.uuid);
      switchView("records");
      renderList();
      renderDetail();
    });
    linkRow.appendChild(link);
    if (record.nature) linkRow.appendChild(el("span", "badge", natureLabel(record.nature)));
  }
  host.appendChild(linkRow);

  const rows = [
    ["路径", record.path || "—", true],
    ["添加", record.added_at || "—", false],
    ["更新", record.updated_at || "—", false],
    ["MD5", record.md5 || "—", true],
    ["DOI", record.doi || "—", true],
    ["arXiv", record.eprint || "—", true],
    ["Markdown", record.md_path || "—", true],
  ];
  for (const [label, value, mono] of rows) {
    const row = el("div", "detail-row");
    row.appendChild(el("span", "detail-label", label));
    row.appendChild(el("span", "detail-value" + (mono ? " mono" : ""), String(value)));
    host.appendChild(row);
  }
  const dup = record.dup && record.dup.kind ? record.dup : null;
  if (dup) {
    host.appendChild(el("h3", "detail-section", "疑似重复"));
    host.appendChild(
      el(
        "p",
        "detail-note",
        "该文件的 " +
          (dup.kind === "doi" ? "DOI" : "arXiv") +
          "「" +
          (dup.value || "") +
          "」与以下已有条目相同："
      )
    );
    for (const item of dup.matches || []) {
      const line = el("div", "detail-note dup-match");
      line.appendChild(
        el(
          "span",
          null,
          (item.kind === "record" ? "文献记录：" : "文件记录：") + (item.label || item.uuid)
        )
      );
      if (item.kind === "record") {
        line.appendChild(
          button("关联此文献", () =>
            attachFileToRecord(
              record,
              item.uuid,
              dup.kind === "eprint" ? "preprint-substitute" : "doi-consistent"
            )
          )
        );
      }
      host.appendChild(line);
    }
  }
  if (record.error) {
    host.appendChild(el("h3", "detail-section", "错误"));
    host.appendChild(el("p", "detail-note", record.error));
  }

  const buttons = el("div", "detail-buttons");
  buttons.appendChild(button("打开文件", () => openFileRecord(record, "source")));
  buttons.appendChild(
    button("打开 Markdown", () => openFileRecord(record, "markdown"), !record.md_path)
  );
  buttons.appendChild(button("关联文献…", () => openFileLinkDialog(record)));
  buttons.appendChild(button("解除关联", () => detachFileRecord(record), !record.record_uuid));
  buttons.appendChild(button("重新解析", () => retryParse(record), record.status === "parsing"));
  buttons.appendChild(button("移除记录", () => removeFileRecord(record)));
  host.appendChild(buttons);
}

// ------------------------------------------------------- 文献 ↔ 文件关联

function recordTitle(uuid) {
  const record = state.records.find((item) => item.uuid === uuid);
  return record ? record.title || "（无标题）" : "（记录不存在）";
}

let linkMode = null;
let linkTarget = null;
let linkPick = null;

function openRecordLinkDialog(record) {
  openLinkDialog({
    mode: "files",
    target: record.uuid,
    title: "关联文件到「" + (record.title || "（无标题）") + "」",
    note: "勾选要挂到这条文献记录下的本地文件；已属其它记录的文件会被改挂。",
  });
}

function openFileLinkDialog(record) {
  openLinkDialog({
    mode: "records",
    target: [record.uuid],
    title: "为「" + (record.name || "（未命名）") + "」选择文献记录",
    note: "选择这条文件要关联的文献记录。",
  });
}

function openRecordPicker(title, note, onPick) {
  openLinkDialog({ mode: "records", onPick, title, note });
}

function openLinkDialog(options) {
  linkMode = options.mode;
  linkTarget = options.target;
  linkPick = options.onPick || null;
  byId("ld-title").textContent = options.title;
  byId("ld-note").textContent = options.note || "";
  byId("ld-nature-row").classList.toggle("hidden", Boolean(linkPick));
  const select = byId("ld-nature");
  select.textContent = "";
  for (const [value, label] of NATURES) {
    const option = el("option", null, label);
    option.value = value;
    select.appendChild(option);
  }
  select.value = options.nature || "doi-consistent";
  byId("ld-search").value = "";
  byId("ld-error").textContent = "";
  renderLinkOptions();
  byId("link-dialog").classList.remove("hidden");
  byId("ld-search").focus();
}

function renderLinkOptions() {
  const host = byId("ld-options");
  host.textContent = "";
  const query = byId("ld-search").value.trim().toLowerCase();
  if (linkMode === "files") {
    let files = state.fileRecords.slice();
    if (query) {
      files = files.filter((record) =>
        ((record.name || "") + "\n" + (record.path || "")).toLowerCase().includes(query)
      );
    }
    files.sort((a, b) => String(b.added_at || "").localeCompare(String(a.added_at || "")));
    if (!files.length) {
      host.appendChild(
        el("p", "hint", state.fileRecords.length ? "没有匹配的文件。" : "还没有本地文件。")
      );
      return;
    }
    for (const record of files) {
      const row = el("label", "link-option");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = record.uuid;
      row.appendChild(box);
      row.appendChild(el("span", "link-option-label", record.name || record.uuid));
      const parts = [fileStatusLabel(record.status)];
      if (record.record_uuid) parts.push("已属：" + recordTitle(record.record_uuid));
      row.appendChild(el("span", "link-option-sub", parts.join(" · ")));
      host.appendChild(row);
    }
    return;
  }
  let records = state.records.slice();
  if (query) {
    records = records.filter((record) =>
      ((record.title || "") + "\n" + (record.doi || "")).toLowerCase().includes(query)
    );
  }
  if (!records.length) {
    host.appendChild(el("p", "hint", "没有匹配的文献记录。"));
    return;
  }
  for (const record of records) {
    const row = el("label", "link-option");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "ld-record";
    radio.value = record.uuid;
    row.appendChild(radio);
    row.appendChild(el("span", "link-option-label", record.title || "（无标题）"));
    const parts = [record.year ? String(record.year) : "", typeLabel(record.type)];
    if (record.doi) parts.push(record.doi);
    row.appendChild(el("span", "link-option-sub", parts.filter(Boolean).join(" · ")));
    host.appendChild(row);
  }
}

async function attachFiles(recordUuid, fileUuids, nature) {
  const data = await api("POST", "/api/links/attach", {
    record_uuid: recordUuid,
    file_uuids: fileUuids,
    nature,
  });
  showMessage(
    "已关联 " + data.linked + " 个文件" + (data.replaced ? "（改挂 " + data.replaced + " 个）" : ""),
    "info"
  );
  await refreshFiles();
  renderList();
  renderDetail();
}

async function submitLinkDialog() {
  const errorHost = byId("ld-error");
  errorHost.textContent = "";
  const nature = byId("ld-nature").value;
  try {
    if (linkMode === "files") {
      const checked = Array.from(document.querySelectorAll("#ld-options input:checked")).map(
        (node) => node.value
      );
      if (!checked.length) {
        errorHost.textContent = "请至少选择一个文件";
        return;
      }
      await attachFiles(linkTarget, checked, nature);
    } else {
      const chosen = document.querySelector("#ld-options input:checked");
      if (!chosen) {
        errorHost.textContent = "请选择一条文献记录";
        return;
      }
      if (linkPick) {
        const record = state.records.find((item) => item.uuid === chosen.value);
        linkPick(chosen.value, record ? record.title : "");
      } else {
        await attachFiles(chosen.value, linkTarget, nature);
      }
    }
  } catch (error) {
    errorHost.textContent = error.message;
    return;
  }
  byId("link-dialog").classList.add("hidden");
  linkMode = null;
  linkTarget = null;
  linkPick = null;
}

async function detachFileRecord(record) {
  if (!record.record_uuid) return;
  if (!confirm("解除「" + (record.name || record.uuid) + "」与文献记录的关联？")) return;
  try {
    await api("POST", "/api/links/detach", { file_uuids: [record.uuid] });
    showMessage("已解除关联", "info");
    await refreshFiles();
    renderList();
    renderDetail();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function attachFileToRecord(record, recordUuid, nature) {
  try {
    await attachFiles(recordUuid, [record.uuid], nature || "doi-consistent");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function renderDetail() {
  if (state.view === "files") {
    renderFileDetail();
    return;
  }
  const host = byId("detail");
  host.textContent = "";
  const chosen = selectedRecords();
  if (!chosen.length) {
    host.appendChild(el("p", "hint", "选择清单中的一条或多条记录（Ctrl／Cmd 加选，Shift 连选）。"));
    return;
  }
  if (chosen.length > 1) {
    host.appendChild(el("h2", "detail-title", "已选 " + chosen.length + " 条记录"));
    host.appendChild(el("p", "hint", "「编辑」菜单的 联网查询／AI 解析／编辑字段／删除 都作用于这 " + chosen.length + " 条。"));
    for (const item of chosen.slice(0, 50)) {
      host.appendChild(el("p", "detail-note", item.title || "（无标题）"));
    }
    if (chosen.length > 50) {
      host.appendChild(el("p", "hint", "……还有 " + (chosen.length - 50) + " 条"));
    }
    return;
  }
  const record = chosen[0];

  host.appendChild(el("h2", "detail-title", record.title || "（无标题）"));
  const meta = [typeLabel(record.type), record.year ? String(record.year) : null, record.venue || null]
    .filter(Boolean)
    .join(" · ");
  if (meta) host.appendChild(el("p", "detail-meta", meta));
  if ((record.authors || []).length) {
    host.appendChild(el("p", "detail-authors", record.authors.join("；")));
  }

  const detailFields = [
    ["editor", "编者"], ["translator", "译者"],
    ["volume", "卷"], ["number", "期号"], ["pages", "页码"], ["eid", "文章号"],
    ["publisher", "出版者"], ["location", "出版地"], ["institution", "机构"],
    ["organization", "主办"], ["series", "丛书"], ["edition", "版次"],
    ["isbn", "ISBN"], ["issn", "ISSN"], ["eprint", "arXiv"],
    ["eventtitle", "会议"], ["eventdate", "会期"], ["url", "链接"], ["keywords", "关键词"],
  ];
  for (const [field, label] of detailFields) {
    const value = Array.isArray(record[field]) ? record[field].join("；") : record[field];
    if (!value) continue;
    const row = el("div", "detail-row");
    row.appendChild(el("span", "detail-label", label));
    row.appendChild(el("span", "detail-value", String(value)));
    host.appendChild(row);
  }

  const doiRow = el("div", "detail-row");
  doiRow.appendChild(el("span", "detail-label", "DOI"));
  doiRow.appendChild(el("span", "detail-value", record.doi || "—"));
  host.appendChild(doiRow);

  const dlRow = el("div", "detail-row");
  dlRow.appendChild(el("span", "detail-label", "下载链接"));
  if (record.download_url) {
    const linkNode = el("span", "detail-value mono link-like", record.download_url);
    linkNode.title = record.download_url;
    linkNode.addEventListener("click", () => openExternal(record.download_url));
    dlRow.appendChild(linkNode);
  } else {
    dlRow.appendChild(el("span", "detail-value", "—"));
  }
  host.appendChild(dlRow);

  const uuidRow = el("div", "detail-row");
  uuidRow.appendChild(el("span", "detail-label", "UUID"));
  uuidRow.appendChild(el("span", "detail-value mono", record.uuid));
  host.appendChild(uuidRow);

  const files = record.files || [];
  host.appendChild(el("h3", "detail-section", "本地文件 (" + files.length + ")"));
  if (!files.length) {
    host.appendChild(el("p", "hint", "无本地文件"));
  } else {
    const entries = (state.scan && state.scan[record.uuid]) || [];
    files.forEach((file, index) => {
      const row = el("div", "file-row");
      const exists = entries.length > index ? entries[index].exists : null;
      row.appendChild(el("span", "badge" + (exists === false ? " bad" : ""), natureLabel(file.nature)));
      row.appendChild(
        el("span", "file-icon " + (exists === true ? "ok" : exists === false ? "bad" : ""),
           exists === true ? "✓" : exists === false ? "✗" : "?")
      );
      const pathNode = el("span", "file-path", file.path);
      pathNode.title = file.note ? file.path + " ｜ " + file.note : file.path;
      if (exists !== false) {
        pathNode.classList.add("clickable");
        pathNode.addEventListener("click", () => openFileIndex(record, index));
      }
      row.appendChild(pathNode);
      host.appendChild(row);
    });
  }

  const linkedFiles = state.fileRecords.filter((item) => item.record_uuid === record.uuid);
  host.appendChild(el("h3", "detail-section", "关联文件 (" + linkedFiles.length + ")"));
  if (!linkedFiles.length) {
    host.appendChild(el("p", "hint", "无关联文件"));
  } else {
    for (const file of linkedFiles) {
      const row = el("div", "file-row");
      row.appendChild(el("span", "badge", natureLabel(file.nature)));
      const icon = file.status === "done" ? "ok" : file.status === "failed" ? "bad" : "";
      const mark = file.status === "done" ? "✓" : file.status === "failed" ? "✗" : "…";
      row.appendChild(el("span", "file-icon " + icon, mark));
      const nameNode = el("span", "file-path clickable", file.name || file.path);
      nameNode.title = file.path || "";
      nameNode.addEventListener("click", () => {
        state.selectedFile = file.uuid;
        switchView("files");
        renderList();
        renderDetail();
      });
      row.appendChild(nameNode);
      row.appendChild(button("解除", () => detachFileRecord(file)));
      host.appendChild(row);
    }
  }

  const projects = activeProjects(record);
  host.appendChild(el("h3", "detail-section", "项目 (" + projects.length + ")"));
  if (!projects.length) host.appendChild(el("p", "hint", "未归项目"));
  else for (const path of projects) host.appendChild(el("div", "project-chip", path));

  const notes = (record.notes || "").trim();
  if (notes) {
    host.appendChild(el("h3", "detail-section", "备注"));
    for (const line of notes.split("\n")) {
      if (line.trim()) host.appendChild(el("p", "detail-note", line));
    }
  }

  const buttons = el("div", "detail-buttons");
  buttons.appendChild(button("打开文件", () => openPrimary(record), files.length === 0));
  buttons.appendChild(button("打开下载链接", () => openExternal(record.download_url), !record.download_url));
  buttons.appendChild(button("打开 DOI", () => openDoi(record), !record.doi));
  buttons.appendChild(button("复制路径", () => copyText(files[0] ? files[0].path : ""), files.length === 0));
  buttons.appendChild(button("编辑记录", () => openEditor(record)));
  buttons.appendChild(button("关联文件…", () => openRecordLinkDialog(record)));
  host.appendChild(buttons);
}

function renderStatus() {
  const visible = state.loaded ? visibleRecords() : [];
  const parts = ["共 " + state.records.length + " 条", "显示 " + visible.length + " 条"];
  if (state.loaded) {
    let filesTotal = 0;
    let filesMissing = 0;
    let scanned = false;
    for (const record of state.records) {
      const linked = linkedFilesOf(record);
      filesTotal += (record.files || []).length + linked.length;
      const entries = state.scan && state.scan[record.uuid];
      if (entries) {
        scanned = true;
        filesMissing += entries.filter((entry) => !entry.exists).length;
      }
      if (linked.length) {
        scanned = true;
        filesMissing += linked.filter((file) => file.exists === false).length;
      }
    }
    parts.push(scanned ? "文件 " + (filesTotal - filesMissing) + " ✓ / 缺失 " + filesMissing + " ✗"
                       : "文件 " + filesTotal + " 条（未扫描）");
    parts.push("库: " + state.path);
    if (state.lastScanAt) parts.push("重扫 " + state.lastScanAt);
  }
  byId("status-text").textContent = parts.join(" · ");
}

let messageTimer = null;
function showMessage(text, kind, sticky) {
  const host = byId("message");
  host.textContent = text || "";
  host.className = "message" + (kind ? " " + kind : "");
  if (messageTimer) clearTimeout(messageTimer);
  messageTimer = null;
  if (sticky) return;  // 常驻（如任务进度），由下一条消息替换
  messageTimer = setTimeout(() => {
    host.textContent = "";
    host.className = "message";
  }, 4000);
}

// ------------------------------------------------------------- 动作

async function refreshState() {
  const data = await api("GET", "/api/state");
  applyState(data);
}

function applyState(data) {
  if (data.loaded) {
    state.loaded = true;
    state.path = data.path || "";
    state.records = data.records || [];
    state.fileRecords = data.file_records || [];
    if (
      state.selectedFile &&
      !state.fileRecords.some((record) => record.uuid === state.selectedFile)
    ) {
      state.selectedFile = null;
    }
    const known = new Set(state.records.map((record) => record.uuid));
    state.selection = new Set([...state.selection].filter((uuid) => known.has(uuid)));
    if (!state.anchor || !known.has(state.anchor)) state.anchor = null;
    state.tree = data.tree || { roots: [], total: 0, unfiled: 0 };
    if (data.scan !== undefined && data.scan !== null) state.scan = data.scan;
    state.loadError = "";
  } else {
    state.loaded = false;
    state.defaultPath = data.default_path || state.defaultPath;
    state.loadError = data.error || "";
    state.selection = new Set();
    state.anchor = null;
    state.fileRecords = [];
    state.selectedFile = null;
  }
}

async function rescan() {
  if (!state.loaded) return;
  try {
    const data = await api("POST", "/api/scan", {});
    state.scan = data.scan || null;
    state.lastScanAt = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    render();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function afterMutation() {
  try {
    await refreshState();
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (!state.loaded) {
    showSetup();
    render();
    return;
  }
  await rescan();
  render();
}

async function openPrimary(record) {
  const files = record.files || [];
  const linked = linkedFilesOf(record);
  const entries = (state.scan && state.scan[record.uuid]) || [];
  let index = entries.findIndex((entry) => entry.exists);
  const unknown = files.length > 0 && entries.length === 0;
  if (index < 0 && unknown) index = 0;
  if (index >= 0) {
    await openFileIndex(record, index);
    return;
  }
  const openable = linked.find((file) => file.exists !== false);
  if (openable) {
    await openFileRecord(openable, "source");
    return;
  }
  if (record.doi) {
    openDoi(record);
    return;
  }
  showMessage(files.length || linked.length ? "本地文件均缺失" : "没有本地文件，也没有 DOI", "error");
}

async function openFileIndex(record, index) {
  try {
    await api("POST", "/api/open", { uuid: record.uuid, index });
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function openDoi(record) {
  if (!record || !record.doi) {
    showMessage("该记录没有 DOI", "error");
    return;
  }
  window.open("https://doi.org/" + String(record.doi).trim(), "_blank", "noopener");
}

function openExternal(url) {
  const text = String(url || "").trim();
  if (!text) {
    showMessage("没有可打开的链接", "error");
    return;
  }
  window.open(text, "_blank", "noopener");
}

async function copyText(text) {
  if (!text) {
    showMessage("没有可复制的内容", "error");
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = el("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    showMessage("已复制", "info");
  } catch (error) {
    showMessage("复制失败：" + error.message, "error");
  }
}

// --------------------------------------------------------- 行右键菜单

let menuEl = null;

function hideRowMenu() {
  if (menuEl) {
    menuEl.remove();
    menuEl = null;
  }
}

function showRowMenu(x, y, record) {
  hideRowMenu();
  menuEl = el("div", "context-menu");
  menuEl.style.left = x + "px";
  menuEl.style.top = y + "px";
  const copyItem = el("div", "context-item", "复制 UUID");
  copyItem.addEventListener("click", () => {
    hideRowMenu();
    copyText(record.uuid);
  });
  const deleteItem = el("div", "context-item", "删除记录");
  deleteItem.addEventListener("click", async () => {
    hideRowMenu();
    if (!confirm("删除记录「" + (record.title || record.uuid) + "」？此操作只改库文件。")) return;
    try {
      await api("DELETE", "/api/records/" + encodeURIComponent(record.uuid));
      state.selection.delete(record.uuid);
      if (state.anchor === record.uuid) state.anchor = null;
      await afterMutation();
    } catch (error) {
      showMessage(error.message, "error");
    }
  });
  menuEl.appendChild(copyItem);
  menuEl.appendChild(deleteItem);
  document.body.appendChild(menuEl);
  setTimeout(() => document.addEventListener("click", hideRowMenu, { once: true }), 0);
}

// ------------------------------------------------------------ 首启页

function showSetup() {
  byId("setup").classList.remove("hidden");
  byId("setup-path").value = state.defaultPath || "./primer.literature.json";
  if (state.loadError) {
    byId("setup-note").textContent = "读取默认库文件失败：";
    byId("setup-error").textContent = state.loadError;
  } else {
    byId("setup-note").textContent = "未找到默认数据库文件（可新建，或指定一个已有的）。";
    byId("setup-error").textContent = "";
  }
}

async function submitSetup() {
  const path = byId("setup-path").value.trim();
  const checked = document.querySelector('input[name="setup-mode"]:checked');
  const mode = checked ? checked.value : "new";
  try {
    const data = await api("POST", "/api/library", { path, mode });
    applyState(data);
    byId("setup").classList.add("hidden");
    await rescan();
    render();
  } catch (error) {
    byId("setup-error").textContent = error.message;
  }
}

// ------------------------------------------------------------ 编辑器

function openEditor(record) {
  state.editingUuid = record ? record.uuid : null;
  state.bulkEdit = null;
  state.editorFiles = ((record && record.files) || []).map((file) => Object.assign({}, file));
  byId("editor-title").textContent = record ? "编辑记录" : "新建记录";
  byId("ed-show-all").checked = false;
  fillEditorForm(record);
  byId("ed-files-block").classList.remove("hidden");
  byId("ed-bulk-note").classList.add("hidden");
  byId("editor").classList.remove("hidden");
  byId("ed-title").focus();
}

function openBulkEditor(records) {
  const anchor = records.find((item) => item.uuid === state.anchor) || records[0];
  state.editingUuid = null;
  state.bulkEdit = { uuids: records.map((item) => item.uuid), anchorUuid: anchor.uuid };
  state.editorFiles = [];
  byId("editor-title").textContent = "批量编辑 " + records.length + " 条";
  byId("ed-show-all").checked = true;
  fillEditorForm(anchor);
  byId("ed-files-block").classList.add("hidden");
  byId("ed-bulk-note").classList.remove("hidden");
  byId("editor").classList.remove("hidden");
  byId("ed-title").focus();
}

function fillTypeOptions(current) {
  const select = byId("ed-type");
  select.textContent = "";
  const none = el("option", null, "（未指定）");
  none.value = "";
  select.appendChild(none);
  for (const [value, label] of Object.entries(TYPE_LABELS)) {
    const option = el("option", null, label);
    option.value = value;
    select.appendChild(option);
  }
  if (current && !(current in TYPE_LABELS)) {
    const option = el("option", null, "自定义：" + current);
    option.value = current;
    select.appendChild(option);
  }
  select.value = current || "";
}

function fillEditorForm(record) {
  byId("ed-title").value = record ? record.title || "" : "";
  byId("ed-authors").value = record && record.authors ? record.authors.join("\n") : "";
  byId("ed-year").value = record && record.year ? String(record.year) : "";
  fillTypeOptions(record ? record.type || "" : "");
  byId("ed-venue").value = record ? record.venue || "" : "";
  byId("ed-doi").value = record ? record.doi || "" : "";
  for (const field of TEXT_FIELDS) {
    byId("ed-" + field).value = record ? record[field] || "" : "";
  }
  byId("ed-editor").value = record && record.editor ? record.editor.join("\n") : "";
  byId("ed-translator").value = record && record.translator ? record.translator.join("\n") : "";
  byId("ed-notes").value = record ? record.notes || "" : "";
  byId("ed-error").textContent = "";
  renderEditorFiles();
  updateEditorFields();
}

function editorFieldValue(field) {
  const input = byId("ed-" + field);
  return input ? String(input.value || "").trim() : "";
}

function updateEditorFields() {
  const type = byId("ed-type").value;
  const showAll = byId("ed-show-all").checked;
  const specific = TYPE_FIELDS[type];
  let hiddenFilled = 0;
  for (const label of document.querySelectorAll("#editor .form-grid label[data-field]")) {
    const field = label.dataset.field;
    const visible =
      COMMON_FIELDS.has(field) || showAll || specific === undefined || specific.includes(field);
    label.classList.toggle("hidden", !visible);
    if (!visible && editorFieldValue(field)) hiddenFilled += 1;
  }
  const note = byId("ed-hidden-note");
  if (hiddenFilled) {
    note.textContent =
      "另有 " + hiddenFilled + " 个字段有值但当前类型未显示（勾选「显示全部字段」可编辑）。";
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }
}

function renderEditorFiles() {
  const host = byId("ed-files");
  host.textContent = "";
  state.editorFiles.forEach((file, index) => {
    const row = el("div", "file-edit-row");
    const pathInput = el("input", "file-edit-path");
    pathInput.type = "text";
    pathInput.placeholder = "相对库文件的路径，如 参考资料/参考文献原文/a.pdf";
    pathInput.value = file.path || "";
    pathInput.addEventListener("input", () => {
      file.path = pathInput.value;
    });
    const natureSelect = el("select", "file-edit-nature");
    for (const [value, label] of NATURES) {
      const option = el("option", null, label);
      option.value = value;
      natureSelect.appendChild(option);
    }
    natureSelect.value = file.nature || "other";
    natureSelect.addEventListener("change", () => {
      file.nature = natureSelect.value;
    });
    const removeButton = button("移除", () => {
      state.editorFiles.splice(index, 1);
      renderEditorFiles();
    });
    row.appendChild(pathInput);
    row.appendChild(natureSelect);
    row.appendChild(removeButton);
    host.appendChild(row);
  });
}

async function saveEditor() {
  const errorHost = byId("ed-error");
  errorHost.textContent = "";
  const title = byId("ed-title").value.trim();
  if (!title) {
    errorHost.textContent = "标题不能为空";
    return;
  }
  const yearText = byId("ed-year").value.trim();
  let year = null;
  if (yearText) {
    year = Number(yearText);
    if (!Number.isInteger(year)) {
      errorHost.textContent = "年份需为整数";
      return;
    }
  }
  const authors = byId("ed-authors").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const editor = byId("ed-editor").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const translator = byId("ed-translator").value.split("\n").map((line) => line.trim()).filter(Boolean);
  const type = byId("ed-type").value.trim();
  const venue = byId("ed-venue").value.trim();
  const doi = byId("ed-doi").value.trim();
  const notes = byId("ed-notes").value;
  const texts = {};
  for (const field of TEXT_FIELDS) texts[field] = editorFieldValue(field);

  if (state.bulkEdit) {
    const anchor = state.records.find((item) => item.uuid === state.bulkEdit.anchorUuid);
    if (!anchor) {
      errorHost.textContent = "基准记录已不存在，请关闭后重开编辑窗口";
      return;
    }
    // 批量模式：只把相对基准记录改动过的字段应用到全部选中记录。
    const fields = {};
    if (title !== (anchor.title || "")) fields.title = title;
    if (JSON.stringify(authors) !== JSON.stringify(anchor.authors || [])) fields.authors = authors;
    if (JSON.stringify(editor) !== JSON.stringify(anchor.editor || [])) fields.editor = editor;
    if (JSON.stringify(translator) !== JSON.stringify(anchor.translator || [])) {
      fields.translator = translator;
    }
    if (year !== (anchor.year ?? null)) fields.year = year;
    if (type !== (anchor.type || "")) fields.type = type;
    if (venue !== (anchor.venue || "")) fields.venue = venue;
    if ((doi || "") !== (anchor.doi || "")) fields.doi = doi || null;
    if (notes !== (anchor.notes || "")) fields.notes = notes;
    for (const field of TEXT_FIELDS) {
      if (texts[field] !== (anchor[field] || "")) fields[field] = texts[field];
    }
    if (!Object.keys(fields).length) {
      errorHost.textContent = "没有修改任何字段";
      return;
    }
    try {
      const data = await api("POST", "/api/records/bulk-update", {
        uuids: state.bulkEdit.uuids,
        fields,
      });
      const updated = data.updated || 0;
      byId("editor").classList.add("hidden");
      state.bulkEdit = null;
      await afterMutation();
      showMessage("已批量更新 " + updated + " 条记录", "info");
    } catch (error) {
      errorHost.textContent = error.message;
    }
    return;
  }

  const original = state.editingUuid
    ? state.records.find((item) => item.uuid === state.editingUuid)
    : null;
  // 整条回传（含未知字段），只覆盖表单里编辑的字段——未知字段因此得以保留。
  const payload = original ? JSON.parse(JSON.stringify(original)) : {};
  payload.title = title;
  payload.authors = authors;
  payload.editor = editor;
  payload.translator = translator;
  payload.year = year;
  payload.type = type;
  payload.venue = venue;
  payload.doi = doi || null;
  payload.notes = notes;
  for (const field of TEXT_FIELDS) payload[field] = texts[field];
  payload.files = state.editorFiles
    .map((file) => Object.assign({}, file, { path: String(file.path || "").trim() }))
    .filter((file) => file.path);
  try {
    if (state.editingUuid) {
      await api("PUT", "/api/records/" + encodeURIComponent(state.editingUuid), payload);
      selectSingle(state.editingUuid);
    } else {
      const data = await api("POST", "/api/records", payload);
      if (data.record) selectSingle(data.record.uuid);
    }
    byId("editor").classList.add("hidden");
    await afterMutation();
  } catch (error) {
    errorHost.textContent = error.message;
  }
}

// ------------------------------------------------ 文件菜单与文件对话框

let fileAction = null;

function updateFileMenu() {
  for (const item of document.querySelectorAll("#file-menu .dropdown-item")) {
    const needsLibrary = item.dataset.action !== "open";
    item.classList.toggle("disabled", needsLibrary && !state.loaded);
  }
}

function suggestCopyPath(suffix) {
  const current = state.path || state.defaultPath || "primer.literature.json";
  const stem = current.toLowerCase().endsWith(".json") ? current.slice(0, -5) : current;
  return stem + suffix + ".json";
}

function openFileDialog(options) {
  fileAction = options.action;
  byId("fd-title").textContent = options.title;
  byId("fd-note").textContent = options.note || "";
  byId("fd-path").value = options.value || "";
  byId("fd-error").textContent = "";
  const scopeRow = byId("fd-scope-row");
  if (options.scope) {
    scopeRow.classList.remove("hidden");
    byId("fd-scope").checked = Boolean(options.scopeDefault);
  } else {
    scopeRow.classList.add("hidden");
  }
  byId("file-dialog").classList.remove("hidden");
  byId("fd-path").focus();
  byId("fd-path").select();
}

async function submitFileDialog() {
  if (!fileAction) return;
  const path = byId("fd-path").value.trim();
  const scope = byId("fd-scope").checked;
  try {
    await fileAction(path, scope);
    byId("file-dialog").classList.add("hidden");
  } catch (error) {
    byId("fd-error").textContent = error.message;
  }
}

function menuOpen() {
  if (!byId("editor").classList.contains("hidden")) {
    if (!confirm("编辑器里有未保存的改动，继续将丢弃它们。")) return;
  }
  openFileDialog({
    title: "打开数据库",
    note: "载入一份库 JSON（相对启动目录或绝对路径）；切换后当前选中会清空。",
    value: state.path || state.defaultPath || "./primer.literature.json",
    action: async (path) => {
      const data = await api("POST", "/api/library", { path, mode: "open" });
      applyState(data);
      clearSelection();
      byId("editor").classList.add("hidden");
      await rescan();
      render();
      showMessage("已打开：" + path, "info");
    },
  });
}

function menuSave() {
  if (!state.loaded) return;
  api("POST", "/api/save")
    .then((data) => showMessage("已保存：" + data.path, "info"))
    .catch((error) => showMessage(error.message, "error"));
}

function menuSaveAs() {
  if (!state.loaded) return;
  openFileDialog({
    title: "另存为",
    note: "写到新文件并切换为当前库（目标已存在时会拒绝）。",
    value: suggestCopyPath("-copy"),
    action: async (path) => {
      const data = await api("POST", "/api/save-as", { path });
      applyState(data);
      await rescan();
      render();
      showMessage("已另存为：" + path, "info");
    },
  });
}

function menuImport() {
  if (!state.loaded) return;
  byId("import-file").click();
}

async function apiUpload(path, file) {
  const response = await fetch(path, { method: "POST", body: file });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    let message = data.message || "HTTP " + response.status;
    if (typeof message === "string" && message.startsWith("no route:")) {
      message =
        "服务端进程是旧版本（缺少该接口）。请重启 web 服务后重试：Ctrl-C 停掉，" +
        "并在原目录重新运行 python -m primer.literature web。";
    }
    throw new Error(message);
  }
  return data;
}

async function handleImportFile(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  let preview;
  try {
    preview = await apiUpload("/api/import/parse?name=" + encodeURIComponent(file.name), file);
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (preview.kind === "library") {
    if (!confirm("检测到库格式（共 " + preview.total + " 条记录），将直接合并；uuid 冲突的记录自动重新分配。继续？")) {
      return;
    }
    try {
      const data = await api("POST", "/api/import/commit", { token: preview.token });
      await finishImport(data);
    } catch (error) {
      showMessage(error.message, "error");
    }
    return;
  }
  openMappingDialog(file.name, preview);
}

async function finishImport(data) {
  await afterMutation();
  const parts = ["已导入 " + data.imported + " 条"];
  if (data.renamed) parts.push(data.renamed + " 条 uuid 冲突重分配");
  if (data.skipped) parts.push(data.skipped + " 条跳过（缺标题）");
  showMessage(parts.join("，"), "info");
}

const FIELD_LABELS = {
  title: "标题 *",
  authors: "作者",
  editor: "编者",
  translator: "译者",
  year: "年份",
  type: "类型",
  venue: "来源",
  volume: "卷",
  number: "期号",
  pages: "页码",
  eid: "文章号",
  publisher: "出版者",
  location: "出版地",
  institution: "机构",
  organization: "主办",
  series: "丛书",
  edition: "版次",
  isbn: "ISBN",
  issn: "ISSN",
  url: "链接",
  download_url: "下载链接",
  eprint: "arXiv",
  eventtitle: "会议",
  eventdate: "会期",
  keywords: "关键词",
  doi: "DOI",
  projects: "项目",
  notes: "备注",
};

let mappingState = null;

function updateMappingUI(preview) {
  mappingState.preview = preview;
  byId("mg-note").textContent =
    "来源：" + mappingState.filename + "（共 " + preview.total + " 条）。请确认各列对应的库字段；标题必填，缺标题的行会被跳过。";
  const scope = byId("mg-scope");
  if (scope && scope.options.length) {
    scope.options[0].textContent = "范围：仅疑似条目（" + (preview.low_count || 0) + " 条）";
  }
  const host = byId("mapping-rows");
  host.textContent = "";
  for (const column of preview.columns) {
    const row = el("div", "mapping-row");
    row.dataset.column = column;
    row.appendChild(el("div", "mapping-col", column));
    row.appendChild(el("div", "mapping-sample", (preview.sample[column] || []).join(" ｜ ")));
    const select = el("select", "mapping-target");
    const ignoreOption = el("option", null, "（忽略）");
    ignoreOption.value = "";
    select.appendChild(ignoreOption);
    for (const [key, label] of Object.entries(FIELD_LABELS)) {
      const option = el("option", null, label);
      option.value = key;
      select.appendChild(option);
    }
    const suggested = preview.suggested[column];
    select.value = suggested && suggested !== "ignore" ? suggested : "";
    row.appendChild(select);
    host.appendChild(row);
  }
}

function openMappingDialog(filename, preview) {
  mappingState = { token: preview.token, filename };
  byId("mg-error").textContent = "";
  byId("mg-refine-status").textContent = "";
  byId("mg-refine").disabled = false;
  byId("mg-verify").disabled = false;
  byId("mg-ok").disabled = false;
  updateMappingUI(preview);
  byId("mapping-dialog").classList.remove("hidden");
}

async function refineImport() {
  await startEnrich("/api/import/refine", "AI 解析");
}

async function verifyImport() {
  await startEnrich("/api/import/verify", "联网比对");
}

async function startEnrich(endpoint, label) {
  if (!mappingState) return;
  const scope = byId("mg-scope").value;
  const statusHost = byId("mg-refine-status");
  byId("mg-refine").disabled = true;
  byId("mg-verify").disabled = true;
  byId("mg-ok").disabled = true;
  statusHost.textContent = label + "启动中…";
  let started;
  try {
    started = await api("POST", endpoint, { token: mappingState.token, scope });
  } catch (error) {
    statusHost.textContent = error.message;
    byId("mg-refine").disabled = false;
    byId("mg-verify").disabled = false;
    byId("mg-ok").disabled = false;
    return;
  }
  if (!started.started) {
    statusHost.textContent =
      started.reason === "no-suspicious"
        ? "未发现疑似条目（可把范围改为“全部条目”）"
        : "未启动";
    byId("mg-refine").disabled = false;
    byId("mg-verify").disabled = false;
    byId("mg-ok").disabled = false;
    return;
  }
  statusHost.textContent = label + "中… 0/" + started.total;
  await pollEnrich(label);
}

async function pollEnrich(label) {
  const statusHost = byId("mg-refine-status");
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    if (!mappingState) return;
    let status;
    try {
      status = await api("POST", "/api/import/refine/status", { token: mappingState.token });
    } catch (error) {
      statusHost.textContent = "查询失败：" + error.message;
      byId("mg-refine").disabled = false;
      byId("mg-verify").disabled = false;
      byId("mg-ok").disabled = false;
      return;
    }
    if (status.status === "running") {
      statusHost.textContent = label + "中… " + status.done + "/" + status.total;
      continue;
    }
    byId("mg-refine").disabled = false;
    byId("mg-verify").disabled = false;
    byId("mg-ok").disabled = false;
    if (status.status === "failed") {
      statusHost.textContent = "失败：" + status.error;
      return;
    }
    if (status.preview) updateMappingUI(status.preview);
    statusHost.textContent =
      "完成：更新 " + status.refined + " 条，保留原值 " + status.failed + " 条；建议映射已刷新，请复核后导入。";
    showMessage(label + "完成", "info");
    return;
  }
}

async function submitMapping() {
  if (!mappingState) return;
  const mapping = {};
  for (const row of document.querySelectorAll("#mapping-rows .mapping-row")) {
    const select = row.querySelector("select");
    if (select && select.value) mapping[row.dataset.column] = select.value;
  }
  if (!Object.values(mapping).includes("title")) {
    byId("mg-error").textContent = "请把某一列映射为「标题」（必填）";
    return;
  }
  try {
    const data = await api("POST", "/api/import/commit", {
      token: mappingState.token,
      mapping,
    });
    byId("mapping-dialog").classList.add("hidden");
    mappingState = null;
    await finishImport(data);
  } catch (error) {
    byId("mg-error").textContent = error.message;
  }
}

function menuExport() {
  if (!state.loaded) return;
  const visibleUuids = visibleRecords().map((record) => record.uuid);
  openFileDialog({
    title: "导出",
    note: "写出库 JSON 的拷贝；不切换当前库。",
    value: suggestCopyPath("-export"),
    scope: true,
    action: async (path, scope) => {
      const payload = { path };
      if (scope) payload.uuids = visibleUuids;
      const data = await api("POST", "/api/export", payload);
      showMessage("已导出 " + data.exported + " 条：" + data.path, "info");
    },
  });
}

const FILE_ACTIONS = {
  open: menuOpen,
  save: menuSave,
  "save-as": menuSaveAs,
  import: menuImport,
  "scan-folder": menuScanFolder,
  "add-files": menuAddFiles,
  "auto-link": autoLink,
  export: menuExport,
};

async function menuScanFolder() {
  if (!state.loaded) return;
  let picked;
  try {
    picked = await api("POST", "/api/files/pick", { kind: "folder" });
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (picked.canceled) return;
  const folder = (picked.paths || [])[0];
  if (!folder) return;
  await addFilePaths({ path: folder }, true);
}

async function menuAddFiles() {
  if (!state.loaded) return;
  let picked;
  try {
    picked = await api("POST", "/api/files/pick", { kind: "files" });
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (picked.canceled) return;
  const paths = picked.paths || [];
  if (!paths.length) return;
  await addFilePaths({ paths }, false);
}

async function addFilePaths(payload, isFolder) {
  try {
    showMessage(isFolder ? "扫描中…" : "添加中…", "info", true);
    const data = isFolder
      ? await api("POST", "/api/files/scan", payload)
      : await api("POST", "/api/files/add", payload);
    switchView("files");
    await refreshFiles();
    ensureFilePolling();
    showMessage(
      (function () {
        let message =
          "已登记 " + (data.added || 0) + " 个文件（跳过 " + (data.skipped || 0) + " 个已有";
        if (data.duplicates && data.duplicates.length) {
          const first = data.duplicates[0];
          message +=
            "；" +
            data.duplicates.length +
            " 个内容重复未登记，如「" +
            String(first.path || "").split("/").pop() +
            "」=「" +
            (first.same_as || "") +
            "」";
        }
        return message + "）；MinerU 解析已开始";
      })(),
      "info"
    );
  } catch (error) {
    showMessage(error.message, "error");
  }
}

// ------------------------------------------------ 批量自动关联（文件 × 文献）

let lbPhase = "options"; // options | running | results
let lbToken = null;
let lbResult = null; // {proposals, missing, stats}
let lbTab = "pairs"; // pairs | missing
let lbChecked = new Set();
let lbIgnored = new Set();
let lbOverride = new Map();
let lbPolling = false;
let lbDone = 0;
let lbTotal = 0;

function autoLink() {
  if (!state.loaded) return;
  if (!(lbPhase === "running" || (lbPhase === "results" && lbResult))) {
    lbPhase = "options";
    lbResult = null;
    lbToken = null;
  }
  byId("lb-error").textContent = "";
  renderLinkBatch();
  byId("linkbatch-dialog").classList.remove("hidden");
  if (lbPhase === "running") pollLinkBatch();
}

function renderLinkBatch() {
  const host = byId("lb-body");
  host.textContent = "";
  const primary = byId("lb-primary");
  if (lbPhase === "options") {
    primary.textContent = "开始匹配";
    primary.disabled = false;
    const parsed = (state.fileRecords || []).filter(
      (item) => item.status === "done" && item.md_path
    );
    const unlinked = parsed.filter((item) => !item.record_uuid).length;
    const row = el("div", "radio-row");
    const makeChoice = (value, label, checked) => {
      const wrap = el("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "lb-scope";
      radio.value = value;
      radio.checked = checked;
      wrap.appendChild(radio);
      wrap.appendChild(document.createTextNode(" " + label));
      row.appendChild(wrap);
    };
    makeChoice("unlinked", "仅未关联文件（" + unlinked + " 个）", true);
    makeChoice("all", "全部已解析文件（" + parsed.length + " 个）", false);
    host.appendChild(row);
    const net = el("label", "check-row");
    const netBox = document.createElement("input");
    netBox.type = "checkbox";
    netBox.id = "lb-online";
    netBox.checked = true;
    net.appendChild(netBox);
    net.appendChild(
      document.createTextNode(
        " 联网增强：对本地未命中的文件，用 DOI／题名查学术引擎链再比一轮（会访问外网、消耗额度）"
      )
    );
    host.appendChild(net);
    host.appendChild(
      el(
        "p",
        "hint",
        "在本地进行：从解析产物 markdown 抽取题名，与全部文献记录比对（标识符／题名／包容判定）；不改库，点「应用选中关联」才写入。"
      )
    );
    host.appendChild(
      el("p", "hint", "「缺文清单」＝尚未找到本地文件的文献记录，用于筛查还需要获取哪些文献。")
    );
    return;
  }
  if (lbPhase === "running") {
    primary.textContent = "匹配中…";
    primary.disabled = true;
    const box = el("div", "lb-progress");
    box.id = "lb-progress";
    box.textContent = "已评估 " + lbDone + "/" + lbTotal + " …";
    host.appendChild(box);
    host.appendChild(el("p", "hint", "匹配在本地进行；可以关闭本对话框，任务会在后台跑完。"));
    return;
  }
  const result = lbResult || { proposals: [], missing: [], stats: {} };
  const proposals = result.proposals || [];
  const missing = result.missing || [];
  const stats = result.stats || {};
  primary.textContent = "应用选中关联";
  host.appendChild(
    el(
      "p",
      "lb-stats",
      "提案 " + proposals.length + " 条（确定 " + (stats.strong || 0) + " ｜ 建议 " +
        (stats.suggest || 0) + " ｜ 存疑 " + (stats.weak || 0) + "）；缺文清单 " +
        missing.length + " 条" +
        (stats.skipped_linked ? "；已关联跳过 " + stats.skipped_linked + " 个文件" : "") +
        (stats.skipped_unparsed ? "；未解析跳过 " + stats.skipped_unparsed + " 个文件" : "")
    )
  );
  const tabs = el("div", "tabs");
  const tabPairs = el("button", "tab" + (lbTab === "pairs" ? " active" : ""), "配对提案 " + proposals.length);
  tabPairs.type = "button";
  tabPairs.addEventListener("click", () => {
    lbTab = "pairs";
    renderLinkBatch();
  });
  const tabMissing = el("button", "tab" + (lbTab === "missing" ? " active" : ""), "缺文清单 " + missing.length);
  tabMissing.type = "button";
  tabMissing.addEventListener("click", () => {
    lbTab = "missing";
    renderLinkBatch();
  });
  tabs.appendChild(tabPairs);
  tabs.appendChild(tabMissing);
  host.appendChild(tabs);
  if (lbTab === "pairs") renderLinkBatchPairs(host, proposals);
  else renderLinkBatchMissing(host, missing);
}

function linkBatchEvidence(item) {
  const labels = {
    doi: "DOI 一致",
    eprint: "arXiv 一致",
    containment: "包容判定",
    "containment-multi": "包容多义",
    online: "联网补全",
  };
  const how = labels[item.how] || "题名";
  const ratio = item.how === "title" || item.how === "containment-multi" ? " " + item.ratio.toFixed(3) : "";
  return how + ratio + (item.tie ? "（并列）" : "");
}

function renderLinkBatchPairs(host, proposals) {
  const toolbar = el("div", "lb-toolbar");
  toolbar.appendChild(
    button("全选确定级", () => {
      for (const item of proposals) if (item.tier === "strong") lbChecked.add(item.file_uuid);
      renderLinkBatch();
    })
  );
  toolbar.appendChild(
    button("全选确定＋建议", () => {
      for (const item of proposals) if (item.tier !== "weak") lbChecked.add(item.file_uuid);
      renderLinkBatch();
    })
  );
  toolbar.appendChild(
    button("全不选", () => {
      lbChecked = new Set();
      renderLinkBatch();
    })
  );
  toolbar.appendChild(el("span", "hint", "勾选＝应用后挂链；「换选」改目标记录，「忽略」只影响本轮。"));
  host.appendChild(toolbar);
  if (!proposals.length) {
    host.appendChild(el("p", "hint", "没有可提案的文件——都已有归属或没有可信候选。"));
    updateLinkBatchPrimary();
    return;
  }
  const list = el("div", "lb-list");
  for (const item of proposals) {
    const ignored = lbIgnored.has(item.file_uuid);
    const row = el("div", "lb-row" + (ignored ? " ignored" : ""));
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = lbChecked.has(item.file_uuid) && !ignored;
    box.addEventListener("change", () => {
      if (box.checked) lbChecked.add(item.file_uuid);
      else lbChecked.delete(item.file_uuid);
      updateLinkBatchPrimary();
    });
    row.appendChild(box);
    const main = el("div", "lb-main");
    main.appendChild(el("div", "lb-file", item.name || item.file_uuid));
    const override = lbOverride.get(item.file_uuid);
    const targetTitle = override ? override.title : item.record_title;
    main.appendChild(
      el(
        "div",
        "lb-target",
        "→ " + (targetTitle || "（无标题）") + (item.record_year ? "（" + item.record_year + "）" : "")
      )
    );
    row.appendChild(main);
    const tierLabels = { strong: "确定", suggest: "建议", weak: "存疑" };
    row.appendChild(el("span", "lb-badge " + item.tier, tierLabels[item.tier] || item.tier));
    row.appendChild(el("span", "lb-evidence", linkBatchEvidence(item)));
    const actions = el("div", "lb-actions");
    actions.appendChild(button("换选", () => pickLinkBatchRecord(item)));
    actions.appendChild(
      button("忽略", () => {
        lbIgnored.add(item.file_uuid);
        lbChecked.delete(item.file_uuid);
        renderLinkBatch();
      })
    );
    row.appendChild(actions);
    list.appendChild(row);
  }
  host.appendChild(list);
  updateLinkBatchPrimary();
}

function pickLinkBatchRecord(item) {
  openRecordPicker(
    "为「" + (item.name || "文件") + "」换选文献记录",
    "在提案之外重新挑选目标记录（搜索标题或 DOI）。",
    (recordUuid, title) => {
      lbOverride.set(item.file_uuid, { uuid: recordUuid, title });
      lbChecked.add(item.file_uuid);
      renderLinkBatch();
    }
  );
}

function updateLinkBatchPrimary() {
  if (lbPhase !== "results") return;
  const primary = byId("lb-primary");
  let count = 0;
  for (const item of (lbResult && lbResult.proposals) || []) {
    if (lbChecked.has(item.file_uuid) && !lbIgnored.has(item.file_uuid)) count += 1;
  }
  primary.textContent = count ? "应用选中关联（" + count + "）" : "应用选中关联";
  primary.disabled = count === 0;
}

function renderLinkBatchMissing(host, missing) {
  const toolbar = el("div", "lb-toolbar");
  toolbar.appendChild(
    button("复制清单（TSV）", async () => {
      const lines = ["题名\t年份\t来源\t最像的文件\t相似度"];
      for (const item of missing) {
        lines.push(
          [item.title || "", item.year || "", item.venue || "", item.closest_file || "", item.closest_ratio || ""].join("\t")
        );
      }
      await copyText(lines.join("\n"));
    })
  );
  toolbar.appendChild(
    button("导出 CSV…", () => {
      openFileDialog({
        title: "导出缺文清单",
        note: "把当前缺文清单写成 CSV 文件（UTF-8；目标已存在时会拒绝）。",
        value: "缺失本地文件清单.csv",
        action: async (path) => {
          const data = await api("POST", "/api/links/batch/export", { kind: "missing", path });
          showMessage("已导出 " + data.exported + " 条：" + data.path, "info");
        },
      });
    })
  );
  toolbar.appendChild(
    el("span", "hint", "这些文献记录尚未找到本地文件（供获取参考）；「疑似有文件」＝有高相似文件但未达配对门槛，建议人工核。")
  );
  host.appendChild(toolbar);
  if (!missing.length) {
    host.appendChild(el("p", "hint", "没有缺文记录。"));
    return;
  }
  const list = el("div", "lb-list");
  for (const item of missing) {
    const row = el("div", "lb-row");
    const main = el("div", "lb-main");
    main.appendChild(el("div", "lb-file", item.title || "（无标题）"));
    const parts = [];
    if (item.year) parts.push(String(item.year));
    if (item.venue) parts.push(item.venue);
    if (item.closest_file) parts.push("最像：" + item.closest_file + "（" + item.closest_ratio + "）");
    main.appendChild(el("div", "lb-target", parts.join(" · ")));
    row.appendChild(main);
    if (item.closest_ratio >= 0.8) row.appendChild(el("span", "lb-suspect", "疑似有文件"));
    list.appendChild(row);
  }
  host.appendChild(list);
}

async function startLinkBatch() {
  byId("lb-error").textContent = "";
  const scopeNode = document.querySelector("input[name='lb-scope']:checked");
  const scope = scopeNode ? scopeNode.value : "unlinked";
  const onlineNode = byId("lb-online");
  const online = onlineNode ? onlineNode.checked : false;
  let started;
  try {
    started = await api("POST", "/api/links/batch/start", { scope, online });
  } catch (error) {
    byId("lb-error").textContent = error.message;
    return;
  }
  lbToken = started.token;
  lbDone = 0;
  lbTotal = started.total;
  lbPhase = "running";
  renderLinkBatch();
  pollLinkBatch();
}

async function pollLinkBatch() {
  if (lbPolling) return;
  lbPolling = true;
  try {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      let status;
      try {
        status = await api("POST", "/api/links/batch/status", { token: lbToken });
      } catch (error) {
        byId("lb-error").textContent = error.message;
        lbPhase = "options";
        renderLinkBatch();
        return;
      }
      if (status.status === "running") {
        lbDone = status.done;
        lbTotal = status.total;
        const box = byId("lb-progress");
        if (box) box.textContent = "已评估 " + status.done + "/" + status.total + " …";
        continue;
      }
      if (status.status === "failed") {
        byId("lb-error").textContent = "匹配失败：" + status.error;
        lbPhase = "options";
        renderLinkBatch();
        return;
      }
      lbResult = status.result || { proposals: [], missing: [], stats: {} };
      lbPhase = "results";
      lbTab = "pairs";
      lbChecked = new Set();
      lbIgnored = new Set();
      lbOverride = new Map();
      for (const item of lbResult.proposals || []) {
        if (item.tier === "strong" || item.tier === "suggest") lbChecked.add(item.file_uuid);
      }
      renderLinkBatch();
      showMessage(
        "自动关联完成：提案 " + (lbResult.proposals || []).length + " 条，缺文 " +
          (lbResult.missing || []).length + " 条",
        "info"
      );
      return;
    }
  } finally {
    lbPolling = false;
  }
}

async function applyLinkBatch() {
  if (!lbResult) return;
  const pairs = [];
  for (const item of lbResult.proposals || []) {
    if (!lbChecked.has(item.file_uuid) || lbIgnored.has(item.file_uuid)) continue;
    const override = lbOverride.get(item.file_uuid);
    pairs.push({
      file_uuid: item.file_uuid,
      record_uuid: override ? override.uuid : item.record_uuid,
      nature: item.nature,
    });
  }
  if (!pairs.length) return;
  byId("lb-error").textContent = "";
  byId("lb-primary").disabled = true;
  try {
    const data = await api("POST", "/api/links/batch/apply", { pairs });
    showMessage(
      "已关联 " + data.linked + " 个文件" + (data.replaced ? "（改挂 " + data.replaced + " 个）" : ""),
      "info"
    );
    lbPhase = "options";
    lbResult = null;
    lbToken = null;
    byId("linkbatch-dialog").classList.add("hidden");
    await refreshFiles();
    renderList();
    renderDetail();
  } catch (error) {
    byId("lb-error").textContent = error.message;
    byId("lb-primary").disabled = false;
  }
}

function switchView(view) {
  state.view = view;
  byId("tab-records").classList.toggle("active", view === "records");
  byId("tab-files").classList.toggle("active", view === "files");
  renderList();
  renderDetail();
}

async function refreshFiles() {
  if (!state.loaded) return;
  try {
    const data = await api("GET", "/api/files");
    state.fileRecords = data.files || [];
    state.filePolling = data.parsing || 0;
    if (state.view === "files") {
      renderList();
      renderDetail();
    }
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function ensureFilePolling() {
  if (state.filePollTimer) {
    clearTimeout(state.filePollTimer);
    state.filePollTimer = null;
  }
  if (!state.filePolling) return;
  state.filePollTimer = setTimeout(async () => {
    state.filePollTimer = null;
    await refreshFiles();
    ensureFilePolling();
  }, 2000);
}

const EDIT_ACTIONS = {
  lookup: enrichSelectedVerify,
  ai: enrichSelectedAi,
  "enrich-all": openEnrichAllDialog,
  "edit-fields": editFields,
  "add-to-project": editAttachProject,
  delete: editDelete,
  clear: editClear,
};

function updateEditMenu() {
  const count = state.selection.size;
  const blocked = state.enriching || !state.loaded || count === 0;
  for (const item of document.querySelectorAll("#edit-menu .dropdown-item")) {
    if (item.dataset.action === "clear" || item.dataset.action === "enrich-all") {
      item.classList.toggle("disabled", !state.loaded || state.enriching || !state.records.length);
    } else {
      item.classList.toggle("disabled", blocked);
    }
  }
}

function editFields() {
  const records = selectedRecords();
  if (!records.length) {
    showMessage("请先在清单里选中记录", "error");
    return;
  }
  if (records.length === 1) openEditor(records[0]);
  else openBulkEditor(records);
}

function editAttachProject() {
  const records = selectedRecords();
  if (!records.length) {
    showMessage("请先在清单里选中记录", "error");
    return;
  }
  openProjectDialog({
    action: "attach",
    title: "加入项目",
    note:
      "把选中的 " + records.length + " 条记录加入目标项目；路径不存在会自动创建，已在该项目下的记录跳过。",
    target: true,
    targetOptions: projectPaths(),
  });
}

function enrichSelectedVerify() {
  return enrichSelected("verify");
}

function enrichSelectedAi() {
  return enrichSelected("ai");
}

async function enrichSelected(mode) {
  const records = selectedRecords();
  if (!records.length) {
    showMessage("请先在清单里选中记录", "error");
    return;
  }
  await enrichUuids(
    records.map((record) => record.uuid),
    mode,
    mode === "verify" ? "联网查询" : "AI 解析"
  );
}

function openEnrichAllDialog() {
  if (!state.loaded || state.enriching || !state.records.length) return;
  const host = byId("ea-scopes");
  host.textContent = "";
  const all = state.records.length;
  const noUrl = state.records.filter((record) => !(record.download_url || "").trim()).length;
  const noKey = state.records.filter(
    (record) => !record.year || !record.venue || !record.doi || !(record.authors || []).length
  ).length;
  const makeChoice = (value, label, checked) => {
    const wrap = el("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "ea-scope";
    radio.value = value;
    radio.checked = checked;
    wrap.appendChild(radio);
    wrap.appendChild(document.createTextNode(" " + label));
    host.appendChild(wrap);
  };
  makeChoice("all", "全部记录（" + all + " 条）", true);
  makeChoice("no-url", "仅缺下载链接（" + noUrl + " 条）", false);
  makeChoice("no-key", "仅缺关键字段（" + noKey + " 条）", false);
  byId("ea-error").textContent = "";
  byId("enrich-all-dialog").classList.remove("hidden");
}

async function startEnrichAll() {
  const node = document.querySelector("input[name='ea-scope']:checked");
  const scope = node ? node.value : "all";
  let uuids;
  if (scope === "no-url") {
    uuids = state.records
      .filter((record) => !(record.download_url || "").trim())
      .map((record) => record.uuid);
  } else if (scope === "no-key") {
    uuids = state.records
      .filter(
        (record) => !record.year || !record.venue || !record.doi || !(record.authors || []).length
      )
      .map((record) => record.uuid);
  } else {
    uuids = state.records.map((record) => record.uuid);
  }
  if (!uuids.length) {
    byId("ea-error").textContent = "没有符合条件的记录";
    return;
  }
  byId("enrich-all-dialog").classList.add("hidden");
  await enrichUuids(uuids, "verify", "全集联网比对");
}

async function enrichUuids(uuids, mode, label) {
  if (state.enriching) return;
  let started;
  try {
    started = await api("POST", "/api/records/enrich", { uuids, mode });
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  state.enriching = true;
  updateEditMenu();
  showMessage(label + "中… 0/" + started.total, "info", true);
  try {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      let status;
      try {
        status = await api("POST", "/api/records/enrich/status", { token: started.token });
      } catch (error) {
        showMessage("查询失败：" + error.message, "error");
        return;
      }
      if (status.status === "running") {
        showMessage(label + "中… " + status.done + "/" + status.total, "info", true);
        continue;
      }
      if (status.status === "failed") {
        showMessage(label + "失败：" + status.error, "error");
        return;
      }
      if (!status.updated) {
        showMessage(
          label + "完成：没有需要变更的字段（未变化/未命中 " + status.skipped +
            " 条，失败 " + status.failed + " 条）",
          "info"
        );
        return;
      }
      let preview;
      try {
        preview = await api("POST", "/api/records/enrich/preview", { token: started.token });
      } catch (error) {
        showMessage("获取变更预览失败：" + error.message, "error");
        return;
      }
      openPreviewDialog(label, started.token, preview);
      return;
    }
  } finally {
    state.enriching = false;
    updateEditMenu();
  }
}

let previewState = null;

function formatChangeValue(value) {
  if (value === null || value === undefined || value === "") return "（空）";
  if (Array.isArray(value)) return value.join("；");
  return String(value);
}

function openPreviewDialog(label, token, payload) {
  const pending = payload.pending || [];
  let totalChanges = 0;
  for (const item of pending) totalChanges += (item.changes || []).length;
  previewState = { token, label };
  byId("pv-title").textContent = label + " · 变更预览";
  const shown = pending.slice(0, 200);
  byId("pv-note").textContent =
    "共 " + pending.length + " 条记录、" + totalChanges + " 处变更；点「应用」才写入，取消则放弃。" +
    (pending.length > shown.length ? "（列表仅显示前 " + shown.length + " 条）" : "");
  const host = byId("pv-body");
  host.textContent = "";
  for (const item of shown) {
    const block = el("div", "pv-record");
    block.appendChild(el("div", "pv-name", item.title || item.uuid));
    for (const change of item.changes || []) {
      const row = el("div", "pv-change");
      row.appendChild(
        el("span", "pv-field", String(FIELD_LABELS[change.field] || change.field).replace(" *", ""))
      );
      row.appendChild(el("span", "pv-old", formatChangeValue(change.old)));
      row.appendChild(el("span", "pv-arrow", "→"));
      row.appendChild(el("span", "pv-new", formatChangeValue(change.new)));
      block.appendChild(row);
    }
    host.appendChild(block);
  }
  byId("pv-error").textContent = "";
  byId("preview-dialog").classList.remove("hidden");
}

async function applyPreview() {
  if (!previewState) return;
  const { token, label } = previewState;
  try {
    const data = await api("POST", "/api/records/enrich/apply", { token });
    byId("preview-dialog").classList.add("hidden");
    previewState = null;
    showMessage(label + "已应用：更新 " + (data.updated || 0) + " 条记录", "info");
    await refreshState();
    render();
  } catch (error) {
    byId("pv-error").textContent = error.message;
  }
}

async function editDelete() {
  const records = selectedRecords();
  if (!records.length) {
    showMessage("请先在清单里选中记录", "error");
    return;
  }
  const what =
    records.length === 1
      ? "记录「" + (records[0].title || records[0].uuid) + "」"
      : records.length + " 条记录";
  if (!confirm("删除" + what + "？此操作只改库文件。")) return;
  try {
    await api("POST", "/api/records/delete", {
      uuids: records.map((record) => record.uuid),
    });
    clearSelection();
    await afterMutation();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function editClear() {
  const total = state.records.length;
  if (!total) return;
  if (!confirm("清空整个库？共 " + total + " 条记录。")) return;
  if (!confirm("再确认一次：" + total + " 条记录将全部移除；库文件会先自动留一份 .bak。")) return;
  try {
    const data = await api("POST", "/api/library/clear");
    clearSelection();
    await afterMutation();
    showMessage("已清空 " + (data.cleared || 0) + " 条记录", "info");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

// ------------------------------------------------------- 项目菜单

const PROJECT_ACTIONS = {
  create: projectCreate,
  "attach-records": projectAttachRecords,
  move: projectMove,
  rename: projectRename,
  delete: projectDelete,
};

let projectAction = null;
let projectSource = "";

function updateProjectMenu() {
  const onNode = state.filter.kind === "node" && Boolean(state.filter.path);
  for (const item of document.querySelectorAll("#project-menu .dropdown-item")) {
    const action = item.dataset.action;
    let disabled = !state.loaded;
    if (action === "attach-records") {
      disabled = disabled || !onNode || state.selection.size === 0;
    } else if (action !== "create") {
      disabled = disabled || !onNode;
    }
    item.classList.toggle("disabled", disabled);
  }
}

function projectPaths() {
  const paths = [];
  const walk = (node) => {
    paths.push(node.path);
    for (const child of node.children || []) walk(child);
  };
  for (const root of state.tree.roots) walk(root);
  return paths;
}

function fillProjectParents(options, selected) {
  const select = byId("pd-parent");
  select.textContent = "";
  for (const value of options) {
    const option = el("option", null, value || "（顶层）");
    option.value = value;
    select.appendChild(option);
  }
  select.value = selected !== undefined && options.includes(selected) ? selected : "";
}

function fillProjectTargets(options) {
  const list = byId("pd-target-options");
  list.textContent = "";
  for (const value of options) {
    const option = el("option");
    option.value = value;
    list.appendChild(option);
  }
  byId("pd-target").value = "";
}

function openProjectDialog(options) {
  projectAction = options.action;
  projectSource = options.source || "";
  byId("pd-title").textContent = options.title;
  byId("pd-note").textContent = options.note || "";
  byId("pd-target-row").classList.toggle("hidden", !options.target);
  if (options.target) fillProjectTargets(options.targetOptions || []);
  byId("pd-parent-row").classList.toggle("hidden", !options.parent);
  if (options.parent) fillProjectParents(options.parentOptions, options.parentValue);
  byId("pd-name-row").classList.toggle("hidden", !options.name);
  if (options.name) byId("pd-name").value = options.nameValue || "";
  byId("pd-attach-row").classList.toggle("hidden", !options.attach);
  byId("pd-attach").checked = true;
  byId("pd-attach-count").textContent = String(options.attachCount || 0);
  byId("pd-error").textContent = "";
  byId("project-dialog").classList.remove("hidden");
  (options.name ? byId("pd-name") : options.target ? byId("pd-target") : byId("pd-parent")).focus();
}

async function submitProjectDialog() {
  if (!projectAction) return;
  const errorHost = byId("pd-error");
  errorHost.textContent = "";
  let data;
  try {
    if (projectAction === "create") {
      const parent = byId("pd-parent").value;
      const name = byId("pd-name").value.trim();
      if (!name) {
        errorHost.textContent = "名称不能为空";
        return;
      }
      const payload = { path: parent ? parent + "/" + name : name };
      if (!byId("pd-attach-row").classList.contains("hidden") && byId("pd-attach").checked) {
        payload.uuids = selectedRecords().map((record) => record.uuid);
      }
      data = await api("POST", "/api/projects/create", payload);
      showMessage(
        "已创建项目「" + data.path + "」" + (data.updated ? "，挂到 " + data.updated + " 条记录" : ""),
        "info"
      );
    } else if (projectAction === "move") {
      data = await api("POST", "/api/projects/move", {
        path: projectSource,
        target: byId("pd-parent").value,
      });
      showMessage(
        "已移动「" + projectSource + "」→「" + data.path + "」（更新 " + data.updated + " 条记录）",
        "info"
      );
    } else if (projectAction === "rename") {
      const name = byId("pd-name").value.trim();
      if (!name) {
        errorHost.textContent = "名称不能为空";
        return;
      }
      data = await api("POST", "/api/projects/rename", { path: projectSource, name });
      showMessage("已重命名为「" + data.path + "」（更新 " + data.updated + " 条记录）", "info");
    } else if (projectAction === "attach") {
      const target = byId("pd-target").value.trim();
      if (!target) {
        errorHost.textContent = "请填写目标项目";
        return;
      }
      data = await api("POST", "/api/projects/attach", {
        path: target,
        uuids: selectedRecords().map((record) => record.uuid),
      });
      showMessage(
        "已加入「" + data.path + "」：更新 " + data.updated + " 条记录" +
          (data.created ? "（新建了该项目）" : ""),
        "info"
      );
    }
  } catch (error) {
    errorHost.textContent = error.message;
    return;
  }
  byId("project-dialog").classList.add("hidden");
  projectAction = null;
  await refreshState();
  pruneFilter();
  render();
}

function pruneFilter() {
  if (state.filter.kind === "node" && !projectPaths().includes(state.filter.path)) {
    state.filter = { kind: "all" };
  }
}

function projectCreate() {
  openProjectDialog({
    action: "create",
    title: "新建项目",
    note: "可建顶层项目，也可在某项目下建子项目；项目声明的空项目记在库文件旁的 <库名>.projects.json。",
    parent: true,
    parentOptions: ["", ...projectPaths()],
    parentValue: state.filter.kind === "node" ? state.filter.path : "",
    name: true,
    nameValue: "",
    attach: state.selection.size > 0,
    attachCount: state.selection.size,
  });
}

async function projectAttachRecords() {
  const path = state.filter.path;
  const records = selectedRecords();
  if (!path || !records.length) {
    showMessage("请先在左栏点选项目、并在清单里选中记录", "error");
    return;
  }
  if (!confirm("把选中的 " + records.length + " 条记录加入「" + path + "」？")) return;
  try {
    const data = await api("POST", "/api/projects/attach", {
      path,
      uuids: records.map((record) => record.uuid),
    });
    showMessage("已加入「" + data.path + "」：更新 " + data.updated + " 条记录", "info");
    await refreshState();
    render();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function projectMove() {
  const source = state.filter.path;
  const candidates = [
    "",
    ...projectPaths().filter((path) => path !== source && !path.startsWith(source + "/")),
  ];
  openProjectDialog({
    action: "move",
    source,
    title: "移动项目",
    note: "把「" + source + "」及其子项目整体移动到：",
    parent: true,
    parentOptions: candidates,
    name: false,
  });
}

function projectRename() {
  const source = state.filter.path;
  openProjectDialog({
    action: "rename",
    source,
    title: "重命名项目",
    note: "当前：「" + source + "」（只改最后一段，子项目路径随之替换）",
    name: true,
    nameValue: source.split("/").pop(),
  });
}

async function projectDelete() {
  const path = state.filter.path;
  if (!path) return;
  if (!confirm("删除项目「" + path + "」及其全部子项目？相关记录的项目字段会移除这些路径。")) return;
  try {
    const data = await api("POST", "/api/projects/delete", { path });
    showMessage("已删除「" + path + "」（更新 " + data.updated + " 条记录）", "info");
    await refreshState();
    pruneFilter();
    render();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

// ------------------------------------------------------- 显示菜单（排序 / 筛选 / 重置）

const DISPLAY_ACTIONS = {
  sort: openSortDialog,
  filter: openFilterDialog,
  reset: displayReset,
};

function displaySortActive() {
  const stack = state.sortStack;
  return !(stack.length === 1 && stack[0].key === "year" && stack[0].dir === "desc");
}

function updateDisplayMenu() {
  for (const item of document.querySelectorAll("#display-menu .dropdown-item")) {
    let disabled = !state.loaded;
    if (item.dataset.action === "reset") {
      const dirty =
        displaySortActive() ||
        activeFilters().length > 0 ||
        state.search.trim() !== "" ||
        state.filter.kind !== "all";
      disabled = disabled || !dirty;
    }
    item.classList.toggle("disabled", disabled);
  }
}

function fieldSelect(rule) {
  const select = el("select", "stack-key");
  for (const [value, label] of Object.entries(SORT_KEYS)) {
    const option = el("option", null, label);
    option.value = value;
    select.appendChild(option);
  }
  select.value = rule.key;
  return select;
}

function sortRow(rule) {
  const row = el("div", "stack-row");
  const dirSelect = el("select", "stack-dir");
  for (const [value, label] of [["asc", "升序"], ["desc", "降序"]]) {
    const option = el("option", null, label);
    option.value = value;
    dirSelect.appendChild(option);
  }
  dirSelect.value = rule.dir;
  row.appendChild(fieldSelect(rule));
  row.appendChild(dirSelect);
  row.appendChild(button("移除", () => row.remove()));
  return row;
}

function filterRow(rule) {
  const row = el("div", "stack-row");
  const opSelect = el("select", "stack-op");
  for (const [value, label] of Object.entries(FILTER_OPS)) {
    const option = el("option", null, label);
    option.value = value;
    opSelect.appendChild(option);
  }
  opSelect.value = rule.op;
  const first = el("input", "stack-value");
  first.type = "text";
  first.placeholder = "值";
  first.value = rule.a || "";
  const second = el("input", "stack-value");
  second.type = "text";
  second.placeholder = "到";
  second.value = rule.b || "";
  const syncSecond = () => second.classList.toggle("hidden", opSelect.value !== "between");
  opSelect.addEventListener("change", syncSecond);
  syncSecond();
  row.appendChild(fieldSelect(rule));
  row.appendChild(opSelect);
  row.appendChild(first);
  row.appendChild(second);
  row.appendChild(button("移除", () => row.remove()));
  return row;
}

function openSortDialog() {
  const host = byId("sd-rows");
  host.textContent = "";
  const rules = state.sortStack.length ? state.sortStack : [{ key: "year", dir: "desc" }];
  for (const rule of rules) host.appendChild(sortRow(rule));
  byId("sd-error").textContent = "";
  byId("sort-dialog").classList.remove("hidden");
}

function submitSortDialog() {
  const stack = [];
  for (const row of document.querySelectorAll("#sd-rows .stack-row")) {
    stack.push({
      key: row.querySelector(".stack-key").value,
      dir: row.querySelector(".stack-dir").value,
    });
  }
  state.sortStack = stack;
  byId("sort-dialog").classList.add("hidden");
  renderList();
  renderStatus();
}

function openFilterDialog() {
  const host = byId("fdr-rows");
  host.textContent = "";
  for (const rule of state.filterStack) host.appendChild(filterRow(rule));
  if (!state.filterStack.length) {
    host.appendChild(filterRow({ key: "year", op: "between", a: "", b: "" }));
  }
  byId("fdr-error").textContent = "";
  byId("filter-dialog").classList.remove("hidden");
}

function submitFilterDialog() {
  const stack = [];
  for (const row of document.querySelectorAll("#fdr-rows .stack-row")) {
    const inputs = row.querySelectorAll(".stack-value");
    stack.push({
      key: row.querySelector(".stack-key").value,
      op: row.querySelector(".stack-op").value,
      a: inputs[0] ? inputs[0].value : "",
      b: inputs[1] ? inputs[1].value : "",
    });
  }
  state.filterStack = stack;
  byId("filter-dialog").classList.add("hidden");
  renderList();
  renderStatus();
}

function displayReset() {
  state.sortStack = [{ key: "year", dir: "desc" }];
  state.filterStack = [];
  state.filter = { kind: "all" };
  state.search = "";
  byId("search").value = "";
  renderTree();
  renderList();
  renderStatus();
}

// ------------------------------------------------------------- 启动

byId("search").addEventListener("input", () => {
  state.search = byId("search").value;
  renderList();
  renderStatus();
});
byId("btn-new").addEventListener("click", () => openEditor(null));
byId("btn-scan").addEventListener("click", rescan);
byId("tab-records").addEventListener("click", () => switchView("records"));
byId("tab-files").addEventListener("click", async () => {
  switchView("files");
  await refreshFiles();
  ensureFilePolling();
});
byId("btn-file").addEventListener("click", (event) => {
  event.stopPropagation();
  byId("edit-menu").classList.add("hidden");
  byId("project-menu").classList.add("hidden");
  byId("display-menu").classList.add("hidden");
  updateFileMenu();
  byId("file-menu").classList.toggle("hidden");
});
byId("btn-edit").addEventListener("click", (event) => {
  event.stopPropagation();
  byId("file-menu").classList.add("hidden");
  byId("project-menu").classList.add("hidden");
  byId("display-menu").classList.add("hidden");
  updateEditMenu();
  byId("edit-menu").classList.toggle("hidden");
});
byId("btn-project").addEventListener("click", (event) => {
  event.stopPropagation();
  byId("file-menu").classList.add("hidden");
  byId("edit-menu").classList.add("hidden");
  byId("display-menu").classList.add("hidden");
  updateProjectMenu();
  byId("project-menu").classList.toggle("hidden");
});
byId("btn-display").addEventListener("click", (event) => {
  event.stopPropagation();
  byId("file-menu").classList.add("hidden");
  byId("edit-menu").classList.add("hidden");
  byId("project-menu").classList.add("hidden");
  updateDisplayMenu();
  byId("display-menu").classList.toggle("hidden");
});
document.addEventListener("click", () => {
  byId("file-menu").classList.add("hidden");
  byId("edit-menu").classList.add("hidden");
  byId("project-menu").classList.add("hidden");
  byId("display-menu").classList.add("hidden");
});
for (const item of document.querySelectorAll("#file-menu .dropdown-item")) {
  item.addEventListener("click", () => {
    byId("file-menu").classList.add("hidden");
    if (item.classList.contains("disabled")) return;
    const action = FILE_ACTIONS[item.dataset.action];
    if (action) action();
  });
}
for (const item of document.querySelectorAll("#edit-menu .dropdown-item")) {
  item.addEventListener("click", () => {
    byId("edit-menu").classList.add("hidden");
    if (item.classList.contains("disabled")) return;
    const action = EDIT_ACTIONS[item.dataset.action];
    if (action) action();
  });
}
for (const item of document.querySelectorAll("#project-menu .dropdown-item")) {
  item.addEventListener("click", () => {
    byId("project-menu").classList.add("hidden");
    if (item.classList.contains("disabled")) return;
    const action = PROJECT_ACTIONS[item.dataset.action];
    if (action) action();
  });
}
for (const item of document.querySelectorAll("#display-menu .dropdown-item")) {
  item.addEventListener("click", () => {
    byId("display-menu").classList.add("hidden");
    if (item.classList.contains("disabled")) return;
    const action = DISPLAY_ACTIONS[item.dataset.action];
    if (action) action();
  });
}
byId("setup-ok").addEventListener("click", submitSetup);
byId("setup-cancel").addEventListener("click", () => {
  byId("setup").classList.add("hidden");
  render();
});
byId("fd-ok").addEventListener("click", submitFileDialog);
byId("fd-cancel").addEventListener("click", () => byId("file-dialog").classList.add("hidden"));
byId("import-file").addEventListener("change", handleImportFile);
byId("mg-ok").addEventListener("click", submitMapping);
byId("mg-refine").addEventListener("click", refineImport);
byId("mg-verify").addEventListener("click", verifyImport);
byId("mg-cancel").addEventListener("click", () => {
  byId("mapping-dialog").classList.add("hidden");
  mappingState = null;
});
byId("ed-addfile").addEventListener("click", () => {
  state.editorFiles.push({ path: "", nature: "doi-consistent" });
  renderEditorFiles();
});
byId("ed-cancel").addEventListener("click", () => byId("editor").classList.add("hidden"));
byId("ed-save").addEventListener("click", saveEditor);
byId("ed-type").addEventListener("change", updateEditorFields);
byId("ed-show-all").addEventListener("change", updateEditorFields);
byId("pd-ok").addEventListener("click", submitProjectDialog);
byId("pd-cancel").addEventListener("click", () => byId("project-dialog").classList.add("hidden"));
byId("sd-add").addEventListener("click", () => {
  byId("sd-rows").appendChild(sortRow({ key: "year", dir: "desc" }));
});
byId("sd-ok").addEventListener("click", submitSortDialog);
byId("sd-cancel").addEventListener("click", () => byId("sort-dialog").classList.add("hidden"));
byId("fdr-add").addEventListener("click", () => {
  byId("fdr-rows").appendChild(filterRow({ key: "year", op: "eq", a: "", b: "" }));
});
byId("fdr-ok").addEventListener("click", submitFilterDialog);
byId("fdr-cancel").addEventListener("click", () => byId("filter-dialog").classList.add("hidden"));
byId("pv-ok").addEventListener("click", applyPreview);
byId("pv-cancel").addEventListener("click", () => {
  byId("preview-dialog").classList.add("hidden");
  previewState = null;
});
byId("ld-ok").addEventListener("click", submitLinkDialog);
byId("ld-cancel").addEventListener("click", () => {
  byId("link-dialog").classList.add("hidden");
  linkMode = null;
  linkTarget = null;
  linkPick = null;
});
byId("ld-search").addEventListener("input", renderLinkOptions);
byId("lb-primary").addEventListener("click", () => {
  if (lbPhase === "options") startLinkBatch();
  else if (lbPhase === "results") applyLinkBatch();
});
byId("lb-cancel").addEventListener("click", () => byId("linkbatch-dialog").classList.add("hidden"));
byId("ea-ok").addEventListener("click", startEnrichAll);
byId("ea-cancel").addEventListener("click", () => byId("enrich-all-dialog").classList.add("hidden"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideRowMenu();
    byId("editor").classList.add("hidden");
    byId("file-dialog").classList.add("hidden");
    byId("file-menu").classList.add("hidden");
    byId("edit-menu").classList.add("hidden");
    byId("project-menu").classList.add("hidden");
    byId("display-menu").classList.add("hidden");
    byId("project-dialog").classList.add("hidden");
    byId("sort-dialog").classList.add("hidden");
    byId("filter-dialog").classList.add("hidden");
    byId("preview-dialog").classList.add("hidden");
    byId("mapping-dialog").classList.add("hidden");
    byId("link-dialog").classList.add("hidden");
    byId("linkbatch-dialog").classList.add("hidden");
    byId("enrich-all-dialog").classList.add("hidden");
    if (!byId("setup").classList.contains("hidden")) {
      byId("setup").classList.add("hidden");
      render();
    }
  }
});

async function boot() {
  try {
    await refreshState();
  } catch (error) {
    showMessage(error.message, "error");
  }
  render();
  if (!state.loaded) {
    showSetup();
    return;
  }
  await rescan();
  state.filePolling = (state.fileRecords || []).filter(
    (item) => item.status === "pending" || item.status === "parsing"
  ).length;
  ensureFilePolling();
}

boot();
