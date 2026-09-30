# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——阶段四：分布式编队场景（含光束可视化）。

用法：
    blender --background --python formation.py            # 预览（1200×900 @64 samples）
    blender --background --python formation.py -- --final  # 正式（1920×1080 @512 + EXR，需人工许可）

依《阶段四_建模规格》v1.3（2026-09-30）：
    * 三器沿基线（Y）一字排开，合束器居中，集光器分居两侧；
      显示基线 B = 12 m（真实 20–100 m 不成比例，记录于 E12）；三器舱心 x/z 对齐。
    * 集光器 A 在 y=−B/2、窗口朝 +Y；B 在 y=+B/2、窗口朝 −Y（A 保持单体姿态，B 绕 Z 转 180°）。
    * 单体**逐字复用** collector.build()／combiner.build()（加了 purge=False 开关），不复制代码。
    * 星光束：粉色粗（Φ0.24），自 +Z 垂直入射筒口，终点落**最内光阑环截面**（内嵌 ≤0.02 m），
      起点高出筒口 2.5 m；器间束：红色细（Φ0.06），出光窗口→载荷舱收光口，两端各内嵌 0.02 m。
    * T1（已确认）：集光器太阳翼沿用单体 ±X 构型不改。
      T2（已确认）：载荷舱基部 ±Y 各开一等径圆口（口径 = 红束径 + 0.04 m，黑色内腔）。
    * 布局父级 EMPTY_LAYOUT 统一控制编队距离（三器与四束均挂其下）。
    * v1.3 观感升级包（§二.7／§四／§五）：在**各器加前缀、挂父级之前**追加推断默认件
      （RCS×4、测控天线×1、翼根铰链＋翼缘描边、筒口光阑环、机构簇转台＋细杆），
      并给金色 MLI 加绗缝 bump 与 roughness 变化、补单一主光——升级件按单体的**构建局部系**
      定位，从而不动阶段一/二/三任何已验收几何（A/C/D 与 E1–E12 回归必须全过）。

端点一律由**实测包围盒**反算（筒口截面／最内光阑环、出光窗口面、收光口面），不写目测偏移。
"""
import math
import os
import sys

import bpy
from mathutils import Euler, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import collector as C
import combiner as M

OUT_DIR = os.path.join(HERE, "out", "formation")

# ============================================================ 参数区
BASELINE_DISPLAY = 12.0        # 显示基线（m）
BASELINE_REAL_M = (20.0, 100.0)  # 真实基线区间（仅记录，E12）

D_LINK = 0.06                  # 器间束直径（红细）
D_STAR = 0.24                  # 星光束直径（粉粗）——与 D_LINK 之比 4:1
PORT_EXTRA = 0.04              # 收光口径外扩量（T2：口径 = 红束径 + 0.04）
PORT_LEN = 0.06                # 收光口罩筒长度
EMBED = 0.02                   # 光束端点内嵌深度（判据 ≤0.02 m）
STAR_ABOVE = 2.5               # 星光束起点高出筒口（判据 ≥2 m）
STAR_BOTTOM_EMBED = 0.015      # 星光束下端没入最内光阑环（判据 ≤0.02 m）

# 光束颜色：规格 §四 给的 (1.0, 0.56, 0.63)/(1.0, 0.165, 0.10) 当线性值直用会渲染成 #FFC4CC
# 那种**淡粉**（本工程约定是"线性值＋注释 sRGB"，见 COL_BUS 0.585→#C9A227），AGX 下实测核心
# R/G 只有 1.06≈白芯粉边，与规格同一行写的"核心保留粉色（不过曝）"冲突。故按 **F3/F5 渲染图
# 实测的束色**反解发光色：粉束核心 (0.886,0.533,0.533) R/G=1.66、红束 (0.651,0.267,0.267)
# R/G=2.44 → 取 (1.0,0.18,0.18)@1.2 与 (1.0,0.05,0.04)@3.0，实测核心 (0.902,0.584,0.569)
# 与 (0.812,0.329,0.298)，与基准图同色相。规格字面值见交付说明差异清单。
COL_STAR = (1.0, 0.18, 0.18)   # 反解自 F5 粉束（#FF8FA0 系）
COL_LINK = (1.0, 0.05, 0.04)   # 反解自 F5 红束（#FF2A1A 系）
STAR_STRENGTH = 1.2            # v1.3：压爆白，核心须保留粉色（试算值，规格 §四）
LINK_STRENGTH = 3.0            # v1.3：同上

BG_TEXTURE = os.path.join(HERE, "..", "..", "assets", "textures", "8k_stars_milky_way.jpg")
BG_STRENGTH = 0.60             # v1.3：预览档试 0.6，暗背景+星场可辨
GLARE_THRESHOLD = 1.0          # 只让光束与筒口过阈
BEAM_DIFFUSE_DAMP = 0.05       # 束体漫反射反照压到 5%（光柱不是反射面，避免核心炸白）
# Glare 跨版本：旧版（≤4.x）走节点属性、size 是 6–9 的档位；新版（5.x）走输入插槽、
# Type 菜单吃显示名 "Fog Glow"、Size 是 0–1 的系数，另多出强度/饱和度/上限三个插槽。
GLARE_TYPE_NEW = "Fog Glow"
GLARE_QUALITY_NEW = "High"
GLARE_SIZE_OLD, GLARE_SIZE_NEW = 8, 0.55
GLARE_STRENGTH_NEW, GLARE_SATURATION_NEW, GLARE_MAX_NEW = 0.35, 1.10, 2.5
SUN_DIR = (1.0, -0.45, 1.0)    # v1.3：单一主光方位（+X 上方 45° 斜入），记入 sun_dir
SUN_ENERGY, FILL_ENERGY = 4.0, 0.7

# ---- v1.3 推断默认件（§二.7）----
RCS_N = 4                      # 每器 RCS 喷管数
RCS_D = 0.060                  # 喷管长
RCS_Y = 0.33                   # 喷管所在 y（舱体四角侧面）
RCS_EMBED = 0.012              # 根部嵌进舱面（舱侧面是斜面，留贴合余量）
RCS_Z_BELOW_TOP = 0.16         # 喷管在舱顶以下的高度
ANT_POS = (0.330, -0.330)      # 测控天线舱顶位置（避开镜筒/载荷舱/机构簇）
ANT_R = (0.090, 0.130)         # 天线口径（集光器小定向 / 合束器高增益小锅）
HINGE = (0.110, 0.110, 0.130)  # 翼根铰链块尺寸
EDGE_W = 0.020                 # 翼缘描边宽（§四）
BAFFLE_RINGS = 2               # 每根筒口内光阑环数
BAFFLE_DZ = 0.050              # 环间距（自筒口向下）
BAFFLE_MINOR = 0.016         # 环截面半径：外缘压进内壁 6 mm
QUILT_SCALE, QUILT_BUMP = 7.0, 0.32
ROUGH_RANGE = (0.55, 0.70)     # 金色 MLI roughness 变化区间（§四）

RES = (1200, 900)
SAMPLES = 64
RES_FINAL = (1920, 1080)
SAMPLES_FINAL = 512
BG = 0.008


# ============================================================ 工具
def vbounds(objs):
    C.refresh()
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def vcenter(o):
    lo, hi = vbounds([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


def _vlocal(objs):
    """顶点在**构建局部系**中的坐标：matrix_basis 不含父级变换。

    升级件必须在加前缀/挂父级之前生成——group() 会给 holder 设 location／rotation，
    此后 matrix_basis 仍是"局部量"而 vbounds 给的是世界量，两者混用会把升级件摆到
    合束器附近（首版补丁即栽在这里）。
    """
    return [o.matrix_basis @ v.co for o in objs for v in o.data.vertices]


def _lbounds(objs):
    pts = _vlocal(objs)
    return ([min(p[i] for p in pts) for i in range(3)],
            [max(p[i] for p in pts) for i in range(3)])


def group(prefix, location, rot_z_deg, objs):
    holder = bpy.data.objects.new(prefix + "_ROOT", None)
    bpy.context.collection.objects.link(holder)
    holder.location = Vector(location)
    holder.rotation_euler = (0.0, 0.0, math.radians(rot_z_deg))
    for o in objs:
        if o.parent is None:
            o.parent = holder
    C.refresh()
    for o in objs:
        o.name = "%s_%s" % (prefix, o.name.split(".")[0])
    return holder


def emit_material(name, color, strength):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.4
    # 自发光：判据 E8 要求四束均为 emission
    if "Emission Color" in b.inputs:
        b.inputs["Emission Color"].default_value = (*color, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    else:                                   # 旧版接口兜底
        b.inputs["Emission"].default_value = (*color, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    # 束体是"光柱"不是"反射面"：关掉高光、把漫反射反照压到近乎零。否则太阳灯（能量 4.0）
    # 在这层粉色面上打出白色高光，radiance＝自发＋反射冲顶被 AgX 去饱和 → 核心炸白
    # （实测核心 R/G 会从 1.7 掉到 1.09，肉眼即"白芯粉边"）。基色 default_value 保留粉色供
    # E8 判色（同 collector.add_panel_grid 的做法：判据读 socket 默认值，与是否接线无关）。
    for spec in ("Specular IOR Level", "Specular"):
        if spec in b.inputs:
            b.inputs[spec].default_value = 0.0
            break
    b.inputs["Metallic"].default_value = 0.0
    damp = m.node_tree.nodes.new("ShaderNodeRGB")
    damp.outputs[0].default_value = (*[c * BEAM_DIFFUSE_DAMP for c in color], 1.0)
    m.node_tree.links.new(damp.outputs[0], b.inputs["Base Color"])
    m.diffuse_color = (*color, 1.0)
    return m


def cylinder_between(name, p0, p1, radius, mat, verts=24):
    """在两点之间生成圆柱（端点即 p0/p1，用于"两端内嵌"）。"""
    p0, p1 = Vector(p0), Vector(p1)
    d = p1 - p0
    length = d.length
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=length,
                                        location=(p0 + p1) / 2)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_euler = d.to_track_quat("Z", "Y").to_euler()
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    ob.data.materials.append(mat)
    # 端点与直径记录在案：验收据此判"内嵌 ≤0.02"与束径比（用包围盒会被斜圆柱的截面半径外扩）
    ob["p_start"] = list(p0)
    ob["p_end"] = list(p1)
    ob["diameter"] = 2 * radius
    return ob


# ============================================================ v1.3 观感升级包（新增件）
def _set_in(node, name, val):
    if name in node.inputs:
        node.inputs[name].default_value = val
        return True
    return False


def _face_half_x(bus_pts, y):
    """梯形舱在给定 y 处的截面半宽：两端面**实测**半宽线性插值（倒角后仍成立）。"""
    ylo = min(p[1] for p in bus_pts)
    yhi = max(p[1] for p in bus_pts)
    half = []
    for yv in (ylo, yhi):
        xs = [p[0] for p in bus_pts if abs(p[1] - yv) < 0.03]
        half.append(max(xs) if xs else 0.0)
    if min(half) <= 0.0:
        return max(p[0] for p in bus_pts)
    t = min(max((y - ylo) / (yhi - ylo), 0.0), 1.0)
    return half[0] + (half[1] - half[0]) * t


def _axis_piece(name, loc, axis, r, depth, mat, verts=20):
    """沿给定轴向放置的圆柱（转台／细杆），端面与贴合面共面。"""
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r, depth=depth, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_euler = Vector(axis).to_track_quat("Z", "Y").to_euler()
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    ob.data.materials.append(mat)
    return ob


def _nozzle(name, loc, axis, mat):
    """RCS 锥形喷管：锥尖朝外。"""
    bpy.ops.mesh.primitive_cone_add(vertices=16, radius1=0.045, radius2=0.012,
                                    depth=RCS_D, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.rotation_euler = Vector(axis).to_track_quat("Z", "Y").to_euler()
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    ob.data.materials.append(mat)
    return ob


def _dish(name, loc, mat, r):
    """测控天线：短桅杆＋朝天张开的小锅（锥台大口朝上）。桅杆根部嵌入舱顶，不悬浮。"""
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.012, depth=0.070,
                                        location=(loc[0], loc[1], loc[2] + 0.020))
    mast = bpy.context.active_object
    mast.data.materials.append(mat)
    bpy.ops.mesh.primitive_cone_add(vertices=24, radius1=0.015, radius2=r, depth=0.050,
                                    location=(loc[0], loc[1], loc[2] + 0.060))
    dish = bpy.context.active_object
    dish.data.materials.append(mat)
    bpy.ops.object.select_all(action="DESELECT")
    mast.select_set(True)
    dish.select_set(True)
    bpy.context.view_layer.objects.active = mast
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = name
    return ob


def _box(name, loc, size, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(mat)
    return ob


def _edge_frame(name, lo, hi, mat):
    """翼缘描边：沿翼板四边各一条细梁。

    不能用一个"略大一圈的盒子"罩住翼板——那会把蓝色电池面整个藏进铝板里（首版补丁的写法）。
    """
    cx, cy, cz = [(lo[i] + hi[i]) / 2 for i in range(3)]
    dx, dy, dz = [hi[i] - lo[i] for i in range(3)]
    ty = dy + 0.004
    bars = (([cx, cy, cz + dz / 2], (dx + 2 * EDGE_W, ty, EDGE_W)),
            ([cx, cy, cz - dz / 2], (dx + 2 * EDGE_W, ty, EDGE_W)),
            ([cx + dx / 2, cy, cz], (EDGE_W, ty, dz + 2 * EDGE_W)),
            ([cx - dx / 2, cy, cz], (EDGE_W, ty, dz + 2 * EDGE_W)))
    parts = [_box("%s_b%d" % (name, i + 1), loc, size, mat)
             for i, (loc, size) in enumerate(bars)]
    bpy.ops.object.select_all(action="DESELECT")
    for o in parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = name
    return ob


def _quilt(mat):
    """金色 MLI 绗缝：两向波带取小值→凹凸，**串在既有褶皱 bump 之后**（两种质感都保留）。

    基色 default_value 不动，A6 判色安全；另加噪波驱动的 roughness 变化（§四 要求 0.55–0.70 带变化）。
    """
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    waves = []
    for direction in ("X", "Z"):
        w = nt.nodes.new("ShaderNodeTexWave")
        w.wave_type = "BANDS"
        w.bands_direction = direction
        _set_in(w, "Scale", QUILT_SCALE)
        _set_in(w, "Distortion", 6.0)
        _set_in(w, "Detail", 2.0)
        nt.links.new(coord.outputs["Object"], w.inputs["Vector"])
        waves.append(w)
    mn = nt.nodes.new("ShaderNodeMath")
    mn.operation = "MINIMUM"
    nt.links.new(waves[0].outputs["Fac"], mn.inputs[0])
    nt.links.new(waves[1].outputs["Fac"], mn.inputs[1])
    bump = nt.nodes.new("ShaderNodeBump")
    _set_in(bump, "Strength", QUILT_BUMP)
    nt.links.new(mn.outputs["Value"], bump.inputs["Height"])
    prev = [n for n in nt.nodes if n.type == "BUMP" and n is not bump]
    if prev:
        nt.links.new(prev[0].outputs["Normal"], bump.inputs["Normal"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    noise = nt.nodes.new("ShaderNodeTexNoise")
    _set_in(noise, "Scale", 12.0)
    nt.links.new(coord.outputs["Object"], noise.inputs["Vector"])
    mr = nt.nodes.new("ShaderNodeMapRange")
    _set_in(mr, "To Min", ROUGH_RANGE[0])
    _set_in(mr, "To Max", ROUGH_RANGE[1])
    nt.links.new(noise.outputs["Fac"], mr.inputs[0])
    nt.links.new(mr.outputs[0], bsdf.inputs["Roughness"])
    return bump


def upgrade(objs, has_tube):
    """给一台器加 v1.3 推断默认件；**在加前缀/挂父级之前调用**，坐标即单体构建局部系。

    ``objs`` 为单体 build() 的返回字典（键为基名）；返回新增对象列表。
    """
    gold = C.new_material("MAT_up_gold", C.COL_BUS, 0.62, 0.15)
    alu = C.new_material("MAT_up_alu", (0.72, 0.72, 0.70), 0.35, 0.85)
    black = C.new_material("MAT_up_baffle", (0.015, 0.015, 0.017), 0.90, 0.10)
    grey = C.new_material("MAT_up_grey", (0.55, 0.56, 0.58), 0.45, 0.60)
    added = []

    bus = objs["bus"]
    bus_pts = _vlocal([bus])
    _, bhi = _lbounds([bus])
    top_z = bhi[2]

    # 1) RCS 推力器组 ×4：舱体四角侧面，锥尖朝外；x 取该 y 处实测截面半宽，根部嵌入舱面
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            y = sy * RCS_Y
            half = _face_half_x(bus_pts, y)
            added.append(_nozzle("RCS_%s_%s" % ("P" if sx > 0 else "N", "F" if sy > 0 else "A"),
                                 (sx * (half + RCS_D / 2 - RCS_EMBED), y, top_z - RCS_Z_BELOW_TOP),
                                 (sx, 0.0, 0.0), gold))

    # 2) 测控天线 ×1：舱顶一角（避开镜筒/载荷舱/机构簇）
    added.append(_dish("ANT_dish", (ANT_POS[0], ANT_POS[1], top_z), grey,
                       ANT_R[0] if has_tube else ANT_R[1]))

    # 3) 太阳翼：根部金色铰链块 ＋ 翼缘铝色描边
    for p in [o for k, o in objs.items() if k.startswith("panel_")]:
        plo, phi = _lbounds([p])
        cz = (plo[2] + phi[2]) / 2
        sx = 1.0 if (plo[0] + phi[0]) / 2 > 0 else -1.0
        tag = "x%s_z%+.2f" % ("P" if sx > 0 else "N", cz)
        root = min(abs(plo[0]), abs(phi[0]))        # 靠舱一侧的翼根面
        added.append(_box("HINGE_" + tag, (sx * (root + HINGE[0] / 2 - 0.035), 0.0, cz),
                          HINGE, gold))
        added.append(_edge_frame("EDGE_" + tag, plo, phi, alu))

    # 4) 镜筒口内光阑环（哑光黑环）：环不能是实心圆盘，否则会堵住筒口、
    #    也会与穿过筒口的星光束相交（E5）。环径按实测内壁半径反算。
    if has_tube:
        tube = objs["tube"]
        tlo, thi = _lbounds([tube])
        cx, cy = (tlo[0] + thi[0]) / 2, (tlo[1] + thi[1]) / 2
        rads = [math.hypot(p[0] - cx, p[1] - cy) for p in _vlocal([tube]) if p[2] < tlo[2] + 0.05]
        r_bore = min(rads) if rads else (thi[0] - tlo[0]) / 2 - 0.05
        for i in range(BAFFLE_RINGS):
            # 环外缘要**压进筒内壁**（外缘 = 内壁半径 + 6 mm），否则悬在筒腔里＝悬浮物
            bpy.ops.mesh.primitive_torus_add(major_segments=48, minor_segments=8,
                                             major_radius=r_bore - 0.010, minor_radius=BAFFLE_MINOR,
                                             location=(cx, cy, thi[2] - BAFFLE_DZ * (i + 1)))
            ring = bpy.context.active_object
            ring.name = "BAFFLE_%d" % (i + 1)
            ring.data.materials.append(black)
            added.append(ring)

    # 5) 机构簇细化（选画级）：每台机构加"两轴转台＋细杆"，按 collector 的构造式复算，端面共面
    if has_tube:
        off = 0.022                                    # _mechanism 里圆柱相对底座的让位量
        for i in range(C.GIMBAL_N):
            a = math.radians(C.GIMBAL_AZIMUTH[i % len(C.GIMBAL_AZIMUTH)])
            axis = Euler((math.radians(C.GIMBAL_TILT) * -math.sin(a),
                          math.radians(C.GIMBAL_TILT) * math.cos(a), a),
                         "XYZ").to_matrix() @ Vector((0.0, 0.0, 1.0))
            ctr = Vector((C.TUBE_X + C.GIMBAL_RING_R * math.cos(a) + off * math.cos(a),
                          C.TUBE_Y + C.GIMBAL_RING_R * math.sin(a) + off * math.sin(a),
                          C.BUS_H / 2 + C.GIMBAL_BASE[2] + C.GIMBAL_CYL_L * 0.40))
            end = ctr + axis * (C.GIMBAL_CYL_L / 2)
            added.append(_axis_piece("TURNTABLE_%02d" % (i + 1), end + axis * 0.011, axis,
                                     0.048, 0.022, grey))
            added.append(_axis_piece("ROD_%02d" % (i + 1), end + axis * 0.067, axis,
                                     0.014, 0.090, grey, verts=12))

    # 6) 金色 MLI 绗缝（材料轨，不改几何）：只动本器舱体材质，避免跨器重复叠加
    mat = bus.data.materials[0] if bus.data.materials else None
    if mat is not None and not mat.get("quilt_done"):
        _quilt(mat)
        mat["quilt_done"] = True
    return added


# ============================================================ 构件
def build_spacecraft():
    """三器：逐字复用单体 build()，各自成组；返回 {tag: {holder, objs, outline_*}}。

    objs 的键是**改名后**的名字（colA_bus 等），供后续按名查找筒口/最内环/窗口。
    升级件在 group() 之前生成（局部系），与单体件一起被加前缀、挂父级。
    """
    half = BASELINE_DISPLAY / 2
    specs = (("colA", C.build(purge=False), (0.0, -half, 0.0), 0.0),    # A 在 −Y：窗口朝 +Y
             ("colB", C.build(purge=False), (0.0, +half, 0.0), 180.0),  # B 在 +Y：窗口朝 −Y
             ("cmb", M.build(purge=False), (0.0, 0.0, 0.0), 0.0))
    out = {}
    for tag, objs, loc, rot in specs:
        parts = list(objs.values())
        before = _lbounds(parts)
        added = upgrade(objs, has_tube=(tag != "cmb"))     # v1.3 升级层
        holder = group(tag, loc, rot, parts + added)
        out[tag] = {"tag": tag, "holder": holder,
                    "objs": {o.name: o for o in parts + added},
                    "outline_before": before, "outline_after": _lbounds(parts + added),
                    "added": [o.name for o in added]}
    return out


def build_ports(cmb_objs):
    """T2：载荷舱基部 ±Y 各开一等径圆口（黑色内腔），口径 = 红束径 + PORT_EXTRA。"""
    mod = next(v for k, v in cmb_objs.items() if k.endswith("_module"))
    lo, hi = vbounds([mod])
    cx = (lo[0] + hi[0]) / 2
    pd = D_LINK + PORT_EXTRA
    zc = lo[2] + pd / 2 + 0.03            # 贴基部
    mat = C.new_material("MAT_port_black", (0.012, 0.012, 0.014), 0.5, 0.1)
    ports = {}
    for tag, sy in (("posY", +1.0), ("negY", -1.0)):
        y_face = hi[1] if sy > 0 else lo[1]
        yc = y_face + sy * (PORT_LEN / 2 - EMBED)   # 罩筒：外露 PORT_LEN-EMBED，内嵌 EMBED
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=pd / 2, depth=PORT_LEN,
                                            location=(cx, yc, zc))
        ob = bpy.context.active_object
        ob.name = "cmb_port_%s" % tag
        ob.rotation_euler = (math.radians(90), 0.0, 0.0)   # 轴向沿 Y
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        ob.data.materials.append(mat)
        ports[tag] = ob
    return ports


def ring_plane_z(ring):
    """光阑环的环心平面高度（世界）。"""
    lo, hi = vbounds([ring])
    return (lo[2] + hi[2]) / 2


def build_star_beams(col_objs, mat):
    """粉色粗光束 ×2：自 +Z 垂直入射镜筒口，终点落**最内光阑环截面**（v1.3 §二.7）。"""
    beams = []
    for tag in ("colA", "colB"):
        objs = col_objs[tag]["objs"]
        tube = next(v for k, v in objs.items() if k.endswith("_tube"))
        lo, hi = vbounds([tube])
        cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
        rings = [v for k, v in objs.items() if "_BAFFLE_" in k]
        inner_z = min(ring_plane_z(r) for r in rings) if rings else hi[2]
        p0 = (cx, cy, inner_z - STAR_BOTTOM_EMBED)      # 下端没入最内环
        p1 = (cx, cy, hi[2] + STAR_ABOVE)               # 上端出画
        beams.append(cylinder_between("BEAM_star_%s" % tag, p0, p1, D_STAR / 2, mat, verts=32))
    return beams


def build_link_beams(col_objs, ports, mat):
    """红色细光束 ×2：集光器出光窗口 → 合束器对应收光口，两端各内嵌 EMBED。"""
    beams = []
    for tag, port_tag in (("colA", "negY"), ("colB", "posY")):
        win = next(v for k, v in col_objs[tag]["objs"].items() if k.endswith("_window_out"))
        wlo, whi = vbounds([win])
        wc = [(wlo[i] + whi[i]) / 2 for i in range(3)]
        # 朝向合束器的一侧：colA 在 −Y → 朝 +Y；colB 在 +Y → 朝 −Y
        sgn = 1.0 if tag == "colA" else -1.0
        face_y = whi[1] if sgn > 0 else wlo[1]
        p0 = (wc[0], face_y - sgn * EMBED, wc[2])       # 起点：窗口面内嵌
        p = ports[port_tag]
        plo, phi = vbounds([p])
        pc = [(plo[i] + phi[i]) / 2 for i in range(3)]
        pface = phi[1] if sgn < 0 else plo[1]           # 收光口朝集光器的那一面
        p1 = (pc[0], pface + sgn * EMBED, pc[2])        # 终点：收光口内嵌
        beams.append(cylinder_between("BEAM_link_%s" % tag, p0, p1, D_LINK / 2, mat, verts=16))
    return beams


# ============================================================ 场景
def _set_glare(gl):
    """跨版本设 Glare 参数，并**回读校验**（设不上就打印，不静默）。

    ≤4.x：glare_type／quality／threshold／size 是**节点属性**，size 为 6–9 的档位整数。
    5.x：这些改成**输入插槽**，且 Type 菜单吃的是**显示名**（"Fog Glow"）而不是标识符——
    喂 "FOG_GLOW" 直接 TypeError。首版补丁把它连异常一起吞了，辉光实际一直停在默认的
    Streaks、Size 还被写成越界的 8，这正是"光束核心炸白"的主因。
    """
    def prop(attr, val):
        if not hasattr(gl, attr):           # None＝该版本没有这个属性，走插槽
            return None
        try:
            setattr(gl, attr, val)
            return True
        except Exception:
            return False

    def sock(name, val, ok=None):
        s = gl.inputs.get(name) if hasattr(gl.inputs, "get") else None
        if s is None:
            return False
        try:
            s.default_value = val
        except Exception:
            return False
        return True if ok is None else bool(ok(s))

    def menu(name, val, want):
        return sock(name, val, lambda s: str(s.default_value).replace(" ", "").lower() == want)

    done = []
    r = prop("glare_type", "FOG_GLOW")
    if r is not True:
        r = menu("Type", GLARE_TYPE_NEW, "fogglow")
    done.append("类型=%s" % ("Fog Glow" if r else "**失败**"))
    r = prop("quality", "HIGH")
    if r is not True:
        r = menu("Quality", GLARE_QUALITY_NEW, "high")
    done.append("质量=%s" % ("High" if r else "**失败**"))
    r = prop("threshold", GLARE_THRESHOLD)
    if r is not True:
        r = sock("Threshold", GLARE_THRESHOLD)
    done.append("阈值=%g" % GLARE_THRESHOLD)
    r = prop("size", GLARE_SIZE_OLD)
    if r is not True:
        r = sock("Size", GLARE_SIZE_NEW)
    done.append("尺寸=%s（旧档 %d／新系数 %.2f）"
                % ("属性" if r else "**失败**", GLARE_SIZE_OLD, GLARE_SIZE_NEW))
    # 5.x 才有：强度／饱和度／上限——旧版没有这几个概念，设得上就设
    for name, val in (("Strength", GLARE_STRENGTH_NEW), ("Saturation", GLARE_SATURATION_NEW),
                      ("Maximum", GLARE_MAX_NEW)):
        if sock(name, val):
            done.append("%s=%g" % (name, val))
    print("[formation] Glare：%s" % "，".join(done), flush=True)


def build_render_env(final=False):
    scene = bpy.context.scene
    world = bpy.data.worlds.new("W") if not bpy.data.worlds else bpy.data.worlds[0]
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    tex_path = os.path.abspath(BG_TEXTURE)
    if os.path.exists(tex_path):
        # 暗星场：靠已有的星图贴图（assets/credits.yaml 已记出处与许可），压暗使用
        tex = nt.nodes.new("ShaderNodeTexEnvironment")
        tex.image = bpy.data.images.load(tex_path, check_existing=True)
        bg.inputs[0].default_value = (BG, BG, BG, 1.0)
        nt.links.new(tex.outputs["Color"], bg.inputs[0])
        bg.inputs[1].default_value = BG_STRENGTH
    else:
        bg.inputs[0].default_value = (BG, BG, BG * 1.1, 1.0)

    # v1.3 §五：单一主光（+X 上方 45° 斜入）＋弱补光；太阳方向记入场景自定义属性
    v = Vector(SUN_DIR).normalized()
    d = bpy.data.lights.new("LGT_Sun", "SUN")
    d.energy = SUN_ENERGY
    d.angle = math.radians(3)
    sun = bpy.data.objects.new("LGT_Sun", d)
    sun.rotation_euler = (-v).to_track_quat("-Z", "Y").to_euler()
    bpy.context.collection.objects.link(sun)
    sun["role"] = "key"                     # E16：主光标识
    sun["sun_dir"] = list(SUN_DIR)
    f = bpy.data.lights.new("LGT_Fill", "SUN")
    f.energy = FILL_ENERGY
    f.angle = math.radians(35)
    fill = bpy.data.objects.new("LGT_Fill", f)
    fill.rotation_euler = (math.radians(70), 0.0, math.radians(-120))
    bpy.context.collection.objects.link(fill)
    fill["role"] = "fill"
    layout = bpy.data.objects.get("EMPTY_LAYOUT")
    if layout:
        layout["sun_dir"] = list(SUN_DIR)
        layout["starfield_strength"] = BG_STRENGTH
        layout["starfield_texture"] = os.path.relpath(tex_path, HERE) if os.path.exists(tex_path) \
            else "（缺失，退回暗色背景）"

    # 色彩管理 AgX / Medium High Contrast
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception as exc:
        print("[formation] 色彩管理设置失败：%s" % exc, flush=True)

    # Compositor：Fog Glow 只让过阈的（光束、筒口）发辉。
    # 版本差异：≤4.x 合成器在 scene.node_tree、Glare 参数是**节点属性**、输出用 CompositorNodeComposite；
    # 5.x 改成**节点组**（scene.compositing_node_group），Glare 参数移到**输入插槽**，输出用 NodeGroupOutput。
    scene.use_nodes = True
    cnt = getattr(scene, "compositing_node_group", None) or getattr(scene, "node_tree", None)
    if cnt is None:
        cnt = bpy.data.node_groups.new("FORMATION_COMP", "CompositorNodeTree")
        scene.compositing_node_group = cnt
    for n in list(cnt.nodes):
        cnt.nodes.remove(n)
    rl = cnt.nodes.new("CompositorNodeRLayers")
    gl = cnt.nodes.new("CompositorNodeGlare")
    _set_glare(gl)
    try:
        out = cnt.nodes.new("NodeGroupOutput")
        if not any(s.name == "Image" for s in out.inputs):
            cnt.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        cnt.links.new(rl.outputs["Image"], gl.inputs["Image"])
        cnt.links.new(gl.outputs["Image"], out.inputs["Image"])
    except Exception:
        out = cnt.nodes.new("CompositorNodeComposite")
        cnt.links.new(rl.outputs["Image"], gl.inputs["Image"])
        cnt.links.new(gl.outputs["Image"], out.inputs["Image"])

    scene.render.engine = "CYCLES"
    res = RES_FINAL if final else RES
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.cycles.samples = SAMPLES_FINAL if final else SAMPLES
    scene.cycles.use_denoising = True
    scene.render.image_settings.file_format = "PNG"
    if final:
        scene.render.image_settings.color_depth = "16"
        scene.render.image_settings.file_format = "OPEN_EXR"
    try:
        scene.cycles.device = "GPU"
    except Exception:
        pass


def add_camera():
    cam_data = bpy.data.cameras.new("RIG_Cam")
    cam_data.lens = 40.0
    cam = bpy.data.objects.new("RIG_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam


def point(cam, location, target, ortho=None, lens=40.0):
    cam.location = Vector(location)
    d = Vector(target) - Vector(location)
    if abs(d.normalized().dot(Vector((0, 0, 1)))) > 0.9995:
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    if ortho:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = "PERSP"
        cam.data.lens = lens


def render_views(final=False):
    scene = bpy.context.scene
    os.makedirs(OUT_DIR, exist_ok=True)
    cam = add_camera()

    layout = bpy.data.objects["EMPTY_LAYOUT"]
    colA_bus = bpy.data.objects["colA_bus"]
    half = BASELINE_DISPLAY / 2
    zc = vcenter(colA_bus)[2]
    wide = BASELINE_DISPLAY + 3.0
    views = (
        # front：斜视（与 F4 同观感——纯沿 ±X 看会把集光器翼看成一条线，F4 是斜视才同时
        # 看得到"三器成排"与"翼展"）；side/top：沿 ±Y 与俯视
        ("front", dict(location=(wide * 1.55, -wide * 0.55, zc + 2.6),
                       target=(0, 0, zc + 0.9), ortho=None, lens=40.0)),
        ("side", dict(location=(0, wide * 2.2, zc), target=(0, 0, zc), ortho=wide * 0.5)),
        ("top", dict(location=(0, 0, wide * 2.2), target=(0, 0, zc), ortho=wide)),
        # iso：colA 与光束的近景（对 F3 分布式_集光器特写）
        ("iso", dict(location=Vector((4.2, -half - 4.0, 2.6)), target=(0.0, -half, zc + 0.35),
                     ortho=None, lens=50.0)),
        # wide：编队全景（对 F6）
        ("wide", dict(location=Vector((wide * 1.1, -wide * 0.95, wide * 0.45)),
                      target=(0, 0, zc), ortho=None, lens=35.0)),
    )
    for name, kw in views:
        point(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)

    if final:      # 正式档另留一张 EXR 底片（规格 §五）
        scene.render.image_settings.file_format = "OPEN_EXR"
        point(cam, **dict(views[4][1]))
        scene.render.filepath = os.path.join(OUT_DIR, "wide.exr")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)


def build():
    """生成分布式编队场景。返回对象字典。"""
    C.purge_scene()
    C.refresh()
    layout = bpy.data.objects.new("EMPTY_LAYOUT", None)
    bpy.context.collection.objects.link(layout)
    layout["baseline_display_m"] = BASELINE_DISPLAY
    layout["baseline_real_m"] = "%g–%g" % BASELINE_REAL_M
    layout["beam_d_star_m"] = D_STAR
    layout["beam_d_link_m"] = D_LINK
    layout["beam_ratio"] = D_STAR / D_LINK
    layout["wing_state"] = "集光器沿用单体 ±X 构型（T1 默认，未改）"
    layout["beam_material"] = "MAT_beam_star 粉 %s / MAT_beam_link 红 %s" % (COL_STAR, COL_LINK)

    craft = build_spacecraft()
    for tag, rec in craft.items():
        holder = rec["holder"]
        holder.parent = layout
        holder["tilt_deg"] = 0.0          # E4：倾角记录
        holder["posture"] = "单体默认：镜筒 +Z" + ("，窗口朝 +Y" if tag == "colA"
                                                else "，窗口朝 −Y" if tag == "colB" else "")
        delta = [max(rec["outline_after"][1][i] - rec["outline_before"][1][i],
                     rec["outline_before"][0][i] - rec["outline_after"][0][i]) for i in range(3)]
        holder["envelope_gain_m"] = [round(v, 4) for v in delta]   # E14
        holder["added_parts"] = rec["added"]
    C.refresh()

    mat_star = emit_material("MAT_beam_star", COL_STAR, STAR_STRENGTH)
    mat_link = emit_material("MAT_beam_link", COL_LINK, LINK_STRENGTH)

    ports = build_ports(craft["cmb"]["objs"])
    beams = build_star_beams(craft, mat_star) + build_link_beams(craft, ports, mat_link)
    for ob in list(ports.values()) + beams:
        ob.parent = layout                # 三器与四束均挂布局父级（E9）
    C.refresh()

    objs = {o.name: o for o in bpy.context.scene.objects}
    report(objs, beams)
    return objs


def report(objs, beams):
    lo, hi = vbounds([o for o in objs.values() if o.type == "MESH"])
    cA, cB = vcenter(objs["colA_bus"]), vcenter(objs["colB_bus"])
    print("[formation] 编队：显示基线 %.1f m（真实 %g–%g m，不成比例）"
          % (BASELINE_DISPLAY, *BASELINE_REAL_M), flush=True)
    print("[formation] 实测基线 |y_colA−y_colB| = %.3f m；合束器 y = %+.3f"
          % (abs(cA[1] - cB[1]), vcenter(objs["cmb_bus"])[1]), flush=True)
    print("[formation] 光束：星光 Φ%.2f（粉）/ 器间 Φ%.2f（红），束径比 %.1f:1"
          % (D_STAR, D_LINK, D_STAR / D_LINK), flush=True)
    for b in beams:
        bl, bh = vbounds([b])
        length = max(bh[i] - bl[i] for i in range(3))     # 束沿不同轴，取其最大跨度
        print("[formation]   %-18s 长 %6.3f m  bbox %.3f×%.3f×%.3f"
              % (b.name, length, bh[0] - bl[0], bh[1] - bl[1], bh[2] - bl[2]), flush=True)
    for tag in ("colA", "colB", "cmb"):
        up = [n for n in objs if n.startswith(tag + "_")
              and any(k in n for k in ("_RCS_", "_ANT_", "_HINGE_", "_EDGE_",
                                       "_BAFFLE_", "_TURNTABLE_", "_ROD_"))]
        print("[formation] %s 升级件 %d 件" % (tag, len(up)), flush=True)
    print("[formation] 包围盒 %.2f × %.2f × %.2f m；对象 %d"
          % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], len(objs)), flush=True)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    final = "--final" in argv
    build()
    build_render_env(final=final)
    render_views(final=final)
    print("[formation] 完成：%d 个对象（%s）" % (len(bpy.context.scene.objects),
                                              "正式" if final else "预览"), flush=True)
