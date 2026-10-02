# -*- coding: utf-8 -*-
"""T2b 渲染器适配器：2034 观象台场景 → **严格双色剪影**（白对象／黑底、关抗锯齿、正交）。

**它不是 T2b 的一部分，而是"任何可渲染场景"接进 `primer-imgpose` 的示例接法**：
`primer-imgpose solve` 只认一个渲染器命令模板，本文件就是给观象台写的那条命令的实现。

**怎么跑**（由 `--renderer-cmd` 模板驱动，本文件必须在 Blender 里执行）::

    /Applications/Blender.app/Contents/MacOS/Blender -noaudio --background \
      --python src/primer/imgcmp/adapters/observatory_silhouette.py -- \
      --task-root <任务根> --jobs <jobs.json> [--target COL0]

`jobs.json`（由 `primer-imgpose` 生成）::

    {"res": [480, 480], "ortho": true, "margin": 2.2,
     "shots": [{"az": 90.0, "el": -32.0, "roll": 40.0, "out": "/abs/path.png"}, ...]}

**相机口径**（写进 `render_manifest.json`，供 `camera.json` 一键重渲）：

- 目标件＝`--target`（任务侧的器体标签，如 `COL0`），取景＝**以其网格包围盒中心为中心、
  固定正交尺度**（`ortho_scale = margin × 包围球直径`）——尺度与中心不随视角变化，
  所以不同 (az,el,roll) 的剪影天然可比。
- 视向：`dir = (cos el·cos az, cos el·sin az, sin el)`，相机位于 `中心 + dir·距离`，
  望向中心；`roll` 为**绕视轴的相机滚转**（正值＝相机逆时针自转，画幅内容顺时针转）。
- 场景里**只保留目标件与器体**：束体、星空、底图天体一律 `hide_render`，剪影里不出现。

本文件**不含任何任务数值**：全部几何/器体标签都来自 `--task-root` 下的任务脚本。
它只按路径 import 任务脚本，不修改任务侧任何文件。

语言纪律：stdout 中文报告；stderr/异常/选项名/JSON 键英文。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

TOOL = "observatory-silhouette"


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog=TOOL)
    parser.add_argument("--task-root", required=True,
                        help="task directory holding array2034.py / meayin_common.py")
    parser.add_argument("--jobs", help="batch job JSON produced by primer-imgpose")
    parser.add_argument("--target", default=None,
                        help="craft tag to frame (task-side identifier, e.g. COL0)")
    parser.add_argument("--az", type=float, default=0.0)
    parser.add_argument("--el", type=float, default=0.0)
    parser.add_argument("--roll", type=float, default=0.0)
    parser.add_argument("--res", default="480,480")
    parser.add_argument("--out", help="single-shot output PNG")
    parser.add_argument("--outdir", help="batch output directory (manifest lands here)")
    return parser.parse_args(argv)


def _jobs_of(args):
    if args.jobs:
        with open(args.jobs, encoding="utf-8") as handle:
            job = json.load(handle)
        res = tuple(int(v) for v in job.get("res", [480, 480]))
        shots = list(job.get("shots") or [])
        if not shots:
            raise SystemExit("jobs file has no shots: %s" % args.jobs)
        return job, res, shots
    if not args.out:
        raise SystemExit("single-shot mode needs --out")
    res = tuple(int(v) for v in str(args.res).split(","))
    return {"res": list(res), "margin": 2.2}, res, [
        {"az": args.az, "el": args.el, "roll": args.roll, "out": args.out}]


def main():
    import bpy                                                  # noqa: F401
    import mathutils

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = _parse_args(argv)
    task_root = os.path.abspath(args.task_root)
    if not os.path.isdir(task_root):
        raise SystemExit("task root not found: %s" % task_root)
    sys.path.insert(0, task_root)

    import array2034 as A                                        # noqa: E402
    import meayin_common as K                                    # noqa: E402

    job, res, shots = _jobs_of(args)
    margin = float(job.get("margin", 2.2))
    target = args.target or job.get("target") or "COL0"

    t0 = time.time()
    data, cam, _views = A.build_scene()
    build_s = time.time() - t0

    # 只留**目标件**：其余器体、束体、星空、底图天体一律不参与渲染
    hidden = []
    groups = list((data.get("relay") or {}).values()) + list((data.get("star") or {}).values())
    objs = [o for grp in groups for o in grp.get("grp", [])]
    sky = data.get("sky") or {}
    for name in sky.get("objects", []) or []:
        obj = bpy.data.objects.get(name)
        if obj:
            objs.append(obj)
    for tag, res_ in (data.get("craft") or {}).items():
        if tag != target:
            objs.extend(res_.get("objs") or [])
    cmb = data.get("cmb") or {}
    if cmb.get("id") != target:
        objs.extend(cmb.get("objs") or [])
    for obj in objs:
        if obj.hide_render is False:
            obj.hide_render = True
            hidden.append(obj.name)
    layout = data.get("layout_obj")
    if layout is not None:
        layout.hide_render = True

    craft = (data.get("craft") or {}).get(target)
    if craft is None and (data.get("cmb") or {}).get("id") == target:
        craft = data.get("cmb")
    if craft is None:
        raise SystemExit("target craft %r not found in scene" % target)
    pts = K._extent_points([o for o in craft["objs"] if o.type == "MESH"])
    center = mathutils.Vector((sum(p.x for p in pts) / len(pts),
                               sum(p.y for p in pts) / len(pts),
                               sum(p.z for p in pts) / len(pts)))
    radius = max((mathutils.Vector(p) - center).length for p in pts) or 1.0

    scene = bpy.context.scene
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

    ortho_scale = margin * radius                       # 直径 = 2·radius，再乘取景余量
    distance = max(40.0 * radius, 1e-3)
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = ortho_scale
    cam.data.clip_start = max(1e-3, 0.5 * radius)
    cam.data.clip_end = distance + 40.0 * radius

    records = []
    for shot in shots:
        az, el, roll = float(shot["az"]), float(shot["el"]), float(shot.get("roll", 0.0))
        azr, elr = math.radians(az), math.radians(el)
        direction = mathutils.Vector((math.cos(elr) * math.cos(azr),
                                      math.cos(elr) * math.sin(azr),
                                      math.sin(elr)))
        quat = (-direction).to_track_quat("-Z", "Y")
        # 相机绕视轴滚转 roll：正值＝相机逆时针自转 → 画幅内容顺时针转（与 2D 旋转同号）
        quat = quat @ mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(roll))
        cam.rotation_mode = "QUATERNION"
        cam.rotation_quaternion = quat
        cam.location = center + direction * distance
        out = shot["out"]
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        t1 = time.time()
        scene.render.filepath = os.path.abspath(out)
        bpy.ops.render.render(write_still=True)
        records.append({
            "az": az, "el": el, "roll": roll, "out": os.path.abspath(out),
            "render_seconds": round(time.time() - t1, 4),
            "camera": {
                "kind": "ortho", "ortho_scale_m": round(ortho_scale, 6),
                "distance_m": round(distance, 6),
                "target_center": [round(center.x, 6), round(center.y, 6), round(center.z, 6)],
                "target_radius_m": round(radius, 6),
                "view_dir": [round(direction.x, 6), round(direction.y, 6),
                             round(direction.z, 6)],
                "roll_deg": roll, "res": [int(res[0]), int(res[1])],
                "target": target,
            },
        })
        print("[silhouette] az %+7.1f el %+6.1f roll %+6.1f → %s (%.3fs)"
              % (az, el, roll, os.path.basename(out), records[-1]["render_seconds"]),
              flush=True)

    manifest = {
        "tool": TOOL,
        "task_root": task_root,
        "target": target,
        "res": [int(res[0]), int(res[1])],
        "ortho_scale_m": round(ortho_scale, 6),
        "distance_m": round(distance, 6),
        "view_transform": "Standard", "render_engine": "BLENDER_WORKBENCH",
        "color_type": "SINGLE(1,1,1)", "background": [0, 0, 0], "aa": "OFF",
        "hidden_objects": sorted(hidden),
        "build_seconds": round(build_s, 4),
        "render_seconds_total": round(sum(r["render_seconds"] for r in records), 4),
        "shots": records,
    }
    outdir = args.outdir or (os.path.dirname(os.path.abspath(shots[0]["out"])))
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "render_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print("[silhouette] %d 张剪影，建场 %.2fs，渲染 %.2fs，清单 → %s"
          % (len(records), build_s, manifest["render_seconds_total"],
             os.path.join(outdir, "render_manifest.json")), flush=True)


if __name__ == "__main__":
    main()
