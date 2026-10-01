# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——阶段四：分布式编队场景（观感 v2）。

用法：
    blender --background --python formation.py            # 预览（1200×900 @64 samples）
    blender --background --python formation.py -- --final  # 正式（1920×1080 @512 + EXR，需人工许可）

依《阶段四_建模规格》v1.5（2026-09-30）：
    * 三器沿基线（Y）一字排开，合束器居中，集光器分居两侧；显示基线 B = 12 m
      （真实 20–100 m 不成比例，记录于 E12）；三器舱心 x/z 对齐。
    * 集光器 A 在 y=−B/2、窗口朝 +Y；B 在 y=+B/2、窗口朝 −Y（A 保持单体姿态，B 绕 Z 转 180°）。
    * **合束器＝阶段二单体整机复用**（§二.3 v1.7；v1.5 的 T3"bay 本体"经《验收反馈_04》作废）：
      `combiner.build()` 整机——金色舱体 1.20×1.20×0.90＋载荷舱 Ø0.48×0.70 立于舱顶＋顶部机构簇
      ＋±X 展开翼＋储箱×2，与集光器同规格（三器体量同级）。**不引用 assembly.py 的 bay**，
      collector.py／combiner.py／assembly.py 三个文件一行不动。
    * 光束：星光粉粗（Φ0.24）自 +Z 垂直入射筒口、终点落最内光阑环截面（内嵌 ≤0.02 m），
      **长度按五张交付视角的视场反算**，任何视角都不在空中露出截止端面（§二.5）；
      器间束红细（Φ0.06）出光窗口→载荷舱收光口，两端各内嵌 0.02 m。
    * 推断默认件（v1.3 起维持）：RCS×4/器、测控天线×1/器、翼根铰链＋翼缘描边、筒口光阑环、
      机构簇转台＋细杆——全部为新增件，按单体**构建局部系**定位，不动已验收几何。
    * **材质物理化（§四 v1.7）**：金色 MLI＝导体性金膜（metallic=1、F0≈(1.00,0.78,0.35)、
      roughness 0.30–0.45）＋**厘米级双尺度分形褶皱**（特征尺度 0.01–0.05 m）＋细针脚低对比缝线；
      储箱 AZ-93 白漆、机构件阳极氧化铝/钛、翼板深蓝紫电介质＋盖玻璃 coat，**参数表写死在脚本里**
      （`MATERIAL_TABLE`），逐行落地并记入 report 供 E15/E19 回读。禁 Wave/Grid 与 Voronoi
      距离边（大尺度分块）充当可见表面分割。
    * **去周期化与随机性**（§二.8）：帆板微弯（crown 0.005–0.02 m，逐板随机）＋舱体大平面微起伏
      （≤5 mm）；MLI 缝线间隔＝基准 ×U[0.8,1.4]；板格线逐板抖动；一切随机量由**固定种子**派生
      （`_rng(key)`）且记入 formation_report.json，可复现。
    * 渲染管线（§五 v1.5）：主光＝星光源（+Z 主导，fill ≤0.2）；iso 加 f/2.8 景深；
      wide 按三分法重裁（编队占画面宽 ≥60%）；AgX＋Medium High Contrast（设后回读）。

端点一律由**实测包围盒**反算（最内光阑环、出光窗口面、收光口面），不写目测偏移。
"""
import json
import math
import os
import random
import sys

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Euler, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import assembly as A          # 只为平台舱（bay）的构造配方与 BAY_* 常量（两状态同一物体）
import collector as C
import combiner as M
import wing as W

OUT_DIR = os.path.join(HERE, "out", "formation")

# ============================================================ 参数区
BASELINE_DISPLAY = 12.0        # 显示基线（m）
BASELINE_REAL_M = (20.0, 100.0)  # 真实基线区间（仅记录，E12）

D_LINK = 0.06                  # 器间束直径（红细）
D_STAR = 0.24                  # 星光束直径（粉粗）——与 D_LINK 之比 4:1
PORT_EXTRA = 0.04              # 收光口径外扩量（T2：口径 = 红束径 + 0.04）
PORT_LEN = 0.06                # 收光口罩筒长度
EMBED = 0.02                   # 光束端点内嵌深度（判据 ≤0.02 m）
STAR_ABOVE_MIN = 2.5           # 星光束上端**下限**（真正长度由视场反算，见 solve_star_top_z）
STAR_BOTTOM_EMBED = 0.015      # 星光束下端没入最内光阑环（判据 ≤0.02 m）
STAR_TOP_MARGIN = 0.6          # 反算出的出画 z 再留的余量
STAR_SCAN_STEP = 0.25          # 反算扫描步长
STAR_TOP_MAX = 120.0           # 反算上限（防呆）

# 光束颜色：规格 §四 给的**基色**保留在材质 socket 上（E8 判色读它）；发光色按 F3/F5 渲染图
# 实测束色反解——基色直读当线性值会渲染成 #FFC4CC 淡粉（本项目约定是"线性值＋注释 sRGB"，
# 见 COL_BUS 0.585→#C9A227），AGX 下核心 R/G 只有 1.06≈白芯粉边，与"核心保留粉色"冲突。
COL_STAR_BASE = (1.0, 0.56, 0.63)    # 规格 §四 基色
COL_STAR_EMIT = (1.0, 0.18, 0.18)    # 实测反解发光色（配 1.0 强度 → 核心 R/G=1.66，基准 1.66）
COL_LINK_BASE = (1.0, 0.165, 0.10)
COL_LINK_EMIT = (1.0, 0.05, 0.04)    # 规格 §四 表值（实测核心 R/G 2.46–2.88，基准 2.44）
STAR_STRENGTH = 1.0              # 亮芯：实测核心 R/G=1.66（基准 F5 1.66）
LINK_STRENGTH = 3.0
CORE_FRAC = 1.0 / 3.0            # 芯径＝壳径/3（§四 v1.8／T4）
SHELL_ALPHA_STAR, SHELL_ALPHA_LINK = 0.30, 0.35   # 外壳半透明（晕）
SHELL_STR_STAR, SHELL_STR_LINK = 0.60, 1.50       # 外壳发光强度（renewable 模式 ×0.3）
END_LEN = 0.12                   # 两端点增亮段长度（判据 ≤0.15 m）
END_STR_STAR, END_STR_LINK = 2.20, 4.00
BEAM_MODE = "illustration"       # 全局开关：illustration（默认交付）｜realistic（准写实）
BEAM_MODE_FACTOR = {"illustration": {"core": 1.0, "shell": 1.0},
                    "realistic": {"core": 0.5, "shell": 0.3}}
BEAM_MODE_NOTE = "illustration＝双层束（默认交付）；realistic＝芯径减半、壳强度×0.3、仅端点辉光"
BEAM_DIFFUSE_DAMP = 0.05       # 束体漫反射反照压到 5%（光柱不是反射面，避免核心炸白）

BG_TEXTURE = os.path.join(HERE, "..", "..", "assets", "textures", "8k_stars_milky_way.jpg")
BG_STRENGTH = 0.80             # §四 允许 0.6–0.8，取上限给金属面一点环境反射
GLARE_THRESHOLD = 1.0          # 只让光束与筒口过阈
# Glare 跨版本：旧版（≤4.x）走节点属性、size 是 6–9 的档位；新版（5.x）走输入插槽、
# Type 菜单吃显示名 "Fog Glow"、Size 是 0–1 的系数，另多出强度/饱和度/上限三个插槽。
GLARE_TYPE_NEW = "Fog Glow"
GLARE_QUALITY_NEW = "High"
GLARE_SIZE_OLD, GLARE_SIZE_NEW = 8, 0.55
GLARE_STRENGTH_NEW, GLARE_SATURATION_NEW, GLARE_MAX_NEW = 0.35, 1.10, 2.5

# §五 v1.5：主光＝星光源（+Z 主导），迎光面亮、背光面近黑；fill ≤0.2 仅防死黑
SUN_DIR = (0.85, 0.42, 1.0)      # +Z 主导（z 分量仍为最大）；约 47° 斜入，金属侧面才有光可反
                                # +Y 掠射分量给储箱/出光窗口所在面（side 视角所看）
SUN_ENERGY, FILL_ENERGY = 4.0, 0.20
LOOK_NAME = "AgX - Medium High Contrast"
TOP_CAM_Z = 5.0                # 俯视机位高度：正交俯视只受裁剪影响，压低才能让星光束"出画"
DOF_ISO = {"fstop": 2.8, "focus": "colA_bus"}     # iso 近景景深（§五）

# ---- 推断默认件（§二.7，维持 v1.3 结论）----
RCS_N = 4                      # 每器 RCS 喷管数
RCS_D = 0.060                  # 喷管长
RCS_YF = 0.55                  # 喷管 y 位置＝本体 y 半宽 ×0.55（集光器舱＝0.33，与 v1.3 一致）
RCS_ZF = 0.18                  # 喷管在舱顶以下的相对高度（×舱高）
RCS_EMBED = 0.012              # 根部嵌进舱面
ANT_F = 0.55                   # 天线在舱顶的落位＝本体半宽 ×0.55
ANT_R = (0.090, 0.130)         # 天线口径（集光器小定向 / 合束器高增益小锅）
EDGE_W = 0.020                 # 翼缘描边宽（§四）
BAFFLE_RINGS = 2               # 每根筒口内光阑环数
BAFFLE_DZ = 0.050              # 环间距（自筒口向下）
BAFFLE_MINOR = 0.016           # 环截面半径：外缘压进内壁 6 mm

# ---- 去周期化（§二.8／§四 v1.5）----
SEED = 20280930                # 全部随机量的主种子（E12/E17 记入 report）
PANEL_CELL = 0.115             # 板格基准格距（与 collector.add_panel_grid 一致）
COL_PANEL_V2 = (0.032, 0.036, 0.082)   # 深蓝紫电池色：压暗降饱和（B>R，仍读得出板格）
COL_PANEL_LINE = (0.150, 0.175, 0.230)  # 盖玻璃拼缝细线（压暗的灰蓝）
PANEL_SCALE_RANGE = ((0.92, 1.08),) * 3  # 逐板格距抖动（与逐器 jitter 合成 ≈±10%）
OFFSET_RANGE = ((-0.9, 0.9), (-0.5, 0.5), (-1.3, 1.3))   # per-object 相位偏移（各轴解耦）
SCALE_RANGE = (0.85, 1.25)     # per-object 缩放偏移（缺省三轴同值）
CROWN_CUTS = 10                # 帆板细分段数（立方体 8 角点必须先细分才弯得动）
UND_CUTS = 6                   # 舱体大平面细分段数
UND_AMP_RANGE = (0.003, 0.005)  # 大平面微起伏幅度（§二.8：≤0.005 m）
PERIODIC_TEX = ("TEX_WAVE", "TEX_CHECKER", "TEX_MAGIC", "TEX_GRID")
MLI_BANNED = PERIODIC_TEX + ("TEX_VORONOI",)   # MLI 禁用：周期纹理＋大尺度分块（Voronoi 距离边）
STITCH_BASE, STITCH_JITTER = 0.055, (0.8, 1.4)  # 细针脚基准间距与间隔抖动（§二.8）
STITCH_BAND = 0.10                            # 阈值带宽（线宽＝带宽/噪声梯度，见 _stitch_width）

# ---- 材料参数表（§四 v1.7：真实热控材料参数，**常量写死在脚本里，不许手调**）----
# 落地位置：三器本体的文件一行不动，故本表在**场景内重建**材质（`_rebuild_materials`），
# 逐行结果写入 report（`MATERIAL_LOG` → material_json），供 E15/E19 回读核验。
MATERIAL_TABLE = {
    "金色 MLI": dict(cls="mli", match=("MAT_BUS_MLI", "MAT_up_gold"),
                     metallic=1.0, f0=(1.00, 0.78, 0.35), rough=(0.36, 0.45),   # 区间内偏上：金属高光更铺得开
                     wrinkle=(0.035, 0.012), stretch=(1.00, 0.45, 1.00), stitch=True),
    "乳白储箱": dict(cls="plain", match=("MAT_TANK_WHITE",),
                  metallic=0.0, f0=(0.87, 0.87, 0.85), rough=(0.50, 0.65)),
    "机构件": dict(cls="plain", match=("MAT_GIMBAL_GREY", "MAT_RECV_GREY", "MAT_AUX_GREY",
                                       "MAT_up_alu", "MAT_up_grey"),
                 metallic=1.0, rough=(0.30, 0.50)),
    "翼板": dict(cls="panel", match=("MAT_PANEL_BLUE",),
               metallic=0.0, f0=COL_PANEL_V2, rough=(0.28, 0.36),
               coat=0.18, coat_rough=0.15, cell=PANEL_CELL, cell_jitter=0.10),   # 盖玻璃：轻镜面，不抢板色
    "镜筒/载荷舱/光阑环（单体已固化）": dict(
        cls="frozen", match=("MAT_TUBE_BLACK", "MAT_MODULE_GREY", "MAT_up_baffle",
                             "MAT_port_black", "MAT_WINDOW_BLACK")),
}
MATERIAL_LOG = {}              # 参数表逐行落地台账（E15/E19 回读）

# ---- 构图（§五 v1.5）----
WIDE_FILL = 0.68               # wide 目标：编队占画面宽（判据 ≥0.60）
FRONT_DIR = (1.55, -0.55, 0.069)   # 与 v1.3 同观察方位（+X 上方斜视），只改推拉距离
FRONT_FILL = 0.90                  # front 目标占宽（正视要看得清三器与翼态）
WIDE_DIR = (1.10, -0.95, 0.45)
WIDE_LENS = 35.0
WIDE_VCENTER = 0.42            # 三分法：编队竖向中心落在画面 0.42 高度

RES = (1200, 900)
SAMPLES = 64
RES_FINAL = (1920, 1080)
SAMPLES_FINAL = 512
BG = 0.008

RANDOM_LOG = {}                # {用途: 值}——随机量台账，落进 report（E12/E17）


# ============================================================ 工具
def _rng(key):
    """按主种子与**用途名**派生独立子序列：任何一件的随机量与其名绑定，改别处不串位。"""
    return random.Random("%s|%s" % (SEED, key))


def _rand(key, lo, hi):
    """取一个记账在案的均匀随机量（可复现、可审计）。"""
    v = round(_rng(key).uniform(lo, hi), 4)
    RANDOM_LOG[key] = v
    return v


def _log(key, val):
    RANDOM_LOG[key] = val
    return val


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
    此后 matrix_basis 仍是"局部量"而 vbounds 给的是世界量，两者混用会把升级件摆错位置。
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


def emit_material(name, base, emit, strength, alpha=1.0, rim=False):
    """光束材质：socket 上留**规格基色**（E8 判色），实际发光用**实测反解色**。

    ``alpha`` <1 ⇒ 半透明外壳（光晕）；``rim`` ⇒ Layer Weight 边缘加权（径向衰减，晕外沿更亮）。

    束体是"光柱"不是"反射面"：关高光、漫反射反照压到 5%，否则太阳灯在粉色面上打出白
    高光，radiance＝自发＋反射冲顶被 AgX 去饱和 → 核心炸白（实测 R/G 从 1.7 掉到 1.09）。
    """
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (*base, 1.0)
    b.inputs["Roughness"].default_value = 0.4
    if "Emission Color" in b.inputs:                 # 自发光：判据 E8 要求四束均为 emission
        b.inputs["Emission Color"].default_value = (*emit, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    else:                                            # 旧版接口兜底
        b.inputs["Emission"].default_value = (*emit, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    for spec in ("Specular IOR Level", "Specular"):
        if spec in b.inputs:
            b.inputs[spec].default_value = 0.0
            break
    b.inputs["Metallic"].default_value = 0.0
    if alpha < 1.0 and "Alpha" in b.inputs:
        b.inputs["Alpha"].default_value = alpha
    if rim and "Emission Strength" in b.inputs:
        lw = m.node_tree.nodes.new("ShaderNodeLayerWeight")
        mr = m.node_tree.nodes.new("ShaderNodeMapRange")
        _set_in(mr, "To Min", strength * 0.25)
        _set_in(mr, "To Max", strength * 1.60)
        m.node_tree.links.new(lw.outputs["Facing"], mr.inputs[0])
        m.node_tree.links.new(mr.outputs[0], b.inputs["Emission Strength"])
    damp = m.node_tree.nodes.new("ShaderNodeRGB")
    damp.outputs[0].default_value = (*[c * BEAM_DIFFUSE_DAMP for c in emit], 1.0)
    m.node_tree.links.new(damp.outputs[0], b.inputs["Base Color"])
    m.diffuse_color = (*emit, 1.0)
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


# ============================================================ 去周期化（§二.8／§四）
def _set_in(node, name, val):
    if name in node.inputs:
        node.inputs[name].default_value = val
        return True
    return False


def _per_object(nt, coord_out, scale_ranges=None):
    """per-object 相位/缩放偏移链：Object Info.Random → Mapping(Location, Scale)。

    各器/各面各自取的 Object Info 随机数不同，于是同一材质在不同对象上相位与尺度都错开，
    不会出现"跨面对齐的同一张纹理"（E17）。``scale_ranges`` 可按轴给区间——三轴不同即得到
    **各向异性**的拉伸（薄膜褶皱是拉长的，不是各向同性的疙瘩）。
    """
    info = nt.nodes.new("ShaderNodeObjectInfo")
    mapn = nt.nodes.new("ShaderNodeMapping")
    locv = nt.nodes.new("ShaderNodeCombineXYZ")
    sclv = nt.nodes.new("ShaderNodeCombineXYZ")
    for i, ax in enumerate(("X", "Y", "Z")):
        mr = nt.nodes.new("ShaderNodeMapRange")
        _set_in(mr, "To Min", OFFSET_RANGE[i][0])
        _set_in(mr, "To Max", OFFSET_RANGE[i][1])
        nt.links.new(info.outputs["Random"], mr.inputs[0])
        nt.links.new(mr.outputs[0], locv.inputs[ax])
    for i, ax in enumerate(("X", "Y", "Z")):
        lo, hi = (scale_ranges or (SCALE_RANGE,) * 3)[i]
        ms = nt.nodes.new("ShaderNodeMapRange")
        _set_in(ms, "To Min", lo)
        _set_in(ms, "To Max", hi)
        nt.links.new(info.outputs["Random"], ms.inputs[0])
        nt.links.new(ms.outputs[0], sclv.inputs[ax])
    nt.links.new(coord_out, mapn.inputs["Vector"])
    nt.links.new(locv.outputs[0], mapn.inputs["Location"])
    nt.links.new(sclv.outputs[0], mapn.inputs["Scale"])
    return info, mapn.outputs["Vector"]


def _domain_warp(nt, vec, key, strength=0.14, scale=1.6):
    """域扭曲：用噪声把坐标本身推歪，纹理走向/间距随之不规则（去周期化的关键一步）。"""
    n = nt.nodes.new("ShaderNodeTexNoise")
    _set_in(n, "Scale", scale)
    _set_in(n, "Detail", 3.0)
    nt.links.new(vec, n.inputs["Vector"])
    mul = nt.nodes.new("ShaderNodeVectorMath")
    mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = (strength, strength, strength)
    nt.links.new(n.outputs["Color"], mul.inputs[0])
    add = nt.nodes.new("ShaderNodeVectorMath")
    add.operation = "ADD"
    nt.links.new(vec, add.inputs[0])
    nt.links.new(mul.outputs["Vector"], add.inputs[1])
    return add.outputs["Vector"]


def _mat_bsdf(m):
    return next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def mat_color(m):
    b = _mat_bsdf(m)
    if not b:
        return None
    c = b.inputs["Base Color"].default_value
    return (round(c[0], 4), round(c[1], 4), round(c[2], 4))


def mat_rough(m):
    b = _mat_bsdf(m)
    return round(b.inputs["Roughness"].default_value, 4) if b else None


def mat_metallic(m):
    b = _mat_bsdf(m)
    return round(b.inputs["Metallic"].default_value, 4) if b else None


def _set_bsdf(b, spec, rough=None):
    """把参数表的 metallic／F0／roughness 写进 Principled（roughness 取区间中值，再接线带变化）。"""
    if "f0" in spec:
        b.inputs["Base Color"].default_value = (*spec["f0"], 1.0)
    b.inputs["Metallic"].default_value = spec["metallic"]
    lo, hi = spec["rough"]
    b.inputs["Roughness"].default_value = round((lo + hi) / 2, 3) if rough is None else rough


def _stitch_width(scale, band=STITCH_BAND):
    """细针脚视觉宽度的**解析估计**：宽度 ≈ 阈值带宽 / |∇噪声|，取 |∇|≈2π·scale·0.25。

    （分形噪声幅度 ~U(0,1)，特征梯度按 2π·scale×0.25 估。）这是可复现的估计式、不是逐像素
    测量，方法写在交付说明里；E15 由 report 台账核验 ≤0.01 m。
    """
    return band / (2 * math.pi * max(scale, 1e-6) * 0.25)


def _aniso_scale(stretch):
    """把各向异性拉伸折算成 per-object 缩放区间：轴向 i 的缩放 = stretch[i] × U(0.85,1.25)。"""
    return tuple((s * SCALE_RANGE[0], s * SCALE_RANGE[1]) for s in stretch)


def _apply_mli(m, spec, key):
    """金色 MLI：导体性金膜（metallic=1＋金色 F0＋roughness 带变化）＋厘米级双尺度分形褶皱
    ＋细针脚低对比缝线；per-object 相位/缩放偏移维持。

    介观红线（§二.8 v1.7）：褶皱特征尺度 0.01–0.05 m；针脚视觉宽度 ≤0.01 m、bump 贡献
    ≤褶皱幅度的 1/3；禁用 Wave/Grid 与 Voronoi 距离边充当可见表面分割。
    """
    r = _rng("mli|" + key)
    feats = [round(f, 4) for f in spec["wrinkle"]]
    jitter = round(r.uniform(*STITCH_JITTER), 3)
    stitch_scale = 1.0 / (STITCH_BASE * jitter)
    bump_w = round(r.uniform(0.22, 0.30), 3)
    bump_s = math.floor(bump_w * 1000 / 3.0) / 1000   # 针脚 bump 严格 ≤ 褶皱的 1/3（下取整，红线）
    rough_lo, rough_hi = spec["rough"]
    nt = m.node_tree
    for n in [n for n in nt.nodes if n.type in MLI_BANNED + ("BUMP",)]:
        nt.nodes.remove(n)                          # 清掉历史实现（v1.5 的 Wave 绗缝／Voronoi 分块）
    b = _mat_bsdf(m)
    _set_bsdf(b, spec)
    for nm in ("Specular IOR Level", "Specular"):
        if nm in b.inputs:
            b.inputs[nm].default_value = 0.5
            break
    coord = nt.nodes.new("ShaderNodeTexCoord")
    _, uv = _per_object(nt, coord.outputs["Object"], _aniso_scale(spec["stretch"]))
    uv = _domain_warp(nt, uv, key, strength=0.05, scale=0.8)

    noises = []
    for i, feat in enumerate(feats):
        n = nt.nodes.new("ShaderNodeTexNoise")
        _set_in(n, "Scale", 1.0 / feat)              # 特征尺度＝1/Scale（记录在案供 E15 核验）
        _set_in(n, "Detail", 6.0 if i == 0 else 4.0)  # ≥3 octaves（§二.8）
        _set_in(n, "Roughness", 0.55)
        nt.links.new(uv, n.inputs["Vector"])
        noises.append(n)
    a = nt.nodes.new("ShaderNodeMath")
    a.operation = "MULTIPLY"
    a.inputs[1].default_value = 0.68
    nt.links.new(noises[0].outputs["Fac"], a.inputs[0])
    mix = nt.nodes.new("ShaderNodeMath")
    mix.operation = "MULTIPLY_ADD"
    mix.inputs[1].default_value = 0.32
    nt.links.new(noises[1].outputs["Fac"], mix.inputs[0])
    nt.links.new(a.outputs[0], mix.inputs[2])
    bump1 = nt.nodes.new("ShaderNodeBump")
    _set_in(bump1, "Strength", bump_w)
    _set_in(bump1, "Distance", 0.004)
    nt.links.new(mix.outputs[0], bump1.inputs["Height"])

    st = nt.nodes.new("ShaderNodeTexNoise")          # 细针脚：高频分形噪声→窄阈值带→细线
    _set_in(st, "Scale", stitch_scale)
    _set_in(st, "Detail", 3.0)
    nt.links.new(uv, st.inputs["Vector"])
    band = nt.nodes.new("ShaderNodeMapRange")
    _set_in(band, "From Min", 0.62)
    _set_in(band, "From Max", 0.62 + STITCH_BAND)
    _set_in(band, "To Min", 0.0)
    _set_in(band, "To Max", 1.0)
    nt.links.new(st.outputs["Fac"], band.inputs[0])
    bump2 = nt.nodes.new("ShaderNodeBump")
    _set_in(bump2, "Strength", bump_s)
    _set_in(bump2, "Distance", 0.002)
    nt.links.new(band.outputs[0], bump2.inputs["Height"])
    nt.links.new(bump1.outputs["Normal"], bump2.inputs["Normal"])
    nt.links.new(bump2.outputs["Normal"], b.inputs["Normal"])

    mr = nt.nodes.new("ShaderNodeMapRange")          # roughness 带变化
    _set_in(mr, "To Min", rough_lo)
    _set_in(mr, "To Max", rough_hi)
    nt.links.new(noises[0].outputs["Fac"], mr.inputs[0])
    nt.links.new(mr.outputs[0], b.inputs["Roughness"])

    MATERIAL_LOG[key] = {
        "material": m.name, "class": "金色 MLI", "metallic": spec["metallic"],
        "base_color_F0": list(spec["f0"]), "rough_range": [rough_lo, rough_hi],
        "wrinkle_feature_m": feats, "noise_detail": [6.0, 4.0],
        "anisotropy_scale": list(spec["stretch"]),
        "bump_wrinkle": bump_w, "bump_stitch": bump_s, "stitch_ratio": round(bump_s / bump_w, 3),
        "stitch_scale_per_m": round(stitch_scale, 3), "stitch_jitter": jitter,
        "stitch_width_est_m": round(_stitch_width(stitch_scale), 4),
        "banned_nodes_present": [n.type for n in nt.nodes if n.type in MLI_BANNED],
    }
    return MATERIAL_LOG[key]


def _apply_plain(m, spec, key, label):
    """储箱白漆／机构件金属：按参数表设 metallic 与 roughness 带（噪波驱动变化）。"""
    nt = m.node_tree
    for n in [n for n in nt.nodes if n.type == "BUMP"]:
        nt.nodes.remove(n)
    b = _mat_bsdf(m)
    _set_bsdf(b, spec)
    rough_lo, rough_hi = spec["rough"]
    coord = nt.nodes.new("ShaderNodeTexCoord")
    _, uv = _per_object(nt, coord.outputs["Object"])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    _set_in(noise, "Scale", 6.0)
    _set_in(noise, "Detail", 3.0)
    nt.links.new(uv, noise.inputs["Vector"])
    mr = nt.nodes.new("ShaderNodeMapRange")
    _set_in(mr, "To Min", rough_lo)
    _set_in(mr, "To Max", rough_hi)
    nt.links.new(noise.outputs["Fac"], mr.inputs[0])
    nt.links.new(mr.outputs[0], b.inputs["Roughness"])
    MATERIAL_LOG[key] = {"material": m.name, "class": label, "metallic": spec["metallic"],
                         "base_color": mat_color(m), "rough_range": [rough_lo, rough_hi]}
    return MATERIAL_LOG[key]


def _apply_panel(m, spec, key):
    """翼板：深蓝紫电介质基底＋**盖玻璃 coat 镜面层**；板格线逐板抖动（域扭曲＋per-object 相位）。

    板格仍是两向波带（矩形电池片的形制），坐标经域扭曲＋逐器格距抖动＋逐板相位偏移后不再呈
    规则网格；周期纹理黑名单只针对 MLI（E15）。
    """
    jit = _rand("panelcell|%s" % key, 1.0 - spec["cell_jitter"], 1.0 + spec["cell_jitter"])
    nt = m.node_tree
    for n in [n for n in nt.nodes if n.type in ("TEX_WAVE", "TEX_CHECKER", "TEX_MAGIC")]:
        nt.nodes.remove(n)
    b = _mat_bsdf(m)
    _set_bsdf(b, spec)
    for nm in ("Specular IOR Level", "Specular"):
        if nm in b.inputs:
            b.inputs[nm].default_value = 0.5
            break
    coat = {}
    for nm, val in (("Coat Weight", spec["coat"]), ("Coat Roughness", spec["coat_rough"]),
                    ("Coat IOR", 1.45)):
        if nm in b.inputs:
            b.inputs[nm].default_value = val
            coat[nm] = val
    coord = nt.nodes.new("ShaderNodeTexCoord")
    _, uv = _per_object(nt, coord.outputs["Object"], PANEL_SCALE_RANGE)
    uv = _domain_warp(nt, uv, key, strength=0.10, scale=1.2)
    waves = []
    # 展开翼板的板面在 XY 平面（展向 X、弦向 Y、法向 Z）→ 板格两向取 X/Y；
    # 若仍按旧竖直翼板取 X/Z，Z 向在 0.03 m 厚度上退化，域扭曲会把板格糊成随机斑块。
    for direction in ("X", "Y"):
        w = nt.nodes.new("ShaderNodeTexWave")
        w.wave_type = "BANDS"
        w.bands_direction = direction
        _set_in(w, "Scale", 1.0 / (spec["cell"] * jit))
        _set_in(w, "Distortion", 1.2)
        _set_in(w, "Detail", 1.0)
        nt.links.new(uv, w.inputs["Vector"])
        waves.append(w)
    mx = nt.nodes.new("ShaderNodeMath")
    mx.operation = "MAXIMUM"
    nt.links.new(waves[0].outputs["Fac"], mx.inputs[0])
    nt.links.new(waves[1].outputs["Fac"], mx.inputs[1])
    thr = nt.nodes.new("ShaderNodeMath")
    thr.operation = "GREATER_THAN"
    thr.inputs[1].default_value = 0.991
    nt.links.new(mx.outputs["Value"], thr.inputs[0])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (*COL_PANEL_V2, 1.0)
    ramp.color_ramp.elements[1].position = 0.5
    ramp.color_ramp.elements[1].color = (*COL_PANEL_LINE, 1.0)
    nt.links.new(thr.outputs["Value"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    MATERIAL_LOG[key] = {
        "material": m.name, "class": "翼板", "metallic": spec["metallic"],
        "base_color_F0": list(spec["f0"]), "rough_range": list(spec["rough"]),
        "coat": coat, "cell_m": round(spec["cell"] * jit, 4), "cell_jitter": jit,
        "thin_film": "未启用（规格标为可选）",
    }
    return MATERIAL_LOG[key]


def _rebuild_materials():
    """按 §四 参数表在**场景内重建**材质（collector.py／combiner.py／assembly.py 一行不动）。

    返回逐行落地台账（写入 report 的 material_json），供 E15/E19 回读核验。
    """
    MATERIAL_LOG.clear()
    for label, spec in MATERIAL_TABLE.items():
        for m in list(bpy.data.materials):
            if not any(m.name.startswith(p) for p in spec["match"]):
                continue
            if spec["cls"] == "frozen":
                MATERIAL_LOG[m.name] = {"material": m.name, "class": label,
                                        "note": "单体已固化值不动（E15 只判 MAT_tube* ≥0.85）",
                                        "roughness": mat_rough(m), "metallic": mat_metallic(m)}
            elif spec["cls"] == "mli":
                _apply_mli(m, spec, m.name)
            elif spec["cls"] == "panel":
                _apply_panel(m, spec, m.name)
            else:
                _apply_plain(m, spec, m.name, label)
    return MATERIAL_LOG


# ============================================================ 几何微起伏（§二.8）
def _crown_mesh(ob, amount, cuts=CROWN_CUTS):
    """帆板微弯：沿**展向（板内最长轴）**弓起 amount，顶点沿**法向（最短轴）**位移 amount·(1−t²)。

    按板自身的尺寸定轴：展开态板的最长轴是展向 X、最短轴是厚度 Z；收拢态则分别是 Z 与 X。
    立方体只有 8 个角点、t 全为 ±1、位移恒 0，必须先 bmesh 细分才弯得动。
    """
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=cuts, use_grid_fill=True)
    ext = [max(v.co[i] for v in bm.verts) - min(v.co[i] for v in bm.verts) for i in range(3)]
    span_ax, norm_ax = ext.index(max(ext)), ext.index(min(ext))
    half = max(abs(v.co[span_ax]) for v in bm.verts) or 1.0
    for v in bm.verts:
        t = v.co[span_ax] / half
        v.co[norm_ax] += amount * (1.0 - t * t)
    bm.to_mesh(me)
    bm.free()
    me.update()


def _undulate_mesh(ob, key, cuts=UND_CUTS):
    """大平面微起伏：低频三轴正弦场沿顶点法向位移，幅度 ≤0.005 m（§二.8 红线：幅度即包络余量）。

    目的是破坏"完美平面"的均匀反光（提出方"熵"评审）；已验收尺寸/包络/端点判据不受影响。
    """
    amp = _rand("undamp|%s" % key, *UND_AMP_RANGE)
    r = _rng("und|%s" % key)
    freq = [round(r.uniform(1.2, 2.4), 3) for _ in range(3)]
    phase = [round(r.uniform(0.0, 2 * math.pi), 3) for _ in range(3)]
    _log("undulate|%s" % key, {"amp": amp, "freq": freq, "phase": phase})
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=cuts, use_grid_fill=True)
    bm.normal_update()
    for v in bm.verts:
        d = (math.sin(freq[0] * v.co.x + phase[0]) * math.sin(freq[1] * v.co.y + phase[1])
             * math.sin(freq[2] * v.co.z + phase[2]))
        v.co += v.normal * (amp * d)
    bm.to_mesh(me)
    bm.free()
    me.update()


# ============================================================ 推断默认件（§二.7）
def _face_half_x(body_pts, y):
    """本体在给定 y 处的截面半宽：两端面**实测**半宽线性插值（梯形舱成立，矩形 bay 退化为常数）。"""
    ylo = min(p[1] for p in body_pts)
    yhi = max(p[1] for p in body_pts)
    half = []
    for yv in (ylo, yhi):
        xs = [p[0] for p in body_pts if abs(p[1] - yv) < 0.03]
        half.append(max(xs) if xs else 0.0)
    if min(half) <= 0.0:
        return max(p[0] for p in body_pts)
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


def upgrade(objs, tag, has_tube):
    """给一台器加推断默认件＋去周期化的几何/材料处理；**在加前缀、挂父级之前**调用。

    ``objs`` 为本体 build() 的返回字典（键为基名）；本体可以是集光器的 bus 或合束器的 bay。
    """
    gold = C.new_material("MAT_up_gold", C.COL_BUS, 0.62, 0.15)
    alu = C.new_material("MAT_up_alu", (0.72, 0.72, 0.70), 0.35, 0.85)
    black = C.new_material("MAT_up_baffle", (0.015, 0.015, 0.017), 0.90, 0.10)
    grey = C.new_material("MAT_up_grey", (0.55, 0.56, 0.58), 0.45, 0.60)
    added = []

    body = objs.get("bus") or objs["bay"]
    # 去周期化①：本体大平面微起伏（幅度 ≤5 mm）——先变形，后续构件才贴在真实面上
    _undulate_mesh(body, "%s|body" % tag)
    # 去周期化②：帆板微弯（crown 0.005–0.02 m）——逐板随机、方向随机
    for p in [o for k, o in objs.items() if k.startswith("panel_")]:
        amt = _rand("crown|%s|%s" % (tag, p.name.split(".")[0]), 0.005, 0.02)
        sign = 1.0 if _rand("crownsign|%s|%s" % (tag, p.name.split(".")[0]), -1.0, 1.0) > 0 else -1.0
        _crown_mesh(p, amt * sign)

    body_pts = _vlocal([body])
    blo, bhi = _lbounds([body])
    top_z = bhi[2]
    half_x = max(abs(blo[0]), abs(bhi[0]))
    half_y = max(abs(blo[1]), abs(bhi[1]))
    body_h = bhi[2] - blo[2]

    # 1) RCS 推力器组 ×4：本体四角侧面，锥尖朝外；x 取该 y 处实测截面半宽，根部嵌入本体面
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            y = sy * RCS_YF * half_y
            half = _face_half_x(body_pts, y)
            added.append(_nozzle("RCS_%s_%s" % ("P" if sx > 0 else "N", "F" if sy > 0 else "A"),
                                 (sx * (half + RCS_D / 2 - RCS_EMBED), y,
                                  top_z - RCS_ZF * body_h), (sx, 0.0, 0.0), gold))

    # 2) 测控天线 ×1：本体顶面一角（避开镜筒/载荷舱/机构簇）
    added.append(_dish("ANT_dish", (ANT_F * half_x, -ANT_F * half_y, top_z), grey,
                       ANT_R[0] if has_tube else ANT_R[1]))

    # 3) 太阳翼的根部铰链与翼缘描边由 wing.py 的 SADA/EDGE 提供（§二.9 机构化）；
    #    此处只做**去周期化②**的帆板微弯（crown 0.005–0.02 m，逐板随机、方向随机）

    # 4) 镜筒口内光阑环（哑光黑环）：环不能是实心圆盘，否则会堵住筒口、
    #    也会与穿过筒口的星光束相交（E5）。环径按实测内壁半径反算，外缘压进内壁 6 mm。
    if has_tube:
        tube = objs["tube"]
        tlo, thi = _lbounds([tube])
        cx, cy = (tlo[0] + thi[0]) / 2, (tlo[1] + thi[1]) / 2
        rads = [math.hypot(p[0] - cx, p[1] - cy) for p in _vlocal([tube]) if p[2] < tlo[2] + 0.05]
        r_bore = min(rads) if rads else (thi[0] - tlo[0]) / 2 - 0.05
        for i in range(BAFFLE_RINGS):
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

    return added


# ============================================================ 构件
def build_cmb_parts():
    """合束器＝**全高平台舱** 3.60×1.24×0.90（§二.3 v1.8）＋载荷舱＋SADA 翼×2＋储箱×2。

    长宽沿用 assembly.py 的 BAY_L/BAY_W，高度 BAY_T 本轮联动勘误 0.50→0.90——**两状态同一物体**。
    平台舱顶面落在 z=+0.45（与集光器舱顶同高）→ 收光口仍 ≈0.51，两状态光路同解（E18）。
    collector.py／combiner.py 一行不动；载荷舱/储箱照旧复用 combiner/collector 的构件函数。
    """
    bay = A.build_bay()
    bay.location = (0.0, 0.0, 0.0)      # 以箱心为原点 ⇒ 顶面 +0.45（assembly 系里顶面在 z=0）
    parts = {"bay": bay}
    grp = [M.build_module()] + M.build_recv() + [M.build_aux()]
    lo = min(p[2] for p in _vlocal(grp))
    for o in grp:                       # 载荷舱落平台舱顶面中央、根部嵌入 MODULE_EMBED
        o.location = (o.location.x, o.location.y,
                      o.location.z + ((A.BAY_T / 2 - A.MODULE_EMBED) - lo))
    for o in grp:
        parts[o.name.split(".")[0]] = o
    for side in (+1.0, -1.0):           # SADA 翼×2（展开，法向朝 −Z）
        for o in W.build_wing(side, A.BAY_W / 2, state="deployed"):
            parts[o.name.split(".")[0]] = o
    for t in C.build_tanks(face="X±"):
        parts[t.name.split(".")[0]] = t
    return parts


def swap_wings(objs, face_x):
    """把单体自带的翼板换成 wing.py 的 SADA＋4 板机构（§二.9 实施路径：单体文件一行不动）。"""
    old = [o for k, o in objs.items() if k.startswith("panel_")]
    new = []
    for side in (+1.0, -1.0):
        new += W.build_wing(side, face_x, state="deployed")
    for o in old:
        objs.pop(o.name.split(".")[0], None)
        bpy.data.objects.remove(o, do_unlink=True)
    for o in new:
        objs[o.name.split(".")[0]] = o
    return new


def build_spacecraft():
    """三器：集光器与合束器都逐字复用阶段一/二单体 build()。返回 {tag: {holder, objs, outline_*}}。

    objs 的键是**改名后**的名字（colA_bus / cmb_module 等）；升级件与去周期化处理都在 group()
    之前完成（局部系），与本体件一起被加前缀、挂父级。
    """
    half = BASELINE_DISPLAY / 2
    specs = (("colA", C.build(purge=False), (0.0, -half, 0.0), 0.0),    # A 在 −Y：窗口朝 +Y
             ("colB", C.build(purge=False), (0.0, +half, 0.0), 180.0),  # B 在 +Y：窗口朝 −Y
             ("cmb", build_cmb_parts(), (0.0, 0.0, 0.0), 0.0))
    out = {}
    for tag, objs, loc, rot in specs:
        if tag != "cmb":                       # 集光器：单体翼板换成 SADA 机构（同制）
            swap_wings(objs, (C.BUS_W + C.BUS_SHORT_W) / 4)
        parts = list(objs.values())
        before = _lbounds(parts)
        added = upgrade(objs, tag, has_tube=(tag != "cmb"))
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


def star_axes_and_mouth(col_objs):
    """两台集光器的星光束轴线 (cx, cy) 与筒口顶高，供长度反算与建模共用。"""
    axes, mouth = [], 0.0
    for tag in ("colA", "colB"):
        tube = next(v for k, v in col_objs[tag]["objs"].items() if k.endswith("_tube"))
        lo, hi = vbounds([tube])
        axes.append(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2))
        mouth = max(mouth, hi[2])
    return axes, mouth


def beam_group(tag, p0, p1, d_shell, mats, verts=24):
    """一条束的三层（§四 v1.8／T4）：半透明外壳晕 ＋ 亮芯（芯径＝壳径/3）＋两端点增亮段。

    外壳对象沿用 ``BEAM_<tag>`` 名并记录 p_start/p_end/diameter（壳径，E5/E6/E7 据此判），
    另记录 core_diameter（E22）。端点段长度 END_LEN ≤0.15 m。
    """
    f = BEAM_MODE_FACTOR[BEAM_MODE]
    core_d = d_shell * CORE_FRAC * f["core"]
    shell = cylinder_between("BEAM_%s" % tag, p0, p1, d_shell / 2, mats["shell"], verts=verts)
    shell["diameter"] = d_shell
    shell["core_diameter"] = core_d
    shell["beam_mode"] = BEAM_MODE
    core = cylinder_between("BEAM_%s_core" % tag, p0, p1, core_d / 2, mats["core"], verts=verts)
    ends = []
    for i, (a, b) in enumerate(((p0, p1), (p1, p0)), start=1):
        n = (Vector(b) - Vector(a)).normalized()
        q0 = Vector(a)
        e = cylinder_between("BEAM_%s_end%d" % (tag, i), q0, q0 + n * END_LEN,
                             d_shell / 2, mats["end"], verts=verts)
        e["end_len"] = END_LEN
        ends.append(e)
    return [shell, core] + ends


def build_beams(craft, ports, top_z, mats_star, mats_link):
    """四束（星光×2 粉粗、器间×2 红细），每条三层。返回 (光束组字典, 外壳列表)。"""
    groups, shells = {}, []
    for tag in ("colA", "colB"):                 # 星光：垂直入射筒口，终点在最内光阑环
        objs = craft[tag]["objs"]
        tube = next(v for k, v in objs.items() if k.endswith("_tube"))
        lo, hi = vbounds([tube])
        cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
        rings = [v for k, v in objs.items() if "_BAFFLE_" in k]
        inner_z = min(ring_plane_z(r) for r in rings) if rings else hi[2]
        p0 = (cx, cy, inner_z - STAR_BOTTOM_EMBED)
        p1 = (cx, cy, top_z)
        groups["star_%s" % tag] = beam_group("star_%s" % tag, p0, p1, D_STAR, mats_star, verts=32)
    for tag, port_tag in (("colA", "negY"), ("colB", "posY")):   # 器间：窗口→收光口
        win = next(v for k, v in craft[tag]["objs"].items() if k.endswith("_window_out"))
        wlo, whi = vbounds([win])
        wc = [(wlo[i] + whi[i]) / 2 for i in range(3)]
        sgn = 1.0 if tag == "colA" else -1.0
        face_y = whi[1] if sgn > 0 else wlo[1]
        p0 = (wc[0], face_y - sgn * EMBED, wc[2])
        plo, phi = vbounds([ports[port_tag]])
        pc = [(plo[i] + phi[i]) / 2 for i in range(3)]
        pface = phi[1] if sgn < 0 else plo[1]
        p1 = (pc[0], pface + sgn * EMBED, pc[2])
        groups["link_%s" % tag] = beam_group("link_%s" % tag, p0, p1, D_LINK, mats_link, verts=16)
    shells = [g[0] for g in groups.values()]
    return groups, shells


def beam_materials(mode=None, tag="star"):
    """按当前（或指定）模式造三条光束材质：壳／芯／端点。"""
    old = BEAM_MODE
    if mode:
        globals()["BEAM_MODE"] = mode
    f = BEAM_MODE_FACTOR[BEAM_MODE]
    if tag == "star":
        base, emit = COL_STAR_BASE, COL_STAR_EMIT
        sh, al, es, ee = SHELL_STR_STAR, SHELL_ALPHA_STAR, STAR_STRENGTH, END_STR_STAR
    else:
        base, emit = COL_LINK_BASE, COL_LINK_EMIT
        sh, al, es, ee = SHELL_STR_LINK, SHELL_ALPHA_LINK, LINK_STRENGTH, END_STR_LINK
    mats = {
        "shell": emit_material("MAT_beam_%s_shell" % tag, base, base, sh * f["shell"],
                               alpha=al, rim=True),
        "core": emit_material("MAT_beam_%s_core" % tag, base, emit, es),
        "end": emit_material("MAT_beam_%s_end" % tag, base, emit, ee),
    }
    globals()["BEAM_MODE"] = old
    return mats


# ============================================================ 相机与构图
def add_camera():
    cam_data = bpy.data.cameras.new("RIG_Cam")
    cam_data.lens = 40.0
    cam = bpy.data.objects.new("RIG_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam


def point(cam, location, target, ortho=None, lens=40.0, dof=None, roll=0.0):
    cam.location = Vector(location)
    d = Vector(target) - Vector(location)
    if abs(d.normalized().dot(Vector((0, 0, 1)))) > 0.9995:
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    if roll:                      # 绕自身视轴滚转：正交俯视靠它把基线摆到画幅长边
        cam.rotation_euler.rotate_axis("Z", math.radians(roll))
    if ortho:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = "PERSP"
        cam.data.lens = lens
    cam.data.dof.use_dof = bool(dof)
    if dof:
        cam.data.dof.aperture_fstop = dof["fstop"]
        cam.data.dof.focus_object = bpy.data.objects.get(dof.get("focus", ""))
    C.refresh()


def craft_extent_points():
    """三器各自包围盒的 8 个角点（供构图与验收核验共用；不含光束/端口）。"""
    pts = []
    for tag in ("colA", "colB", "cmb"):
        objs = [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith(tag + "_")]
        if not objs:
            continue
        lo, hi = vbounds(objs)
        pts += [Vector((hi[0] if i & 1 else lo[0], hi[1] if i & 2 else lo[1],
                        hi[2] if i & 4 else lo[2])) for i in range(8)]
    return pts


def project(cam, pts):
    """各点在相机画面里的归一化范围：u/v∈[0,1] 为画框内，depth∈[0,1] 为裁剪范围内。"""
    scene = bpy.context.scene
    C.refresh()
    us, vs, ds = [], [], []
    for p in pts:
        u, v, d = world_to_camera_view(scene, cam, p)
        us.append(u)
        vs.append(v)
        ds.append(d)
    return min(us), max(us), min(vs), max(vs), min(ds), max(ds)


def framed_view(cam, zc, pts, dirv, lens, fill, vcenter):
    """按"编队占画面宽"反解机位：沿 dirv 推拉 + 抬降视线目标，同时定占宽与竖向位置。

    wide 用 fill=0.68／vcenter=0.42（§五 三分法）；front 用 fill≈0.9（正视要看得清翼态）。
    """
    dirv = Vector(dirv).normalized()
    d = 20.0
    aim = Vector((0.0, 0.0, zc))
    for _ in range(6):
        point(cam, location=aim + dirv * d, target=aim, lens=lens)
        u0, u1, v0, v1, _, _ = project(cam, pts)
        frac = max(u1 - u0, 1e-3)
        vc = (v0 + v1) / 2
        d = min(max(d * (frac / fill), 4.0), 400.0)      # 占宽 ∝ 1/距离
        sensor_h = cam.data.sensor_width * bpy.context.scene.render.resolution_y \
            / bpy.context.scene.render.resolution_x
        frame_h = 2 * d * math.tan(math.atan(sensor_h / (2 * lens)))
        aim = Vector((0.0, 0.0, aim.z + (vc - vcenter) * frame_h))
    point(cam, location=aim + dirv * d, target=aim, lens=lens)
    u0, u1, v0, v1, _, _ = project(cam, pts)
    return (dict(location=aim + dirv * d, target=aim, ortho=None, lens=lens),
            d, u1 - u0, (v0 + v1) / 2)


def view_table(cam, zc):
    """五张交付视角的相机参数——**交付渲染与验收核验共用这一份**（E5 出画判据依赖它）。"""
    wide = BASELINE_DISPLAY + 3.0
    half = BASELINE_DISPLAY / 2
    pts = craft_extent_points()
    kw_front, d_f, fill_f, vc_f = framed_view(cam, zc, pts, FRONT_DIR, 40.0, FRONT_FILL, 0.55)
    kw_wide, d_w, fill_w, vc_w = framed_view(cam, zc, pts, WIDE_DIR, WIDE_LENS, WIDE_FILL,
                                             WIDE_VCENTER)
    _log("framing", {"front": {"distance_m": round(d_f, 3), "fill": round(fill_f, 4),
                               "v_center": round(vc_f, 4)},
                     "wide": {"distance_m": round(d_w, 3), "fill": round(fill_w, 4),
                              "v_center": round(vc_w, 4)}})
    print("[formation] 构图：front 距离 %.1f m 占宽 %.3f／v心 %.3f；"
          "wide 距离 %.1f m 占宽 %.3f／v心 %.3f"
          % (d_f, fill_f, vc_f, d_w, fill_w, vc_w), flush=True)
    return (
        # front：斜视（与 F4 同观感——纯沿 ±X 看会把集光器翼看成一条线，F4 是斜视才同时
        # 看得到"三器成排"与"翼展"）；side/top：沿 ±Y 与俯视
        ("front", kw_front),
        ("side", dict(location=(0, wide * 2.2, zc), target=(0, 0, zc), ortho=wide * 0.5)),
        # top：正交俯视（只受裁剪影响），机位压低到 TOP_CAM_Z，星光束才可能"从相机后面出画"
        ("top", dict(location=(0, 0, TOP_CAM_Z), target=(0, 0, zc),
                     ortho=wide + 1.0, roll=90.0)),
        # iso：colA 与光束的近景（对 F3 分布式_集光器特写），v1.5 加 f/2.8 景深
        ("iso", dict(location=Vector((4.2, -half - 4.0, 2.6)), target=(0.0, -half, zc + 0.35),
                     ortho=None, lens=50.0, dof=DOF_ISO)),
        # wide：编队全景（对 F6），v1.5 按三分法重裁
        ("wide", kw_wide),
    )


def solve_star_top_z(cam, axes, start_z, views):
    """反算星光束上端 z：**每一张交付视角、两种画幅**下，束顶都落到画框外或深度裁剪外。

    §二.5 v1.5：v1.0–v1.4 把视觉要求误写成几何数值（"≥2 m 出画"），front/wide 里光束曾在
    空中截止。这里改为按视场反算——逐视角把束顶往上推到投影出框（或越过相机＝深度裁剪外）。
    """
    scene = bpy.context.scene
    res0 = (scene.render.resolution_x, scene.render.resolution_y)
    margin = 0.005
    need = start_z
    for name, kw in views:
        point(cam, **kw)
        for res in (RES, RES_FINAL):
            scene.render.resolution_x, scene.render.resolution_y = res
            C.refresh()
            for (cx, cy) in axes:
                z = start_z
                while z < STAR_TOP_MAX:
                    u, v, dep = world_to_camera_view(scene, cam, Vector((cx, cy, z)))
                    # 注意：world_to_camera_view 的第三分量是**世界单位**的视轴距离，
                    # 不是 0–1 归一值；"在画内"＝ u/v 在画框内且深度落在相机裁剪区间内。
                    in_clip = cam.data.clip_start <= dep <= cam.data.clip_end
                    inside = (margin <= u <= 1 - margin and margin <= v <= 1 - margin
                              and in_clip)
                    if not inside:
                        break
                    z += STAR_SCAN_STEP
                need = max(need, z)
    scene.render.resolution_x, scene.render.resolution_y = res0
    C.refresh()
    return need + STAR_TOP_MARGIN


# ============================================================ 渲染环境
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
    for name, val in (("Strength", GLARE_STRENGTH_NEW), ("Saturation", GLARE_SATURATION_NEW),
                      ("Maximum", GLARE_MAX_NEW)):      # 5.x 才有：强度/饱和度/上限
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

    # §五 v1.5：主光＝星光源（+Z 主导，与星光同向）；fill ≤0.2 仅防死黑
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
    f.angle = math.radians(40)
    fill = bpy.data.objects.new("LGT_Fill", f)
    fill.rotation_euler = (math.radians(80), 0.0, math.radians(-140))
    bpy.context.collection.objects.link(fill)
    fill["role"] = "fill"
    layout = bpy.data.objects.get("EMPTY_LAYOUT")
    if layout:
        layout["sun_dir"] = list(SUN_DIR)
        layout["starfield_strength"] = BG_STRENGTH
        layout["starfield_texture"] = os.path.relpath(tex_path, HERE) if os.path.exists(tex_path) \
            else "（缺失，退回暗色背景）"

    # 色彩管理 AgX ＋ Medium High Contrast（**设后回读**：look 在新版里偶有改名/设不上）
    scene.view_settings.view_transform = "AgX"
    got = None
    try:
        scene.view_settings.look = LOOK_NAME
        got = scene.view_settings.look
    except Exception as exc:
        print("[formation] 色彩管理 look 设置异常：%s" % exc, flush=True)
    ok = got and str(got).replace(" ", "").lower() == LOOK_NAME.replace(" ", "").lower()
    print("[formation] 色彩管理：view_transform=%s，look=%r%s"
          % (scene.view_settings.view_transform, got, "" if ok else "  ← **未生效**"), flush=True)
    if layout:
        layout["look"] = str(got)

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


def render_views(final=False):
    scene = bpy.context.scene
    os.makedirs(OUT_DIR, exist_ok=True)
    cam = bpy.data.objects.get("RIG_Cam") or add_camera()
    scene.render.resolution_x, scene.render.resolution_y = RES_FINAL if final else RES
    zc = vcenter(bpy.data.objects["colA_bus"])[2]
    views = view_table(cam, zc)
    for name, kw in views:
        point(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)

    # §四/T4：准写实对照——同相机同光路，仅芯径减半、壳强度 ×0.3、仅端点辉光
    set_beam_mode("realistic")
    point(cam, **dict(views[4][1]))
    scene.render.filepath = os.path.join(OUT_DIR, "wide_realistic.png")
    bpy.ops.render.render(write_still=True)
    print("[formation] 出图 %s（准写实模式对照）" % scene.render.filepath, flush=True)

    if final:      # 正式档另留一张 EXR 底片（规格 §五）
        scene.render.image_settings.file_format = "OPEN_EXR"
        point(cam, **dict(views[4][1]))
        scene.render.filepath = os.path.join(OUT_DIR, "wide.exr")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)


def set_beam_mode(mode):
    """切换光束模式并**按记录端点重建**束体：端点/走向/口径比完全不变（E22）。

    重建而非缩放：几何由同一组 p_start/p_end 生成，两模式的光路逐点一致可核。
    """
    old = bpy.data.objects.get("BEAM_star_colA")
    if old is None:
        return
    ends = {o.name: (list(o["p_start"]), list(o["p_end"]), o["diameter"])
            for o in bpy.data.objects if o.name.startswith("BEAM_") and "p_start" in o.keys()
            and "_core" not in o.name and "_end" not in o.name}
    layout = bpy.data.objects["EMPTY_LAYOUT"]
    for o in [o for o in bpy.data.objects if o.name.startswith("BEAM_")]:
        bpy.data.objects.remove(o, do_unlink=True)
    globals()["BEAM_MODE"] = mode
    for m in [m for m in bpy.data.materials if m.name.startswith("MAT_beam_")]:
        bpy.data.materials.remove(m, do_unlink=True)
    mats = {"star": beam_materials(tag="star"), "link": beam_materials(tag="link")}
    made = []
    for nm, (p0, p1, d) in ends.items():
        kind = "star" if nm.startswith("BEAM_star") else "link"
        made += beam_group(nm.replace("BEAM_", ""), p0, p1, d, mats[kind],
                           verts=32 if kind == "star" else 16)
    for ob in made:
        ob.parent = layout
    C.refresh()
    layout["BEAM_MODE"] = mode
    print("[formation] BEAM_MODE → %s（束体按记录端点重建，光路不变）" % mode, flush=True)
    return made


def build():
    """生成分布式编队场景。返回对象字典。"""
    RANDOM_LOG.clear()
    C.purge_scene()
    C.refresh()
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = RES      # 反算视场前先把画幅定死
    layout = bpy.data.objects.new("EMPTY_LAYOUT", None)
    bpy.context.collection.objects.link(layout)
    layout["baseline_display_m"] = BASELINE_DISPLAY
    layout["baseline_real_m"] = "%g–%g" % BASELINE_REAL_M
    layout["beam_d_star_m"] = D_STAR
    layout["beam_d_link_m"] = D_LINK
    layout["beam_ratio"] = D_STAR / D_LINK
    layout["wing_state"] = "集光器沿用单体 ±X 构型（T1 默认，未改）"
    layout["beam_material"] = ("MAT_beam_star 基色 %s／发光 %s／强度 %g；"
                               "MAT_beam_link 基色 %s／发光 %s／强度 %g"
                               % (COL_STAR_BASE, COL_STAR_EMIT, STAR_STRENGTH,
                                  COL_LINK_BASE, COL_LINK_EMIT, LINK_STRENGTH))
    layout["cmb_body"] = ("全高平台舱 %.2f×%.2f×%.2f（与组合体同一物体，§二.3 v1.8）"
                          "＋载荷舱 Ø%.2f×%.2f" % (A.BAY_L, A.BAY_W, A.BAY_T,
                                                  2 * M.MODULE_R + 2 * M.MODULE_RIM_OVER,
                                                  M.MODULE_H))

    craft = build_spacecraft()
    _rebuild_materials()                      # §四 参数表：场景内重建（三器文件不动）
    layout["material_json"] = json.dumps(MATERIAL_LOG, ensure_ascii=False, sort_keys=True)
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

    mats_star = beam_materials(tag="star")          # 壳／芯／端点三层（§四 v1.8）
    mats_link = beam_materials(tag="link")
    layout["BEAM_MODE"] = BEAM_MODE
    layout["BEAM_MODE_note"] = BEAM_MODE_NOTE

    ports = build_ports(craft["cmb"]["objs"])
    # 星光束长度按五张交付视角的视场反算（§二.5）——必须先有相机与视角表
    cam = add_camera()
    zc = vcenter(bpy.data.objects["colA_bus"])[2]
    axes, mouth = star_axes_and_mouth(craft)
    views = view_table(cam, zc)
    top_z = solve_star_top_z(cam, axes, mouth + STAR_ABOVE_MIN, views)
    layout["beam_star_top_z"] = round(top_z, 3)
    layout["beam_star_len_m"] = round(top_z - mouth, 3)
    print("[formation] 星光束长度反算：筒口 z=%.2f → 束顶 z=%.2f（长 %.2f m，五视角均出画）"
          % (mouth, top_z, top_z - mouth), flush=True)

    groups, shells = build_beams(craft, ports, top_z, mats_star, mats_link)
    beam_objs = [o for g in groups.values() for o in g]
    for ob in list(ports.values()) + beam_objs:
        ob.parent = layout                # 三器与四束（含芯/端点段）均挂布局父级（E9）
    C.refresh()
    layout["random_seed"] = SEED
    layout["random_json"] = json.dumps(RANDOM_LOG, ensure_ascii=False, sort_keys=True)

    objs = {o.name: o for o in bpy.context.scene.objects}
    report(objs, shells)
    return objs


def report(objs, beams):
    lo, hi = vbounds([o for o in objs.values() if o.type == "MESH"])
    cA, cB = vcenter(objs["colA_bus"]), vcenter(objs["colB_bus"])
    cmb_body = objs.get("cmb_bay") or objs["cmb_bus"]
    bl, bh = vbounds([cmb_body])
    print("[formation] 编队：显示基线 %.1f m（真实 %g–%g m，不成比例）"
          % (BASELINE_DISPLAY, *BASELINE_REAL_M), flush=True)
    print("[formation] 实测基线 |y_colA−y_colB| = %.3f m；合束器 y = %+.3f"
          % (abs(cA[1] - cB[1]), vcenter(cmb_body)[1]), flush=True)
    print("[formation] 合束器（全高平台舱）：%.3f × %.3f × %.3f m；载荷舱 %.3f × %.3f × %.3f m"
          % (bh[0] - bl[0], bh[1] - bl[1], bh[2] - bl[2],
             *[vbounds([objs["cmb_module"]])[1][i] - vbounds([objs["cmb_module"]])[0][i]
               for i in range(3)]), flush=True)
    print("[formation] 光束：星光 Φ%.2f（粉）/ 器间 Φ%.2f（红），束径比 %.1f:1"
          % (D_STAR, D_LINK, D_STAR / D_LINK), flush=True)
    for b in beams:
        bbl, bbh = vbounds([b])
        length = max(bbh[i] - bbl[i] for i in range(3))     # 束沿不同轴，取其最大跨度
        print("[formation]   %-18s 长 %6.3f m  bbox %.3f×%.3f×%.3f"
              % (b.name, length, bbh[0] - bbl[0], bbh[1] - bbl[1], bbh[2] - bbl[2]), flush=True)
    for tag in ("colA", "colB", "cmb"):
        up = [n for n in objs if n.startswith(tag + "_")
              and any(k in n for k in ("_RCS_", "_ANT_", "_BAFFLE_",
                                       "_TURNTABLE_", "_ROD_"))]     # 推断默认件（翼机构另计）
        print("[formation] %s 升级件 %d 件" % (tag, len(up)), flush=True)
    print("[formation] 包围盒 %.2f × %.2f × %.2f m；对象 %d"
          % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], len(objs)), flush=True)
    print("[formation] 随机量台账 %d 项（种子 %d，见 report.random_json）"
          % (len(RANDOM_LOG), SEED), flush=True)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    final = "--final" in argv
    build()
    build_render_env(final=final)
    render_views(final=final)
    print("[formation] 完成：%d 个对象（%s）" % (len(bpy.context.scene.objects),
                                              "正式" if final else "预览"), flush=True)
