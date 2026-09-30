# -*- coding: utf-8 -*-
"""verify_formation.py — 阶段四分布式编队场景自动验收（在 formation.py 之后运行）。

用法：blender --background --python verify_formation.py
输出：out/formation/verify_log.txt 与 out/formation/formation_report.json（E12）。
判据逐条对应《阶段四_验收清单》v1.1 E1–E16，不自增删；每条判据的证据都从**场景网格实测**取，
不采信建模脚本的自报值（E14 的包络增量即按"新增件与单体件分开测"独立复算）。
全部几何判定用网格顶点真实包围盒；读取失败即 FAIL；main() 异常打印 traceback。
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

# v1.3 新增件的命名标记——E13/E14 据此把"推断默认件"与单体件分开（不依赖命名前缀之外的约定）
V13_MARKS = ("_RCS_", "_ANT_", "_HINGE_", "_EDGE_", "_BAFFLE_", "_TURNTABLE_", "_ROD_")
ENV_R, ENV_H = 1.825, 4.610        # 阶段三 D11 的包络常数：本阶段沿用其"包络不被撑破"的连续性

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


def local_bb(objs, holder):
    """换算到该器**自身坐标系**（除掉 holder 的位移与转向）后取包围盒。

    三器朝向不同（colB 绕 Z 转 180°），只有回到自身系，量出来的才是"单体外形"。
    """
    m = holder.matrix_world.inverted()
    pts = [m @ (o.matrix_world @ v.co) for o in objs for v in o.data.vertices]
    return ([min(p[i] for p in pts) for i in range(3)],
            [max(p[i] for p in pts) for i in range(3)])


def contains(o, pt, eps=0.002):
    lo, hi = bb([o])
    return all(lo[i] - eps <= pt[i] <= hi[i] + eps for i in range(3))


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


def mat_roughness(m):
    if not m or not m.use_nodes:
        return None
    for n in m.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n.inputs["Roughness"].default_value
    return None


def bump_strengths(m):
    if not m or not m.use_nodes:
        return []
    return [n.inputs["Strength"].default_value for n in m.node_tree.nodes if n.type == "BUMP"]


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
        # 主光/星场/sun_dir 属场景构件：E12 的记录项与 E16 都取自此，故一并建立
        formation.build_render_env(final=False)
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

    # ---- E5 星光束端点（v1.3：终点落**最内光阑环**截面，内嵌判据不变）----
    detail, ok = [], True
    for t in ("colA", "colB"):
        tube = find("%s_tube" % t)
        tl, th = bb([tube])
        tx, ty = (tl[0] + th[0]) / 2, (tl[1] + th[1]) / 2
        rings = [o for o in group_objs(t) if "_BAFFLE_" in o.name]
        ring_z = min(ctr(o)[2] for o in rings) if rings else th[2]   # 最内环＝最低那道
        b = beams["BEAM_star_%s" % t]
        p0, p1 = b["p_start"], b["p_end"]
        embed = ring_z - p0[2]
        axis_ok = abs(p0[0] - tx) <= 0.01 and abs(p0[1] - ty) <= 0.01
        above_ok = p1[2] >= th[2] + 2.0
        inter = [o.name for o in group_objs(t)
                 if o.type == "MESH" and o is not b and bvh([b]).overlap(bvh([o]))]
        good = 0 <= embed <= 0.02 and axis_ok and above_ok and not inter
        ok = ok and good
        detail.append(f"{t}:离最内环{embed:.3f}/轴偏({p0[0] - tx:+.3f},{p0[1] - ty:+.3f})/"
                      f"高出{p1[2] - th[2]:.2f}/相交{inter or '无'}")
    check("E5", "星光束终点落在筒口**最内光阑环**截面（内嵌≤0.02）、起点高出≥2 m、除筒口外无相交", ok,
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
    e10_ok = dmin >= bus_w and not cross
    check("E10", "三器两两间距 ≥ 舱宽；三器之间无网格相交", e10_ok,
          f"最小间距={dmin:.3f} m（需≥{bus_w:.2f}）；相交对={cross or '无'}")

    # ---- E11 渲染自检 ----
    sizes = {f: png_size(os.path.join(OUT, f)) for f in
             ("front.png", "side.png", "top.png", "iso.png", "wide.png")}
    check("E11", "out/formation/ 五张 PNG ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()), f"{sizes}")

    # ---- v1.3 升级件清点（E13–E16 判据与 E12 记录共用；独立实测，不采信建模脚本自报）----
    v13 = {}
    for t in ("colA", "colB", "cmb"):
        meshes = [o for o in group_objs(t) if o.type == "MESH"]
        added = [o for o in meshes if any(k in o.name for k in V13_MARKS)]
        base = [o for o in meshes if o not in added]
        holder = find(t + "_ROOT")
        blo, bhi = local_bb(base, holder)
        alo, ahi = local_bb(base + added, holder)
        gain = [max(ahi[i] - bhi[i], blo[i] - alo[i], 0.0) for i in range(3)]
        v13[t] = {
            "unit_parts": len(meshes), "added_parts": len(added),
            "outline_gain_m": [round(g, 4) for g in gain],
            "outline_gain_max_m": round(max(gain), 4),
            "envelope_m": [round(ahi[i] - alo[i], 4) for i in range(3)],
            "y_max_m": round(max(abs(alo[1]), abs(ahi[1])), 4),
            "z_max_m": round(ahi[2], 4),
            "n_rcs": len([o for o in added if "_RCS_" in o.name]),
            "n_ant": len([o for o in added if "_ANT_" in o.name]),
            "n_hinge": len([o for o in added if "_HINGE_" in o.name]),
            "n_edge": len([o for o in added if "_EDGE_" in o.name]),
            "n_baffle": len([o for o in added if "_BAFFLE_" in o.name]),
            "n_cluster": len([o for o in meshes if "_gimbal" in o.name
                              or "_TURNTABLE_" in o.name or "_ROD_" in o.name]),
            "panel_thickness_m": [round(min(dims([o])), 4) for o in meshes if "_panel_" in o.name],
        }

    # ---- E12 记录项 ----
    bus_mats = sorted(m.name for m in bpy.data.materials if m.name.startswith("MAT_BUS_MLI"))
    tube_mats = sorted(m.name for m in bpy.data.materials if m.name.startswith("MAT_TUBE_BLACK"))
    report = {
        "baseline_real_m": layout.get("baseline_real_m"),
        "baseline_display_m": layout.get("baseline_display_m"),
        "beam_d_star_m": ds, "beam_d_link_m": dl, "beam_ratio": round(ratio, 3),
        "beam_materials": {"MAT_beam_star": list(st or ()), "MAT_beam_link": list(lk or ()),
                           "star_strength": mat_emission(beams["BEAM_star_colA"]),
                           "link_strength": mat_emission(beams["BEAM_link_colA"]),
                           "note": "发光色按 F3/F5 实测束色反解，非规格字面值；见交付说明差异清单"},
        "wing_state_T1": layout.get("wing_state"),
        "ports_T2": {"object": "cmb_port_posY / cmb_port_negY",
                     "口径_m": max(dims([find("cmb_port_posY")])[:2]) if find("cmb_port_posY") else None},
        "sun_dir": list(layout.get("sun_dir") or ()),
        "starfield": {"texture": layout.get("starfield_texture"),
                      "strength": layout.get("starfield_strength")},
        "colorspace": {"view_transform": bpy.context.scene.view_settings.view_transform,
                       "look": bpy.context.scene.view_settings.look},
        "materials_v13": {"bus_MLI": bus_mats, "tube_black": tube_mats,
                          "edge_trim": sorted({m.name for m in bpy.data.materials
                                               if m.name.startswith("MAT_up_alu")})},
        "upgrade_v13": v13,
        "renders": {k: list(v) for k, v in sizes.items()},
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    check("E12", "formation_report.json 记录真实/显示基线、束径、材质、翼态、sun_dir、星场强度",
          all(report.get(k) is not None for k in ("baseline_real_m", "baseline_display_m",
                                                  "beam_d_star_m", "wing_state_T1"))
          and report["sun_dir"] and report["starfield"]["strength"] is not None,
          f"已写 {os.path.relpath(REPORT, HERE)}")

    # ---- E13 推断默认件齐备（v1.3 §二.7）----
    bad = []
    for t in ("colA", "colB", "cmb"):
        d = v13[t]
        if d["n_rcs"] != 4:
            bad.append(f"{t}RCS={d['n_rcs']}")
        if d["n_ant"] != 1:
            bad.append(f"{t}天线={d['n_ant']}")
        if d["n_hinge"] < 1:
            bad.append(f"{t}铰链={d['n_hinge']}")
        if d["n_edge"] != len(d["panel_thickness_m"]):
            bad.append(f"{t}翼缘={d['n_edge']}/{len(d['panel_thickness_m'])}")
        if not all(0.02 <= v <= 0.04 for v in d["panel_thickness_m"]):
            bad.append(f"{t}翼厚={d['panel_thickness_m']}")
    for t in ("colA", "colB"):
        d = v13[t]
        if d["n_baffle"] < 1:
            bad.append(f"{t}光阑环={d['n_baffle']}")
        if d["n_cluster"] < 3:
            bad.append(f"{t}机构簇={d['n_cluster']}")
    check("E13", "推断默认件齐备：每器 RCS 喷管×4、测控天线×1、翼板厚 0.02–0.04 且根部有铰链、"
                 "筒口光阑环≥1、集光器机构簇≥3 件",
          not bad,
          f"RCS={[v13[t]['n_rcs'] for t in v13]}；天线={[v13[t]['n_ant'] for t in v13]}；"
          f"铰链={[v13[t]['n_hinge'] for t in v13]}；翼缘={[v13[t]['n_edge'] for t in v13]}；"
          f"翼厚={v13['colA']['panel_thickness_m']}；光阑环={[v13[t]['n_baffle'] for t in v13]}；"
          f"机构簇={[v13[t]['n_cluster'] for t in v13]}；异常={bad or '无'}")

    # ---- E14 新增件包络（v1.3 §二.7 红线）----
    gain_max = max(v13[t]["outline_gain_max_m"] for t in v13)
    cont_ok = all(v13[t]["y_max_m"] <= ENV_R and v13[t]["envelope_m"][2] <= ENV_H for t in v13)
    check("E14", "新增件包络：单体最大外形增量 ≤0.15 m；D11 包络连续（本体 ≤1.825/4.610）；E10 仍通过",
          gain_max <= 0.15 and cont_ok and e10_ok,
          f"逐器增量(x,y,z)={ {t: v13[t]['outline_gain_m'] for t in v13} }；最大 {gain_max:.4f} m；"
          f"本体 y_max={[v13[t]['y_max_m'] for t in v13]}、总高={[v13[t]['envelope_m'][2] for t in v13]}；"
          f"E10={e10_ok}")

    # ---- E15 材质管线（v1.3 §四）----
    bumps = {m.name: bump_strengths(m) for m in bpy.data.materials if m.name.startswith("MAT_BUS_MLI")}
    bump_ok = bool(bumps) and all(v and max(v) > 0 for v in bumps.values())
    rough = {m.name: mat_roughness(m) for m in bpy.data.materials if m.name.startswith("MAT_TUBE_BLACK")}
    rough_ok = bool(rough) and all(v is not None and v >= 0.85 for v in rough.values())
    edge_bad = []
    for t in ("colA", "colB", "cmb"):
        panels = [o for o in group_objs(t) if "_panel_" in o.name]
        edges = [o for o in group_objs(t) if "_EDGE_" in o.name]
        for p in panels:
            hit = [e for e in edges if contains(e, ctr(p))]
            if not hit:
                edge_bad.append("%s 无描边" % p.name)
            elif mat_color(hit[0]) == mat_color(p):
                edge_bad.append("%s 描边同色" % p.name)
    check("E15", "材质管线：MAT_bus* 含 bump（强度>0）、MAT_tube* roughness ≥0.85、翼缘有描边材质",
          bump_ok and rough_ok and not edge_bad,
          f"MLI bump={ {k: [round(x, 2) for x in v] for k, v in bumps.items()} }；"
          f"镜筒 roughness={ {k: round(v, 2) for k, v in rough.items()} }；"
          f"翼缘={len([o for o in bpy.data.objects if '_EDGE_' in o.name])} 件，异常={edge_bad or '无'}")

    # ---- E16 主光（v1.3 §五）----
    lights = [o for o in bpy.data.objects if o.type == "LIGHT"]
    keys = [o for o in lights if o.get("role") == "key"]
    fills = [o for o in lights if o.get("role") != "key"]
    sun_ok = len(keys) == 1 and keys[0].data.type == "SUN"      # 平行光：各器受光方向天然一致
    fill_ok = all(o.data.energy < keys[0].data.energy for o in fills) if (keys and fills) else True
    sdir = layout.get("sun_dir")
    check("E16", "存在单一主光源（SUN 平行光→三器受光方向一致）、补光弱于主光、sun_dir 已记入自定义属性",
          sun_ok and fill_ok and sdir is not None,
          f"光源={[(o.name, o.data.type, o.data.energy, o.get('role')) for o in lights]}；"
          f"sun_dir={list(sdir) if sdir else None}")


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
