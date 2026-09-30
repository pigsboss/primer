# -*- coding: utf-8 -*-
"""verify_assembly.py — 三器组合体装配的自动验收（在 assembly.py 之后运行）。

用法：blender --background --python verify_assembly.py，日志写 out/assembly/verify_log.txt。
判据**逐条照抄**《验收反馈 03》（2026-09-30）§三 D1–D12，不得自增删。

三处按字面会误判、已在代码里写明读法（并在交付说明中回报，不擅自改判据文字）：
  * D1 "平板类对象" → 除豁免件（筒/载荷舱/机构/翼/窗口/储箱）外的结构件，若网格真实
    包围盒最小边 ≤0.08 m 即视为薄板；另按对象名子串 deck/beam/truss/plate 直接判违规。
  * D4 "平台舱顶面上方对象清单恰好为 {集光器A, 集光器B, 载荷舱}" → 按**坐在该面上**判定
    （对象包围盒底面落在面高 ±0.05 m 内），按所属件归并。否则合束器那副展开翼（其几何
    本就在平台舱之上）会被误判，而规格 §二.4 明确要求它保持展开。
  * D7 "±X 侧面" → 平台舱为矩形，集光器舱为梯形，故用顶点插值求该 y 处的面半宽，不假定面是平的。
"""
import math
import os
import struct
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "out", "assembly")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")

ENV_R, ENV_H = 1.825, 4.610
EXEMPT = ("tube", "module", "gimbal", "recv", "aux", "panel", "window", "tank")
FORBIDDEN_NAME = ("deck", "beam", "truss", "plate")
results = []


def check(cid, desc, ok, detail=""):
    results.append((cid, desc, ok, detail))


def find(name):
    return bpy.data.objects.get(name)


def wverts(o):
    mw = o.matrix_world
    return [mw @ v.co for v in o.data.vertices]


def vbounds(objs):
    pts = [p for o in objs for p in wverts(o)]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def vdims(objs):
    lo, hi = vbounds(objs)
    return [hi[i] - lo[i] for i in range(3)]


def vcenter(o):
    lo, hi = vbounds([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


def max_color(obj):
    if not obj or not obj.data or not hasattr(obj.data, "materials"):
        return None
    for m in obj.data.materials:
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    c = n.inputs["Base Color"].default_value
                    return (c[0], c[1], c[2])
        elif m:
            c = m.diffuse_color
            return (c[0], c[1], c[2])
    return None


def owner(name):
    return name.split("_")[0]


def face_half_width(bus, y):
    """梯形舱在该 y 处的 ±X 面半宽（顶点插值，对 180° 旋转也成立）。"""
    lo, hi = vbounds([bus])
    ylo, yhi = lo[1], hi[1]
    span = yhi - ylo
    if span <= 0:
        return max(hi[0] - lo[0], 0) / 2
    t = (y - ylo) / span
    # 两端各自的 x 半宽
    xs_lo = [abs(v.x) for v in wverts(bus) if abs(v.y - ylo) < 0.03]
    xs_hi = [abs(v.x) for v in wverts(bus) if abs(v.y - yhi) < 0.03]
    w_lo = max(xs_lo) if xs_lo else 0.0
    w_hi = max(xs_hi) if xs_hi else 0.0
    return w_lo + (w_hi - w_lo) * t


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", head[16:24])


def main():
    try:
        import assembly
        if hasattr(assembly, "build"):
            assembly.build()
    except Exception:
        traceback.print_exc()
        check("EX", "assembly 导入/构建异常", False, traceback.format_exc(limit=3))

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    bay = find("bay")
    buses = {t: find("%s_bus" % t) for t in ("colA", "colB")}
    tubes = {t: find("%s_tube" % t) for t in ("colA", "colB")}
    module = find("cmbmod_module")

    # ---- D1 对象齐备且无独立连接件 ----
    need = ("bus", "tube", "panel_X1", "panel_X2", "panel_-X1", "panel_-X2", "window_out",
            "tank_01", "tank_02", "gimbal_01", "gimbal_02", "gimbal_03")
    miss = [f"{t}_{n}" for t in ("colA", "colB") for n in need if not find(f"{t}_{n}")]
    missing_cmb = [n for n in ("bay", "cmbmod_module", "cmbmod_recv_01", "cmbmod_recv_02")
                   if not find(n)]
    by_name = [o.name for o in meshes if any(k in o.name.lower() for k in FORBIDDEN_NAME)]
    struct = [o for o in meshes if not any(k in o.name for k in EXEMPT)]
    thin = [(o.name, round(min(vdims([o])), 3)) for o in struct if min(vdims([o])) <= 0.08]
    check("D1", "对象齐备且无独立连接件（deck/beam/truss/plate）",
          not miss and not missing_cmb and not by_name and not thin,
          f"缺件={miss + missing_cmb or '无'}；按名命中={by_name or '无'}；薄板类结构件={thin or '无'}")

    if not (bay and all(buses.values()) and all(tubes.values()) and module):
        raise SystemExit("关键对象缺失，终止后续检查")

    bay_lo, bay_hi = vbounds([bay])

    # ---- D2 基线 ----
    ya, yb = vcenter(tubes["colA"])[1], vcenter(tubes["colB"])[1]
    check("D2", "基线（两 tube 轴线 y 之差）∈ [2.3,2.6] m", 2.3 <= abs(ya - yb) <= 2.6,
          f"baseline={abs(ya - yb):.3f} m")

    # ---- D3 对称居中 ----
    mc = vcenter(module)
    check("D3", "对称居中：|y_A+y_B|≤0.1；载荷舱 |y|≤0.3、|x|≤0.15",
          abs(ya + yb) <= 0.1 and abs(mc[1]) <= 0.3 and abs(mc[0]) <= 0.15,
          f"y_A+y_B={ya + yb:+.3f}；载荷舱 y={mc[1]:+.3f}, x={mc[0]:+.3f}")

    # ---- D4 两层支撑 ----
    gaps = {t: vbounds([buses[t]])[0][2] - bay_hi[2] for t in ("colA", "colB")}
    g_mod = vbounds([module])[0][2] - bay_hi[2]
    ok_gap = all(-0.05 <= g <= 0.05 for g in gaps.values()) and -0.05 <= g_mod <= 0.05
    # 坐在平台舱顶面上的对象（按其所属件归并）
    sitters = sorted({owner(o.name) for o in meshes
                      if o is not bay and abs(vbounds([o])[0][2] - bay_hi[2]) <= 0.05})
    check("D4", "两层支撑：两集光器舱底与载荷舱底直接贴合平台舱顶面；面上对象恰为 {集光器A,B,载荷舱}",
          ok_gap and sitters == ["cmbmod", "colA", "colB"],
          f"间隙={[round(g, 3) for g in gaps.values()]}+载荷舱{g_mod:+.3f}；面上件={sitters}")

    # ---- D5 同层成排 ----
    dh = abs(vbounds([module])[1][2] - max(vbounds([buses[t]])[1][2] for t in ("colA", "colB")))
    check("D5", "载荷舱顶面与集光器舱顶面高差 ≤0.3 m（顶视三者同层成排）", dh <= 0.3, f"高差={dh:.3f} m")

    # ---- D6 金色包覆 ----
    bad = []
    for o in struct:
        c = max_color(o)
        if not (c and c[0] > c[1] > c[2]):
            bad.append((o.name, c))
    check("D6", "金色包覆：除豁免件外一切可见结构件金色 MLI", not bad, f"非金色结构件={bad or '无'}")

    # ---- D7 集光器翼折叠 ----
    detail, ok = [], True
    bus_w = max(vdims([buses["colA"]])[0], vdims([buses["colA"]])[1])
    for t in ("colA", "colB"):
        bus = buses[t]
        blo, bhi = vbounds([bus])
        for k in ("X1", "X2", "-X1", "-X2"):
            p = find("%s_panel_%s" % (t, k))
            plo, phi = vbounds([p])
            dims = [phi[i] - plo[i] for i in range(3)]
            inner = min(abs(plo[0]), abs(phi[0]))
            gap = inner - face_half_width(bus, (plo[1] + phi[1]) / 2)
            proj_ok = (phi[1] - plo[1]) <= 1.1 * (bhi[1] - blo[1]) and \
                      (phi[2] - plo[2]) <= 1.1 * (bhi[2] - blo[2])
            good = dims[0] <= 0.08 and abs(gap) <= 0.10 and proj_ok
            ok = ok and good
            detail.append(f"{t}{k}:厚{dims[0]:.3f}/间隙{gap:+.3f}/投影{'ok' if proj_ok else 'BAD'}")
    check("D7", "集光器翼折叠：翼面与舱 ±X 侧面平行贴合（间隙≤0.10），投影不超舱体 1.1 倍",
          ok, "；".join(detail))

    # ---- D8 合束器翼展开 ----
    cmb_panels = [find("cmb_panel_%s" % k) for k in ("X1", "X2", "-X1", "-X2")]
    cmb_panels = [p for p in cmb_panels if p]
    if len(cmb_panels) == 4:
        lo, hi = vbounds(cmb_panels)
        vertical = all(vdims([p])[1] <= 0.08 and vdims([p])[2] >= 0.5 for p in cmb_panels)
        xs = [vcenter(p)[0] for p in cmb_panels]
        check("D8", "合束器翼展开：±X 两翼、板面竖立（弦向沿 Z）、X 跨度 ≥3×舱宽",
              vertical and hi[0] - lo[0] >= 3 * bus_w and min(xs) < 0 < max(xs),
              f"X 跨度={hi[0] - lo[0]:.3f}（需≥{3 * bus_w:.2f}），竖立={vertical}，x={[round(v, 2) for v in xs]}")
    else:
        check("D8", "合束器翼展开", False, f"合束器翼面不足 4 块：{len(cmb_panels)}")

    # ---- D9 储箱朝向 ----
    detail, ok = [], True
    for t in ("colA", "colB"):
        by = vcenter(buses[t])[1]
        for k in (1, 2):
            ty = vcenter(find("%s_tank_%02d" % (t, k)))[1]
            off = ty - by
            good = (off > 0) != (by > 0) and abs(off) < 1.0
            ok = ok and good
            detail.append(f"{t}{k}: 偏移{off:+.2f}")
    for k in (1, 2):
        tx = vcenter(find("cmb_tank_%02d" % k))[0]
        bw = vdims([bay])[0] / 2
        good = abs(abs(tx) - bw) <= 0.15
        ok = ok and good
        detail.append(f"bay{k}: |x|={abs(tx):.2f}/半宽{bw:.2f}")
    check("D9", "储箱朝向：集光器朝内（相对舱心偏移指向合束器）、合束器在 ±X 侧面", ok,
          "；".join(detail))

    # ---- D10 材质色 ----
    cb, ct, cm = max_color(buses["colA"]), max_color(tubes["colA"]), max_color(module)
    ck, cp = max_color(find("colA_tank_01")), max_color(find("cmb_panel_X1"))
    ok = (cb and cb[0] > cb[1] > cb[2]) and (ct and all(v < 0.1 for v in ct)) \
        and (cm and all(v < 0.35 for v in cm)) and (ck and all(v > 0.8 for v in ck)) \
        and (cp and cp[2] > cp[0])
    check("D10", "材质色：舱/架金、筒与载荷舱近黑、储箱乳白、翼蓝", bool(ok),
          f"bus={cb}, tube={ct}, module={cm}, tank={ck}, panel={cp}")

    # ---- D11 包络 ----
    ymax = 0.0
    for o in (buses["colA"], buses["colB"], bay):
        l, h = vbounds([o])
        ymax = max(ymax, abs(l[1]), abs(h[1]))
    lo, hi = vbounds(meshes)
    check("D11", "包络：三器舱体外缘 max|y|≤1.85 m、总高≤4.61 m",
          ymax <= 1.85 and (hi[2] - lo[2]) <= ENV_H,
          f"max|y|={ymax:.3f}/1.85；总高={hi[2] - lo[2]:.3f}/{ENV_H}")

    # ---- D12 渲染自检 ----
    sizes = {}
    for f in ("side.png", "front.png", "iso.png", "top.png"):
        p = os.path.join(OUT, f)
        sizes[f] = png_size(p) if os.path.exists(p) else (0, 0)
    check("D12", "out/assembly/ 四视角 PNG ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()), f"{sizes}")


def report():
    lines = [f"[{'PASS' if ok else 'FAIL'}] {cid} {desc} | {detail}"
             for cid, desc, ok, detail in results]
    n = sum(1 for r in results if r[2])
    lines.append(f"\n{n}/{len(results)} PASS")
    text = "\n".join(lines)
    print(text)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(text + "\n")


try:
    main()
except Exception:
    traceback.print_exc()
    results.append(("EX", "执行异常", False, traceback.format_exc(limit=2)))
report()
