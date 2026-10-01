# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——集光器（Collector）单体建模。

用法：
    blender --background --python collector.py     # 建模 + 四视角自检渲染到 out/

坐标与形态约定
--------------
1 Blender 单位 = 1 m；+Z 为舱顶（镜筒指向 +Z，星光入射方向）；舱体中心为原点。
编队连线为 **Y** 轴：+Y 指向合束器（相邻器），−Y 指向编队外侧。
因此：
  * 平台舱**平面是梯形**，平行边沿 X（垂直于编队连线）：长边 1.20 m 在 +Y（相邻面），
    短边 0.78 m 在 −Y（组合体状态下朝外）。顶面完整、水平；侧面轮廓仍为规整矩形
    （各层宽度取并集即满宽）。
  * ±X 两侧面因收窄而是**斜面**，折叠展开式太阳翼就装在这两个斜面上。
  * **推进剂储箱** 2 只（白色竖向胶囊状）装在长边所在侧面（+Y）沿 X 并列。
    （这对件曾长期被误标为"散热器"；已按用户更正与 kimi work 修订后的规格改名为 tank_*。）
  * 镜筒根部的侧向出光窗口朝 **+Y**（朝合束器）。

对外接口（combiner.py 依赖，勿改签名）
------------------------------------
build()、build_bus()、build_tanks()、build_panels()（返回 (对象列表, X 中心字典)）、
new_material()、purge_scene()、refresh()、setup_render_rig()、point_camera()。
"""
import math
import os

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

# ============================================================ 参数区（改这里即可）
# ---- 平台舱：顶面完整水平的梯形平面箱体 ----
BUS_W = 0.91          # 顶面长边（沿 X＝深，⊥基线）——T8：长边 0.91 朝 +Y
BUS_SHORT_W = 0.59    # 顶面短边（沿 X）≈0.65×长边，在 −Y（组合体状态下朝外）
BUS_D = 0.65          # Y 向宽（编队连线方向）——T8：0.65
BUS_H = 0.91          # Z 向高（沿光轴）——T8：维持 0.91
BUS_BEVEL = 0.018     # 小倒角软化 MLI 边缘，不牺牲顶面平整度

# ---- 镜筒：黑色哑光圆筒，顶部开口带遮光罩沿，根部嵌入舱顶 ----
TUBE_D = 0.580        # 筒身外径（A2 区间 0.55–0.65；含口沿后 0.63）
TUBE_H = 0.850        # 净高（A4：净高出舱顶 0.83 m，0.83/0.90 = 0.92）
TUBE_WALL = 0.030     # 壁厚
TUBE_EMBED = 0.020    # 根部嵌入舱顶深度（A11 允许嵌入 ≤0.3 m）
TUBE_X = 0.00         # 镜筒取消偏心（T8 后舱深 0.91↔0.59，Φ0.63 偏心会探出 −X 斜面）
TUBE_Y = 0.0
HOOD_H = 0.060        # 筒口遮光罩沿高
HOOD_OVER = 0.025     # 遮光罩沿外凸量

# ---- 灰色机构簇：镜筒根部 3 台斜置两轴机构 ----
# 方位角取 0/120/240：三台的底座外角都落在梯形顶面内（取 35/145/255 时，145° 那台
# 会探出 −X 斜面约 68 mm）。
GIMBAL_N = 3
GIMBAL_RING_R = 0.36   # 贴筒根：T8 后舱顶余量仅 0.06–0.38 m 环带，底座内缘与筒面相接
GIMBAL_AZIMUTH = (0.0, 120.0, 240.0)
GIMBAL_BASE = (0.090, 0.090, 0.050)
GIMBAL_CYL_R = 0.030
GIMBAL_CYL_L = 0.115
GIMBAL_TILT = 32.0    # 斜置角（度）；单台总高 ≈0.21 m ≈ 筒径的 1/3

# ---- 推进剂储箱 ----
TANK_R = 0.085
TANK_LEN = 0.630      # ≈0.7×舱高
TANK_X = 0.240        # 两只沿 X 并列
TANK_OUT = 0.020      # 凸出舱面
TANK_Z = 0.000        # 贴舱侧面中部

# ---- 太阳翼：每侧两块竖向叠放（板面法线 ±Y），展向 ±X ----
# 此解同时满足 §二.5（单板 1.5×舱宽、弦向 0.6×舱宽）、§五.4（总宽 3.5–4×舱宽）
# 与判据 A5（3.0–4.5）——三者在竖向叠放下本不冲突。
PANEL_L = 1.800       # 展向（X）
PANEL_W = 0.720       # 弦向（Z）= 0.6×舱宽
PANEL_T = 0.030       # 厚度（Y）
PANEL_GAP = 0.020     # 两块之间的竖向缝
PANEL_Z = 0.000       # 安装点：舱体侧面中部
PANEL_FLUSH = 0.015   # 内缘与斜面的贴合量

# ---- 出光窗口：朝 +Y（朝合束器）----
WINDOW_D = 0.130
WINDOW_L = 0.120
WINDOW_Z = 0.105      # 相对舱顶的高度

# ---- 材质色（verify A6 逐项判据）----
COL_BUS = (0.585, 0.361, 0.022)          # 深 MLI 金 ≈#C9A227（R>G>B）
COL_TUBE = (0.016, 0.016, 0.019)         # 哑光近黑（三通道 <0.1）
COL_GIMBAL = (0.340, 0.350, 0.365)       # 中灰
COL_TANK = (0.880, 0.880, 0.860)         # 乳白（三通道 >0.8）
COL_PANEL = (0.020, 0.030, 0.105)        # 深蓝近黑（B>R）
COL_PANEL_LINE = (0.560, 0.585, 0.620)   # 电池片分隔线（细灰白线）
COL_WINDOW = (0.020, 0.020, 0.022)

# ---- 自检渲染 ----
RES = (1000, 750)
SAMPLES = 32
BG = 0.012

REQUIRED = ("bus", "tube", "gimbal_01", "gimbal_02", "gimbal_03",
            "tank_01", "tank_02", "panel_X1", "panel_X2",
            "panel_-X1", "panel_-X2", "window_out")


# ============================================================ 工具
def refresh():
    """读 matrix_world 前必须先刷新：新对象设完 location 后矩阵仍是旧值。"""
    bpy.context.view_layer.update()


def purge_scene():
    """清空场景，保证 collector 与 verify 各自调用 build() 都从零开始。"""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras,
                 bpy.data.lights, bpy.data.images):
        for item in list(coll):
            if item.users == 0:
                coll.remove(item)


def new_material(name, color, rough=0.5, metallic=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metallic
    m.diffuse_color = (*color, 1.0)
    return m


def _set_input(node, names, value):
    """跨版本设输入：socket 名在不同 Blender 版本间偶有差异，逐个试。"""
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return True
    return False


def add_noise_bump(mat, scale=48.0, strength=0.25, detail=6.0):
    """MLI 褶皱感：噪波 → 凹凸法线。不动基色，避免影响 A6 判色。"""
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    tex = nt.nodes.new("ShaderNodeTexNoise")
    tex.inputs["Scale"].default_value = scale
    tex.inputs["Detail"].default_value = detail
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    nt.links.new(tex.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def add_panel_grid(mat, cell=0.115):
    """电池片分隔线：X/Z 两向波带取大值 → 二值化 → 细线色带 → 驱动基色。

    bands_direction 必须对准所取坐标轴，否则该向退化为常数；阈值要贴着波峰，
    否则大片过阈会把整面板糊成白（第一版就栽在这）。基色 default_value 保留深蓝，
    供 verify 读色（A6）——它读的是 Principled 的基色默认值，与是否接线无关。
    """
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    waves = []
    for direction in ("X", "Z"):
        w = nt.nodes.new("ShaderNodeTexWave")
        w.wave_type = "BANDS"
        w.bands_direction = direction
        _set_input(w, ("Scale",), 1.0 / max(cell, 1e-6))
        _set_input(w, ("Distortion",), 0.0)
        nt.links.new(coord.outputs["Object"], w.inputs["Vector"])
        waves.append(w)
    mx = nt.nodes.new("ShaderNodeMath")
    mx.operation = "MAXIMUM"
    nt.links.new(waves[0].outputs["Fac"], mx.inputs[0])
    nt.links.new(waves[1].outputs["Fac"], mx.inputs[1])
    thr = nt.nodes.new("ShaderNodeMath")
    thr.operation = "GREATER_THAN"
    thr.inputs[1].default_value = 0.990
    nt.links.new(mx.outputs["Value"], thr.inputs[0])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (*COL_PANEL, 1.0)
    ramp.color_ramp.elements[1].position = 0.5
    ramp.color_ramp.elements[1].color = (*COL_PANEL_LINE, 1.0)
    nt.links.new(thr.outputs["Value"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Base Color"].default_value = (*COL_PANEL, 1.0)


def apply_bevel(ob, width, segments=2):
    bpy.context.view_layer.objects.active = ob
    mod = ob.modifiers.new("Bevel", "BEVEL")
    mod.width = width
    mod.segments = segments
    mod.limit_method = "ANGLE"
    bpy.ops.object.modifier_apply(modifier=mod.name)


def join_into(objs, name, material):
    """合并为一对象并**把原点归到几何中心**。

    verify 用 location ± dimensions/2 量端面，只有原点居中时该式才等于真实端面；
    join 之后原点会停在主动对象的原点，必须显式归心。
    """
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = name
    ob.data.materials.clear()
    ob.data.materials.append(material)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    return ob


# ============================================================ 构件
def build_bus():
    """平台舱：顶面完整水平的梯形平面箱体。

    平面：平行边沿 X，长边在 +Y（朝合束器）、短边在 −Y（朝外）；沿 Y 收窄使 ±X 两侧面
    成为斜面。顶面是完整水平梯形，侧视轮廓仍是矩形。
    """
    w, d, h = BUS_W / 2, BUS_D / 2, BUS_H / 2
    sw = BUS_SHORT_W / 2
    verts = [
        (-sw, -d, -h), (sw, -d, -h), (w, d, -h), (-w, d, -h),          # 底（梯形）
        (-sw, -d, h), (sw, -d, h), (w, d, h), (-w, d, h),              # 顶（完整水平梯形）
    ]
    faces = [(3, 2, 1, 0), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new("bus")
    me.from_pydata(verts, [], faces)
    me.validate()
    bus = bpy.data.objects.new("bus", me)
    bpy.context.collection.objects.link(bus)
    bus.data.materials.append(new_material("MAT_BUS_MLI", COL_BUS, 0.62, 0.15))
    add_noise_bump(bus.data.materials[0])
    apply_bevel(bus, BUS_BEVEL, 2)
    return bus


def build_tube():
    """镜筒：筒身（外壳＋内壁）＋筒口遮光罩沿＋筒底环，合并为一对象，根部嵌入舱顶。"""
    z0 = BUS_H / 2 - TUBE_EMBED
    r = TUBE_D / 2
    parts = []
    for rad, nm in ((r, "shell"), (r - TUBE_WALL, "bore")):
        bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=rad, depth=TUBE_H,
                                            end_fill_type="NOTHING",
                                            location=(TUBE_X, TUBE_Y, z0 + TUBE_H / 2))
        bpy.context.active_object.name = "TUBE_%s" % nm
        parts.append(bpy.context.active_object)
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=r + HOOD_OVER, depth=HOOD_H,
                                        end_fill_type="NOTHING",
                                        location=(TUBE_X, TUBE_Y, z0 + TUBE_H - HOOD_H / 2))
    parts.append(bpy.context.active_object)
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=r, depth=0.02,
                                        location=(TUBE_X, TUBE_Y, z0 + 0.01))
    parts.append(bpy.context.active_object)
    return join_into(parts, "tube", new_material("MAT_TUBE_BLACK", COL_TUBE, 0.88, 0.03))


def _mechanism(name, px, py, top_z, base_size, cyl_r, cyl_l, tilt_deg, azimuth_deg, material):
    """单台"底座方块＋斜置圆柱"机构；底座坐在 top_z 上并略微嵌入。"""
    bpy.ops.mesh.primitive_cube_add(
        size=1, location=(px, py, top_z + base_size[2] / 2 - 0.002))
    base = bpy.context.active_object
    base.dimensions = base_size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    a = math.radians(azimuth_deg)
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=16, radius=cyl_r, depth=cyl_l,
        location=(px + 0.022 * math.cos(a), py + 0.022 * math.sin(a),
                  top_z + base_size[2] + cyl_l * 0.40))
    cyl = bpy.context.active_object
    # 绕 Y 轴按方位倾倒，使各台朝外斜置
    cyl.rotation_euler = (math.radians(tilt_deg) * -math.sin(a),
                          math.radians(tilt_deg) * math.cos(a), a)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    return join_into([base, cyl], name, material)


def build_gimbals():
    """灰色机构簇 3 台，绕镜筒根部坐在舱顶面上。"""
    mat = new_material("MAT_GIMBAL_GREY", COL_GIMBAL, 0.45, 0.35)
    top_z = BUS_H / 2
    out = []
    for i in range(GIMBAL_N):
        a = GIMBAL_AZIMUTH[i % len(GIMBAL_AZIMUTH)]
        rad = math.radians(a)
        out.append(_mechanism(
            "gimbal_%02d" % (i + 1),
            TUBE_X + GIMBAL_RING_R * math.cos(rad),
            TUBE_Y + GIMBAL_RING_R * math.sin(rad),
            top_z, GIMBAL_BASE, GIMBAL_CYL_R, GIMBAL_CYL_L, GIMBAL_TILT, a, mat))
    return out


def build_tanks(face="Y+"):
    """推进剂储箱 2 只（原误标"散热器"，已按用户更正改名）。

    * ``face="Y+"``（默认；单体/分布式构型）：两只沿 X 并列，贴 **+Y 长边侧面**
      （朝相邻器一侧），微凸出舱面。
    * ``face="X±"``（组合体中的合束器）：一只贴 +X、一只贴 −X，舱侧面外露（D9）。

    胶囊形制沿用参考图；贴哪一面由参数决定，压扁方向随面法线。
    """
    mat = new_material("MAT_TANK_WHITE", COL_TANK, 0.72, 0.02)
    if face == "X±":
        slots = [(BUS_W / 2 + TANK_OUT, 0.0), (-(BUS_W / 2 + TANK_OUT), 0.0)]
        squash = (0.62, 1.0, 1.0)
    else:
        slots = [(x, BUS_D / 2 + TANK_OUT) for x in (-TANK_X, TANK_X)]
        squash = (1.0, 0.62, 1.0)
    out = []
    for i, (px, py) in enumerate(slots, start=1):
        parts = []
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=TANK_R,
                                            depth=TANK_LEN - 2 * TANK_R,
                                            location=(px, py, TANK_Z))
        parts.append(bpy.context.active_object)
        for dz in (-(TANK_LEN / 2 - TANK_R), (TANK_LEN / 2 - TANK_R)):
            bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=12, radius=TANK_R,
                                                 location=(px, py, TANK_Z + dz))
            parts.append(bpy.context.active_object)
        body = join_into(parts, "tank_%02d" % i, mat)
        body.scale = squash                     # 压扁成"扁胶囊"
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        out.append(body)
    return out


def build_panels(stowed=False):
    """太阳翼四板。命名不变（panel_X1/X2/-X1/-X2），两种状态：

    * ``stowed=False``（默认，分布式状态）：每侧两块竖向叠放，展向 ±X、板面法线 ±Y，
      翼根落在 ±X 斜面上（斜面在 y=0 处的半宽内推 PANEL_FLUSH）。
    * ``stowed=True``（组合体/发射状态，规格："收为左右两翼，每侧两块板"）：
      每侧两块**子板叠合平贴**在 ±X 斜面上——子板 0.9(Z)×0.72(Y)、法线 ±X。
      1.8 m 整板无法平贴 1.2 m 的舱面，故按子板收拢；这样收拢后**不外扩**，
      发射包络才守得住（展开态翼展 4.56 m 远超 Φ3650）。
    """
    mat = new_material("MAT_PANEL_BLUE", COL_PANEL, 0.42, 0.10)
    add_panel_grid(mat)
    face_half = (BUS_W + BUS_SHORT_W) / 4
    out, centers = [], {}
    for sign in (+1, -1):
        for idx in (1, 2):
            nm = "panel_%s%d" % ("X" if sign > 0 else "-X", idx)
            if not stowed:
                cx = sign * (face_half - PANEL_FLUSH + PANEL_L / 2)
                cz = PANEL_Z + (idx - 1.5) * (PANEL_W + PANEL_GAP)   # idx1 在下、idx2 在上
                dims = (PANEL_L, PANEL_T, PANEL_W)                   # 展向X / 厚Y / 弦向Z
            else:
                # 子板平贴斜面：X 为厚度、Y 为弦向、Z 为子板长；两块沿 X 叠出
                cx = sign * (face_half + PANEL_T * (0.5 + 1.15 * (idx - 1)))
                cz = PANEL_Z
                dims = (PANEL_T, PANEL_W, PANEL_L / 2)
            bpy.ops.mesh.primitive_cube_add(size=1, location=(cx, 0.0, cz))
            p = bpy.context.active_object
            p.name = nm
            p.dimensions = dims
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            p.data.materials.append(mat)
            centers[nm] = cx
            out.append(p)
    return out, centers


def build_window():
    """出光窗口：镜筒根部侧向折转镜舱，朝 +Y（朝合束器）。"""
    y = BUS_D / 2 - WINDOW_L / 2 + 0.012
    bpy.ops.mesh.primitive_cube_add(size=1, location=(TUBE_X, y, BUS_H / 2 + WINDOW_Z))
    ob = bpy.context.active_object
    ob.name = "window_out"
    ob.dimensions = (WINDOW_D, WINDOW_L, WINDOW_D)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    apply_bevel(ob, 0.012, 2)
    ob.data.materials.append(new_material("MAT_WINDOW_BLACK", COL_WINDOW, 0.35, 0.2))
    return ob


def build(purge=True):
    """生成整器。返回对象字典，供 verify.py 与后续阶段（合束器/组合体/编队）复用。

    ``purge=False`` 时**不清场景**——供上层场景（formation.py）在同一次运行里连续
    摆放多台器，从而**逐字复用**本函数而不必复制单体代码。
    """
    if purge:
        purge_scene()
    objs = {}
    objs["bus"] = build_bus()
    objs["tube"] = build_tube()
    for g in build_gimbals():
        objs[g.name.split(".")[0]] = g      # 按基名记账：多次调用时 Blender 会加 .001
    for t in build_tanks():
        objs[t.name.split(".")[0]] = t
    panels, centers = build_panels()
    for p in panels:
        objs[p.name.split(".")[0]] = p
    objs["window_out"] = build_window()
    report(objs, centers)
    return objs


def report(objs, centers):
    bus, tube = objs["bus"], objs["tube"]
    bus_w = max(bus.dimensions[0], bus.dimensions[1])
    bus_h = bus.dimensions[2]
    tube_d = max(tube.dimensions[0], tube.dimensions[1])
    bus_top = bus.location.z + bus_h / 2
    over = (tube.location.z + tube.dimensions[2] / 2) - bus_top
    gap = (tube.location.z - tube.dimensions[2] / 2) - bus_top
    xs = [centers[n] for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")]
    ws = [objs[n].dimensions[0] for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")]
    span = max(x + w / 2 for x, w in zip(xs, ws)) - min(x - w / 2 for x, w in zip(xs, ws))
    zs = [objs[n].location.z for n in ("panel_X1", "panel_X2")]
    hz = [objs[n].dimensions[2] for n in ("panel_X1", "panel_X2")]
    wing_h = max(z + h / 2 for z, h in zip(zs, hz)) - min(z - h / 2 for z, h in zip(zs, hz))
    tanks = [objs["tank_01"], objs["tank_02"]]

    print("[collector] 舱 长边%.2f/短边%.2f × 深%.2f × 高%.2f  镜筒 Φ%.3f 高%.3f"
          % (BUS_W, BUS_SHORT_W, bus.dimensions[1], bus_h, tube_d, tube.dimensions[2]), flush=True)
    print("[collector] 平面梯形：短边/长边 = %.3f（目估 0.65）" % (BUS_SHORT_W / BUS_W), flush=True)
    print("[collector] A3 筒径/Y 向舱宽 = %.3f (需 0.85–1.0)" % (tube_d / BUS_D), flush=True)
    print("[collector] A4 高出舱顶/舱高 = %.3f (需 0.7–1.1)" % (over / bus_h), flush=True)
    print("[collector] A5 翼展 = %.3f m (需 3.6–5.4，绝对值口径)" % span, flush=True)
    print("[collector] A11 镜筒底面与舱顶间隙 = %+.3f m (需 -0.30–0.05，负值＝嵌入)" % gap, flush=True)
    print("[collector] 单翼竖向总高 = %.3f m（舱高 %.2f）；板弦向 %.3f = %.2f×舱宽"
          % (wing_h, bus_h, objs["panel_X1"].dimensions[2],
             objs["panel_X1"].dimensions[2] / bus_w), flush=True)
    print("[collector] 储箱 2 只贴 +Y 面（朝合束器）y=%.3f；出光窗口 y=%.3f（朝 +Y）"
          % (tanks[0].location.y, objs["window_out"].location.y), flush=True)


# ============================================================ 自检渲染
def setup_render_rig():
    """四视角自检的灯组与相机。返回相机对象。"""
    scene = bpy.context.scene
    world = bpy.data.worlds.new("RIG_World") if not bpy.data.worlds else bpy.data.worlds[0]
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (BG, BG, BG * 1.1, 1.0)

    # 太阳朝 (θx, 0, θz) 的传播方向 = (−sinθz·sinθx, cosθz·sinθx, −cosθx)。
    # 主光与补光必须分居 −Y 两侧，否则 side 视角（看 −Y…此处看 +Y 面）会出现无光可补的死黑。
    for name, energy, rot in (("RIG_Key", 3.0, (58, 0, 38)),
                              ("RIG_Fill", 1.6, (70, 0, -115)),
                              ("RIG_Fill2", 2.2, (60, 0, -40)),
                              ("RIG_Rim", 2.0, (108, 0, 200))):
        d = bpy.data.lights.new(name, "SUN")
        d.energy = energy
        d.angle = math.radians(20 if name == "RIG_Fill2" else 6)
        ob = bpy.data.objects.new(name, d)
        ob.rotation_euler = tuple(math.radians(v) for v in rot)
        bpy.context.collection.objects.link(ob)

    cam_data = bpy.data.cameras.new("RIG_Cam")
    cam = bpy.data.objects.new("RIG_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam
    return cam


def point_camera(cam, location, target, ortho=None, lens=50.0):
    """把相机摆到 location 并对准 target；ortho 给定则用正交，否则透视。"""
    cam.location = Vector(location)
    d = Vector(target) - Vector(location)
    if abs(d.normalized().dot(Vector((0, 0, 1)))) > 0.9995:
        cam.rotation_euler = (0.0, 0.0, 0.0)      # 正俯视：track_quat 的 up 提示退化
    else:
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    if ortho:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = "PERSP"
        cam.data.lens = lens


def render_views():
    scene = bpy.context.scene
    cam = setup_render_rig()
    scene.render.resolution_x, scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = True
    try:
        scene.cycles.device = "GPU"
    except Exception:
        pass
    os.makedirs(OUT_DIR, exist_ok=True)

    bus, tube = bpy.data.objects["bus"], bpy.data.objects["tube"]
    top_z = tube.location.z + tube.dimensions[2] / 2
    mid_z = (bus.location.z - bus.dimensions[2] / 2 + top_z) / 2
    views = (
        # side 从 +Y 看：+Y 才是长边、储箱、出光窗口所在面，与参考图同侧
        ("side", dict(location=(0, 8, mid_z), target=(0, 0, mid_z), ortho=5.2)),
        ("front", dict(location=(-8, 0, mid_z), target=(0, 0, mid_z), ortho=2.4)),
        ("iso", dict(location=(5.0, -6.4, 3.4), target=(0, 0, mid_z * 0.95), lens=52.0)),
        # top 正俯视正交：顶视应为**梯形**（长边在 +Y），故不做任何倾斜
        ("top", dict(location=(TUBE_X, TUBE_Y, 6), target=(TUBE_X, TUBE_Y, BUS_H / 2), ortho=1.9)),
    )
    for name, kw in views:
        point_camera(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[collector] 出图 %s" % scene.render.filepath, flush=True)


if __name__ == "__main__":
    build()
    render_views()
    print("[collector] 完成：%d 个对象" % len(bpy.context.scene.objects), flush=True)
