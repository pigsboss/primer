/* primer 会话舱 Phase 0 —— 画布（圈注/套索/橡皮）＋问题卡＋只读台账＋上传
 *
 * 数据方向：/state 轮询（模型侧写 canvas/cards/record）；/answer、/upload 提交（人侧写）。
 * 覆盖层几何一律使用原图像素坐标；套索/擦除回传同口径，不做任何坐标折损。
 */
"use strict";

const NS = "http://www.w3.org/2000/svg";
const GAP = 30;
const POLL_MS = 1500;

const S = {
  data: null,
  hash: "",
  canvasHash: "",
  fitted: false,
  layout: {},         // itemId -> {x, y, w, h}
  T: { x: 40, y: 40, k: 1 },
  tool: "pan",
  drag: null,         // {mode:"pan"|"lasso"|"erase", itemId, pts, el, startX, startY}
  sel: {},            // itemId -> {lassos:[{id,pts}], erases:[{id,pts}]}
  choices: {},        // cardId -> option key
  cardText: {},       // cardId -> text
  seq: 0,
  chatAgentSeen: 0,   // 上次看"对话"页签时已有的 agent 消息数（未读角点用）
  chatScrolled: false,
};

const $ = (s) => document.querySelector(s);
const el = (tag) => document.createElementNS(NS, tag);

/* ---------------------------------------------------------------- utils */

function apiGet(path) { return fetch(path).then((r) => r.json()); }
function apiPost(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json());
}

function itemById(canvas, id) {
  return (canvas.items || []).find((it) => it.id === id);
}

function trayCount() {
  let n = 0;
  for (const s of Object.values(S.sel)) n += (s.lassos || []).length + (s.erases || []).length;
  return n;
}

function traySelections() {
  const out = [];
  const canvas = S.data ? S.data.canvas : { items: [] };
  for (const [itemId, s] of Object.entries(S.sel)) {
    const it = itemById(canvas, itemId);
    const image = it ? it.image : itemId;
    for (const l of s.lassos || []) out.push({ image, item: itemId, mode: "lasso", points: l.pts });
    for (const e of s.erases || []) out.push({ image, item: itemId, mode: "erase", points: e.pts });
  }
  return out;
}

function clearTray() {
  S.sel = {};
  redrawUser();
  updateTray();
}

function updateTray() {
  $("#tray").textContent = `待提交：${trayCount()} 项`;
}

function flash(msg, isErr) {
  const t = $("#upload-note");
  t.textContent = msg;
  t.style.color = isErr ? "var(--red)" : "var(--green)";
  if (msg) setTimeout(() => { t.textContent = ""; }, 5000);
}

/* ---------------------------------------------------------------- canvas */

function applyTransform() {
  $("#world").style.transform =
    `translate(${S.T.x}px, ${S.T.y}px) scale(${S.T.k})`;
}

function fitView() {
  const items = (S.data && S.data.canvas.items) || [];
  if (!items.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const it of items) {
    const L = S.layout[it.id];
    if (!L) continue;
    minX = Math.min(minX, L.x); minY = Math.min(minY, L.y);
    maxX = Math.max(maxX, L.x + L.w); maxY = Math.max(maxY, L.y + L.h);
  }
  const sw = $("#stage").clientWidth, sh = $("#stage").clientHeight;
  const w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
  const k = Math.max(0.05, Math.min((sw - 60) / w, (sh - 60) / h, 1.6));
  S.T.k = k;
  S.T.x = (sw - k * w) / 2 - k * minX;
  S.T.y = (sh - k * h) / 2 - k * minY;
  applyTransform();
}

function buildCanvas(canvas) {
  const world = $("#world");
  world.innerHTML = "";
  S.layout = {};
  const items = canvas.items || [];
  let x = 0;
  const maxH = Math.max(1, ...items.map((it) => it.h || 0));
  for (const it of items) {
    const w = it.w || 900, h = it.h || 620;
    const div = document.createElement("div");
    div.className = "item";
    div.dataset.id = it.id;
    const y = Math.max(0, (maxH - h) / 2);
    div.style.left = x + "px";
    div.style.top = y + "px";
    div.style.width = w + "px";
    div.style.height = h + "px";
    S.layout[it.id] = { x, y, w, h };

    const img = document.createElement("img");
    img.src = "/f/" + it.image;
    img.draggable = false;
    div.appendChild(img);

    const svg = el("svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.dataset.id = it.id;
    div.appendChild(svg);

    if (it.title || it.caption) {
      const cap = document.createElement("div");
      cap.className = "caption";
      cap.style.top = (y + h + 6) + "px";
      cap.textContent = [it.title, it.caption].filter(Boolean).join("　·　");
      div.appendChild(cap);
    }
    world.appendChild(div);
    x += w + GAP;
  }
  drawOverlays(canvas);
  applyTransform();
}

function drawOverlays(canvas) {
  for (const it of canvas.items || []) {
    const svg = $("#world svg[data-id='" + CSS.escape(it.id) + "']");
    if (!svg) continue;
    const w = it.w || 900, h = it.h || 620;

    const defs = el("defs");
    const mask = el("mask");
    mask.id = "mask-" + it.id;
    mask.setAttribute("maskUnits", "userSpaceOnUse");
    mask.setAttribute("x", 0); mask.setAttribute("y", 0);
    mask.setAttribute("width", w); mask.setAttribute("height", h);
    const mrect = el("rect");
    mrect.setAttribute("x", 0); mrect.setAttribute("y", 0);
    mrect.setAttribute("width", w); mrect.setAttribute("height", h);
    mrect.setAttribute("fill", "#fff");
    mask.appendChild(mrect);
    const mg = el("g");
    mg.dataset.role = "mask-erases";
    mask.appendChild(mg);
    defs.appendChild(mask);
    svg.appendChild(defs);

    const mine = el("g");
    mine.setAttribute("class", "mine");
    mine.setAttribute("mask", `url(#mask-${it.id})`);
    for (const ov of it.overlays || []) {
      mine.appendChild(shapeFor(ov, it));
      if (ov.label && ov.type !== "label") mine.appendChild(labelFor(ov, it));
    }
    svg.appendChild(mine);

    const user = el("g"); user.setAttribute("class", "user"); svg.appendChild(user);
    const marks = el("g"); marks.setAttribute("class", "eraseMarks"); svg.appendChild(marks);

    const hit = el("rect");
    hit.setAttribute("class", "hit");
    hit.setAttribute("x", 0); hit.setAttribute("y", 0);
    hit.setAttribute("width", w); hit.setAttribute("height", h);
    svg.appendChild(hit);
  }
  redrawUser();
}

function shapeFor(ov, item) {
  let node;
  const color = ov.color || "#ff9500";
  const width = ov.width || 2.5;
  const fill = ov.fill != null ? ov.fill : 0.12;
  if (ov.type === "polygon" || ov.type === "polyline") {
    node = el(ov.type);
    node.setAttribute("points", (ov.points || []).map((p) => p.join(",")).join(" "));
    node.setAttribute("fill", ov.type === "polygon" ? color : "none");
    node.setAttribute("fill-opacity", ov.type === "polygon" ? fill : 0);
    node.setAttribute("stroke", color);
    node.setAttribute("stroke-width", width);
    if (ov.dash) node.setAttribute("stroke-dasharray", "9 7");
  } else if (ov.type === "rect") {
    node = el("rect");
    node.setAttribute("x", ov.xy[0]); node.setAttribute("y", ov.xy[1]);
    node.setAttribute("width", ov.w); node.setAttribute("height", ov.h);
    node.setAttribute("fill", color); node.setAttribute("fill-opacity", fill);
    node.setAttribute("stroke", color); node.setAttribute("stroke-width", width);
    if (ov.dash) node.setAttribute("stroke-dasharray", "9 7");
  } else if (ov.type === "ellipse") {
    node = el("ellipse");
    node.setAttribute("cx", ov.center[0]); node.setAttribute("cy", ov.center[1]);
    node.setAttribute("rx", ov.rx); node.setAttribute("ry", ov.ry || ov.rx);
    node.setAttribute("fill", color); node.setAttribute("fill-opacity", fill);
    node.setAttribute("stroke", color); node.setAttribute("stroke-width", width);
    if (ov.dash) node.setAttribute("stroke-dasharray", "9 7");
  } else if (ov.type === "label") {
    node = el("text");
    node.setAttribute("x", ov.at[0]); node.setAttribute("y", ov.at[1]);
    node.setAttribute("fill", color);
    node.setAttribute("font-size", ov.size || 15);
    node.setAttribute("paint-order", "stroke");
    node.setAttribute("stroke", "rgba(0,0,0,.75)");
    node.setAttribute("stroke-width", 3.5);
    node.textContent = ov.label || "";
  } else {
    node = el("g");
  }
  if (ov.id) node.dataset.ov = item.id + ":" + ov.id;
  return node;
}

function strokeFor(pts, color, width, dash) {
  const n = el("polyline");
  n.setAttribute("points", pts.map((p) => p.join(",")).join(" "));
  n.setAttribute("fill", "none");
  n.setAttribute("stroke", color);
  n.setAttribute("stroke-width", width);
  if (dash) n.setAttribute("stroke-dasharray", "7 6");
  return n;
}

function labelFor(ov, item) {
  let ax = 8, ay = 16;
  if (ov.type === "rect") { ax = ov.xy[0]; ay = ov.xy[1] - 6; }
  else if (ov.type === "ellipse") {
    ax = ov.center[0] - ov.rx;
    ay = ov.center[1] - (ov.ry || ov.rx) - 6;
  } else if (ov.type === "polygon" || ov.type === "polyline") {
    const p0 = (ov.points && ov.points[0]) || [8, 20];
    ax = p0[0]; ay = p0[1] - 6;
  }
  const t = el("text");
  t.setAttribute("x", Math.max(4, ax));
  t.setAttribute("y", Math.max(14, ay));
  t.setAttribute("fill", ov.color || "#ff9500");
  t.setAttribute("font-size", ov.size || 14);
  t.setAttribute("paint-order", "stroke");
  t.setAttribute("stroke", "rgba(0,0,0,.8)");
  t.setAttribute("stroke-width", 3.5);
  t.textContent = ov.label || "";
  return t;
}

function redrawUser() {
  const canvas = S.data ? S.data.canvas : { items: [] };
  for (const it of canvas.items || []) {
    const svg = $("#world svg[data-id='" + CSS.escape(it.id) + "']");
    if (!svg) continue;
    const user = svg.querySelector("g.user");
    const marks = svg.querySelector("g.eraseMarks");
    const mg = svg.querySelector("g[data-role='mask-erases']");
    user.innerHTML = ""; marks.innerHTML = ""; mg.innerHTML = "";
    const s = S.sel[it.id] || { lassos: [], erases: [] };
    for (const l of s.lassos || []) {
      if (l.pts.length < 3) continue;
      const poly = strokeFor(l.pts.concat([l.pts[0]]), "#37d67a", 2.2, false);
      poly.setAttribute("fill", "rgba(55,214,122,.15)");
      user.appendChild(poly);
    }
    for (const e of s.erases || []) {
      marks.appendChild(strokeFor(e.pts, "rgba(255,93,93,.85)", 2.2, true));
      mg.appendChild(strokeFor(e.pts, "#000", 22, false));
    }
  }
}

/* ------------------------------------------------------- pointer events */

function ptFromEvent(svg, e) {
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  const m = svg.getScreenCTM();
  if (!m) return [0, 0];
  const q = pt.matrixTransform(m.inverse());
  return [Math.round(q.x * 10) / 10, Math.round(q.y * 10) / 10];
}

function onWheel(e) {
  e.preventDefault();
  const rect = $("#stage").getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const k2 = Math.max(0.05, Math.min(20, S.T.k * Math.exp(-e.deltaY * 0.0016)));
  S.T.x = mx - (mx - S.T.x) * (k2 / S.T.k);
  S.T.y = my - (my - S.T.y) * (k2 / S.T.k);
  S.T.k = k2;
  applyTransform();
}

function onPointerDown(e) {
  if (e.button !== 0 && e.button !== 1) return;
  const stage = $("#stage");
  const svg = e.target.closest ? e.target.closest("svg") : null;
  const wantDraw = (S.tool === "lasso" || S.tool === "erase") && e.button === 0 && svg;
  if (wantDraw) {
    const pts = [ptFromEvent(svg, e)];
    const itemId = svg.dataset.id;
    const temp = el("polyline");
    temp.setAttribute("fill", "none");
    temp.setAttribute("stroke", S.tool === "erase" ? "rgba(255,93,93,.9)" : "#37d67a");
    temp.setAttribute("stroke-width", 2);
    if (S.tool === "erase") temp.setAttribute("stroke-dasharray", "7 6");
    const layer = S.tool === "erase"
      ? svg.querySelector("g.eraseMarks") : svg.querySelector("g.user");
    layer.appendChild(temp);
    S.drag = { mode: S.tool, itemId, pts, el: temp };
    stage.classList.add("drawing");
    return;
  }
  S.drag = { mode: "pan", startX: e.clientX - S.T.x, startY: e.clientY - S.T.y };
  stage.classList.add("panning");
}

function onPointerMove(e) {
  const d = S.drag;
  if (!d) return;
  if (d.mode === "pan") {
    S.T.x = e.clientX - d.startX;
    S.T.y = e.clientY - d.startY;
    applyTransform();
    return;
  }
  const svg = $("#world svg[data-id='" + CSS.escape(d.itemId) + "']");
  if (!svg) return;
  const p = ptFromEvent(svg, e);
  const last = d.pts[d.pts.length - 1];
  if (Math.abs(p[0] - last[0]) + Math.abs(p[1] - last[1]) < 1.2) return;
  d.pts.push(p);
  d.el.setAttribute("points", d.pts.map((q) => q.join(",")).join(" "));
}

function onPointerUp() {
  const d = S.drag;
  S.drag = null;
  $("#stage").classList.remove("panning", "drawing");
  if (!d || d.mode === "pan") return;
  if (d.el && d.el.parentNode) d.el.parentNode.removeChild(d.el);
  if (d.pts.length < 3) return;
  const slot = S.sel[d.itemId] || (S.sel[d.itemId] = { lassos: [], erases: [] });
  const rec = { id: "sel" + (++S.seq), pts: d.pts };
  if (d.mode === "eraser" || d.mode === "erase") slot.erases.push(rec);
  else slot.lassos.push(rec);
  redrawUser();
  updateTray();
}

/* ---------------------------------------------------------- overlay 聚焦 */

function overlayBBox(it, ov) {
  if (ov.type === "polygon" || ov.type === "polyline") {
    const xs = ov.points.map((p) => p[0]), ys = ov.points.map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  if (ov.type === "rect") return [ov.xy[0], ov.xy[1], ov.xy[0] + ov.w, ov.xy[1] + ov.h];
  if (ov.type === "ellipse") {
    return [ov.center[0] - ov.rx, ov.center[1] - (ov.ry || ov.rx),
            ov.center[0] + ov.rx, ov.center[1] + (ov.ry || ov.rx)];
  }
  if (ov.type === "label") return [ov.at[0] - 40, ov.at[1] - 24, ov.at[0] + 160, ov.at[1] + 12];
  return null;
}

function focusOverlay(image, ovId) {
  const canvas = S.data ? S.data.canvas : { items: [] };
  const it = (canvas.items || []).find((x) => x.image === image || x.id === image);
  if (!it) return;
  let box = null;
  for (const ov of it.overlays || []) if (ov.id === ovId) box = overlayBBox(it, ov);
  const L = S.layout[it.id];
  if (!L) return;
  const sw = $("#stage").clientWidth, sh = $("#stage").clientHeight;
  if (box) {
    const [x0, y0, x1, y1] = box;
    const bw = Math.max(30, x1 - x0), bh = Math.max(30, y1 - y0);
    const k = Math.max(0.2, Math.min(3.2, Math.min(sw * 0.6 / bw, sh * 0.6 / bh)));
    S.T.k = k;
    S.T.x = sw / 2 - k * (L.x + (x0 + x1) / 2);
    S.T.y = sh / 2 - k * (L.y + (y0 + y1) / 2);
  } else {
    S.T.k = Math.max(S.T.k, 1);
    S.T.x = sw / 2 - S.T.k * (L.x + L.w / 2);
    S.T.y = sh / 2 - S.T.k * (L.y + L.h / 2);
  }
  applyTransform();
  const node = $("#world svg[data-id='" + CSS.escape(it.id) + "'] [data-ov='" +
    CSS.escape(it.id + ":" + ovId) + "']");
  if (node) {
    node.classList.remove("pulse");
    void node.getBoundingClientRect();
    node.classList.add("pulse");
    setTimeout(() => node.classList.remove("pulse"), 1600);
  }
}

/* ------------------------------------------------------------- 问题卡 */

function renderCards() {
  const wrap = $("#tab-cards");
  wrap.innerHTML = "";
  if (!S.data) return;
  const cards = (S.data.cards && S.data.cards.cards) || [];
  const answered = S.data.answers || {};

  const head = document.createElement("div");
  head.className = "row";
  head.style.marginBottom = "10px";
  const btnAll = document.createElement("button");
  btnAll.textContent = "提交全部已作答";
  btnAll.onclick = async () => {
    let n = 0;
    for (const c of cards) {
      const choice = S.choices[c.id] || null;
      const text = (S.cardText[c.id] || "").trim() || null;
      if (!choice && !text) continue;
      await apiPost("/answer", { card_id: c.id, choice, text, selections: [] });
      n++;
    }
    flash(n ? `已提交 ${n} 张卡（圈选请随单卡提交）` : "没有可提交的卡");
  };
  head.appendChild(btnAll);
  wrap.appendChild(head);

  for (const c of cards) {
    const div = document.createElement("div");
    div.className = "card";

    const h = document.createElement("h3");
    h.textContent = c.title || c.id;
    div.appendChild(h);

    if (c.text) {
      const p = document.createElement("div");
      p.className = "qtext";
      p.textContent = c.text;
      div.appendChild(p);
    }

    if (c.attach && c.attach.image) {
      const b = document.createElement("button");
      b.className = "ghost";
      b.textContent = "看图";
      b.onclick = () => focusOverlay(c.attach.image, c.attach.overlay);
      div.appendChild(b);
    }

    const opts = document.createElement("div");
    opts.className = "opts";
    for (const o of c.options || []) {
      const b = document.createElement("button");
      b.className = "opt" + (S.choices[c.id] === o.key ? " active" : "");
      const k = document.createElement("span");
      k.className = "k"; k.textContent = o.key;
      b.appendChild(k);
      b.appendChild(document.createTextNode(o.label || ""));
      if (o.desc) {
        const d = document.createElement("span");
        d.className = "d"; d.textContent = o.desc;
        b.appendChild(d);
      }
      b.onclick = () => {
        S.choices[c.id] = (S.choices[c.id] === o.key) ? null : o.key;
        renderCards();
      };
      opts.appendChild(b);
    }
    if ((c.options || []).length) div.appendChild(opts);

    if (c.allow_text !== false) {
      const ta = document.createElement("textarea");
      ta.rows = 2;
      ta.placeholder = "补充说明（可空）";
      ta.value = S.cardText[c.id] || "";
      ta.oninput = () => { S.cardText[c.id] = ta.value; };
      div.appendChild(ta);
    }

    const row = document.createElement("div");
    row.className = "row";
    const sub = document.createElement("button");
    sub.textContent = "提交本卡";
    sub.onclick = async () => {
      const body = {
        card_id: c.id,
        choice: S.choices[c.id] || null,
        text: (S.cardText[c.id] || "").trim() || null,
        selections: traySelections(),
      };
      if (!body.choice && !body.text && !body.selections.length) {
        flash("本卡尚未作答"); return;
      }
      const r = await apiPost("/answer", body);
      if (r.ok) { clearTray(); flash(`已提交：${c.id}`); }
      else flash("提交失败：" + (r.error || ""), true);
    };
    row.appendChild(sub);

    const prev = answered[c.id];
    if (prev) {
      const a = document.createElement("span");
      a.className = "answered";
      const bits = [];
      if (prev.choice) bits.push("选项 " + prev.choice);
      if (prev.text) bits.push("文字");
      if ((prev.selections || []).length) bits.push(`圈选 ${prev.selections.length}`);
      a.textContent = "已答：" + (bits.join(" · ") || "（空）") +
        (prev.received_at ? "　" + prev.received_at.slice(11, 16) : "");
      row.appendChild(a);
    }
    div.appendChild(row);
    wrap.appendChild(div);
  }
  if (!cards.length) {
    const e = document.createElement("div");
    e.className = "note";
    e.textContent = "（模型侧尚未投放问题卡）";
    wrap.appendChild(e);
  }
}

/* -------------------------------------------------------------- 台账 */

function renderRecord() {
  const wrap = $("#tab-record");
  wrap.innerHTML = "";
  if (!S.data) return;
  const rows = (S.data.record && S.data.record.rows) || [];
  if (!rows.length) {
    wrap.innerHTML = '<div class="note">（暂无台账行）</div>';
    return;
  }
  const table = document.createElement("table");
  table.className = "record";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>参数</th><th>当前值</th><th>状态</th><th>依据</th></tr>";
  table.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of rows) {
    const tr = document.createElement("tr");
    const st = ["open", "measured", "adjudicated", "inferred"].includes(r.status)
      ? r.status : "other";
    tr.innerHTML =
      `<td>${escapeHtml(r.subject || r.id || "")}</td>` +
      `<td>${escapeHtml(r.value || "")}</td>` +
      `<td><span class="badge st-${st}">${escapeHtml(r.status || "")}</span></td>` +
      `<td class="ev">${escapeHtml(r.evidence || "")}</td>`;
    if (r.highlight && r.highlight.image) {
      tr.className = "rowlink";
      tr.onclick = () => focusOverlay(r.highlight.image, r.highlight.overlay);
      tr.title = "点击在画布上查看证据";
    }
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  wrap.appendChild(table);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}

/* -------------------------------------------------------------- 上传 */

function renderUploads() {
  const wrap = $("#uploads-list");
  wrap.innerHTML = "";
  if (!S.data) return;
  for (const u of S.data.uploads || []) {
    const d = document.createElement("div");
    d.className = "u";
    const head = document.createElement("div");
    head.textContent = u.name + (u.desc ? "　—　" + u.desc : "");
    d.appendChild(head);
    if ((u.pages || []).length) {
      const line = document.createElement("div");
      for (const p of u.pages) {
        const a = document.createElement("span");
        a.className = "p";
        a.textContent = "预览 " + p.split("/").pop();
        a.onclick = () => window.open("/f/" + p, "_blank");
        line.appendChild(a);
      }
      d.appendChild(line);
    }
    if (u.note) {
      const n = document.createElement("div");
      n.className = "note"; n.textContent = u.note;
      d.appendChild(n);
    }
    wrap.appendChild(d);
  }
}

async function sendUpload(file) {
  const desc = $("#upload-desc").value || "";
  const r = await fetch(`/upload?name=${encodeURIComponent(file.name)}&desc=${encodeURIComponent(desc)}`,
    { method: "POST", body: file });
  return r.json();
}

async function handleFiles(files) {
  for (const f of files) {
    flash(`上传中：${f.name}…`);
    try {
      const j = await sendUpload(f);
      if (j.ok && j.upload) {
        const u = j.upload;
        flash(`已上传 ${u.name}（${(u.pages || []).length} 页已入画布池）`);
      } else {
        flash("上传失败：" + (j.error || "unknown"), true);
      }
    } catch (err) {
      flash("上传失败：" + err, true);
    }
  }
  poll();
}

/* -------------------------------------------------------------- 对话 */

const ROLE_LABEL = { user: "我", agent: "primer-LLM", system: "系统" };
const DRIVER_STALE_MS = 60000;

function driverLive(meta) {
  if (!meta || !meta.heartbeat_ts) return false;
  const t = Date.parse(meta.heartbeat_ts);
  return !isNaN(t) && (Date.now() - t) < DRIVER_STALE_MS;
}

function actionText(a) {
  if (!a || !a.kind) return "?";
  if (a.kind === "run") {
    const s = a.seconds != null ? ` ${a.seconds}s` : "";
    return `run(${a.name || "?"})${a.ok ? s : " 失败"}`;
  }
  if (a.kind === "edit_params") return `edit_params×${a.edits != null ? a.edits : 0}`;
  if (a.kind === "session_update") {
    const bits = [];
    if (a.record_rows) bits.push(`台账${a.record_rows}`);
    if (a.canvas_items) bits.push(`画布${a.canvas_items}`);
    if (a.cards) bits.push(`卡片${a.cards}`);
    return "session_update(" + (bits.join("/") || "0") + ")";
  }
  if (a.kind === "escalate") return "escalate";
  return a.kind + (a.ok === false ? "(失败)" : "");
}

function renderChat() {
  const wrap = $("#tab-chat");
  wrap.innerHTML = "";
  const chat = (S.data && S.data.chat) || { messages: [], meta: null };
  if (chat.escalation) {
    const b = document.createElement("div");
    b.className = "escalation";
    b.textContent = "⚠ 本会话已置升级标记（ESCALATION.md）：有事项需要改 primer 代码，已交给 kimi code 回路。";
    wrap.appendChild(b);
  }
  const messages = chat.messages || [];
  if (!messages.length) {
    const n = document.createElement("div");
    n.className = "note";
    n.textContent = "（还没有对话。在下方输入框发第一条消息；驱动会把上下文交给所配的 LLM。）";
    wrap.appendChild(n);
  }
  for (const m of messages) {
    const div = document.createElement("div");
    div.className = "msg " + (m.role || "user");
    const head = document.createElement("div");
    head.className = "mhead";
    const badge = document.createElement("span");
    badge.className = "role " + (m.role || "user");
    badge.textContent = ROLE_LABEL[m.role] || (m.role || "?");
    head.appendChild(badge);
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = String(m.ts || "").replace("T", " ").slice(0, 19);
    head.appendChild(t);
    if (m.model) {
      const mm = document.createElement("span");
      mm.className = "m";
      mm.textContent = m.model;
      head.appendChild(mm);
    }
    div.appendChild(head);
    const body = document.createElement("div");
    body.className = "mtext";
    body.textContent = m.text || "";
    div.appendChild(body);
    const acts = m.actions_executed || [];
    if (acts.length) {
      const a = document.createElement("div");
      a.className = "acts";
      a.textContent = "已执行：" + acts.map(actionText).join("、");
      div.appendChild(a);
    }
    for (const ref of m.refs || []) {
      const a = document.createElement("a");
      a.className = "ref";
      a.href = "/f/" + String(ref).split("/").map(encodeURIComponent).join("/");
      a.target = "_blank";
      a.textContent = "附件：" + ref;
      div.appendChild(a);
    }
    const bits = [];
    if (m.seconds != null) bits.push(`${m.seconds}s`);
    if (m.usage && m.usage.total_tokens) bits.push(`tokens ${m.usage.total_tokens}`);
    if (bits.length) {
      const u = document.createElement("div");
      u.className = "mmeta";
      u.textContent = bits.join(" · ");
      div.appendChild(u);
    }
    wrap.appendChild(div);
  }
  const nearBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 160;
  if (nearBottom || !S.chatScrolled) {
    wrap.scrollTop = wrap.scrollHeight;
    S.chatScrolled = true;
  }
}

function renderChatMeta() {
  const chat = (S.data && S.data.chat) || {};
  const meta = chat.meta;
  const sel = $("#chat-model");
  const models = (meta && meta.models) || [];
  const sig = models.map((m) => `${m.id}|${m.label}`).join(",");
  if (sel.dataset.sig !== sig) {
    const prev = sel.value;
    sel.innerHTML = "";
    if (!models.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "（无可用模型）";
      sel.appendChild(o);
    }
    for (const m of models) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.label || m.id;
      sel.appendChild(o);
    }
    sel.dataset.sig = sig;
    const fallback = (meta && meta.default_role) || (models[0] && models[0].id) || "";
    sel.value = models.some((m) => m.id === prev) ? prev : fallback;
  }
  const live = driverLive(meta);
  sel.disabled = !live || !models.length;
  sel.title = meta && meta.models_source
    ? "模型清单来源：" + meta.models_source
    : "模型清单来源：未知（驱动未写 chat_meta）";
  const note = $("#chat-driver");
  if (!meta) note.textContent = "驱动未运行";
  else if (!live) note.textContent = "驱动心跳过期";
  else note.textContent = "驱动运行中";
  note.className = "drv " + (live ? "ok" : "bad");
}

function updateUnread() {
  const messages = ((S.data && S.data.chat) || {}).messages || [];
  const agents = messages.filter((m) => m.role === "agent").length;
  const active = $("#tabs .tab.active");
  const chatActive = active && active.dataset.tab === "chat";
  if (chatActive) S.chatAgentSeen = agents;
  const badge = $("#chat-unread");
  const diff = agents - S.chatAgentSeen;
  if (diff > 0 && !chatActive) {
    badge.textContent = String(diff);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}

function activateTab(name) {
  for (const x of document.querySelectorAll("#tabs .tab")) {
    x.classList.toggle("active", x.dataset.tab === name);
  }
  for (const p of document.querySelectorAll(".tabpane")) p.classList.add("hidden");
  const pane = $("#tab-" + name);
  if (pane) pane.classList.remove("hidden");
  if (name === "chat") {
    renderChat();
    setTimeout(updateUnread, 0);
  }
}

async function sendChat() {
  const text = ($("#free-text").value || "").trim();
  const note = $("#chat-driver");
  if (!text) {
    note.textContent = "消息为空";
    note.className = "drv bad";
    return;
  }
  const model = $("#chat-model").value || null;
  const r = await apiPost("/chat", { text, model });
  if (r.ok) {
    $("#free-text").value = "";
    activateTab("chat");
    await poll();
  } else {
    note.textContent = "发送失败：" + (r.error || "");
    note.className = "drv bad";
  }
}

/* -------------------------------------------------------------- 轮询 */

async function poll() {
  try {
    const st = await apiGet("/state");
    S.data = st;
    $("#status-dot").className = "dot ok";
    $("#status-text").textContent = "已连接";
    $("#session-path").textContent = st.session || "";
    const chat = st.chat || {};
    const h = JSON.stringify([st.canvas, st.cards, st.record, (st.uploads || []).length,
      Object.keys(st.answers || {}).length, Object.values(st.answers || {}).map((a) => a.received_at),
      (chat.messages || []).length, chat.escalation || false]);
    renderChatMeta();
    if (h !== S.hash) {
      S.hash = h;
      const ch = JSON.stringify(st.canvas);
      if (ch !== S.canvasHash) {
        S.canvasHash = ch;
        buildCanvas(st.canvas);
        if (!S.fitted) { fitView(); S.fitted = true; }
      }
      renderCards();
      renderRecord();
      renderUploads();
      renderChat();
      updateTray();
    }
    updateUnread();
  } catch (err) {
    $("#status-dot").className = "dot bad";
    $("#status-text").textContent = "连接断开";
  }
}

/* -------------------------------------------------------------- 装配 */

function bindUI() {
  for (const b of document.querySelectorAll("#toolbar .tool")) {
    b.onclick = () => {
      S.tool = b.dataset.tool;
      for (const x of document.querySelectorAll("#toolbar .tool")) x.classList.remove("active");
      b.classList.add("active");
    };
  }
  $("#show-annot").onchange = (e) => {
    $("#world").classList.toggle("hide-annot", !e.target.checked);
  };
  $("#fit-btn").onclick = fitView;
  $("#tray-clear").onclick = clearTray;

  for (const t of document.querySelectorAll("#tabs .tab")) {
    t.onclick = () => activateTab(t.dataset.tab);
  }

  $("#stage").addEventListener("wheel", onWheel, { passive: false });
  $("#stage").addEventListener("pointerdown", onPointerDown);
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);

  $("#upload-btn").onclick = () => {
    const inp = $("#file-input");
    if (inp.files && inp.files.length) handleFiles([...inp.files]);
    else flash("先选择文件");
  };
  $("#file-input").onchange = () => {
    if ($("#file-input").files.length) handleFiles([...$("#file-input").files]);
  };
  const dz = $("#dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleFiles([...e.dataTransfer.files]);
  });

  $("#free-send").onclick = sendChat;
  $("#free-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}

function bindChatInput() {
  const ta = $("#free-text");
  if (!ta) return;
  const saved = Number(localStorage.getItem("primerChatInputHeight") || 0);
  if (saved > 30) ta.style.height = saved + "px";
  ta.addEventListener("mouseup", () => {
    if (ta.style.height) localStorage.setItem("primerChatInputHeight", parseInt(ta.style.height, 10) || 0);
  });
  ta.addEventListener("keyup", () => {
    if (ta.style.height) localStorage.setItem("primerChatInputHeight", parseInt(ta.style.height, 10) || 0);
  });
}

bindUI();
bindChatInput();
poll();
setInterval(poll, POLL_MS);
