# -*- coding: utf-8 -*-
"""太阳翼机构（SADA＋4 板 Z 折）——阶段三/四**共用同一份实现**。

依《阶段四_建模规格》v1.8 §二.9／文据 G4（提出方 2026-10-01 描述的照录结构化）：

    每翼 = **SADA**（帆板驱动机构，抽象为球轴铰链：连接 0 号板与探测器侧面舱板）
         + **0/1/2/3 号帆板 ×4**（等分原板长 1.80/4 = 0.45 m）
         + **板间铰链 ×3**（0–1、1–2、2–3）
    展开终态：4 板共线自 SADA 向外伸展，**板面法向朝尾部 −Z**（观测时太阳在尾部，
              规避光束转发路径），法向与 −Z 夹角 ≤30°；
    收拢终态：1→0、2→1、3→2 依次 Z 折成摞，经 SADA 转至侧向贴紧 ±X 舱板，
              整摞外廓距舱面 ≤0.16 m（＝4×0.03＋折缝）。

**为什么单列一个文件**：组合体（集光器收拢、合束器展开）与分布式（全展开）必须用
**同一套机构**——两处各写一遍必然漂移；而 collector.py／combiner.py 又要求一行不动，
故把机构放在这个只为场景脚本服务的新模块里，由 assembly.py 与 formation.py 共同 import。
"""
import math

import bpy
from mathutils import Vector

import collector as C

PANEL_N = 4                        # 每翼板数（0/1/2/3 号）
PANEL_L = C.PANEL_L / PANEL_N      # 单板展向长＝等分原板长 0.45
PANEL_W = C.PANEL_W                # 弦向宽 0.72
PANEL_T = C.PANEL_T                # 厚 0.03
PANEL_GAP = 0.006                  # 展开态板间缝
ROOT_OUT = 0.050                   # 0 号板内缘相对舱面的外推量（SADA 必须落在舱面之外）
SADA_R = 0.042                     # 球铰半径
SADA_BASE = (0.040, 0.150, 0.130)  # 底座（沿舱面贴装）
ARM_R, ARM_L = 0.016, 0.060        # 球铰到 0 号板的短臂
HINGE_R, HINGE_L = 0.020, 0.110    # 板间铰链（轴沿 Y）
STACK_GAP = 0.004                  # 收拢叠板间隙（Z 折折缝）
Z_PLANE = 0.0                      # 展开态翼面高度（舱体中部）
COL_PANEL = C.COL_PANEL
COL_ALU = (0.62, 0.63, 0.64)       # 阳极氧化铝（机构件，§四）
COL_MECH = (0.55, 0.56, 0.58)


def _mat(name, color, rough=0.40, metal=1.0):
    """机构件一律 metallic=1（§四 机构件行）；同名复用，避免多次 build 产生 .001 副本。"""
    m = bpy.data.materials.get(name)
    return m if m else C.new_material(name, color, rough, metal)


def _box(name, loc, size, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(mat)
    return ob


def _cyl(name, loc, axis, r, depth, mat, verts=20):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r, depth=depth, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_euler = Vector(axis).to_track_quat("Z", "Y").to_euler()
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    ob.data.materials.append(mat)
    return ob


def _join(objs, name, mat):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = name
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    return ob


def _edge_frame(name, center, dims, mat, w=0.02):
    """翼缘描边：沿板四边各一条细梁（不能用一个"略大一圈的盒子"，那会罩住电池面）。"""
    cx, cy, cz = center
    dx, dy, dz = dims
    thin = dims[2] + 0.004 if dims[2] < dims[1] else dims[1] + 0.004
    bars = (([cx, cy, cz + dz / 2], (dx + 2 * w, thin, w)),
            ([cx, cy, cz - dz / 2], (dx + 2 * w, thin, w)),
            ([cx + dx / 2, cy, cz], (w, thin, dz + 2 * w)),
            ([cx - dx / 2, cy, cz], (w, thin, dz + 2 * w)))
    parts = [_box("%s_b%d" % (name, i + 1), loc, size, mat)
             for i, (loc, size) in enumerate(bars)]
    return _join(parts, name, mat)


def build_wing(side, face_x, state="deployed", prefix="", z_plane=Z_PLANE):
    """建一翼：SADA＋4 板＋板间铰链×3。返回对象列表（世界坐标、未挂父级）。

    ``side`` ∈ {+1, −1}（±X 侧）；``face_x``＝该侧舱面的 |x|（梯形舱取 y=0 处半宽，
    与 collector 的斜面一致）；``state`` ∈ {"deployed", "stowed"}。

    命名（供阶段四的清单判据按名前缀清点）：
      ``panel_X1..X4``／``panel_-X1..-X4``、``SADA_X``／``SADA_-X``、
      ``HINGE_X01..03``／``HINGE_-X01..03``、``EDGE_X1..4``／``EDGE_-X1..4``
    """
    side = 1.0 if side > 0 else -1.0
    sx = "X" if side > 0 else "-X"
    panel_mat = _mat("MAT_PANEL_BLUE", COL_PANEL, 0.45, 0.10)
    if not any(n.type in ("TEX_WAVE", "TEX_IMAGE") for n in panel_mat.node_tree.nodes):
        C.add_panel_grid(panel_mat)
    alu = _mat("MAT_up_alu", COL_ALU, 0.40, 1.0)
    mech = _mat("MAT_up_grey", COL_MECH, 0.40, 1.0)
    out = []

    if state == "deployed":
        # ── SADA：底座贴舱面 ＋ 球铰 ＋ 短臂，翼面在 z_plane 平面内、法向 −Z ──
        base = _box("SADA_%s_base" % sx, (side * (face_x - 0.005), 0.0, z_plane),
                    SADA_BASE, mech)
        ball = _cyl("SADA_%s_ball" % sx, (side * (face_x + 0.030), 0.0, z_plane),
                    (side, 0.0, 0.0), SADA_R, 0.045, mech, verts=24)
        arm = _cyl("SADA_%s_arm" % sx, (side * (face_x + 0.055), 0.0, z_plane),
                   (side, 0.0, 0.0), ARM_R, ARM_L, mech, verts=16)
        out.append(_join([base, ball, arm], "SADA_%s" % sx, mech))

        # ── 4 板共线自 SADA 外伸：展向沿 X、弦向沿 Y、法向 ±Z ──
        root = face_x + ROOT_OUT
        hinges = []
        for i in range(PANEL_N):
            x0 = root + i * (PANEL_L + PANEL_GAP)
            cx = side * (x0 + PANEL_L / 2)
            nm = "panel_%s%d" % (sx, i + 1)
            p = _box(nm, (cx, 0.0, z_plane), (PANEL_L, PANEL_W, PANEL_T), panel_mat)
            out.append(p)
            out.append(_edge_frame("EDGE_%s%d" % (sx, i + 1), (cx, 0.0, z_plane),
                                   (PANEL_L, PANEL_W, PANEL_T), alu))
            if i < PANEL_N - 1:                       # 板间铰链×3，落在板缝上
                jx = side * (x0 + PANEL_L + PANEL_GAP / 2)
                hinges.append(_cyl("HINGE_%s0%d" % (sx, i + 1), (jx, 0.0, z_plane),
                                   (0.0, 1.0, 0.0), HINGE_R, HINGE_L, mech, verts=16))
        out += hinges
    else:
        # ── 收拢：1→0、2→1、3→2 依次 Z 折成摞，经 SADA 转至侧向贴紧 ±X 舱板 ──
        base = _box("SADA_%s_base" % sx, (side * (face_x - 0.005), 0.0, z_plane),
                    SADA_BASE, mech)
        ball = _cyl("SADA_%s_ball" % sx, (side * (face_x + 0.026), 0.0, z_plane),
                    (side, 0.0, 0.0), SADA_R, 0.045, mech, verts=24)
        out.append(_join([base, ball], "SADA_%s" % sx, mech))
        hinges = []
        for i in range(PANEL_N):
            cx = side * (face_x + 0.010 + PANEL_T / 2 + i * (PANEL_T + STACK_GAP))
            nm = "panel_%s%d" % (sx, i + 1)
            p = _box(nm, (cx, 0.0, z_plane), (PANEL_T, PANEL_W, PANEL_L), panel_mat)
            out.append(p)
            out.append(_edge_frame("EDGE_%s%d" % (sx, i + 1), (cx, 0.0, z_plane),
                                   (PANEL_T, PANEL_W, PANEL_L), alu))
            if i < PANEL_N - 1:                       # 折缝铰链：在相邻两板之间、沿 Y
                hx = side * (face_x + 0.010 + PANEL_T + STACK_GAP / 2 + i * (PANEL_T + STACK_GAP))
                hz = z_plane + (PANEL_L / 2 - 0.02) * (1.0 if i % 2 == 0 else -1.0)
                hinges.append(_cyl("HINGE_%s0%d" % (sx, i + 1), (hx, 0.0, hz),
                                   (0.0, 1.0, 0.0), HINGE_R, HINGE_L, mech, verts=16))
        out += hinges
    C.refresh()
    if prefix:
        for o in out:
            o.name = "%s_%s" % (prefix, o.name)
    return out


def span_of(objs):
    """一器两翼的 X 跨度（供 D8/E20 核验）。"""
    xs = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    return max(p.x for p in xs) - min(p.x for p in xs)


def normal_angle_to_tail(objs):
    """展开翼法向与 −Z 的夹角（度）——板是薄盒，法向取局部最短边方向。"""
    ang = []
    for o in objs:
        if "_" not in o.name and "panel" not in o.name:
            continue
        d = o.dimensions
        axis = d.index(min(d))
        if axis == 2:
            ang.append(0.0)
        elif axis == 0:
            ang.append(90.0)
        elif axis == 1:
            ang.append(90.0)
    return max(ang) if ang else None
