# -*- coding: utf-8 -*-
"""生成 ``hex_plate.glb``：六边形板（主镜镜坯），供 refgen 受控缺陷对 ``mirror_hex`` 用。

用法（Blender 无头模式）::

    Blender -noaudio --background --python make_hex_plate.py -- hex_plate.glb

几何：六棱柱。底面六边形顶点在 0°,60°,…,300°（尖顶朝 ±X），再按 (a, b) 各向异性
缩放 → **六条直边**、对边比 ``b/a = 1.35``；板面落在 XY 平面（厚 0.185 沿 Z），
与 ``disc.glb`` 的原生朝向**同一约定**，因此拼装时两者都写 ``rot_deg: [90, 0, 0]``
即可正视，spec 之间**只差 ``part:`` 一行**。正视包围盒 2.40(X) × 2.81(Y)。

为什么不是正六边形：T2a 的 ``circle`` 判据只用两个量（``circle_fill = 4A/(πwh) ∈
[0.88, 1.12]`` 与 PCA 涨宽比 ≤ 1.25）。正六边形的 ``circle_fill`` = 1.103（在带内）、
六重对称使其 PCA 涨宽比 = 1.00，且与圆盘经受同一投影压缩因子 —— 两者在数学上不可分，
``shape_class`` 会给同一个 ``circle``。把轮廓自身拉长到涨宽比 1.35，T2a 才判 ``other``；
这是 T2a 形状类的能力边界，已登记在 ``docs/guides/imgcmp_triage.md``。
"""

from __future__ import annotations

import math
import sys

import bpy

R = 1.0
SCALE_X = 1.2          # 板面 X 半宽 → 正视宽 2.40
SCALE_Y = 1.62         # 板面 Y 半高 → 正视高 2.81（对边比 1.35）
HALF_THICK = 0.0925    # 板半厚（总厚 0.185）


def build_mesh():
    verts = []
    for sign in (-1.0, 1.0):
        for k in range(6):
            angle = math.radians(60.0 * k)
            verts.append((SCALE_X * R * math.cos(angle), SCALE_Y * R * math.sin(angle),
                          sign * HALF_THICK))
    faces = [list(range(6))[::-1], list(range(6, 12))]
    for k in range(6):
        k2 = (k + 1) % 6
        faces.append([k, k2, 6 + k2, 6 + k])
    return verts, faces


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = argv[0] if argv else "hex_plate.glb"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    verts, faces = build_mesh()
    mesh = bpy.data.meshes.new("hex_plate")
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    mesh.update()
    obj = bpy.data.objects.new("hex_plate", mesh)
    bpy.context.collection.objects.link(obj)

    # 不给材质：与 box/bar/disc.glb 一致，用 Blender 默认表面。
    # （首轮曾给"金属镜面"材质 metallic=1/rough=0.12：黑底单主光下镜面把黑环境反射回来，
    #   EEVEE 渲染里这块板几乎与背景同色，T2a 前景掩膜整个丢掉它 → 黑底表只剩 4 件。
    #   受控对要求"只有镜形不同"，材质必须与其它部件同一口径。）

    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
    print("[make_hex_plate] wrote %s" % out)


if __name__ == "__main__":
    main()
