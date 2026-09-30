# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——合束器（Combiner）单体建模脚本（阶段二）。

用法：
    blender --background --python combiner.py      # 建模 + 四视角自检渲染到 out/combiner/

同族约定（阶段二任务说明）：平台舱、金色 MLI 材质、太阳翼形制、散热器形制**一律沿用
collector.py**——本脚本直接 import 并调用它的 build_bus / build_tanks / build_panels，
不复制常量、不另起配色，这样两边不可能漂移。

与集光器的唯一结构差异：**没有镜筒**，取而代之是舱顶居中的深色圆柱载荷舱（明显更细更矮），
其顶面坐接收折转镜 ×2（朝 ±X，即两侧集光器方向）与敏感器 ×1。
"""
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import collector as C          # 同族构件与材质的唯一来源

OUT_DIR = os.path.join(HERE, "out", "combiner")

# ============================================================ 差异项常量（共用项取 C.*）
# ---- 载荷舱（module）：深灰近黑圆柱，居中立于舱顶，显著矮细于集光器镜筒 ----
MODULE_R = 0.240          # 舱体半径（顶沿外凸后外径 0.50 = 0.417×舱宽）
MODULE_H = 0.700          # 总高（含顶沿）。原 0.540 与组合体判据 D5 冲突：载荷舱底面贴甲板时，
                          # 其顶面须与集光器舱顶面高差 ≤0.3 m，而集光器舱高 0.90 → 露高须 ≥0.60。
                          # 0.70（露出 0.68）同时满足阶段二 C3（0.756∈[0.5,0.9]）与阶段三 D5（高差 0.22）。
MODULE_EMBED = 0.020      # 根部嵌入舱顶（C4 允许嵌入 ≤0.3 m）
MODULE_RIM_H = 0.070      # 顶沿高（参考图中筒口一圈明显外凸）
MODULE_RIM_OVER = 0.018   # 顶沿外凸量
COL_MODULE = (0.045, 0.046, 0.050)   # 深灰近黑（C7：三通道 <0.35）

# ---- 顶部机构簇：接收折转镜 ×2（朝 ±X）＋敏感器 ×1，合计 3 台 ----
RECV_X = 0.140            # 两台接收机构在载荷舱顶面的 ±X 位置
RECV_BASE = (0.130, 0.130, 0.045)
RECV_R = 0.042
RECV_L = 0.100
RECV_TILT = 35.0          # 斜置角（度），朝外侧倾倒
RECV_EMBED = 0.002        # 底座略微嵌入顶面，保证接触（C9 判"坐载荷舱顶"）
AUX_Y = -0.150            # 敏感器位置（载荷舱顶面 −Y 侧）
AUX_BASE = (0.085, 0.085, 0.038)
AUX_R = 0.030
AUX_L = 0.060

# ---- 自检渲染 ----
RES = (1000, 750)
SAMPLES = 32

REQUIRED = ("bus", "module", "recv_01", "recv_02", "aux_01",
            "tank_01", "tank_02", "panel_X1", "panel_X2",
            "panel_-X1", "panel_-X2")


# ============================================================ 构件
def build_module():
    """载荷舱：深灰近黑圆柱 + 顶沿，居中立于舱顶，根部嵌入 MODULE_EMBED。"""
    z0 = C.BUS_H / 2 - MODULE_EMBED
    mat = C.new_material("MAT_MODULE_GREY", COL_MODULE, 0.45, 0.25)

    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=MODULE_R, depth=MODULE_H,
                                        location=(0.0, 0.0, z0 + MODULE_H / 2))
    body = bpy.context.active_object
    body.name = "MOD_body"

    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=MODULE_R + MODULE_RIM_OVER,
                                        depth=MODULE_RIM_H,
                                        location=(0.0, 0.0, z0 + MODULE_H - MODULE_RIM_H / 2))
    rim = bpy.context.active_object
    rim.name = "MOD_rim"

    bpy.ops.object.select_all(action="DESELECT")
    body.select_set(True)
    rim.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    module = bpy.context.active_object
    module.name = "module"
    module.data.materials.clear()
    module.data.materials.append(mat)
    # 原点归到几何中心：verify 用 location ± dimensions/2 量"落在舱顶"与"高出舱顶"
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    return module


def build_recv():
    """接收折转镜装置 ×2：底座方块 + 朝外侧倾倒的圆柱，坐载荷舱顶面，分处 ±X。"""
    mat = C.new_material("MAT_RECV_GREY", C.COL_GIMBAL, 0.45, 0.35)
    mod_top = C.BUS_H / 2 - MODULE_EMBED + MODULE_H
    out = []
    for i, sign in enumerate((+1, -1), start=1):
        px = sign * RECV_X
        bpy.ops.mesh.primitive_cube_add(
            size=1, location=(px, 0.0, mod_top + RECV_BASE[2] / 2 - RECV_EMBED))
        base = bpy.context.active_object
        base.dimensions = RECV_BASE
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        bpy.ops.mesh.primitive_cylinder_add(
            vertices=16, radius=RECV_R, depth=RECV_L,
            location=(px + sign * 0.022, 0.0, mod_top + RECV_BASE[2] + RECV_L * 0.40))
        cyl = bpy.context.active_object
        # 绕 Y 轴倾倒：+X 侧朝 +X 倾，−X 侧朝 −X 倾 → 两台"背靠背"分处两侧
        cyl.rotation_euler = (0.0, -sign * math.radians(RECV_TILT), 0.0)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

        bpy.ops.object.select_all(action="DESELECT")
        base.select_set(True)
        cyl.select_set(True)
        bpy.context.view_layer.objects.active = base
        bpy.ops.object.join()
        ob = bpy.context.active_object
        ob.name = "recv_%02d" % i
        ob.data.materials.clear()
        ob.data.materials.append(mat)
        # join 后原点仍在**底座**的原点，而 location±dimensions/2 只有原点居中时才等于真实端面。
        # C9 正是按 location.z - dimensions[2]/2 判"坐载荷舱顶"，不归心会差 5.5 mm 判 FAIL。
        bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
        out.append(ob)
    return out


def build_aux():
    """敏感器 ×1：更小的底座 + 直立小圆柱，散布在载荷舱顶面。"""
    mat = C.new_material("MAT_AUX_GREY", C.COL_GIMBAL, 0.5, 0.3)
    mod_top = C.BUS_H / 2 - MODULE_EMBED + MODULE_H
    bpy.ops.mesh.primitive_cube_add(
        size=1, location=(0.0, AUX_Y, mod_top + AUX_BASE[2] / 2 - RECV_EMBED))
    base = bpy.context.active_object
    base.dimensions = AUX_BASE
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    bpy.ops.mesh.primitive_cylinder_add(
        vertices=16, radius=AUX_R, depth=AUX_L,
        location=(0.0, AUX_Y, mod_top + AUX_BASE[2] + AUX_L / 2 - RECV_EMBED))
    cyl = bpy.context.active_object

    bpy.ops.object.select_all(action="DESELECT")
    base.select_set(True)
    cyl.select_set(True)
    bpy.context.view_layer.objects.active = base
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = "aux_01"
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    return ob


def build(stowed=False, tank_face="Y+"):
    """生成合束器整器。同族构件直接复用 collector.py 的函数。

    stowed=True 时太阳翼按组合体状态收拢（见 collector.build_panels）；
    tank_face="X±" 时储箱分贴 ±X 侧面（组合体判据 D9 要求）。
    """
    C.purge_scene()
    C.refresh() if hasattr(C, "refresh") else None
    objs = {}
    objs["bus"] = C.build_bus()
    for r in C.build_tanks(face=tank_face):
        objs[r.name] = r
    panels, centers = C.build_panels(stowed=stowed)
    for p in panels:
        objs[p.name] = p
    objs["module"] = build_module()
    for r in build_recv():
        objs[r.name] = r
    objs["aux_01"] = build_aux()
    report(objs, centers)
    return objs


def report(objs, centers):
    bus, module = objs["bus"], objs["module"]
    bus_w = max(bus.dimensions[0], bus.dimensions[1])
    bus_h = bus.dimensions[2]
    mod_d = max(module.dimensions[0], module.dimensions[1])
    bus_top = bus.location.z + bus_h / 2
    mod_over = (module.location.z + module.dimensions[2] / 2) - bus_top
    xs = [centers[n] for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")]
    ws = [objs[n].dimensions[0] for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")]
    span = max(x + w / 2 for x, w in zip(xs, ws)) - min(x - w / 2 for x, w in zip(xs, ws))
    recv = [objs["recv_01"], objs["recv_02"]]
    gap = (module.location.z - module.dimensions[2] / 2) - bus_top
    print("[combiner] 舱 %.2f×%.2f×%.2f（沿用 collector）  载荷舱 Φ%.3f 总高 %.3f"
          % (bus.dimensions[0], bus.dimensions[1], bus_h, mod_d, module.dimensions[2]), flush=True)
    print("[combiner] C2 载荷舱外径/舱宽 = %.3f (需 0.35–0.50)" % (mod_d / bus_w), flush=True)
    print("[combiner] C3 高出舱顶/舱高 = %.3f (需 0.5–0.9)" % (mod_over / bus_h), flush=True)
    print("[combiner] C4 载荷舱底面与舱顶间隙 = %+.3f m (需 -0.30–0.05)" % gap, flush=True)
    print("[combiner] 规格§四.3 载荷舱总高 %.3f < 集光器镜筒 %.3f : %s"
          % (module.dimensions[2], C.TUBE_H, module.dimensions[2] < C.TUBE_H), flush=True)
    print("[combiner] 翼展/舱宽 = %.3f (需 3.0–4.5，翼展 %.3f m)" % (span / bus_w, span), flush=True)
    print("[combiner] 接收机构 x = %s  底面对载荷舱顶 %s"
          % ([round(r.location.x, 3) for r in recv],
             [round((r.location.z - r.dimensions[2] / 2) - (module.location.z + module.dimensions[2] / 2), 3)
              for r in recv]), flush=True)


# ============================================================ 自检渲染
def render_views():
    scene = bpy.context.scene
    cam = C.setup_render_rig()
    # 灯组由 collector.setup_render_rig() 提供，其中已含反向补光 RIG_Fill2
    # （主光与补光分居两侧，否则 side 视角被舱体挡出的那片翼上无光可补，是死黑）。
    # 此处不再另加补光——重写 collector 时它已内置，重复添加会过曝。
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
    lo = Vector((1e30,) * 3)
    hi = Vector((-1e30,) * 3)
    for o in meshes:
        for v in o.data.vertices:
            w = o.matrix_world @ v.co
            lo = Vector(min(lo[i], w[i]) for i in range(3))
            hi = Vector(max(hi[i], w[i]) for i in range(3))
    center = (lo + hi) / 2
    radius = max(((o.matrix_world @ v.co) - center).length for o in meshes for v in o.data.vertices)
    print("[combiner] 包围盒 %.2f × %.2f × %.2f m" % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]),
          flush=True)

    mod = bpy.data.objects["module"]
    mod_top = mod.location.z + mod.dimensions[2] / 2
    # 视角：side/front 正交（对比参考图轮廓）；iso 透视；top 正交特写机构簇
    lens = 52.0
    half_v = math.atan(cam.data.sensor_width * RES[1] / RES[0] / 2 / lens)
    iso_dist = radius / math.sin(half_v) * 1.08
    views = [
        ("side", dict(location=(0, -8, center.z), target=(0, 0, center.z), ortho=5.2)),
        ("front", dict(location=(-8, 0, center.z), target=(0, 0, center.z), ortho=2.4)),
        ("iso", dict(location=Vector((0.78, -1.0, 0.50)).normalized() * iso_dist
                     + Vector((0, 0, center.z)), target=(0, 0, center.z * 0.9), ortho=None)),
        # top 正俯视正交：平面为梯形，倾斜会引入梯形错觉，故正俯视
        ("top", dict(location=(0.0, 0.0, 6), target=(0.0, 0.0, mod_top - 0.12), ortho=2.0)),
    ]
    for name, kw in views:
        C.point_camera(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[combiner] 出图 %s" % scene.render.filepath, flush=True)


if __name__ == "__main__":
    build()
    render_views()
    print("[combiner] 完成：%d 个对象" % len(bpy.context.scene.objects), flush=True)
