# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——三器组合体（**在轨组合体测试状态**）装配。

用法：
    blender --background --python assembly.py     # 装配 + 四视角渲染到 out/assembly/

依《阶段三_建模规格》（2026-09-30）：
    * 布局＝**共机架两级堆叠**：下层金色共机架（`deck`）+ 吊在其下中央的合束器平台舱；
      上层甲板上 集光器B ｜ 合束器载荷舱 ｜ 集光器A 成排，三者在 deck 顶面**同层**。
    * **太阳翼全部展开**（在轨测试状态；发射收拢状态本阶段不做）。
    * 一切连接结构**金色 MLI 包覆**，场景中不得出现深灰/黑色独立梁体。
    * 定长基线取 2.366 m（PPT"~2.5 m"；上限由 Φ3650 包络反算，见《阶段三_交付说明》）。

修正记录：上一版把三器一字拉开、用一根外露深灰转接梁承托，且把太阳翼收拢——三处均与
PPT 文字（"3 个探测器共机架安装"）和渲染图不符，本版按规格重做。
"""
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import collector as C
import combiner as M

OUT_DIR = os.path.join(HERE, "out", "assembly")

# ============================================================ 参数区
# ---- 共机架（deck）：金色箱体，顶面即上层甲板 ----
DECK_W = 1.24          # X 向宽（略宽于舱，保证两台集光器舱底面落在甲板范围内）
DECK_L = 3.60          # Y 向长（≥ 基线 + 0.9 m）
DECK_T = 0.40          # Z 向厚（规格 0.3–0.5）
DECK_BEVEL = 0.06      # 倒角：同时把四角收进 Φ3650 包络圈内
DECK_TOP = 0.0         # 甲板顶面取 z=0，其余构件以此为基准

# ---- 基线：PPT"~2.5 m"；上限由包络反算（短边朝外时舱角须落在 Φ3650 圈内）----
ENV_R = 1.825
ENV_H = 4.610
BASELINE = 2.366

MODULE_EMBED_DECK = 0.020   # 载荷舱底面嵌入甲板的深度（D4 允许 ±0.05）

RES = (1200, 900)
SAMPLES = 48
BG = 0.012


# ============================================================ 工具
def vbounds(objs):
    C.refresh()
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def _group(objs, prefix, location, rot_z_deg):
    """把一组新建对象挂到该件的空物体下，改名并整体就位。

    **不设** matrix_parent_inverse：它用于"保持子对象原世界位置"，设成 holder 矩阵的逆
    会把位移与旋转整个抵消（上一版两台集光器因此留在原点）。
    """
    holder = bpy.data.objects.new(prefix + "_ROOT", None)
    bpy.context.collection.objects.link(holder)
    holder.location = Vector(location)
    holder.rotation_euler = (0.0, 0.0, math.radians(rot_z_deg))
    for o in objs:
        if o.parent is None:
            o.parent = holder
    C.refresh()                      # 设完父级必须刷新，否则世界矩阵仍是旧值
    for o in objs:
        o.name = "%s_%s" % (prefix, o.name.split(".")[0])
    return holder


# ============================================================ 构件
def build_deck():
    """共机架：金色箱体，顶面为上层甲板。"""
    mat = C.new_material("MAT_DECK_MLI", C.COL_BUS, 0.62, 0.15)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0.0, 0.0, DECK_TOP - DECK_T / 2))
    deck = bpy.context.active_object
    deck.name = "deck"
    deck.dimensions = (DECK_W, DECK_L, DECK_T)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    deck.data.materials.append(mat)
    C.add_noise_bump(mat)
    C.apply_bevel(deck, DECK_BEVEL, 2)
    return deck


def build_collector(prefix, y, rot_z_deg):
    """一台集光器（在轨状态：太阳翼展开）。"""
    objs = [C.build_bus(), C.build_tube()]
    objs += C.build_gimbals()
    objs += C.build_tanks()                    # 单体构型：两只贴 +Y 长边（朝相邻器）
    panels, _ = C.build_panels()               # 展开
    objs += panels
    objs.append(C.build_window())
    return _group(objs, prefix, (0.0, y, DECK_TOP + C.BUS_H / 2), rot_z_deg)


def build_combiner(prefix):
    """合束器：平台舱吊在甲板下方中央；载荷舱组坐甲板顶面中央。

    规格 §一.4：载荷舱经承力筒与下方平台舱相连，连接段藏在共机架内——故两者在装配里
    分居甲板两侧，不直接相接。
    """
    # 1) 下层：平台舱（含 ±X 储箱、展开的太阳翼）
    lower = [C.build_bus()]
    lower += C.build_tanks(face="X±")          # D9：合束器两只储箱在 ±X 侧面外露
    panels, _ = C.build_panels()               # 展开
    lower += panels
    _group(lower, prefix + "low", (0.0, 0.0, DECK_TOP - DECK_T - C.BUS_H / 2), 0.0)

    # 2) 上层：载荷舱 + 接收机构 + 敏感器，底面贴甲板顶面
    grp = [M.build_module()] + M.build_recv() + [M.build_aux()]
    lo, _ = vbounds(grp)
    dz = (DECK_TOP - MODULE_EMBED_DECK) - lo[2]
    _group(grp, prefix + "mod", (0.0, 0.0, dz), 0.0)


def build():
    """装配三器组合体。返回对象字典。"""
    C.purge_scene()
    build_deck()
    # 集光器 B（−Y 侧）：长边朝 +Y（朝合束器）→ 不转
    build_collector("colB", -BASELINE / 2, 0.0)
    # 集光器 A（+Y 侧）：长边朝 −Y（朝合束器）→ 绕 Z 转 180°
    build_collector("colA", +BASELINE / 2, 180.0)
    build_combiner("cmb")
    objs = {o.name: o for o in bpy.context.scene.objects}
    report(objs)
    return objs


def report(objs):
    C.refresh()
    meshes = [o for o in objs.values() if o.type == "MESH"]
    lo, hi = vbounds(meshes)
    tubeA, tubeB = objs["colA_tube"], objs["colB_tube"]
    ya = (vbounds([tubeA])[0][1] + vbounds([tubeA])[1][1]) / 2
    yb = (vbounds([tubeB])[0][1] + vbounds([tubeB])[1][1]) / 2
    buses = [objs["colA_bus"], objs["colB_bus"], objs["cmblow_bus"]]
    ymax = 0.0
    for b in buses:
        lo_b, hi_b = vbounds([b])
        ymax = max(ymax, abs(lo_b[1]), abs(hi_b[1]))
    dlo, dhi = vbounds([objs["deck"]])
    ymax = max(ymax, abs(dlo[1]), abs(dhi[1]))
    print("[assembly] 布局：共机架两级堆叠——下层 deck + 合束器平台舱（吊挂），"
          "上层 colB｜载荷舱｜colA 同层成排", flush=True)
    print("[assembly] 基线（两镜筒轴距） = %.3f m（D2 需 2.3–2.6；PPT『~2.5 m』）" % abs(ya - yb), flush=True)
    print("[assembly] D11 包络记录：三器舱体外缘 max|y| = %.3f m（需 ≤1.85）；总高 %.3f m（需 ≤%.2f）"
          % (ymax, hi[2] - lo[2], ENV_H), flush=True)
    print("[assembly] 包围盒 %.2f × %.2f × %.2f m；对象 %d（网格 %d）"
          % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], len(objs), len(meshes)), flush=True)


# ============================================================ 渲染
def render_views():
    scene = bpy.context.scene
    cam = C.setup_render_rig()
    scene.render.resolution_x, scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = True
    try:
        scene.cycles.device = "GPU"
    except Exception:
        pass
    os.makedirs(OUT_DIR, exist_ok=True)

    meshes = [o for o in scene.objects if o.type == "MESH"]
    lo, hi = vbounds(meshes)
    center = Vector(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2))
    radius = max(((o.matrix_world @ v.co) - center).length for o in meshes for v in o.data.vertices)
    lens = 52.0
    half_v = math.atan(cam.data.sensor_width * RES[1] / RES[0] / 2 / lens)
    iso_dist = radius / math.sin(half_v) * 1.06

    views = (
        ("side", dict(location=(0, 12, center.z), target=(0, 0, center.z), ortho=5.2)),
        ("front", dict(location=(-12, 0, center.z), target=(0, 0, center.z), ortho=5.2)),
        ("iso", dict(location=Vector((0.85, -1.0, 0.40)).normalized() * iso_dist
                     + Vector((0, 0, center.z)), target=(0, 0, center.z), lens=lens)),
        ("top", dict(location=(0.0, 0.0, 10), target=(0.0, 0.0, DECK_TOP), ortho=4.4)),
    )
    for name, kw in views:
        C.point_camera(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[assembly] 出图 %s" % scene.render.filepath, flush=True)


if __name__ == "__main__":
    build()
    render_views()
    print("[assembly] 完成：%d 个对象" % len(bpy.context.scene.objects), flush=True)
