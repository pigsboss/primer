# -*- coding: utf-8 -*-
"""T2b 渲染器适配器（通用）：任意 glb/gltf/stl → **严格双色剪影**，一次会话批量渲。

与 :mod:`primer.imgcmp.adapters.observatory_silhouette` 是两个接法示例：

- 本文件＝**通用模型**接法，相机口径与 `primer-imgrefgen` **同源**（同一套 az/el/roll →
  视向 → 正交取景的算法），所以拿 refgen 渲出来的 `silhouette.png` 当伪基准时，
  真值机位与反求机位在**同一约定**下，误差表才有意义。
- 观象台适配器＝**任务场景**接法，跑任务侧的建场脚本。

**怎么跑**::

    /Applications/Blender.app/Contents/MacOS/Blender -noaudio --background \
      --python src/primer/imgcmp/adapters/model_silhouette.py -- \
      --model <glb> [--scale-to M] --jobs <jobs.json> [--outdir DIR]

`jobs.json` 同观象台适配器：`{"res":[W,H], "shots":[{"az":..,"el":..,"roll":..,"out":".."}]}`。

本文件不含任何任务数值；模型、尺度、机位全部来自命令行。

语言纪律：stdout 中文报告；stderr/异常/选项名/JSON 键英文。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

TOOL = "model-silhouette"
DEFAULT_MARGIN = 1.08          # 与 primer-imgrefgen 的取景边距一致


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog=TOOL)
    parser.add_argument("--model", required=True, help="glb/gltf/stl file to render")
    parser.add_argument("--jobs", required=True, help="batch job JSON")
    parser.add_argument("--outdir", help="batch output directory (manifest lands here)")
    parser.add_argument("--scale-to", type=float, default=None, metavar="M",
                        help="uniformly scale so the longest dimension is M metres")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    return parser.parse_args(argv)


def main():
    import bpy                                                  # noqa: F401
    import mathutils

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = _parse_args(argv)
    if not os.path.isfile(args.model):
        raise SystemExit("model not found: %s" % args.model)
    with open(args.jobs, encoding="utf-8") as handle:
        job = json.load(handle)
    res = tuple(int(v) for v in job.get("res", [480, 480]))
    shots = list(job.get("shots") or [])
    if not shots:
        raise SystemExit("jobs file has no shots")

    t0 = time.time()
    # 与 primer-imgrefgen 相同：从空场景起（避免启动文件/插件带的设置污染渲染口径）
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    path = os.path.abspath(args.model)
    lowered = path.lower()
    if lowered.endswith((".glb", ".gltf")):
        bpy.ops.import_scene.gltf(filepath=path)
    elif lowered.endswith(".stl"):
        try:
            bpy.ops.wm.stl_import(filepath=path)
        except AttributeError:
            bpy.ops.import_mesh.stl(filepath=path)
    else:
        raise SystemExit("unsupported model format: %s" % path)
    # **先拆父级**（保持世界变换）再删非网格对象，最后才做归一化。
    # 根因（2026-10-02 定位）：glTF 导入会建一层 EMPTY 父级；此时给**子对象**直接赋
    # `obj.matrix_world = M @ obj.matrix_world`，Blender 会按父级反解 matrix_basis，
    # 该往返在带旋转/缩放的父链上不闭合——实测 `dish` 壳面被算歪，同机位只剩 6 px
    # （primer-imgrefgen 里同一对象是 21596 px）。primer-imgrefgen 的 import_file() 正是
    # 先 `flatten()` 再归一化，所以没踩到；本适配器此前漏了这一步。
    fresh = [o for o in bpy.data.objects]
    for ob in fresh:
        if ob.parent is not None:
            world = ob.matrix_world.copy()
            ob.parent = None
            ob.matrix_world = world
    for ob in [o for o in fresh if o.type != "MESH"]:
        bpy.data.objects.remove(ob, do_unlink=True)
    objs = [o for o in bpy.data.objects if o.type == "MESH"]
    if not objs:
        raise SystemExit("model has no meshes: %s" % path)

    # 与 primer-imgrefgen 同口径：按最长边归一到 scale_to（bound_box 口径），再取角点取景
    verts = [o.matrix_world @ mathutils.Vector(c) for o in objs for c in o.bound_box]
    lo = mathutils.Vector((min(v.x for v in verts), min(v.y for v in verts),
                           min(v.z for v in verts)))
    hi = mathutils.Vector((max(v.x for v in verts), max(v.y for v in verts),
                           max(v.z for v in verts)))
    norm_dim = max(hi - lo)
    scale = 1.0
    if args.scale_to and norm_dim > 0:
        scale = float(args.scale_to) / norm_dim
    center = (lo + hi) * 0.5
    for obj in objs:
        obj.matrix_world = (mathutils.Matrix.Scale(scale, 4)
                            @ mathutils.Matrix.Translation(-center) @ obj.matrix_world)
    bpy.context.view_layer.update()
    # 取景角点＝**每个网格各自的 bound_box 角点**（与 primer-imgrefgen 逐字同口径：
    # frame_camera 对这些点取均值当取景中心，所以"逐网格角点"与"全局 AABB"结果不同）
    pts = []
    for ob in objs:
        for corner in ob.bound_box:
            p = ob.matrix_world @ mathutils.Vector(corner)
            pts.append((p.x, p.y, p.z))

    cam_data = bpy.data.cameras.new("T2B_CAM")
    cam = bpy.data.objects.new("T2B_CAM", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam

    scene.render.resolution_x, scene.render.resolution_y = int(res[0]), int(res[1])
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.dither_intensity = 0.0
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light = "FLAT"
    shading.color_type = "SINGLE"
    shading.single_color = (1.0, 1.0, 1.0)
    shading.show_shadows = False
    shading.show_cavity = False
    shading.show_object_outline = False
    shading.show_specular_highlight = False
    scene.display.render_aa = "OFF"
    if scene.world is None:                      # Workbench 的黑底走 world.color
        scene.world = bpy.data.worlds.new("T2B_WORLD")
    scene.world.color = (0.0, 0.0, 0.0)
    shading.background_type = "WORLD"
    shading.background_color = (0.0, 0.0, 0.0)

    records = []
    for shot in shots:
        az, el, roll = float(shot["az"]), float(shot["el"]), float(shot.get("roll", 0.0))
        info = _frame(cam, pts, az, el, roll, res, args.margin)
        out = os.path.abspath(shot["out"])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        t1 = time.time()
        scene.render.filepath = out
        bpy.ops.render.render(write_still=True)
        records.append({"az": az, "el": el, "roll": roll, "out": out,
                        "render_seconds": round(time.time() - t1, 4), "camera": info})
        print("[silhouette] az %+7.1f el %+6.1f roll %+6.1f → %s (%.3fs)"
              % (az, el, roll, os.path.basename(out), records[-1]["render_seconds"]),
              flush=True)

    manifest = {
        "tool": TOOL, "model": os.path.abspath(args.model), "res": [int(res[0]), int(res[1])],
        "scale_to_m": args.scale_to, "scale_applied": round(scale, 9),
        "normalized_dim_m": round(norm_dim * scale, 6),
        "camera_convention": "same as primer-imgrefgen: view dir "
                             "(sin az cos el, -cos az cos el, sin el), ortho, roll about the "
                             "view axis; framing from the projected bbox corners + margin",
        "margin": args.margin, "build_seconds": round(time.time() - t0, 4),
        "render_seconds_total": round(sum(r["render_seconds"] for r in records), 4),
        "shots": records,
    }
    outdir = args.outdir or os.path.dirname(records[0]["out"])
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "render_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print("[silhouette] %d 张剪影，建场 %.2fs，渲染 %.2fs，清单 → %s"
          % (len(records), manifest["build_seconds"], manifest["render_seconds_total"],
             os.path.join(outdir, "render_manifest.json")), flush=True)


def _frame(cam, corners, az, el, roll, res, margin):
    """与 primer-imgrefgen 的 ``frame_camera`` 同一套口径（正交）。"""
    import mathutils

    w, h = res
    azr, elr = math.radians(az), math.radians(el)
    u = mathutils.Vector((math.sin(azr) * math.cos(elr),
                          -math.cos(azr) * math.cos(elr),
                          math.sin(elr)))
    center = mathutils.Vector((sum(c[0] for c in corners) / len(corners),
                               sum(c[1] for c in corners) / len(corners),
                               sum(c[2] for c in corners) / len(corners)))
    quat = (-u).to_track_quat("-Z", "Y")
    quat = quat @ mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(roll))
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = quat
    inv = quat.inverted()
    xs, ys, zs = [], [], []
    for c in corners:
        v = inv @ (mathutils.Vector(c) - center)
        xs.append(v.x)
        ys.append(v.y)
        zs.append(v.z)
    ex = (max(xs) - min(xs)) / 2.0 * margin
    ey = (max(ys) - min(ys)) / 2.0 * margin
    depth = max(zs) - min(zs)
    aspect = w / float(h)
    data = cam.data
    data.sensor_fit = "HORIZONTAL"
    data.type = "ORTHO"
    data.ortho_scale = max(2.0 * ex, 2.0 * ey * aspect)
    dist = max(4.0 * (ex + ey) + depth, 1e-6) + 10.0
    data.clip_start = max(1e-3, 0.01 * dist)
    data.clip_end = dist * 4.0 + 100.0
    cam.location = center + u * dist
    return {"kind": "ortho", "azimuth_deg": az, "elevation_deg": el, "roll_deg": roll,
            "view_dir": [round(u.x, 6), round(u.y, 6), round(u.z, 6)],
            "distance_m": round(dist, 6), "ortho_scale_m": round(data.ortho_scale, 6),
            "center": [round(center.x, 6), round(center.y, 6), round(center.z, 6)],
            "res": [int(w), int(h)]}


if __name__ == "__main__":
    main()
