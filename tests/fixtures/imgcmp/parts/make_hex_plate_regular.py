# -*- coding: utf-8 -*-
"""生成 ``hex_plate_regular.glb``：**正六边形**板（主镜镜坯），供 refgen 受控缺陷对
``mirror_hex_regular`` 用。

用法（Blender 无头模式）::

    Blender -noaudio --background --python make_hex_plate_regular.py -- hex_plate_regular.glb

几何：**正**六棱柱。底面正六边形顶点在 0°,60°,…,300°（尖顶朝 ±X），各向同性缩放
（``SCALE_X == SCALE_Y``）→ 六条等长直边、对边比 **1.00**；板面落在 XY 平面（厚 0.185 沿 Z），
与 ``disc.glb`` 的原生朝向**同一约定**，因此拼装时两者都写 ``rot_deg: [90, 0, 0]``
即可正视，spec 之间**只差 ``part:`` 一行**。正视包围盒 2.40(X) × 2.81(Y)。

为什么单独造一块：T2a 原先的 ``circle`` 判据只看"圆度 + 长宽比"两个量，正六边形的
``circle_fill = 1.103``（在带内）、六重对称使 PCA 涨宽比 = 1.00——与圆盘数学上不可分。
第 5 轮给 T2a 补了 **polygon 判据**（凸包抽稀后的直边数 + 残差 + 凸包充实度），
本件就是它的验收样本：正六边形必须判 ``polygon/hexagon``，圆盘仍判 ``circle``。
"""

from __future__ import annotations

import math
import sys

import bpy

R = 1.0
SCALE_X = 1.2          # 板面 X 半宽 → 正视宽 2.40
SCALE_Y = 1.2          # 板面 Y 半高 → 正视高 2.40（**正**六边形，对边比 1.00）
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
    mesh = bpy.data.meshes.new("hex_plate_regular")
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    mesh.update()
    obj = bpy.data.objects.new("hex_plate_regular", mesh)
    bpy.context.collection.objects.link(obj)

    # 不给材质：与 box/bar/disc.glb 一致，用 Blender 默认表面。
    # （首轮曾给"金属镜面"材质 metallic=1/rough=0.12：黑底单主光下镜面把黑环境反射回来，
    #   EEVEE 渲染里这块板几乎与背景同色，T2a 前景掩膜整个丢掉它 → 黑底表只剩 4 件。
    #   受控对要求"只有镜形不同"，材质必须与其它部件同一口径。）

    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
    print("[make_hex_plate_regular] wrote %s" % out)


if __name__ == "__main__":
    main()
