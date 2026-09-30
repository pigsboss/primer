# -*- coding: utf-8 -*-
"""bench_compare.py — 材质基准对比图（规格 §五 v1.7 新增环节）。

用法：
    blender --background --python bench_compare.py
输出：out/formation/mat_bench.png（同一光照、同一相机、同一帧内并排：本工程器体 | NASA jwst.glb）

要点
----
* **同光照**：直接复用 `formation.build_render_env()` —— 同一 sun_dir／能量／星场／Cycles／
  AgX(Medium High Contrast)／Fog Glow，与五张交付预览完全一致（规格 §五）。
* **同帧**：两个模型在同一场景、同一相机距离下渲染，故**每米像素数相同**，两器的表面特征
  像素尺度可直接互比（这也是"褶皱尺度逐项比对"的依据）。
* **尺度处理**：jwst.glb 为真实尺度（展开态 ~20 m），为并排可读按固定倍率缩到 ~6 m，
  倍率记入 report 台账与交付说明（缩放的只是 JWST，本工程器体保持真实尺度）。
* 标签用 ASCII（Blender 内置字体不含中文字形），并做自发光，暗场里可读。
"""
import json
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import collector as C
import formation as F

OUT_PNG = os.path.join(HERE, "out", "formation", "mat_bench.png")
JWST = os.path.abspath(os.path.join(HERE, "..", "..", "assets", "models", "spacecraft", "jwst.glb"))
JWST_TARGET_M = 6.0          # JWST 缩到约 6 m 以便并排（倍率记账）
MINE_AT = (-4.2, 0.0, 0.0)   # 本工程器体摆位（真实尺度）
JWST_AT = (4.6, 0.0, 0.0)
JWST_YAW_DEG = 90.0          # 模型自带的朝向：绕 Z 转 90° 才把金镜对着日照
CAM_DIR = (0.55, 0.72, 0.42)  # 机位取**日照同侧**（+X/+Y 上方），否则只看得到背光面


def bbox(objs):
    """只算网格：GLB 导入会带空物体，而空物体的 bound_box 是默认单位盒，会把包围盒撑爆。"""
    los, his = [], []
    for o in [o for o in objs if o.type == "MESH"]:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            los.append(w)
            his.append(w)
    lo = Vector((min(v[i] for v in los) for i in range(3)))
    hi = Vector((max(v[i] for v in his) for i in range(3)))
    return lo, hi


def shift_into(objs, target_center):
    """把一组对象整体平移到指定中心（自带层级不动，只挪根对象）。"""
    lo, hi = bbox(objs)
    delta = Vector(target_center) - (lo + hi) / 2
    for o in objs:
        if o.parent is None:
            o.location = o.location + delta
    C.refresh()


def scale_group(objs, factor):
    for o in objs:
        if o.parent is None:
            o.scale = [s * factor for s in o.scale]
            o.location = [v * factor for v in o.location]
    C.refresh()


def label(text, location, size, cam):
    bpy.ops.object.text_add(location=location)
    t = bpy.context.active_object
    t.name = "label_" + text.split()[0]
    t.data.body = text
    t.data.size = size
    t.data.align_x = "CENTER"
    t.rotation_euler = cam.rotation_euler.copy()      # 面向相机（billboard）
    m = bpy.data.materials.new("MAT_label")
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (1, 1, 1, 1)
    if "Emission Color" in b.inputs:
        b.inputs["Emission Color"].default_value = (1, 1, 1, 1)
        b.inputs["Emission Strength"].default_value = 1.2
    t.data.materials.append(m)
    return t


def main():
    if not os.path.exists(JWST):
        print("[bench] 缺 jwst.glb：%s" % JWST, flush=True)
        return
    F.RANDOM_LOG.clear()
    C.purge_scene()
    C.refresh()

    mine = list(C.build(purge=False).values())         # 本工程器体（真实尺度）
    F._rebuild_materials()                             # 套 §四 参数表（与交付预览同一材质）
    lo, hi = bbox(mine)
    shift_into(mine, MINE_AT)

    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=JWST)
    jwst = [o for o in bpy.data.objects if o not in before]
    jlo, jhi = bbox(jwst)
    span = max(jhi - jlo)
    factor = JWST_TARGET_M / span if span else 1.0
    for o in jwst:                                   # 把金镜转向日照方向，才读得到金属高光
        if o.parent is None:
            o.rotation_euler = (o.rotation_euler[0], o.rotation_euler[1],
                                o.rotation_euler[2] + math.radians(JWST_YAW_DEG))
    C.refresh()
    scale_group(jwst, factor)
    shift_into(jwst, JWST_AT)

    F.build_render_env(final=False)                    # 同一光照／星场／Cycles／AgX／Glare
    cam = bpy.data.objects.get("RIG_Cam") or F.add_camera()
    all_objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    lo, hi = bbox(all_objs)
    center = (lo + hi) / 2
    width = (hi[0] - lo[0]) * 1.06
    lens = 40.0
    dist = width / (2 * math.tan(math.atan(36.0 / (2 * lens))))
    F.point(cam, location=center + Vector(CAM_DIR).normalized() * dist,
            target=center, lens=lens)

    label("OURS (real scale)", Vector((MINE_AT[0], MINE_AT[1], hi[2] + 0.5)), 0.34, cam)
    label("NASA JWST jwst.glb (x%.2f)" % factor,
          Vector((JWST_AT[0], JWST_AT[1], hi[2] + 0.5)), 0.34, cam)

    sc = bpy.context.scene
    sc.render.filepath = OUT_PNG
    bpy.ops.render.render(write_still=True)
    F.MATERIAL_LOG["bench"] = {
        "output": os.path.relpath(OUT_PNG, HERE), "jwst": os.path.relpath(JWST, HERE),
        "jwst_scale_factor": round(factor, 4), "jwst_span_m": round(JWST_TARGET_M, 3),
        "same_lighting_as_previews": True, "camera_distance_m": round(dist, 3),
        "note": "同帧同距离＝每米像素数相同，表面特征像素尺度可直接互比",
    }
    with open(os.path.join(HERE, "out", "formation", "bench_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(F.MATERIAL_LOG["bench"], f, ensure_ascii=False, indent=2)
    print("[bench] 出图 %s（JWST 缩放 ×%.3f，机位距离 %.1f m）"
          % (OUT_PNG, factor, dist), flush=True)


if __name__ == "__main__":
    main()
