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

**几何 v3：集光器十字翼改为斜面安装＋SADA 双自由度方位修正**（《验收反馈 09》§三 α 方案）。
集光器舱（``collector.build_bus``）±X 面是**梯形斜面**：平行边沿 X，长边 0.91 在 y=+BUS_D/2、
短边 0.59 在 y=−BUS_D/2 ⇒ 斜角 ``SLANT_DEG``。整改前翼建在 |x|=face_x 的**竖直中位面**上，
宽端没入、窄端离缝。现在（仅 ``topology="cross"``）：

* 装翼一律换算成**垂距** ``d_perp = face_x·cos(SLANT)``，并沿斜面滑移
  ``dy = d_perp·tan(SLANT)``，使翼中线落在斜面 y=0 处（不滑移则整翼落到**垂足**、
  沿舱轴偏置 ≈0.086 m，摞/底座将探出斜面窄端）；
* 收拢态：整翼（SADA＋铰链＋翼缘）绕舱轴转 ``MOUNT_ROT_Z_DEG(side) = −side·SLANT``
  ⇒ 摞面与斜面平行、SADA 底座/球铰贴面、板-舱没入＝0；
* 展开态：SADA 在主旋转之外做 ＋side·SLANT 的**方位修正**（反向抵消），十字臂回到
  正交位（对边两板 ∥ 基线、跨 1.71×1.14）——面板与整改前**同一组数值**，只把翼根由
  中位面改到斜面垂距上、并按新几何加长短臂。

``topology="chain"``（合束器，矩形舱面，斜角 0）路径**逐字不变**：斜面角、滑移量、
安装角恒为 0。
"""
import math

import bpy
from mathutils import Matrix, Vector

import collector as C

PANEL_N = 4                        # 每翼板数（0/1/2/3 号）
PANEL_L = C.PANEL_L / PANEL_N      # A 类（合束器，链式 Z 折）：单板展向长＝原板长/4 = 0.45
PANEL_W = C.PANEL_W                # A 类弦向宽 0.72
PANEL_SQ = 0.57                    # B 类（集光器，十字）：方板边长 0.57（4×0.57²＝1.2996≈1.296 m²）
PANEL_T = C.PANEL_T                # 厚 0.03
PANEL_GAP = 0.006                  # 展开态板间缝
ROOT_OUT = 0.100                   # cross：0 号板内缘相对**斜面垂距**的外推量（原 0.050，见 docstring）
CHAIN_ROOT_OUT = 0.050             # chain（合束器，矩形舱面）：**保持整改前数值**，不随上一条变
SADA_R = 0.042                     # 球铰半径
SADA_BASE = (0.040, 0.150, 0.130)  # 底座（沿舱面贴装）
SADA_CLEAR = 0.002                 # cross：底座内表面离斜面的装配间隙（贴面、不没入）
SADA_BALL_DX = 0.030               # cross：球铰心相对斜面垂距（沿面法向外推）
ARM_R, ARM_L = 0.016, 0.060        # chain（合束器）球铰到 0 号板的短臂——数值不动
ARM_OVER = 0.020                   # cross 短臂两端各嵌入球铰/0 号板，保证连接无离缝
HINGE_R, HINGE_L = 0.020, 0.110    # 板间铰链（轴沿 Y）
STACK_GAP = 0.004                  # 收拢叠板间隙（Z 折折缝）
STOW_IN = 0.010                    # 收拢：整摞内表面距舱面（cross/chain 同值，原写在 build_wing 内）
PITCH = PANEL_T + STACK_GAP        # 摞层节距 0.034（＝收拢摞厚/4 的层间距）
SADA_FOLD_DEG = 90.0               # SADA 折叠角：收拢⇄展开绕折叠轴转 90°
ARM_FOLD_DEG = 180.0               # 板间 Z 折角：收拢 180° 对折
Z_PLANE = 0.0                      # 展开态翼面高度（舱体中部）
COL_PANEL = C.COL_PANEL
COL_ALU = (0.62, 0.63, 0.64)       # 阳极氧化铝（机构件，§四）
COL_MECH = (0.55, 0.56, 0.58)

# ── 集光器舱 ±X 斜面的斜角（由 collector 的梯形舱尺寸导出，不改 collector）──
SLANT_DEG = math.degrees(math.atan2((C.BUS_W - C.BUS_SHORT_W) / 2.0, C.BUS_D))   # 13.8287°
SLANT_RAD = math.radians(SLANT_DEG)
AXIS_VEC = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}   # 铰轴名→矢量


def MOUNT_ROT_Z_DEG(side):
    """该侧翼的**安装方位角**（度，绕集光器舱轴 Z）＝−side·SLANT_DEG。

    SADA 在展开态的方位修正＝其反向（＋side·SLANT_DEG）。chain（矩形舱面）斜角为 0。
    """
    return -side * SLANT_DEG


def MOUNT_FACE_X(face_x):
    """斜面装翼：调用方给的 face_x（＝y=0 处水平半宽）→ 舱面**垂距** d_perp。"""
    return face_x * math.cos(SLANT_RAD)


def MOUNT_FACE_DY(face_x):
    """斜面装翼：沿斜面滑移量，使翼中线落在斜面 y=0 处（不滑移就落到垂足、偏置 0.086 m）。"""
    return MOUNT_FACE_X(face_x) * math.tan(SLANT_RAD)


def cross_fold_kinematics(side, face_x):
    """集光器十字翼（cross）的**折叠运动学**：一份数值，供 ``build_wing`` 与动画脚本共用。

    ``side`` ∈ {±1}；``face_x`` 是调用方口径（该侧舱面在 **y=0 处**的水平半宽，与 build_wing 一致），
    内部一律换算成垂距 ``d_perp``。**返回量全部在"施加安装方位旋转 R_m 之前"的构建系内**，即
    ``build_wing`` 收拢分支末尾 ``_rot_z(out, rot)`` 之前的那一套坐标：

      d_perp / dy / mount_rot_z_deg  斜面安装三量（垂距／沿面滑移／安装方位角）
      hinge / hinge_axis             SADA 折叠轴（轴点在构建系内，轴沿 Y）
      arm_hinges                     {板号 2／4／3: (摞层次序 level, 铰点, 铰轴)}——0 号板无板间铰
      stow_in / pitch / settle_dz    摞内表面距面 0.010／摞层节距 0.034／折转落位量
      fold_deg / arm_fold_deg        SADA 折叠角 90°／板间 Z 折角 180°
      panel_sq / panel_t             方板边长 0.57／板厚 0.03
      root_x                         展开 0 号板心 x
      stack_x / stow_hinge           收拢 4 层板心 x（底→顶）／3 道折缝铰位置 (x, z)
      layers                         {板号: 摞层次序}（底＝0）
      deployed_center / stow_center  {板号: 板心}（展开在 bus 系、收拢在构建系）
      sada_base_dx / sada_ball_dx    底座／球铰相对舱面的外推量（cross 装配量）

    **分层次序按 P9**（收拢序"1、3 号先折、2 号压顶"）⇒ 底→顶为 1、(2)、(4)、(3)：panel_2 压在最上。
    注意 ``build_wing(state="stowed", topology="cross")`` 是按 1、2、3、4 顺序分层的（两处差一层，
    阶段五 F7 台账里如实量出，不改 wing.py 的收拢构建——超出限域）。

    折叠复合式（阶段五动画用它驱动，见 separation.py ``wing_basis``）：``R_trim @ R_m @ F @ REST``，
    其中 F 由本函数的 ``hinge／hinge_axis／arm_hinges／dy／settle_dz／pitch`` 构成：SADA 铰折（绕
    ``hinge`` 的 ``hinge_axis`` 转 ``fold_deg``）＋沿面滑移 ``dy`` ＋ ``settle_dz`` 落位＋板间
    ``arm_fold_deg`` 折。两端点与 wing.py 自身两态构建**逐顶点一致**（F7 台账复算）。
    """
    side = 1.0 if side > 0 else -1.0
    d_perp = MOUNT_FACE_X(face_x)
    dy = MOUNT_FACE_DY(face_x)
    rot = MOUNT_ROT_Z_DEG(side)
    root_x = side * (d_perp + ROOT_OUT + PANEL_SQ / 2)                 # 展开 0 号板心
    hinge = Vector((side * (d_perp + STOW_IN + PANEL_T / 2), 0.0, 0.0))  # 折叠轴心＝收拢底板板心
    settle_dz = (ROOT_OUT + PANEL_SQ / 2) - (STOW_IN + PANEL_T / 2)     # 折转后沿 +Z 落位量
    stack_x = [side * (d_perp + STOW_IN + PANEL_T / 2 + i * PITCH) for i in range(PANEL_N)]
    layers = {1: 0, 2: 1, 3: 3, 4: 2}          # P9：panel_2（对象名 panel_*3）压顶
    arm_hinges = {
        2: (1.0, Vector((root_x, +PANEL_SQ / 2, 0.0)), "X"),
        4: (2.0, Vector((root_x, -PANEL_SQ / 2, 0.0)), "X"),
        3: (3.0, Vector((root_x + side * PANEL_SQ / 2, 0.0, 0.0)), "Y"),
    }
    deployed_center = {1: Vector((root_x, 0.0, 0.0)),
                       2: Vector((root_x, +PANEL_SQ, 0.0)),
                       3: Vector((root_x + side * PANEL_SQ, 0.0, 0.0)),
                       4: Vector((root_x, -PANEL_SQ, 0.0))}
    stow_center = {k: Vector((stack_x[layers[k]], dy, 0.0)) for k in (1, 2, 3, 4)}
    stow_hinge = [(stack_x[i] + side * PITCH / 2,
                   (PANEL_SQ / 2 - 0.02) * (1.0 if i % 2 == 0 else -1.0)) for i in range(3)]
    return {
        "side": side, "face_x": face_x, "d_perp": d_perp, "dy": dy,
        "mount_rot_z_deg": rot, "mount_rot_z_rad": math.radians(rot),
        "hinge": hinge, "hinge_axis": "Y", "arm_hinges": arm_hinges, "layers": layers,
        "stow_in": STOW_IN, "pitch": PITCH, "settle_dz": settle_dz,
        "fold_deg": SADA_FOLD_DEG, "arm_fold_deg": ARM_FOLD_DEG,
        "panel_sq": PANEL_SQ, "panel_t": PANEL_T, "panel_n": PANEL_N,
        "root_x": root_x, "stack_x": stack_x, "stow_hinge": stow_hinge,
        "deployed_center": deployed_center, "stow_center": stow_center,
        "sada_base_dx": SADA_CLEAR + SADA_BASE[0] / 2.0, "sada_ball_dx": SADA_BALL_DX,
    }


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


def _rot_z(objs, deg, pivot=(0.0, 0.0, 0.0)):
    """把一组已建好的对象整体绕**过 pivot 的 Z 轴**转 deg 度（pivot 默认舱轴＝局部原点）。"""
    p = Vector(pivot)
    m = (Matrix.Translation(p) @ Matrix.Rotation(math.radians(deg), 4, "Z")
         @ Matrix.Translation(-p))
    for o in objs:
        o.matrix_world = m @ o.matrix_world


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


def build_wing(side, face_x, state="deployed", topology="chain", prefix="", z_plane=Z_PLANE):
    """建一翼：SADA＋4 板＋板间铰链×3。返回对象列表（世界坐标、未挂父级）。

    ``side`` ∈ {+1, −1}（±X 侧）；``face_x``＝该侧舱面的 |x|（梯形舱取 y=0 处半宽，
    与 collector 的斜面一致；内部按**垂距** d_perp 建，见模块 docstring）；
    ``state`` ∈ {"deployed", "stowed"}；
    ``topology`` ∈ {"chain"（A 类，合束器：4 板 0.45×0.72 链式共线）,
    "cross"（B 类，集光器：0 号方板居中，1/2/3 号方板铰接其相邻三边，展开成十字）}。

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

    # ── 集光器（cross）挂在梯形**斜面**上：face_x 是 y=0 处水平半宽，装翼按垂距 d_perp
    #    并沿面滑移 dy；合束器（chain）舱面是矩形平面 ⇒ 三量恒为 0，路径逐字不变。
    #    斜面安装/折叠的全部数值由 ``cross_fold_kinematics`` 一处给出（阶段五动画同一份）──
    cross = (topology == "cross")
    if cross:
        K = cross_fold_kinematics(side, face_x)
        face_x, dy, rot = K["d_perp"], K["dy"], K["mount_rot_z_deg"]
    else:
        K = None
        dy, rot = 0.0, 0.0

    if state == "deployed" and topology == "cross":
        # ── B 类十字（反馈 06 N1）：0 号根板居中，对边两板沿 ±Y（∥基线）、第三边板沿 +X 外伸；
        #    展开轮廓十字：对边轴 1.71＝0.57×3、第三边轴 1.14＝0.57×2；板面法向 ±Z。
        #    斜面安装（几何 v3）：SADA 底座/球铰随安装角 R_m 转到斜面相平行并贴面，
        #    翼面（板/铰链/翼缘）保持正交位＝SADA 方位修正 −R_m 后的结果 ──
        base = _box("SADA_%s_base" % sx,
                    (side * (face_x + K["sada_base_dx"]), dy, z_plane),
                    SADA_BASE, mech)
        ball = _cyl("SADA_%s_ball" % sx, (side * (face_x + K["sada_ball_dx"]), dy, z_plane),
                    (side, 0.0, 0.0), SADA_R, 0.045, mech, verts=24)
        sada = [base, ball]
        _rot_z(sada, rot)
        # 短臂：自球铰心横跨到 0 号板内缘，两端各嵌入 ARM_OVER ⇒ 球铰↔0 号板连接无离缝
        ball_c = ball.matrix_world.translation.copy()
        p_root = Vector((side * (face_x + ROOT_OUT), 0.0, z_plane))
        d_arm = p_root - ball_c
        arm = _cyl("SADA_%s_arm" % sx, ball_c + d_arm * 0.5, d_arm.normalized(), ARM_R,
                   d_arm.length + 2.0 * ARM_OVER, mech, verts=16)
        sada.append(arm)
        out.append(_join(sada, "SADA_%s" % sx, mech))
        slots = [("%d" % k, tuple(K["deployed_center"][k][:2]) + (z_plane,))
                 for k in (1, 2, 3, 4)]        # 0 号根板居中；2/4 对边（±Y）；3 号第三边（+X 外伸）
        for i, (nm, c) in enumerate(slots):
            out.append(_box("panel_%s%s" % (sx, nm), c, (PANEL_SQ, PANEL_SQ, PANEL_T), panel_mat))
            out.append(_edge_frame("EDGE_%s%s" % (sx, nm), c, (PANEL_SQ, PANEL_SQ, PANEL_T), alu))
            if i == 0:
                continue                              # 0 号板无铰链；1/2/3 各自铰接其一条边
            _lvl, p_h, axis = K["arm_hinges"][i + 1]  # 板间铰＝该臂的折叠轴（同一点/轴，一处定义）
            jx = (p_h.x, p_h.y, z_plane)
            out.append(_cyl("HINGE_%s0%d" % (sx, i), jx, AXIS_VEC[axis], HINGE_R, HINGE_L * 1.6,
                            mech, verts=16))
    elif state == "deployed":
        # ── SADA：底座贴舱面 ＋ 球铰 ＋ 短臂，翼面在 z_plane 平面内、法向 −Z ──
        base = _box("SADA_%s_base" % sx, (side * (face_x - 0.005), 0.0, z_plane),
                    SADA_BASE, mech)
        ball = _cyl("SADA_%s_ball" % sx, (side * (face_x + 0.030), 0.0, z_plane),
                    (side, 0.0, 0.0), SADA_R, 0.045, mech, verts=24)
        arm = _cyl("SADA_%s_arm" % sx, (side * (face_x + 0.055), 0.0, z_plane),
                   (side, 0.0, 0.0), ARM_R, ARM_L, mech, verts=16)
        out.append(_join([base, ball, arm], "SADA_%s" % sx, mech))

        # ── 4 板共线自 SADA 外伸：展向沿 X、弦向沿 Y、法向 ±Z ──
        root = face_x + CHAIN_ROOT_OUT
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
        if cross:      # 斜面安装：底座/球铰的装配外推量、摞层位置由运动学一处给出
            base_dx, ball_dx = K["sada_base_dx"], K["sada_ball_dx"]
        else:
            base_dx, ball_dx = -0.005, 0.026
        base = _box("SADA_%s_base" % sx, (side * (face_x + base_dx), dy, z_plane),
                    SADA_BASE, mech)
        ball = _cyl("SADA_%s_ball" % sx, (side * (face_x + ball_dx), dy, z_plane),
                    (side, 0.0, 0.0), SADA_R, 0.045, mech, verts=24)
        out.append(_join([base, ball], "SADA_%s" % sx, mech))
        hinges = []
        pw, pl = (PANEL_SQ, PANEL_SQ) if topology == "cross" else (PANEL_W, PANEL_L)
        for i in range(PANEL_N):
            nm = "panel_%s%d" % (sx, i + 1)
            if cross:                                  # 摞层 x：cross 按 P9 分层（底板在最下）
                cx = K["stack_x"][i]
            else:
                cx = side * (face_x + STOW_IN + PANEL_T / 2 + i * PITCH)
            p = _box(nm, (cx, dy, z_plane), (PANEL_T, pw, pl), panel_mat)
            out.append(p)
            out.append(_edge_frame("EDGE_%s%d" % (sx, i + 1), (cx, dy, z_plane),
                                   (PANEL_T, pw, pl), alu))
            if i < PANEL_N - 1:                       # 折缝铰链：在相邻两板之间、沿 Y
                if cross:
                    hx, hz = K["stow_hinge"][i]
                else:
                    hx = side * (face_x + STOW_IN + PANEL_T + STACK_GAP / 2 + i * PITCH)
                    hz = (PANEL_L / 2 - 0.02) * (1.0 if i % 2 == 0 else -1.0)
                hinges.append(_cyl("HINGE_%s0%d" % (sx, i + 1), (hx, dy, z_plane + hz),
                                   (0.0, 1.0, 0.0), HINGE_R, HINGE_L, mech, verts=16))
        out += hinges
        if cross:      # 整翼绕舱轴转到与斜面平行（SADA 底座/球铰随之贴面）
            _rot_z(out, rot)
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
