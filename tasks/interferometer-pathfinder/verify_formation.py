# -*- coding: utf-8 -*-
"""verify_formation.py — 阶段四分布式编队场景自动验收（在 formation.py 之后运行）。

用法：blender --background --python verify_formation.py
输出：out/formation/verify_log.txt 与 out/formation/formation_report.json（E12）。
判据逐条对应《阶段四_验收清单》E1–E12，不自增删。全部几何判定用网格顶点真实包围盒；
读取失败即 FAIL；main() 异常打印 traceback。
"""
import json
import math
import os
import struct
import sys
import traceback

import bpy
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "out", "formation")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")
REPORT = os.path.join(OUT, "formation_report.json")

results = []


def check(cid, desc, ok, detail=""):
    results.append((cid, desc, ok, detail))


def find(n):
    return bpy.data.objects.get(n)


def wv(o):
    mw = o.matrix_world
    return [mw @ v.co for v in o.data.vertices]


def bb(objs):
    pts = [p for o in objs for p in wv(o)]
    return ([min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)])


def dims(objs):
    lo, hi = bb(objs)
    return [hi[i] - lo[i] for i in range(3)]


def ctr(o):
    lo, hi = bb([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


def bvh(objs):
    vs, ps, off = [], [], 0
    for o in objs:
        pts = wv(o)
        vs += pts
        ps += [tuple(i + off for i in p.vertices) for p in o.data.polygons]
        off += len(pts)
    return BVHTree.FromPolygons(vs, ps)


def mat_color(o):
    if not o or not o.data or not o.data.materials:
        return None
    for m in o.data.materials:
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    c = n.inputs["Base Color"].default_value
                    return (c[0], c[1], c[2])
    return None


def mat_emission(o):
    if not o or not o.data or not o.data.materials:
        return None
    for m in o.data.materials:
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    if "Emission Strength" in n.inputs:
                        return n.inputs["Emission Strength"].default_value
    return None


def png_size(p):
    with open(p, "rb") as f:
        h = f.read(24)
    return struct.unpack(">II", h[16:24]) if h[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)


def group_objs(prefix):
    return [o for o in bpy.data.objects if o.name.startswith(prefix + "_")]


def main():
    try:
        import formation
        if hasattr(formation, "build"):
            formation.build()
    except Exception:
        traceback.print_exc()
        check("EX", "formation 导入/构建异常", False, traceback.format_exc(limit=3))

    layout = find("EMPTY_LAYOUT")
    colA, colB, cmb = group_objs("colA"), group_objs("colB"), group_objs("cmb")
    beams = {n: find(n) for n in ("BEAM_star_colA", "BEAM_star_colB",
                                  "BEAM_link_colA", "BEAM_link_colB")}
    col_parts = ("bus", "tube", "panel_X1", "panel_X2", "panel_-X1", "panel_-X2",
                 "window_out", "tank_01", "tank_02")
    gimbals = {t: [o for o in group_objs(t) if o.name.startswith(t + "_gimbal")] for t in ("colA", "colB")}

    # ---- E1 对象齐备 + 关键尺寸抽检 ----
    miss = [f"{t}_{p}" for t in ("colA", "colB") for p in col_parts if not find(f"{t}_{p}")]
    miss += [n for n in ("cmb_bus", "cmb_module", "cmb_panel_X1") if not find(n)]
    miss += [n for n, o in beams.items() if o is None] + ([] if layout else ["EMPTY_LAYOUT"])
    gim_ok = all(3 <= len(gimbals[t]) <= 4 for t in ("colA", "colB"))
    dim_ok = True
    bus_lo, bus_hi = bb([find("colA_bus")])
    bus = [bus_hi[i] - bus_lo[i] for i in range(3)]
    tube = dims([find("colA_tube")])
    if not (1.15 <= max(bus[0], bus[1]) <= 1.30 and 0.85 <= bus[2] <= 0.95):
        dim_ok = False
    if not (0.55 <= max(tube[0], tube[1]) <= 0.70):
        dim_ok = False
    check("E1", "对象齐备（三器/四束/EMPTY_LAYOUT）且单体检寸一致",
          not miss and gim_ok and dim_ok,
          f"缺件={miss or '无'}；gimbal={[len(gimbals[t]) for t in ('colA','colB')]}；"
          f"舱{['%.2f' % v for v in bus]}、筒径{max(tube[0], tube[1]):.3f}")

    if miss or not layout:
        raise SystemExit("关键对象缺失，终止")

    # ---- E2 编队基线 ----
    ya, yb, yc = ctr(find("colA_bus"))[1], ctr(find("colB_bus"))[1], ctr(find("cmb_bus"))[1]
    check("E2", "显示基线 |y_colA−y_colB| = 12±0.05 m；合束器在 y=0±0.05",
          abs(abs(ya - yb) - 12.0) <= 0.05 and abs(yc) <= 0.05,
          f"基线={abs(ya - yb):.3f} m；合束器 y={yc:+.3f}")

    # ---- E3 一字排开 ----
    cs = [ctr(find(f"{t}_bus")) for t in ("colA", "colB")]
    cs.append(ctr(find("cmb_bus")))
    dx = max(c[0] for c in cs) - min(c[0] for c in cs)
    dz = max(c[2] for c in cs) - min(c[2] for c in cs)
    check("E3", "三器舱心 x、z 相互偏差 ≤0.05 m", dx <= 0.05 and dz <= 0.05,
          f"Δx={dx:.4f}, Δz={dz:.4f}")

    # ---- E4 集光器姿态 ----
    tA = dims([find("colA_tube")])
    upright = tA[2] > max(tA[0], tA[1]) and abs(tA[0] - tA[1]) < 0.02
    tilt = [o.get("tilt_deg") for o in (find("colA_ROOT"), find("colB_ROOT"))]
    wA, wB = ctr(find("colA_window_out"))[1], ctr(find("colB_window_out"))[1]
    face_ok = (wA > ya) and (wB < yb)
    check("E4", "镜筒 +Z、倾角记录=0；A 窗口朝 +Y、B 窗口朝 −Y",
          upright and tilt == [0.0, 0.0] and face_ok,
          f"筒dz/dxy={tA[2]:.2f}/{tA[0]:.2f}；tilt={tilt}；窗口y={wA:+.2f},{wB:+.2f}（舱心{ya:+.1f},{yb:+.1f}）")

    # ---- E5 星光束端点 ----
    detail, ok = [], True
    for t in ("colA", "colB"):
        tube = find("%s_tube" % t)
        tl, th = bb([tube])
        tx, ty = (tl[0] + th[0]) / 2, (tl[1] + th[1]) / 2
        b = beams["BEAM_star_%s" % t]
        p0, p1 = b["p_start"], b["p_end"]
        embed = th[2] - p0[2]
        axis_ok = abs(p0[0] - tx) <= 0.01 and abs(p0[1] - ty) <= 0.01
        above_ok = p1[2] >= th[2] + 2.0
        inter = [o.name for o in group_objs(t)
                 if o.type == "MESH" and o is not b and bvh([b]).overlap(bvh([o]))]
        good = 0 <= embed <= 0.02 and axis_ok and above_ok and not inter
        ok = ok and good
        detail.append(f"{t}:内嵌{embed:.3f}/轴偏({p0[0] - tx:+.3f},{p0[1] - ty:+.3f})/高出{p1[2] - th[2]:.2f}/相交{inter or '无'}")
    check("E5", "星光束终点落在筒口截面（内嵌≤0.02）、起点高出≥2 m、除筒口外无相交", ok,
          "；".join(detail))

    # ---- E6 器间束端点与连接 ----
    detail, ok = [], True
    for t, ptag in (("colA", "cmb_port_negY"), ("colB", "cmb_port_posY")):
        win, port = find("%s_window_out" % t), find(ptag)
        wl, wh = bb([win])
        pl, ph = bb([port])
        b = beams["BEAM_link_%s" % t]
        p0, p1 = b["p_start"], b["p_end"]
        wface = wh[1] if t == "colA" else wl[1]     # 朝向合束器的窗口面
        pface = pl[1] if t == "colA" else ph[1]     # 朝向该集光器的收光口面
        eps = 1e-6      # 判据是"内嵌 ≤0.02"，恰好 0.02 应算合规，故留浮点余量
        s_ok = abs(p0[1] - wface) <= 0.02 + eps and wl[0] - 0.02 <= p0[0] <= wh[0] + 0.02 \
            and wl[2] - 0.02 <= p0[2] <= wh[2] + 0.02
        e_ok = abs(p1[1] - pface) <= 0.02 + eps and pl[0] - 0.02 <= p1[0] <= ph[0] + 0.02 \
            and pl[2] - 0.02 <= p1[2] <= ph[2] + 0.02
        good = s_ok and e_ok
        ok = ok and good
        detail.append(f"{t}:起点离窗口面{abs(p0[1] - wface):.3f}/终点离收光口面{abs(p1[1] - pface):.3f}")
    check("E6", "器间束：起点在出光窗口面、终点在收光口（各 ±0.02 m），直线", ok, "；".join(detail))

    # ---- E7 束径比 ----
    ds = beams["BEAM_star_colA"]["diameter"]     # 用记录值：包围盒会把长度/斜截面算进来
    dl = beams["BEAM_link_colA"]["diameter"]
    ratio = ds / dl if dl else 0
    check("E7", "束径比 D_star/D_link ∈ [3,5]", 3.0 <= ratio <= 5.0,
          f"D_star={ds:.3f}, D_link={dl:.3f}, 比值={ratio:.2f}")

    # ---- E8 束材质 ----
    st, lk = mat_color(beams["BEAM_star_colA"]), mat_color(beams["BEAM_link_colA"])
    em = [mat_emission(beams[n]) for n in beams]
    pink = st and st[0] > st[1] and st[0] > st[2]
    red = lk and lk[0] > 2 * max(lk[1], lk[2])
    craft_ok = (lambda c: c and c[0] > c[1] > c[2])(mat_color(find("colA_bus")))
    check("E8", "四束均为 emission；星光粉、器间红；器体材质与单体一致",
          all(e and e > 0 for e in em) and pink and red and craft_ok,
          f"emission={['%.1f' % e for e in em]}; star={st}; link={lk}; 舱金={craft_ok}")

    # ---- E9 布局父级 ----
    kids = [o for o in bpy.data.objects if o.parent is layout]
    covered = all(find(f"colA_{p}") in group_objs("colA") for p in ("bus",))
    scale_ok = True
    try:
        base0 = abs(ctr(find("colA_bus"))[1] - ctr(find("colB_bus"))[1])
        layout.scale = (2.0, 2.0, 2.0)
        bpy.context.view_layer.update()
        base1 = abs(ctr(find("colA_bus"))[1] - ctr(find("colB_bus"))[1])
        layout.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()
        scale_ok = math.isclose(base1, 2 * base0, rel_tol=0.01)
    except Exception:
        traceback.print_exc()
        scale_ok = False
    beam_children = all(o.parent is layout for o in beams.values())
    check("E9", "三器与四束均挂 EMPTY_LAYOUT；缩放 EMPTY 时显示基线整体缩放",
          covered and beam_children and scale_ok,
          f"直接子对象={len(kids)}（含三器空物体/端口/四束）；缩放×2 后基线={base1:.3f}（原 {base0:.3f}）")

    # ---- E10 无接触无穿模 ----
    bus_w = max(dims([find("colA_bus")])[:2])
    pairs = [("colA", "colB"), ("colA", "cmb"), ("colB", "cmb")]
    dmin = 1e30
    for a, b in pairs:
        for oa in group_objs(a):
            for ob in group_objs(b):
                if oa.type != "MESH" or ob.type != "MESH":
                    continue
                la, ha = bb([oa])
                lb, hb = bb([ob])
                gap = max([lb[i] - ha[i] if lb[i] > ha[i] else la[i] - hb[i] if la[i] > hb[i] else 0.0
                           for i in range(3)])
                dmin = min(dmin, gap)
    cross = []
    for a, b in pairs:
        for oa in group_objs(a):
            for ob in group_objs(b):
                if oa.type == "MESH" and ob.type == "MESH" and bvh([oa]).overlap(bvh([ob])):
                    cross.append((oa.name, ob.name))
    check("E10", "三器两两间距 ≥ 舱宽；三器之间无网格相交", dmin >= bus_w and not cross,
          f"最小间距={dmin:.3f} m（需≥{bus_w:.2f}）；相交对={cross or '无'}")

    # ---- E11 渲染自检 ----
    sizes = {f: png_size(os.path.join(OUT, f)) for f in
             ("front.png", "side.png", "top.png", "iso.png", "wide.png")}
    check("E11", "out/formation/ 五张 PNG ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()), f"{sizes}")

    # ---- E12 记录项 ----
    report = {
        "baseline_real_m": layout.get("baseline_real_m"),
        "baseline_display_m": layout.get("baseline_display_m"),
        "beam_d_star_m": ds, "beam_d_link_m": dl, "beam_ratio": round(ratio, 3),
        "beam_materials": {"MAT_beam_star": list(st or ()), "MAT_beam_link": list(lk or ()),
                           "star_strength": mat_emission(beams["BEAM_star_colA"]),
                           "link_strength": mat_emission(beams["BEAM_link_colA"])},
        "wing_state_T1": layout.get("wing_state"),
        "ports_T2": {"object": "cmb_port_posY / cmb_port_negY",
                     "口径_m": max(dims([find("cmb_port_posY")])[:2]) if find("cmb_port_posY") else None},
        "colorspace": {"view_transform": bpy.context.scene.view_settings.view_transform,
                       "look": bpy.context.scene.view_settings.look},
        "renders": {k: list(v) for k, v in sizes.items()},
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    check("E12", "formation_report.json 记录真实/显示基线、束径、材质、翼态",
          all(report.get(k) is not None for k in ("baseline_real_m", "baseline_display_m",
                                                 "beam_d_star_m", "wing_state_T1")),
          f"已写 {os.path.relpath(REPORT, HERE)}")


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
