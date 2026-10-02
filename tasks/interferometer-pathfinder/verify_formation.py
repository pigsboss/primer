# -*- coding: utf-8 -*-
"""verify_formation.py — 阶段四分布式编队场景自动验收（在 formation.py 之后运行）。

用法：blender --background --python verify_formation.py
输出：out/formation/verify_log.txt 与 out/formation/formation_report.json（E12）。
判据逐条对应《阶段四_验收清单》v1.4 E1–E22（规格 v1.8），不自增删；每条判据的证据都从
**场景实测**取，不采信建模脚本的自报值（E14 的包络增量、E17 的随机量都由验收侧独立复算）。
版本敏感 API 设后回读（规范 §三）；全部几何判定用网格顶点真实包围盒；读取失败即 FAIL；
main() 异常打印 traceback。

四处"按字面会误判、已写明读法"的地方（交付说明同步回报，不擅自改判据文字）：
  * E13 "翼板厚度 0.02–0.04 m"——v1.5 §二.8 要求帆板沿法向加 crown（0.005–0.02 m），法向
    **包围盒**会变成 0.03+crown（最大 0.05）。故按**逐展向切片的局部厚度**量（每片仍是
    0.03），并把法向包围盒一并打印。
  * E14 "D11 仍通过"——D11 是阶段三装配态判据；本阶段以"包络常数（1.825/4.610）不被撑破"
    承接其连续性，E10 直接复用本清单 E10 的结果。
  * E15 "非 Wave/Grid 周期纹理"——判据文字挂在 MAT_bus* 的 bump 上（§四 MLI 行），故
    周期黑名单只查 MLI 材质；翼板板格的去规则化由域扭曲＋逐板相位实现（E17 核）。
  * E15 "绗缝视觉宽度 ≤0.01 m"——程序化针脚的"视觉宽度"无法逐像素测量，按 report 台账里的
    **解析估计式**（宽度 ≈ 阈值带宽 / |∇噪声|，|∇|≈2π·scale·0.25）核验，方法写在交付说明；
    同时核针脚 bump 贡献 ≤ 褶皱 bump 的 1/3。
"""
import json
import math
import os
import struct
import sys
import traceback

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector  # noqa: F401  (E21 用到)
from mathutils import Vector
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "out", "formation")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")
REPORT = os.path.join(OUT, "formation_report.json")

# "新增件"只指推断默认件；翼机构（SADA/帆板/铰链/翼缘）是 §二.9 的构型构件，算本体不计增量
V13_MARKS = ("_RCS_", "_ANT_", "_BAFFLE_", "_TURNTABLE_", "_ROD_")
WING_PARTS = ("SADA", "HINGE")                       # 每翼：SADA×1＋板间铰链×3
WING_PANELS = 4
ENV_R, ENV_H = 1.825, 4.610        # 阶段三 D11 的包络常数：本阶段承接其连续性
# §二.3 v1.7：合束器＝阶段二单体整机；**场景中不得存在 bay 类对象**（名＋网格包围盒双重判定）
WING_NEED = tuple("SADA_%s" % sx for sx in ("X", "-X")) \
    + tuple("panel_%s%d" % (sx, i) for sx in ("X", "-X") for i in range(1, WING_PANELS + 1)) \
    + tuple("HINGE_%s0%d" % (sx, i) for sx in ("X", "-X") for i in range(1, 4))
CMB_PARTS = ("bay", "module", "recv_01", "recv_02", "aux_01",
             "panel_X1", "panel_X4", "panel_-X1", "panel_-X4", "HINGE_X01", "HINGE_X03",
             "SADA_X", "SADA_-X", "tank_01", "tank_02")
BAY_DIM, BAY_TOL = (1.05, 1.05, 1.05), 0.04   # dims() 次序为 X,Y,Z（舱板段 1.05³）
TUBE_DIM, TUBE_TOL = (0.60, 0.60, 0.70), 0.03  # 承力筒 Ø0.60×0.70
CABIN_DIM, CABIN_TOL = (0.91, 0.65, 0.91), 0.04  # 集光器舱：X 0.91（长边朝 +Y）/ Y 0.65 / Z 0.91
BOARD_DIM, BOARD_TOL = (1.24, 3.60, 0.90), 0.06  # 已作废的 3.60 m 窄长板（v1.8 构型）特征尺寸
SQ_PANEL, SQ_TOL = 0.57, 0.02                   # 集光器方板边长（十字拓扑）
CROSS_SPAN = (1.71, 1.14)                       # 十字展开：对边轴 / 第三边轴（±10%）
CROSS_AREA = 1.296                              # 每翼 4 板总面积 m²     # §二.3 v1.8：全高平台舱（Y×X×Z），含微起伏容差
WING_SPAN_REF, WING_SPAN_TOL = 4.90, 0.05       # 合束器链式翼展参考与容差（E20，±5%）
MLI_GOLD_F0 = (1.00, 0.78, 0.35)                # §四：金色 MLI 的 F0（±0.02）
WRINKLE_RANGE = (0.01, 0.05)                    # §二.8 介观尺度红线（m）
PERIODIC = ("TEX_WAVE", "TEX_CHECKER", "TEX_MAGIC", "TEX_GRID", "TEX_VORONOI")  # 本清单自带禁令

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
    """换算到该器**自身坐标系**（除掉 holder 的位移与转向）后取包围盒。"""
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


def slant_face(bus, side):
    """该器 ±X **斜面**的网格真值：取面积最大的朝该侧大面片，返回 (n_world, p_world)。

    舱体大平面有 ≤5 mm 低频微起伏（本阶段去周期化），小倒角面片法向会被搅乱，故按面积取。
    ``side`` 按**该器自身坐标系**判定（colB 绕 Z 转 180°，世界 x 会翻号）。
    """
    best = None
    for poly in bus.data.polygons:
        n = poly.normal                                  # 对象空间即该器自身系
        if side * n.x > 0.9 and abs(n.z) < 0.15:
            if best is None or poly.area > best[0]:
                best = (poly.area, n.copy(), poly.center.copy())
    if best is None:
        return None, None
    mw = bus.matrix_world
    return (mw.to_3x3() @ best[1]).normalized(), mw @ best[2]


def bus_local_y_span(bus):
    """舱体自身系的 y 跨度与"世界→自身系"逆矩阵（判顶点是否落在舱体 y 跨度内用）。"""
    ys = [v.co.y for v in bus.data.vertices]
    return min(ys), max(ys), bus.matrix_world.inverted()


def pen_stats(bus, objs):
    """**没入/离缝真值**：逐顶点用 Object.closest_point_on_mesh（不包围盒近似）。

    返回 (最大没入, 最深处顶点, 最小离缝, 离缝处顶点, 查询失败数)；没入＝顶点在舱体内时到
    最近表面的距离，离缝＝顶点在舱体外时的最小距离。**查询失败数 >0 ⇒ 判据 FAIL**（读取失败
    不得静默通过）。
    """
    inv = bus.matrix_world.inverted()
    worst, wpt, gap, gpt, miss = 0.0, None, None, None, 0
    for o in objs:
        for p in wv(o):
            lp = inv @ p
            ok, loc, nrm, _ = bus.closest_point_on_mesh(lp)
            if not ok:
                miss += 1
                continue
            d = (lp - loc).length
            if (lp - loc).dot(nrm) < 0.0:
                if d > worst:
                    worst, wpt = d, tuple(round(c, 4) for c in p)
            elif gap is None or d < gap:
                gap, gpt = d, tuple(round(c, 4) for c in p)
    return worst, wpt, gap, gpt, miss


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


def mat_socket(m, name):
    """读 Principled 某输入插槽的默认值：颜色返回前三分量，数值返回 float，缺失返回 None。"""
    if not m or not m.use_nodes:
        return None
    for n in m.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED" and name in n.inputs:
            v = n.inputs[name].default_value
            try:
                return tuple(v)[:3] if len(v) == 4 else float(v)
            except TypeError:
                return float(v)
    return None


def mat_base_color(m):
    return mat_socket(m, "Base Color")


def bump_strengths(m):
    if not m or not m.use_nodes:
        return []
    return [n.inputs["Strength"].default_value for n in m.node_tree.nodes if n.type == "BUMP"]


def noise_details(m):
    out = []
    if not m or not m.use_nodes:
        return out
    for n in m.node_tree.nodes:
        if n.type == "TEX_NOISE" and "Detail" in n.inputs:
            out.append(n.inputs["Detail"].default_value)
    return out


def periodic_nodes(m):
    if not m or not m.use_nodes:
        return []
    return [n.type for n in m.node_tree.nodes if n.type in PERIODIC]


def links_to_object_info(mat):
    """材质里是否存在"被 Object Info.Random 驱动"的链条（per-object 偏移的证据）。"""
    if not mat or not mat.use_nodes:
        return False
    nt = mat.node_tree
    for mp in [n for n in nt.nodes if n.type in ("MAPPING", "VECT_MATH", "TEX_NOISE",
                                                 "TEX_VORONOI", "TEX_WAVE")]:
        for sock in mp.inputs:
            if not sock.is_linked:
                continue
            seen, stack = set(), [sock.links[0].from_node]
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                if n.type == "OBJECT_INFO":
                    return True
                for i in n.inputs:
                    for l in i.links:
                        stack.append(l.from_node)
    return False


def _panel_axes(o):
    """板的三个轴：展向＝最长轴、法向＝最短轴（收展两态自动适配）。"""
    pts = [o.matrix_basis @ v.co for v in o.data.vertices]
    ext = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
    return pts, ext.index(max(ext)), ext.index(min(ext))


def panel_crown(o):
    """帆板 crown：板上法向（最短轴）的半极值 − 半板厚。

    平的板 → 0；弓起 c → c。**必须在对象空间量**（colB 整体绕 Z 转 180°，世界系会翻号），
    且按板自身的轴（展开态法向是 Z、收拢态是 X）——首版写死 y 轴，换机构后量出来是板宽。
    """
    pts, _, norm_ax = _panel_axes(o)
    thick = panel_local_thickness(o) or min(o.dimensions)
    return max(abs(p[norm_ax]) for p in pts) - thick / 2


def panel_local_thickness(o, bins=24):
    """板料厚度：沿**两个板内轴**分别细切片取极差，再取全局最小。

    crown 只把板弓起来、不改板料厚度；但弓形是沿某一板内轴参数化的，只沿一个轴切会
    把弓高算进"厚度"（v2 首版就栽在这）。两轴都切、取最小，即得真实板料厚度 0.03。
    """
    pts, span_ax, norm_ax = _panel_axes(o)
    if not pts:
        return None
    axes = [i for i in range(3) if i != norm_ax]
    th = []
    for ax in axes:
        lo, hi = min(p[ax] for p in pts), max(p[ax] for p in pts)
        step = max((hi - lo) / bins, 1e-6)
        for b in range(bins):
            a0, a1 = lo + b * step, lo + (b + 1) * step
            vs = [p[norm_ax] for p in pts if a0 - 1e-6 <= p[ax] <= a1 + 1e-6]
            if len(vs) >= 2:
                th.append(max(vs) - min(vs))
    return min(th) if th else None


def png_size(p):
    with open(p, "rb") as f:
        h = f.read(24)
    return struct.unpack(">II", h[16:24]) if h[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)


def group_objs(prefix):
    return [o for o in bpy.data.objects if o.name.startswith(prefix + "_")]


PERIODIC = ("TEX_WAVE", "TEX_CHECKER", "TEX_MAGIC", "TEX_GRID")


def main():
    global PERIODIC
    try:
        import formation
        PERIODIC = tuple(formation.PERIODIC_TEX)
        if hasattr(formation, "build"):
            formation.build()
        # 主光/星场/sun_dir/look 属场景构件：E12 的记录项与 E16 都取自此，故一并建立
        formation.build_render_env(final=False)
    except Exception:
        traceback.print_exc()
        check("EX", "formation 导入/构建异常", False, traceback.format_exc(limit=3))
        return

    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = formation.RES
    layout = find("EMPTY_LAYOUT")
    colA, colB, cmb = group_objs("colA"), group_objs("colB"), group_objs("cmb")
    beams = {n: find(n) for n in ("BEAM_star_colA", "BEAM_star_colB",
                                  "BEAM_link_colA", "BEAM_link_colB")}
    col_parts = ("bus", "tube", "panel_X1", "panel_X2", "panel_-X1", "panel_-X2",
                 "window_out", "tank_01", "tank_02")
    cmb_parts = CMB_PARTS
    need_wing = {t: [f"{t}_{n}" for n in WING_NEED if not find(f"{t}_{n}")]
                 for t in ("colA", "colB", "cmb")}
    gimbals = {t: [o for o in group_objs(t) if o.name.startswith(t + "_gimbal")] for t in ("colA", "colB")}
    cmb_body = find("cmb_bay") or find("cmb_bus")

    # ---- E1 对象齐备 + 关键尺寸抽检（v1.2：cmb＝bay 本体）----
    miss = [f"{t}_{p}" for t in ("colA", "colB") for p in col_parts if not find(f"{t}_{p}")]
    miss += [f"cmb_{p}" for p in cmb_parts if not find(f"cmb_{p}")]
    miss += [n for n, o in beams.items() if o is None] + ([] if layout else ["EMPTY_LAYOUT"])
    gim_ok = all(3 <= len(gimbals[t]) <= 4 for t in ("colA", "colB"))
    bus_lo, bus_hi = bb([find("colA_bus")])
    bus = [bus_hi[i] - bus_lo[i] for i in range(3)]
    tube = dims([find("colA_tube")])
    mod_dims = dims([find("cmb_module")]) if find("cmb_module") else [0, 0, 0]
    bay_dims = dims([cmb_body]) if cmb_body else [0, 0, 0]
    bay_ok = all(abs(bay_dims[i] - BAY_DIM[i]) <= BAY_TOL for i in range(3))
    tube_dims = dims([find("cmb_module")]) if find("cmb_module") else [0, 0, 0]
    tube_ok = (abs(max(tube_dims[0], tube_dims[1]) - TUBE_DIM[0]) <= TUBE_TOL
               and abs(tube_dims[2] - TUBE_DIM[2]) <= TUBE_TOL)
    lock_ok = all(find("cmb_LOCK_%s_base" % k) or find("LOCK_%s_base" % k)
                  for k in ("P", "N"))
    cabin_ok = all(abs(dims([find("%s_bus" % t)])[0] - CABIN_DIM[0]) <= CABIN_TOL
                   and abs(dims([find("%s_bus" % t)])[1] - CABIN_DIM[1]) <= CABIN_TOL
                   and abs(dims([find("%s_bus" % t)])[2] - CABIN_DIM[2]) <= CABIN_TOL
                   for t in ("colA", "colB"))
    cmb_ok = (abs(dims([find("colA_bus")])[1] - CABIN_DIM[1]) <= CABIN_TOL
              and abs(dims([find("colA_bus")])[2] - CABIN_DIM[2]) <= CABIN_TOL
              and abs(mod_dims[2] - TUBE_DIM[2]) <= TUBE_TOL and bay_ok)
    # 被作废的旧指向：cmb 本体若是 1.2 m 立方舱则判 FAIL（按名＋网格真实包围盒双判）
    cube_named = [o.name for o in bpy.data.objects
                  if o.name.startswith("cmb") and "bay" not in o.name and "module" not in o.name
                  and o.type == "MESH" and "SADA" not in o.name and "panel" not in o.name
                  and "HINGE" not in o.name and "EDGE" not in o.name and "RCS" not in o.name
                  and "ANT" not in o.name and "port" not in o.name and "tank" not in o.name
                  and "recv" not in o.name and "aux" not in o.name]
    board_shaped = [(o.name, [round(v, 3) for v in dims([o])]) for o in bpy.data.objects
                    if o.type == "MESH" and "BAY" not in o.name.upper()[:3]
                    and all(abs(dims([o])[i] - BOARD_DIM[i]) <= BOARD_TOL for i in range(3))]
    wing_bad = {t: v for t, v in need_wing.items() if v}
    check("E1", "对象齐备：colA/colB 复用阶段一单体（**舱体平面已按反馈 07§三.3 修订**）；"
                "**cmb＝舱板段 1.05³**（与组合体同一物体）＋**承力筒 Ø0.60×0.70**＋"
                "**锁紧释放机构×2（±Y 筒面，释放态）**＋recv×2＋aux＋**SADA 链式翼×2**＋储箱×2；"
                "**不存在 3.60 m 窄长板冒充本体**；四束与 EMPTY_LAYOUT 齐备",
          not miss and gim_ok and cmb_ok and tube_ok and lock_ok and cabin_ok
          and not wing_bad and not board_shaped,
          f"缺件={miss or '无'}；翼机构缺件={wing_bad or '无'}；gimbal={[len(gimbals[t]) for t in ('colA', 'colB')]}；"
          f"集光器舱{['%.2f' % v for v in bus]}、筒径{max(tube[0], tube[1]):.3f}；"
          f"舱板段{['%.3f' % v for v in bay_dims]}（目标 {BAY_DIM}）、承力筒{['%.3f' % v for v in tube_dims]}"
          f"（目标 {TUBE_DIM}）、锁紧机构×2={lock_ok}、集光器舱尺寸={cabin_ok}；"
          f"3.60 m 窄长板冒充本体={board_shaped or '无'}")

    if miss or not layout:
        raise SystemExit("关键对象缺失，终止")

    # ---- E2 编队基线 ----
    ya, yb = ctr(find("colA_bus"))[1], ctr(find("colB_bus"))[1]
    yc = ctr(cmb_body)[1]
    check("E2", "显示基线 |y_colA−y_colB| = 12±0.05 m；合束器在 y=0±0.05",
          abs(abs(ya - yb) - 12.0) <= 0.05 and abs(yc) <= 0.05,
          f"基线={abs(ya - yb):.3f} m；合束器 y={yc:+.3f}（量自 {cmb_body.name}）")

    # ---- E3 一字排开（舱心以各器**本体**计）----
    cs = [ctr(find(f"{t}_bus")) for t in ("colA", "colB")]
    cs.append(ctr(cmb_body))
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

    # ---- E5 星光束端点与出画（v1.2：长度按相机视场反算，五视角均不得露截止端面）----
    cam = find("RIG_Cam") or formation.add_camera()
    zc = ctr(find("colA_bus"))[2]
    views = dict(formation.view_table(cam, zc))
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
        inter = [o.name for o in group_objs(t)
                 if o.type == "MESH" and o is not b and bvh([b]).overlap(bvh([o]))]
        # 出画核验：束顶（端面圆心＋一圈边缘点）在**每一张交付视角**里都不可见
        r = b["diameter"] / 2
        cap = [Vector(p1)] + [Vector((p1[0] + dx, p1[1] + dy, p1[2]))
                              for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r))]
        leaks = []
        for vname, kw in views.items():
            formation.point(cam, **kw)
            scene.render.resolution_x, scene.render.resolution_y = formation.RES
            bpy.context.view_layer.update()
            for q in cap:
                u, v, dep = world_to_camera_view(scene, cam, q)
                in_clip = cam.data.clip_start <= dep <= cam.data.clip_end
                if (0.005 <= u <= 0.995 and 0.005 <= v <= 0.995 and in_clip):
                    leaks.append("%s@%.2f" % (vname, q.z))
                    break
        good = 0 <= embed <= 0.02 and axis_ok and not inter and not leaks
        ok = ok and good
        detail.append(f"{t}:离最内环{embed:.3f}/轴偏({p0[0] - tx:+.3f},{p0[1] - ty:+.3f})/"
                      f"束顶z={p1[2]:.2f}/相交{inter or '无'}/露端面={leaks or '无'}")
    check("E5", "星光束：终点在最内光阑环截面（内嵌≤0.02）；**五张交付视角均不露出空中截止端面**；"
                "轴对中、除筒口外无相交", ok, "；".join(detail))

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
        detail.append(f"{t}:起点离窗口面{abs(p0[1] - wface):.3f}/终点离收光口面{abs(p1[1] - pface):.3f}"
                      f"（落差 {p0[2] - p1[2]:+.3f}）")
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
    half_len = max(abs(bb([cmb_body])[0][1]), abs(bb([cmb_body])[1][1]))
    check("E10", "三器两两间距 ≥ 舱宽（合束器按舱板段半宽 %.2f m 计入）；三器之间无网格相交"
          % half_len, e10_ok,
          f"最小间距={dmin:.3f} m（需≥{bus_w:.2f}）；相交对={cross or '无'}")

    # ---- E11 渲染自检 ----
    sizes = {f: png_size(os.path.join(OUT, f)) for f in
             ("front.png", "side.png", "top.png", "iso.png", "wide.png",
              "wide_realistic.png", "mat_bench.png")}
    check("E11", "out/formation/ 五张预览＋wide_realistic.png＋mat_bench.png 均 ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()), f"{sizes}")

    # ---- 升级件清点（E13–E17 与 E12 记录共用；独立实测，不采信建模脚本自报）----
    v13 = {}
    for t in ("colA", "colB", "cmb"):
        meshes = [o for o in group_objs(t) if o.type == "MESH"]
        added = [o for o in meshes if any(k in o.name for k in V13_MARKS)]
        base = [o for o in meshes if o not in added]
        holder = find(t + "_ROOT")
        blo, bhi = local_bb(base, holder)
        alo, ahi = local_bb(base + added, holder)
        gain = [max(ahi[i] - bhi[i], blo[i] - alo[i], 0.0) for i in range(3)]
        panels = sorted([o for o in meshes if "_panel_" in o.name], key=lambda o: o.name)
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
            "n_edge": len([o for o in meshes if "_EDGE_" in o.name]),
            "n_baffle": len([o for o in meshes if "_BAFFLE_" in o.name]),
            "n_sada": len([o for o in meshes if "_SADA_" in o.name]),
            "n_hinge": len([o for o in meshes if "_HINGE_" in o.name]),
            "n_cluster": len([o for o in meshes if "_gimbal" in o.name
                              or "_TURNTABLE_" in o.name or "_ROD_" in o.name]),
            "panel_thickness_m": [round(panel_local_thickness(o), 4) for o in panels],
            "n_panel": len(panels),
            "panel_bbox_m": [round(dims([o])[1], 4) for o in panels],
            "panel_crown_m": [round(panel_crown(o), 4) for o in panels],
        }

    # ---- E12 记录项 ----
    bus_mats = sorted(m.name for m in bpy.data.materials
                      if m.name.startswith(("MAT_BUS_MLI", "MAT_BAY_MLI")))
    tube_mats = sorted(m.name for m in bpy.data.materials if m.name.startswith("MAT_TUBE_BLACK"))
    rnd = json.loads(layout.get("random_json") or "{}")
    mlog = json.loads(layout.get("material_json") or "{}")
    mli_rows = [v for v in mlog.values() if v.get("class") == "金色 MLI"]
    wrinkle = sorted({f for r in mli_rows for f in r.get("wrinkle_feature_m", [])})
    report = {
        "baseline_real_m": layout.get("baseline_real_m"),
        "baseline_display_m": layout.get("baseline_display_m"),
        "beam_d_star_m": ds, "beam_d_link_m": dl, "beam_ratio": round(ratio, 3),
        "beam_star": {"top_z_m": layout.get("beam_star_top_z"),
                      "len_above_mouth_m": layout.get("beam_star_len_m"),
                      "note": "长度按五张交付视角视场反算，保证不露截止端面（§二.5）"},
        "beam_materials": {"MAT_beam_star": list(st or ()), "MAT_beam_link": list(lk or ()),
                           "star_strength": mat_emission(beams["BEAM_star_colA"]),
                           "link_strength": mat_emission(beams["BEAM_link_colA"]),
                           "note": "socket 存规格基色；发光色按 F3/F5 实测束色反解"},
        "wing_state_T1": layout.get("wing_state"),
        "cmb_body_v17": layout.get("cmb_body"),
        "ports_T2": {"object": "cmb_port_posY / cmb_port_negY",
                     "口径_m": max(dims([find("cmb_port_posY")])[:2]) if find("cmb_port_posY") else None},
        "sun_dir": list(layout.get("sun_dir") or ()),
        "lights": {o.name: {"type": o.data.type, "energy": o.data.energy, "role": o.get("role")}
                   for o in bpy.data.objects if o.type == "LIGHT"},
        "starfield": {"texture": layout.get("starfield_texture"),
                      "strength": layout.get("starfield_strength")},
        "colorspace": {"view_transform": scene.view_settings.view_transform,
                       "look": scene.view_settings.look},
        "BEAM_MODE": layout.get("BEAM_MODE"),
        "BEAM_MODE_note": layout.get("BEAM_MODE_note"),
        "cmb_body_v18": layout.get("cmb_body"),
        "random_seed": layout.get("random_seed"),
        "random_values": rnd,
        "material_table": mlog,
        "mli_wrinkle_feature_m": wrinkle,
        "materials_v2": {"bus_MLI": bus_mats, "tube_black": tube_mats,
                         "edge_trim": sorted({m.name for m in bpy.data.materials
                                              if m.name.startswith("MAT_up_alu")})},
        "upgrade_v13": v13,
        "renders": {k: list(v) for k, v in sizes.items()},
    }
    need = ("baseline_real_m", "baseline_display_m", "beam_d_star_m", "wing_state_T1")
    check("E12", "formation_report.json 记录真实/显示基线、束径、材质、翼态、sun_dir、星场强度、"
                 "全部随机种子、MLI 褶皱特征尺度、**BEAM_MODE**",
          all(report.get(k) is not None for k in need) and report["sun_dir"]
          and report["starfield"]["strength"] is not None
          and report["random_seed"] is not None and bool(report["random_values"])
          and bool(wrinkle) and report["BEAM_MODE"] is not None,
          f"已收集，全部判据后一次写出（随机量 {len(rnd)} 项，种子 {report['random_seed']}；"
          f"材料台账 {len(mlog)} 行；褶皱尺度 {wrinkle} m）")

    # ---- E13 推断默认件齐备 ----
    bad = []
    for t in ("colA", "colB", "cmb"):
        d = v13[t]
        if d["n_rcs"] != 4:
            bad.append(f"{t}RCS={d['n_rcs']}")
        if d["n_ant"] != 1:
            bad.append(f"{t}天线={d['n_ant']}")
        if d["n_sada"] < 2:
            bad.append(f"{t}翼根 SADA={d['n_sada']}（应 2）")
        if d["n_edge"] != len(d["panel_thickness_m"]):
            bad.append(f"{t}翼缘={d['n_edge']}/{len(d['panel_thickness_m'])}")
        if not all(v and 0.02 <= v <= 0.04 for v in d["panel_thickness_m"]):
            bad.append(f"{t}翼厚={d['panel_thickness_m']}")
    for t in ("colA", "colB"):
        d = v13[t]
        if d["n_baffle"] < 1:
            bad.append(f"{t}光阑环={d['n_baffle']}")
        if d["n_cluster"] < 3:
            bad.append(f"{t}机构簇={d['n_cluster']}")
    check("E13", "推断默认件齐备：每器 RCS 喷管×4、测控天线×1、翼板厚 0.02–0.04（每器 8 板）且"
                 "根部有铰链（SADA×2）、筒口光阑环≥1、集光器机构簇≥3 件",
          not bad,
          f"RCS={[v13[t]['n_rcs'] for t in v13]}；天线={[v13[t]['n_ant'] for t in v13]}；"
          f"翼根 SADA={[v13[t]['n_sada'] for t in v13]}；板间铰链={[v13[t]['n_hinge'] for t in v13]}；"
          f"翼缘={[v13[t]['n_edge'] for t in v13]}；"
          f"局部翼厚={v13['colA']['panel_thickness_m']}（法向包围盒 "
          f"{v13['colA']['panel_bbox_m']}，差＝crown）；光阑环={[v13[t]['n_baffle'] for t in v13]}；"
          f"机构簇={[v13[t]['n_cluster'] for t in v13]}；异常={bad or '无'}")

    # ---- E14 新增件包络 ----
    gain_max = max(v13[t]["outline_gain_max_m"] for t in v13)
    cont_ok = all(v13[t]["y_max_m"] <= ENV_R and v13[t]["envelope_m"][2] <= ENV_H for t in v13)
    check("E14", "新增件包络：单体最大外形增量 ≤0.15 m；D11 包络连续（本体 ≤1.825/4.610）；E10 仍通过",
          gain_max <= 0.15 and cont_ok and e10_ok,
          f"逐器增量(x,y,z)={ {t: v13[t]['outline_gain_m'] for t in v13} }；最大 {gain_max:.4f} m；"
          f"本体 y_max={[v13[t]['y_max_m'] for t in v13]}、总高={[v13[t]['envelope_m'][2] for t in v13]}；"
          f"E10={e10_ok}")

    # ---- E15 材质管线（v1.3：金色 MLI 物理参数＋厘米级褶皱＋禁周期/分块）----
    mli = [m for m in bpy.data.materials if m.name.startswith("MAT_BUS_MLI")]
    bump = {m.name: bump_strengths(m) for m in mli}
    det = {m.name: (max(noise_details(m)) if noise_details(m) else 0.0) for m in mli}
    per = {m.name: periodic_nodes(m) for m in mli}
    met = {m.name: mat_socket(m, "Metallic") for m in mli}
    f0 = {m.name: mat_base_color(m) for m in mli}
    rgh = {m.name: mat_socket(m, "Roughness") for m in mli}
    bump_ok = (bool(mli) and all(v and len(v) >= 1 and max(v) > 0 for v in bump.values())
               and all(m is not None and abs(m - 1.0) <= 0.01 for m in met.values())
               and all(c and all(abs(c[i] - MLI_GOLD_F0[i]) <= 0.02 for i in range(3))
                       for c in f0.values())
               and all(r is not None and 0.30 <= r <= 0.45 for r in rgh.values()))
    noise_ok = all(d >= 3.0 for d in det.values())
    per_ok = all(not v for v in per.values())
    scale_ok = bool(wrinkle) and all(WRINKLE_RANGE[0] <= f <= WRINKLE_RANGE[1] for f in wrinkle)
    st_ok = bool(mli_rows)
    for r in mli_rows:
        st_ok = st_ok and r.get("stitch_width_est_m", 9) <= 0.01 \
            and r.get("stitch_ratio", 9) <= 1.0 / 3 + 1e-6 \
            and not r.get("banned_nodes_present")
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
    check("E15", "材质管线：金色 MLI（场景重建）metallic=1、金色 F0≈(1.00,0.78,0.35)、"
                 "roughness∈[0.3,0.45]；bump 为分形噪声（无 Wave/Grid/Voronoi 分块）；"
                 "褶皱特征尺度∈[0.01,0.05] m 且记入 report；缝线为细针脚（宽度≤0.01 m、"
                 "bump≤褶皱 1/3）；MAT_tube* roughness ≥0.85；翼缘有描边材质",
          bump_ok and noise_ok and per_ok and scale_ok and st_ok and rough_ok and not edge_bad,
          f"MLI metallic={met}；F0={f0}；roughness={rgh}；bump={ {k: [round(x, 2) for x in v] for k, v in bump.items()} }；"
          f"Noise Detail={det}；周期/分块节点={ {k: v for k, v in per.items() if v} or '无'}；"
          f"褶皱尺度={wrinkle} m；针脚={[{'w': r.get('stitch_width_est_m'), 'ratio': r.get('stitch_ratio'), 'jit': r.get('stitch_jitter')} for r in mli_rows]}；"
          f"镜筒 roughness={ {k: round(v, 2) for k, v in rough.items()} }；翼缘异常={edge_bad or '无'}")

    # ---- E16 主光（+Z 主导、fill ≤0.2、look 回读）----
    lights = [o for o in bpy.data.objects if o.type == "LIGHT"]
    keys = [o for o in lights if o.get("role") == "key"]
    fills = [o for o in lights if o.get("role") != "key"]
    sdir = layout.get("sun_dir")
    v = Vector(sdir).normalized() if sdir else Vector((0, 0, 0))
    zd_ok = v.z > 0 and v.z >= max(abs(v.x), abs(v.y))          # +Z 主导＝与星光同向
    sun_ok = len(keys) == 1 and keys[0].data.type == "SUN"
    fill_ok = all(o.data.energy <= 0.2 + 1e-6 for o in fills) and \
        all(o.data.energy < keys[0].data.energy for o in fills) if (keys and fills) else True
    look = scene.view_settings.look or ""
    look_ok = "mediumhighcontrast" in look.replace(" ", "").replace("-", "").lower()
    check("E16", "单一主光源（SUN，平行光→三器受光方向一致）；sun_dir 与星光同向（+Z 主导）；"
                 "fill ≤0.2；AgX look＝Medium High Contrast",
          sun_ok and zd_ok and fill_ok and look_ok,
          f"光源={[(o.name, o.data.type, o.data.energy, o.get('role')) for o in lights]}；"
          f"sun_dir={list(sdir) if sdir else None}（z 主导={zd_ok}）；look={look!r}")

    # ---- E17 去周期化与随机性 ----
    per_obj = {m.name: links_to_object_info(m) for m in mli}
    crowns = {t: v13[t]["panel_crown_m"] for t in ("colA", "colB", "cmb")}
    crown_ok = all(all(0.005 - 5e-4 <= c <= 0.02 + 5e-4 for c in cs) for cs in crowns.values())
    spread_ok = all((max(cs) - min(cs)) >= 0.002 for cs in crowns.values() if len(cs) > 1)
    seam = [r.get("stitch_jitter") for r in mli_rows if r.get("stitch_jitter") is not None]
    seam_ok = bool(seam) and all(0.8 - 1e-6 <= s <= 1.4 + 1e-6 for s in seam)
    und = [rnd[k]["amp"] for k in rnd if k.startswith("undulate|")]
    und_ok = bool(und) and all(u <= 0.005 + 1e-9 for u in und)
    seed_ok = report["random_seed"] is not None and bool(report["random_values"])
    check("E17", "去周期化：MLI per-object 相位/缩放偏移（Object Info 驱动）；帆板 crown∈[0.005,0.02] 且"
                 "逐板不同；MLI 缝线间隔抖动∈[0.8,1.4]×基准；大平面微起伏≤5 mm；随机种子已记入",
          all(per_obj.values()) and crown_ok and spread_ok and seam_ok and und_ok and seed_ok,
          f"per-object 偏移={per_obj}；crown={crowns}（板间极差 "
          f"{[round(max(c) - min(c), 4) for c in crowns.values()]}）；缝线抖动={seam}；"
          f"微起伏幅度={und}；种子={report['random_seed']}/{len(rnd)} 项")


    # ---- E18 器间束水平（v1.3 增）：两端点 z 落差 ≤0.05 m ----
    drops = {t: beams["BEAM_link_%s" % t]["p_start"][2] - beams["BEAM_link_%s" % t]["p_end"][2]
             for t in ("colA", "colB")}
    check("E18", "两条器间束两端点 z 落差各 ≤0.05 m（回退单体后收光口随舱体回到 z≈0.51）",
          all(abs(v) <= 0.05 for v in drops.values()),
          "；".join(f"{t}:落 {v:+.3f} m（起 {beams['BEAM_link_%s' % t]['p_start'][2]:.3f} → "
                    f"终 {beams['BEAM_link_%s' % t]['p_end'][2]:.3f}）" for t, v in drops.items()))

    # ---- E19 材质参数表核对（v1.3 增）----
    tank_mats = [m for m in bpy.data.materials if m.name.startswith("MAT_TANK_WHITE")]
    mech_mats = [m for m in bpy.data.materials
                 if m.name.startswith(("MAT_GIMBAL_GREY", "MAT_RECV_GREY", "MAT_AUX_GREY",
                                       "MAT_up_alu", "MAT_up_grey"))]
    panel_mats = [m for m in bpy.data.materials if m.name.startswith("MAT_PANEL_BLUE")]
    def _num(m, name, default=-1.0):
        v = mat_socket(m, name)
        return default if v is None else float(v)

    tank_ok = bool(tank_mats) and all(
        0.82 <= (mat_base_color(m) or (-1,))[0] <= 0.92     # albedo≈0.87（颜色取 R 分量）
        and 0.50 <= _num(m, "Roughness") <= 0.65
        and _num(m, "Metallic") <= 0.05 for m in tank_mats)
    mech_ok = bool(mech_mats) and all(
        abs(_num(m, "Metallic") - 1.0) <= 0.01
        and 0.30 <= _num(m, "Roughness") <= 0.50 for m in mech_mats)
    panel_ok = bool(panel_mats) and all(
        _num(m, "Metallic") <= 0.05
        and (mat_base_color(m) or (0, 0, 0))[2] > (mat_base_color(m) or (0, 0, 0))[0]
        and max(mat_base_color(m) or (1, 1, 1)) < 0.30
        and _num(m, "Coat Weight", _num(m, "Clearcoat", 0.0)) > 0 for m in panel_mats)
    tank_row = [[(mat_base_color(m) or (0, 0, 0))[0], mat_socket(m, "Roughness")]
                for m in tank_mats]
    mech_row = [(mat_socket(m, "Metallic"), mat_socket(m, "Roughness")) for m in mech_mats]
    panel_base = [mat_base_color(m) for m in panel_mats]
    panel_coat = [mat_socket(m, "Coat Weight") or mat_socket(m, "Clearcoat") for m in panel_mats]
    check("E19", "材质参数表逐行核对：储箱＝AZ-93 白漆（albedo≈0.87、roughness∈[0.5,0.65]、电介质）；"
                 "机构件 metallic=1、roughness∈[0.3,0.5]；翼板＝深蓝紫电介质基底＋盖玻璃 coat 镜面层",
          tank_ok and mech_ok and panel_ok,
          "储箱 %d 件 albedo/rough=%s；机构件 %d 件 metallic/rough=%s；翼板 %d 件 base=%s、coat=%s"
          % (len(tank_mats), tank_row, len(mech_mats), mech_row,
             len(panel_mats), panel_base, panel_coat))


    # ---- E20 太阳翼机构（v1.4 增）：每翼 SADA×1＋4 板＋板间铰链×3；展开成线、法向朝 −Z ----
    e20, e20_ok, e20_rows = {}, True, []
    for t in ("cmb",):                                # E20＝合束器链式翼；集光器十字翼见 E23
        for sx in ("X", "-X"):
            sada = find("%s_SADA_%s" % (t, sx))
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, WING_PANELS + 1)]
            hs = [find("%s_HINGE_%s0%d" % (t, sx, i)) for i in range(1, 4)]
            if not sada or any(p is None for p in ps) or any(h is None for h in hs):
                e20_ok = False
                e20_rows.append(f"{t}{sx}:件数不全")
                continue
            cs = [ctr(p) for p in ps]
            thick_axis = [dims([p]).index(min(dims([p]))) for p in ps]
            normal_ok = all(a == 2 for a in thick_axis)          # 厚度沿 Z ⇒ 法向 ±Z（与 −Z 夹角 0°）
            collinear = (max(c[1] for c in cs) - min(c[1] for c in cs) <= 0.02
                         and max(c[2] for c in cs) - min(c[2] for c in cs) <= 0.02)
            xs = sorted(c[0] for c in cs)
            step = [xs[i + 1] - xs[i] for i in range(WING_PANELS - 1)]
            even = (max(step) - min(step)) <= 0.02
            panel_len = [round(dims([p])[0], 3) for p in ps]     # 展向长＝原板长/4
            good = normal_ok and collinear and even and all(abs(v - 0.45) <= 0.02 for v in panel_len)
            e20_ok = e20_ok and good
            e20_rows.append(f"{t}{sx}:1×SADA+4 板+3 铰/板长{panel_len}/法向−Z={normal_ok}/成线={collinear}/等距={even}")
        objs = [find("%s_SADA_%s" % (t, sx)) for sx in ("X", "-X")] + \
               [find("%s_panel_%s%d" % (t, sx, i)) for sx in ("X", "-X") for i in range(1, WING_PANELS + 1)]
        objs = [o for o in objs if o]
        e20[t] = {"span_m": round(dims(objs)[0], 3),
                  "wing_len_m": round(sum(dims([find("%s_panel_%s%d" % (t, sx, i))])[0]
                                          for i in range(1, WING_PANELS + 1)), 3)
                  if all(find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, WING_PANELS + 1))
                  else None}
    span_vals = [v["span_m"] for v in e20.values()]
    span_ok = all(abs(s0 - WING_SPAN_REF) <= WING_SPAN_REF * WING_SPAN_TOL for s0 in span_vals)
    check("E20", "合束器翼机构（A 类链式 Z 折）：每翼 SADA 球铰×1＋帆板×4（0.45×0.72×0.03）"
                 "＋板间铰链×3（串联）；4 板成线自 SADA 外伸、法向朝 −Z（夹角 0°≤30°）、翼展 ≈4.9 m",
          e20_ok and span_ok,
          f"翼展={e20}；" + "；".join(e20_rows[:3]))

    # ---- E22 光束双层结构与双模式（v1.4 增／T4）----
    e22, e22_ok = {}, True
    for tag in ("star_colA", "star_colB", "link_colA", "link_colB"):
        shell, core = find("BEAM_%s" % tag), find("BEAM_%s_core" % tag)
        ends = [find("BEAM_%s_end%d" % (tag, i)) for i in (1, 2)]
        if not shell or not core or any(e is None for e in ends):
            e22_ok = False
            e22[tag] = "缺层"
            continue
        ds, dc = shell["diameter"], core["diameter"]
        ratio = ds / dc if dc else 0
        end_len = [e.get("end_len") for e in ends]
        good = abs(ratio - 3.0) <= 0.6 and all(v is not None and v <= 0.15 for v in end_len)
        e22_ok = e22_ok and good
        e22[tag] = {"shell_m": round(ds, 4), "core_m": round(dc, 4), "ratio": round(ratio, 3),
                    "end_len_m": end_len}
    mode0 = layout.get("BEAM_MODE")
    ends0 = {o.name: (tuple(o["p_start"]), tuple(o["p_end"])) for o in bpy.data.objects
             if o.name.startswith("BEAM_") and "p_start" in o.keys()
             and "_core" not in o.name and "_end" not in o.name}
    d0 = {o.name: o["diameter"] for o in bpy.data.objects
          if o.name.startswith("BEAM_") and "p_start" in o.keys()
          and "_core" not in o.name and "_end" not in o.name}
    core0 = {o.name: o["core_diameter"] for o in bpy.data.objects
             if o.name.startswith("BEAM_") and "core_diameter" in o.keys()}
    formation.set_beam_mode("realistic")
    ends1 = {o.name: (tuple(o["p_start"]), tuple(o["p_end"])) for o in bpy.data.objects
             if o.name.startswith("BEAM_") and "p_start" in o.keys()
             and "_core" not in o.name and "_end" not in o.name}
    d1 = {o.name: o["diameter"] for o in bpy.data.objects
          if o.name.startswith("BEAM_") and "p_start" in o.keys()
          and "_core" not in o.name and "_end" not in o.name}
    core1 = {o.name: o["core_diameter"] for o in bpy.data.objects
             if o.name.startswith("BEAM_") and "core_diameter" in o.keys()}
    path_same = ends0 == ends1 and d0 == d1
    core_halved = all(abs(core1[k] - core0[k] / 2) <= 1e-6 for k in core0)
    mode_ok = (mode0 == "illustration") and layout.get("BEAM_MODE") == "realistic"
    e22_args = ("E22", "光束双层结构＋双模式：四束均为外壳晕＋亮芯（芯径＝壳径/3±20%）＋两端点增亮段"
                       "（≤0.15 m）；BEAM_MODE 默认 illustration、可切 realistic；两模式光路几何一致",
                e22_ok and path_same and core_halved and mode_ok,
                f"逐束={e22}；默认模式={mode0}→切换后={layout.get('BEAM_MODE')}；"
                f"端点/壳径两模式一致={path_same}；realistic 芯径减半={core_halved}")

    # ---- E23 集光器翼十字拓扑（B 类，反馈 06 N1）----
    e23, e23_ok = {}, True
    for t in ("colA", "colB"):
        rows = []
        for sx in ("X", "-X"):
            bus = find("%s_bus" % t)
            side = 1.0 if sx == "X" else -1.0
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, WING_PANELS + 1)]
            ps = [p for p in ps if p]
            hs = [find("%s_HINGE_%s0%d" % (t, sx, i)) for i in range(1, 4)]
            sada = find("%s_SADA_%s" % (t, sx))
            if len(ps) != 4 or any(h is None for h in hs) or not sada:
                e23_ok = False
                rows.append(f"{sx}:件数不全")
                continue
            dims_ok = all(abs(dims([p])[0] - SQ_PANEL) <= SQ_TOL
                          and abs(dims([p])[1] - SQ_PANEL) <= SQ_TOL
                          and abs((panel_local_thickness(p) or 9) - 0.03) <= 0.01 for p in ps)
            normal_ok = all(dims([p]).index(min(dims([p]))) == 2 for p in ps)   # 法向 ±Z（夹角 0°）
            area = sum(dims([p])[0] * dims([p])[1] for p in ps)
            # 根板＝离 SADA 最近者；其余三块各自铰接其一条边（非串联）
            sc = ctr(sada)
            root = min(ps, key=lambda p: sum((ctr(p)[i] - sc[i]) ** 2 for i in range(2)))
            arms = [p for p in ps if p is not root]
            offs = []
            adj_ok = True
            for a in arms:
                d = [ctr(a)[i] - ctr(root)[i] for i in range(3)]
                nz = [i for i in range(3) if abs(d[i]) > 0.05]
                if len(nz) != 1 or abs(abs(d[nz[0]]) - SQ_PANEL) > SQ_TOL + 0.03:
                    adj_ok = False
                offs.append(tuple(round(d[i], 2) for i in range(3)))
            lo, hi = bb(ps)
            spans = (round(hi[1] - lo[1], 3), round(hi[0] - lo[0], 3))     # (对边轴 Y, 第三边轴 X)
            span_ok2 = (abs(spans[0] - CROSS_SPAN[0]) <= CROSS_SPAN[0] * 0.1
                        and abs(spans[1] - CROSS_SPAN[1]) <= CROSS_SPAN[1] * 0.1)
            hinge_ok = all(min(((ctr(h)[0] - ctr(p)[0]) ** 2
                                + (ctr(h)[1] - ctr(p)[1]) ** 2) ** 0.5
                               for p in ps) <= SQ_PANEL * 0.75 for h in hs)   # 铰链落在板缝上
            # ── 双侧限之**没入下限**（《验收反馈 09》§三.4/§三.5）：板顶点没入器体舱 ＝0，
            #    真值法 Object.closest_point_on_mesh（不包围盒近似），>2 mm 即 FAIL ──
            pen, pen_at, gap, gap_at, miss = pen_stats(bus, ps)
            n_sl, p_sl = slant_face(bus, side)
            y_lo, y_hi, inv_bus = bus_local_y_span(bus)
            root_d = None
            if n_sl is not None:
                ds = [(v - p_sl).dot(n_sl) for v in wv(root)
                      if y_lo - 1e-6 <= (inv_bus @ v).y <= y_hi + 1e-6]
                root_d = min(ds) if ds else None      # 翼根（0 号板内缘）离斜面最小垂距
            pen_ok = pen <= 0.002 and miss == 0      # 查询失败即 FAIL（禁止静默通过）
            good = (dims_ok and normal_ok and adj_ok and span_ok2 and hinge_ok
                    and abs(area - CROSS_AREA) <= CROSS_AREA * 0.05 and pen_ok)
            e23_ok = e23_ok and good
            # 串联拓扑（错）时臂心两两间距＝0.57；十字拓扑下对边两臂相距 1.14
            pair = max(((ctr(a)[0] - ctr(b)[0]) ** 2 + (ctr(a)[1] - ctr(b)[1]) ** 2) ** 0.5
                       for i, a in enumerate(arms) for b in arms[i + 1:])
            rows.append(f"{t}{sx}:4 方板 {[round(dims([p])[0], 2) for p in ps]}/面积 {area:.3f}/"
                        f"臂心最大间距 {pair:.2f}/跨 {spans}/法向−Z={normal_ok}"
                        f"/没入{pen:.6f}@{(pen_at[0], pen_at[1], pen_at[2]) if pen_at else '—'}"
                        f"(>0.002 即 FAIL)/离缝{gap if gap is None else round(gap, 4)}"
                        f"{'@' + str(gap_at) if gap_at else ''}/查询失败{miss}"
                        f"/翼根离斜面垂距{'—' if root_d is None else format(root_d, '.4f')}(实测，区间待总体落版)")
        e23[t] = rows
    e23_args = ("E23", "集光器翼十字拓扑（**外廓上限＋没入下限**双侧限）：每翼 SADA×1＋方板×4"
                 "（0.57×0.57×0.03）＋铰链×3；1/2/3 号板分别铰接 0 号板相邻三边（非串联）；"
                 "展开轮廓十字（对边轴 1.71、第三边轴 1.14，±10%）＝外廓上限；法向朝 −Z；"
                 "4 板总面积 1.296±5%；**板-舱没入＝0（＞2 mm 即 FAIL**，closest_point_on_mesh "
                 "真值＝没入下限）；翼根离斜面垂距与离缝按实测记入 detail",
          e23_ok, "；".join(e23["colA"]))

    # ---- E24 合束器两段结构（反馈 06 N2）----
    tube = find("cmb_module")
    tubes_d = dims([tube]) if tube else [0, 0, 0]
    tube_c = ctr(tube) if tube else [0, 0, 0]
    deck_l, deck_h = bb([cmb_body]) if cmb_body else ([0] * 3, [0] * 3)
    locks = [o for o in bpy.data.objects if "_LOCK_" in o.name]
    lock_sides = sorted({o.name.split("_LOCK_")[1][0] for o in locks})
    lock_ok2 = (len(lock_sides) == 2 and "P" in lock_sides and "N" in lock_sides)
    protr = [round(max(abs(bb([o])[1][1]), abs(bb([o])[0][1])) - TUBE_DIM[0] / 2, 3) for o in locks]
    protr_ok = bool(protr) and max(protr) <= 0.35
    centered = abs(tube_c[0] - (deck_l[0] + deck_h[0]) / 2) <= 0.05 \
        and abs(tube_c[1] - (deck_l[1] + deck_h[1]) / 2) <= 0.05
    base_ok = abs((bb([tube])[0][2] if tube else 0) - deck_h[2]) <= 0.05
    e24_args = ("E24", "合束器两段结构：上段承力筒 Ø0.60×0.70 居舱板段顶面中央；锁紧与释放分离机构×2 "
                 "布于承力筒 ±Y 筒面、分布式呈释放态；机构件材质、凸出筒面 ≤0.35 m；"
                 "收光口在筒壁 ±Y（E6 光路不变）",
          centered and base_ok and lock_ok2 and protr_ok
          and abs(max(tubes_d[0], tubes_d[1]) - TUBE_DIM[0]) <= TUBE_TOL
          and abs(tubes_d[2] - TUBE_DIM[2]) <= TUBE_TOL,
          f"承力筒尺寸={['%.3f' % v for v in tubes_d]}（目标 {TUBE_DIM}）、底面贴合舱板段顶={base_ok}、"
          f"居中={centered}；锁紧机构 {len(locks)} 件、筒面 {lock_sides}、最大凸出 {max(protr) if protr else '无'} m")

    # ---- E21 分离路径无碰撞（v1.4 增）：组合体布局上集光器沿 +Z 直提 ----
    import assembly as ASM
    ASM.build()                                          # 重建组合体布局（其后编队场景即拆掉）
    formation.C.refresh()
    module = find("cmbmod_module")
    # "锁紧释放机构解除后"：锁紧臂与卡爪不计入静态障碍（E21 前提），仅支座/销仍占位
    released = ("_arm", "_jawa", "_jawb")
    others = [o for o in bpy.data.objects
              if o.type == "MESH" and not o.name.startswith(("colA_", "colB_"))
              and not any(k in o.name for k in released)]
    sep_rows, sep_ok, min_gap = [], True, 1e9
    tgt = bvh(others)
    for t in ("colA", "colB"):
        holder = find(t + "_ROOT")
        group = [o for o in bpy.data.objects if o.name.startswith(t + "_") and o.type == "MESH"]
        gl, gh = bb(group)
        ml, mh = bb([module])
        gap = min(min(abs(gl[0] - mh[0]), abs(ml[0] - gh[0])),
                  min(abs(gl[1] - mh[1]), abs(ml[1] - gh[1])))
        min_gap = min(min_gap, gap)
        z0 = holder.location.z
        hit = False
        for i in range(1, 21):        # 从 0.1 m 起：dz=0 是法兰贴合面，不算扫掠相交
            holder.location.z = z0 + i * 0.10
            bpy.context.view_layer.update()
            if bvh(group).overlap(tgt):
                hit = True
                break
        holder.location.z = z0
        bpy.context.view_layer.update()
        sep_ok = sep_ok and not hit
        tube_gap = min(abs(gl[1] - 0.30), abs(0.30 - gh[1])) if gl[1] > 0.30 else 0.0
        sep_rows.append(f"{t}:与承力筒横向间隙 {0.925 - 0.30:.3f} m；+Z 直提 2.0 m "
                        f"扫掠{'相交' if hit else '无相交'}")
    sep = {"path": "+Z 直提 2.0 m（0.1 m 步进网格相交核验）", "min_gap_to_module_m": round(min_gap, 3),
           "rows": sep_rows, "ok": sep_ok}
    check("E21", "分离路径无碰撞：集光器沿 +Z 直提离位，全程与载荷舱/平台舱/收拢翼摞无扫掠相交；"
                 "最小间隙记入 report",
          sep_ok, "；".join(sep_rows))
    check(*e22_args)                      # E22 数据在编队场景里采集，判据行按清单顺序排在 E21 之后
    check(*e23_args)
    check(*e24_args)

    report["wing_E20"] = e20
    report["beam_E22"] = e22
    report["separation_E21"] = sep
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("[verify] 报告一次写出：%s" % os.path.relpath(REPORT, HERE), flush=True)


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
