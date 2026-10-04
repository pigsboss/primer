/* 三维视图（primer 会话舱 · 资产树里 kind=stl 的节点）
 *
 * 数据：open() 传入的 {files:[{url,label,craft}], label}——单件（整机、单个分件）或一组（分件整组）。
 * 文件走 /model/<rel>；渲染用本地 vendor 的 three.js（r160，MIT；见 vendor/three/README.md）。
 *
 * 约定：STL 导出时 1 单位 = 1 m、Blender Z-up；这里把模型根节点绕 X 轴转 -90° 成 three 的
 * Y-up（视野里 Y 朝上＝Blender 的 +Z），网格铺在 XZ 平面。分件带 craft（COL0–3／CMB）按器配色，
 * 其余整机单色灰蓝。场景只初始化一次：切到别的资产时若文件集合没变，不重载、不重建。
 */
import * as THREE from "./vendor/three/three.module.js";
import { OrbitControls } from "./vendor/three/OrbitControls.js";
import { STLLoader } from "./vendor/three/STLLoader.js";

const CRAFT_COLORS = {
  COL0: 0x6fa8ff, COL1: 0x6ad2a0, COL2: 0xffc46b, COL3: 0xd08bff, CMB: 0xff8a5c,
};
const FALLBACK_COLOR = 0x9fb3c8;

const V = {
  ready: false, container: null, renderer: null, scene: null, camera: null,
  controls: null, root: null, grid: null, axes: null, loader: null,
  materials: [], statsEl: null, noteEl: null,
  files: [], label: "", loadedKey: null, token: 0,
};

function $(sel) { return document.querySelector(sel); }

const wireOn = () => localStorage.getItem("primer3dWire") === "1";
const gridOn = () => localStorage.getItem("primer3dGrid") !== "0";

function setNote(text) {
  if (V.noteEl) V.noteEl.textContent = text || "";
}

function renderOnce() {
  if (V.ready) V.renderer.render(V.scene, V.camera);
}

/* --------------------------------------------------------------- 初始化 */

function ensureScene() {
  if (V.ready) return true;
  const container = $("#stage3d");
  const canvas = $("#gl");
  if (!container || !canvas) return false;
  V.container = container;
  V.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  V.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  V.scene = new THREE.Scene();
  V.scene.background = new THREE.Color(0x0b0e12);
  V.camera = new THREE.PerspectiveCamera(45, 1, 0.05, 5000);
  V.controls = new OrbitControls(V.camera, canvas);
  V.controls.enableDamping = false;          // 按需渲染：不拖不画，省电
  V.controls.addEventListener("change", renderOnce);
  V.scene.add(new THREE.HemisphereLight(0xdff0ff, 0x18202a, 1.25));
  const key = new THREE.DirectionalLight(0xffffff, 1.8);
  key.position.set(1.2, 1.6, 0.9);
  const fill = new THREE.DirectionalLight(0x9fc4ff, 0.7);
  fill.position.set(-1.1, -0.6, -0.8);
  V.scene.add(key, fill);
  V.root = new THREE.Group();
  V.root.rotation.x = -Math.PI / 2;          // Blender Z-up → three Y-up
  V.scene.add(V.root);
  V.grid = new THREE.GridHelper(100, 20, 0x2a3140, 0x1a2029);
  V.axes = new THREE.AxesHelper(5);
  V.scene.add(V.grid, V.axes);
  V.loader = new STLLoader();
  V.statsEl = $("#model-stats");
  V.noteEl = $("#gl-note");
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => {
      if (document.body.classList.contains("mode-3d")) resize();
    }).observe(container);
  }
  V.ready = true;
  return true;
}

/* --------------------------------------------------------------- 载入与取景 */

function fileKey(files) {
  return (files || []).map((f) => f.url || f.rel || "").join("|");
}

function fileUrl(f) {
  return encodeURI(f.url || ("/model/" + (f.rel || "")));
}

function clearModel() {
  for (const child of [...V.root.children]) {
    V.root.remove(child);
    if (child.geometry) child.geometry.dispose();
  }
  for (const m of V.materials) m.dispose();
  V.materials = [];
}

function loadFiles(files, label) {
  V.token += 1;
  const token = V.token;
  clearModel();
  const total = files.length;
  if (!total) {
    if (V.statsEl) V.statsEl.textContent = "";
    setNote("该资产没有可载入的 *.stl（服务端 --assets 目录里没有模型）。");
    return;
  }
  const t0 = performance.now();
  let done = 0;
  setNote(`载入 ${label}：${total} 件…`);
  for (const f of files) {
    const color = CRAFT_COLORS[f.craft] || FALLBACK_COLOR;
    const mat = new THREE.MeshStandardMaterial({
      color, metalness: 0.25, roughness: 0.62, flatShading: true,
    });
    mat.wireframe = wireOn();
    V.materials.push(mat);
    V.loader.load(fileUrl(f), (geometry) => {
      if (token !== V.token) { geometry.dispose(); return; }
      V.root.add(new THREE.Mesh(geometry, mat));
      done += 1;
      if (done === total) finishLoad(label, t0);
      else setNote(`载入 ${label}：${done}/${total} 件…`);
    }, (ev) => {
      if (token === V.token && ev && ev.total) {
        const pct = Math.round(100 * ev.loaded / ev.total);
        setNote(`载入 ${label}：${done}/${total} 件 · 当前件 ${pct}%`);
      }
    }, (err) => {
      if (token !== V.token) return;
      done += 1;
      setNote(`载入失败：${f.label || f.url}（${(err && err.message) || err}）`);
      if (done === total) finishLoad(label, t0);
    });
  }
}

function finishLoad(label, t0) {
  V.root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(V.root);
  const size = box.getSize(new THREE.Vector3());
  const span = Math.max(size.x, size.z, 1);
  V.scene.remove(V.grid, V.axes);
  V.grid = new THREE.GridHelper(span * 2.2, 22, 0x2a3140, 0x1a2029);
  V.axes = new THREE.AxesHelper(span * 0.08);
  V.grid.visible = gridOn();
  V.axes.visible = gridOn();
  V.scene.add(V.grid, V.axes);
  fit(box);
  let tris = 0;
  V.root.traverse((o) => {
    if (o.isMesh && o.geometry && o.geometry.attributes.position) {
      tris += o.geometry.attributes.position.count / 3;
    }
  });
  if (V.statsEl) {
    const m = (v) => (v >= 1 ? `${v.toFixed(2)}m` : `${(v * 1000).toFixed(0)}mm`);
    // 画面里 Y 朝上＝Blender 的 +Z；对外报 Blender 口径（X×Y×Z）
    V.statsEl.textContent = `三角 ${Math.round(tris).toLocaleString()} · `
      + `包围盒 ${m(size.x)}×${m(size.z)}×${m(size.y)} · `
      + `载入 ${((performance.now() - t0) / 1000).toFixed(1)}s`;
    V.statsEl.title = `文件：${label}；1 单位 = 1 m（STL 口径）`;
  }
  setNote(`${label} 已载入 · 左键旋转 · 右键平移 · 滚轮缩放 · 1 单位 = 1 m（Blender 口径）`);
}

function fit(box) {
  const b = box || new THREE.Box3().setFromObject(V.root);
  if (b.isEmpty()) return;
  const center = b.getCenter(new THREE.Vector3());
  const radius = Math.max(b.getSize(new THREE.Vector3()).length() * 0.5, 1);
  const dist = radius / Math.sin((V.camera.fov * Math.PI / 180) / 2) * 1.12;
  const dir = new THREE.Vector3(0.85, 0.5, 0.95).normalize();
  V.camera.position.copy(center).addScaledVector(dir, dist);
  V.camera.near = Math.max(dist / 2000, 0.03);
  V.camera.far = dist * 30;
  V.camera.updateProjectionMatrix();
  V.controls.target.copy(center);
  V.controls.update();
  renderOnce();
}

/* --------------------------------------------------------------- 对外接口 */

export function open(opts) {
  const o = opts || {};
  if (!ensureScene()) return;
  const files = o.files || [];
  const label = o.label || "模型";
  V.files = files;
  V.label = label;
  resize();
  const key = fileKey(files);
  if (key === V.loadedKey) { renderOnce(); return; }   // 同一份文件：不重载、不丢相机
  V.loadedKey = key;
  loadFiles(files, label);
}

export function resize() {
  if (!V.ready || !V.container) return;
  const w = V.container.clientWidth || 1;
  const h = V.container.clientHeight || 1;
  V.renderer.setSize(w, h, false);
  V.camera.aspect = w / h;
  V.camera.updateProjectionMatrix();
  renderOnce();
}

export function setWireframe(on) {
  localStorage.setItem("primer3dWire", on ? "1" : "0");
  for (const m of V.materials) m.wireframe = !!on;
  renderOnce();
}

export function setGrid(on) {
  localStorage.setItem("primer3dGrid", on ? "1" : "0");
  if (V.grid) V.grid.visible = !!on;
  if (V.axes) V.axes.visible = !!on;
  renderOnce();
}

export function resetView() { fit(); }

export function reload() { loadFiles(V.files, V.label); }
