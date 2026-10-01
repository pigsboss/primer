# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——三器组合体（在轨组合体测试状态）装配。

用法：
    blender --background --python assembly.py     # 装配 + 四视角渲染到 out/assembly/

依《验收反馈 03》（2026-09-30，取代反馈 02 第 1、2 条与《阶段三_建模规格》§一.2/§二）：

    * **两层结构**：下层只有合束器平台舱本体（金色大箱，与共机架一体）；两台集光器
      舱底**直接**落在它顶面，器与箱之间**没有**任何独立平板或梁件。
    * **集光器太阳翼折叠平贴**各自舱体 ±X 侧面；**只有合束器一副大翼展开**。
      依据：`refs/frames/opt_f040.png`（正视）、`组合体_整体渲染.png`、`组合体_顶部特写.png`；
      PPT 第 51 页"2 个集光器与合束器承力筒相连"、第 57 页"3 个探测器共机架安装"。
    * 一切连接结构金色 MLI 包覆，不得出现外露深灰/黑色独立结构件。
    * 定长基线 2.366 m（PPT"~2.5 m"，上限由 Φ3650 包络反算）。

修正记录：上一版做成 deck 平板＋吊挂平台舱的三层结构，且把集光器翼做成展开——两处均与
参考图不符，本版按反馈 03 重做。
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
import wing as W

OUT_DIR = os.path.join(HERE, "out", "assembly")

# ============================================================ 参数区
# ---- 合束器**舱板段**（下段；上段为承力筒）----
# 反馈 07 §三.1 / T7：三向尺寸集中在下列常量，一处改处处改；1.05³ 系图据重建工作值待设计确认。
# （v1.8 的 3.60×1.24×0.90 系伪文据"L=3.60 吃满 Φ3650"＋未验收遗产值，已作废）
BAY_W = 1.05           # X（深，⊥基线）
BAY_L = 1.05           # Y（宽，∥基线）
BAY_T = 1.05           # Z（高，沿光轴）
BAY_BEVEL = 0.03
BAY_TOP = 0.0          # 顶面取 z=0，其余构件以此为基准

ENV_R = 1.825          # 发射包络 Φ3650 半径
ENV_H = 4.610
BASELINE = 2.50        # 两集光器**入瞳（筒轴）中心距**（T9：PPT P56"~2.5 m"＋提出方 11:03）
TUBE_D_LOAD = 0.60     # 承力筒外径（上段；反馈 07 §三.2，旧 Ø0.52 系 module 遗产值）
LOCK_REACH = 0.625     # 锁紧释放机构跨距（承力筒筒面 ±0.30 → 集光器舱内侧面 ±0.925）

MODULE_EMBED = 0.020   # 载荷舱底面嵌入平台舱顶面的深度（D4 允许 ≤0.05）

RES = (1200, 900)
SAMPLES = 48
BG = 0.012

EXEMPT = ("tube", "module", "gimbal", "recv", "aux", "panel", "window", "tank")


# ============================================================ 工具
def vbounds(objs):
    C.refresh()
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def _box(name, loc, size, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(mat)
    return ob


def _group(objs, prefix, location, rot_z_deg=0.0):
    """把一组新建对象挂到该件的空物体下，改名并整体就位。

    **不设** matrix_parent_inverse：它用于"保持子对象原世界位置"，设成 holder 矩阵的逆
    会把位移与旋转整个抵消。
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
def build_bay():
    """合束器平台舱＝共机架本体：金色箱体（同族 MLI 材质与褶皱）。"""
    mat = C.new_material("MAT_BAY_MLI", C.COL_BUS, 0.62, 0.15)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0.0, 0.0, BAY_TOP - BAY_T / 2))
    bay = bpy.context.active_object
    bay.name = "bay"
    bay.dimensions = (BAY_W, BAY_L, BAY_T)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bay.data.materials.append(mat)
    C.add_noise_bump(mat)
    C.apply_bevel(bay, BAY_BEVEL, 2)
    return bay


def build_bay_fittings():
    """平台舱的附件：**展开的太阳翼**（SADA＋4 板，法向朝尾部 −Z）与 ±X 侧面储箱。

    翼机构由 wing.py 生成（阶段三/四同一份实现）；这里把翼根放到平台舱的 ±X 面上
    （平台舱宽 1.24 > 单体舱宽 1.20）。
    """
    wings = []
    for side in (+1.0, -1.0):
        wings += W.build_wing(side, BAY_W / 2, state="deployed")
    tanks = C.build_tanks(face="X±")
    # 挂到平台舱中层高度：单体构件以舱心为原点，平台舱中层即 BAY_TOP − BAY_T/2
    _group(wings + tanks, "cmb", (0.0, 0.0, BAY_TOP - BAY_T / 2), 0.0)
    return wings + tanks


def build_lock_mech(side, state="locked"):
    """锁紧与释放分离机构（支座＋卡爪抽象，机构件材质）：装在承力筒 ±Y 筒面。

    组合体（``locked``）：卡爪臂伸到集光器舱内侧面（跨距 LOCK_REACH ≈0.63 m），承担发射载荷；
    分布式（``released``）：臂退回，仅留支座与短销（凸出筒面 ≤0.35 m）。
    """
    mat = C.new_material("MAT_LOCK_GREY", (0.52, 0.53, 0.55), 0.40, 1.0)
    y0 = side * (TUBE_D_LOAD / 2)                       # 筒面
    parts = [_box("LOCK_%s_base" % ("P" if side > 0 else "N"),
                  (0.0, y0 + side * 0.05, 0.0), (0.16, 0.10, 0.14), mat)]
    if state == "locked":
        arm_len = LOCK_REACH - 0.10
        parts.append(_box("LOCK_%s_arm" % ("P" if side > 0 else "N"),
                          (0.0, y0 + side * (0.10 + arm_len / 2), 0.0),
                          (0.09, arm_len, 0.09), mat))
        for dx in (-0.075, 0.075):                      # 卡爪两片
            parts.append(_box("LOCK_%s_jaw%s" % ("P" if side > 0 else "N",
                                                 "a" if dx < 0 else "b"),
                              (dx, y0 + side * (LOCK_REACH - 0.07), 0.0),
                              (0.06, 0.14, 0.20), mat))
    else:
        parts.append(_box("LOCK_%s_pin" % ("P" if side > 0 else "N"),
                          (0.0, y0 + side * 0.14, 0.0), (0.07, 0.08, 0.07), mat))
    return parts


def build_collector(prefix, y, rot_z_deg):
    """一台集光器：舱底直接落在平台舱顶面；**太阳翼折叠平贴**（反馈 03 §二.4）。"""
    objs = [C.build_bus(), C.build_tube()]
    objs += C.build_gimbals()
    objs += C.build_tanks()                    # 两只贴 +Y 长边（朝相邻器）
    for side in (+1.0, -1.0):                  # 收拢：4 方板摞贴 ±X 舱板（十字拓扑折叠）
        objs += W.build_wing(side, (C.BUS_W + C.BUS_SHORT_W) / 4, state="stowed",
                             topology="cross")
    objs.append(C.build_window())
    return _group(objs, prefix, (0.0, y, BAY_TOP + C.BUS_H / 2), rot_z_deg)


def build_module_group():
    """**承力筒**（上段）＋顶部机构簇：坐舱板段顶面中央，与两镜筒同层成排。

    反馈 07 §三.2：上段＝承力筒 Ø0.60×0.70（旧 Ø0.52 系 module 遗产值）——把 module 的
    筒身在 X/Y 上缩放到外径 0.60、高度仍 0.70；筒内载荷不外显。
    """
    mod = M.build_module()
    cur = max(mod.dimensions[0], mod.dimensions[1])
    f = TUBE_D_LOAD / cur
    mod.scale = (f, f, 1.0)
    bpy.context.view_layer.objects.active = mod
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    grp = [mod] + M.build_recv() + [M.build_aux()]
    lo, _ = vbounds(grp)
    dz = (BAY_TOP - MODULE_EMBED) - lo[2]
    _group(grp, "cmbmod", (0.0, 0.0, dz), 0.0)


def build():
    """装配三器组合体（两层）。返回对象字典。"""
    C.purge_scene()
    build_bay()
    build_bay_fittings()
    # 集光器 B（−Y 侧）：长边朝 +Y（朝合束器）→ 不转
    build_collector("colB", -BASELINE / 2, 0.0)
    # 集光器 A（+Y 侧）：长边朝 −Y（朝合束器）→ 绕 Z 转 180°
    build_collector("colA", +BASELINE / 2, 180.0)
    mod_grp = build_module_group()
    locks = []
    for side in (+1.0, -1.0):                    # 锁紧释放机构×2（承力筒 ±Y 筒面，锁紧态）
        locks += build_lock_mech(side, state="locked")
    lo_m, hi_m = vbounds([objs_ref["cmbmod_module"]] if False else [find_obj("cmbmod_module")])
    dz_lock = (lo_m[2] + hi_m[2]) / 2            # 与承力筒同高安装
    _group(locks, "cmblock", (0.0, 0.0, dz_lock), 0.0)
    objs = {o.name: o for o in bpy.context.scene.objects}
    report(objs)
    return objs


def find_obj(name):
    return bpy.data.objects.get(name)


def report(objs):
    C.refresh()
    meshes = [o for o in objs.values() if o.type == "MESH"]
    lo, hi = vbounds(meshes)
    tubeA, tubeB = objs["colA_tube"], objs["colB_tube"]
    ya = (vbounds([tubeA])[0][1] + vbounds([tubeA])[1][1]) / 2
    yb = (vbounds([tubeB])[0][1] + vbounds([tubeB])[1][1]) / 2
    ymax = 0.0
    for n in ("colA_bus", "colB_bus", "bay"):
        l, h = vbounds([objs[n]])
        ymax = max(ymax, abs(l[1]), abs(h[1]))
    print("[assembly] 两层：下层仅合束器平台舱（%.2f×%.2f×%.2f）；上层 colB｜载荷舱｜colA 同层成排"
          % (BAY_W, BAY_L, BAY_T), flush=True)
    print("[assembly] 集光器翼折叠平贴、合束器翼展开（依据 refs/frames/opt_f040.png）", flush=True)
    print("[assembly] 基线 %.3f m（D2 需 2.3–2.6）" % abs(ya - yb), flush=True)
    print("[assembly] D11 包络：三器舱体外缘 max|y| = %.3f m（需 ≤1.85）；总高 %.3f m（需 ≤%.2f）"
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
        # front 沿 ±X 看：与反馈指定的基准帧 opt_f040.png **同视角**（集光器侧翼平贴与否
        # 只有这个方向看得出），也是"镜筒—载荷舱—镜筒"成排的方向
        ("front", dict(location=(12, 0, center.z), target=(0, 0, center.z), ortho=5.4)),
        ("side", dict(location=(0, 12, center.z), target=(0, 0, center.z), ortho=5.4)),
        ("iso", dict(location=Vector((0.85, -1.0, 0.40)).normalized() * iso_dist
                     + Vector((0, 0, center.z)), target=(0, 0, center.z), lens=lens)),
        ("top", dict(location=(0.0, 0.0, 10), target=(0.0, 0.0, BAY_TOP), ortho=4.4)),
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
