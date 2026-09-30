# -*- coding: utf-8 -*-
"""觅音计划 2028 干涉探测任务——阶段四：分布式编队场景（含光束可视化）。

用法：
    blender --background --python formation.py            # 预览（1200×900 @64 samples）
    blender --background --python formation.py -- --final  # 正式（1920×1080 @512 + EXR，需人工许可）

依《阶段四_建模规格》v1.0（2026-09-30）：
    * 三器沿基线（Y）一字排开，合束器居中，集光器分居两侧；
      显示基线 B = 12 m（真实 20–100 m 不成比例，记录于 E12）；三器舱心 x/z 对齐。
    * 集光器 A 在 y=−B/2、窗口朝 +Y；B 在 y=+B/2、窗口朝 −Y（A 保持单体姿态，B 绕 Z 转 180°）。
    * 单体**逐字复用** collector.build()／combiner.build()（加了 purge=False 开关），不复制代码。
    * 星光束：粉色粗（Φ0.24），自 +Z 垂直入射筒口，终点内嵌 0.02 m，起点高出筒口 2.5 m；
      器间束：红色细（Φ0.06），出光窗口→载荷舱收光口，两端各内嵌 0.02 m。束径比 4:1。
    * T1（已确认）：集光器太阳翼沿用单体 ±X 构型不改。
      T2（已确认）：载荷舱基部 ±Y 各开一等径圆口（口径 = 红束径 + 0.04 m，黑色内腔）。
    * 布局父级 EMPTY_LAYOUT 统一控制编队距离（三器与四束均挂其下）。

端点一律由**实测包围盒**反算（筒口截面、出光窗口面、收光口面），不写目测偏移。
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

OUT_DIR = os.path.join(HERE, "out", "formation")

# ============================================================ 参数区
BASELINE_DISPLAY = 12.0        # 显示基线（m）
BASELINE_REAL_M = (20.0, 100.0)  # 真实基线区间（仅记录，E12）

D_LINK = 0.06                  # 器间束直径（红细）
D_STAR = 0.24                  # 星光束直径（粉粗）——与 D_LINK 之比 4:1
PORT_EXTRA = 0.04              # 收光口径外扩量（T2：口径 = 红束径 + 0.04）
PORT_LEN = 0.06                # 收光口罩筒长度
EMBED = 0.02                   # 光束端点内嵌深度（判据 ≤0.02 m）
STAR_ABOVE = 2.5               # 星光束起点高出筒口（判据 ≥2 m）
STAR_BOTTOM_EMBED = 0.02       # 星光束下端没入筒口

COL_STAR = (1.0, 0.56, 0.63)   # ≈#FF8FA0 粉
COL_LINK = (1.0, 0.165, 0.10)  # ≈#FF2A1A 红
STAR_STRENGTH = 6.0            # AgX 下轻微过曝
LINK_STRENGTH = 4.0

BG_TEXTURE = os.path.join(HERE, "..", "..", "assets", "textures", "8k_stars_milky_way.jpg")
BG_STRENGTH = 0.35             # 压暗成"暗星场"
GLARE_THRESHOLD = 1.0          # 只让光束与筒口过阈

RES = (1200, 900)
SAMPLES = 64
RES_FINAL = (1920, 1080)
SAMPLES_FINAL = 512
BG = 0.008


# ============================================================ 工具
def vbounds(objs):
    C.refresh()
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def vcenter(o):
    lo, hi = vbounds([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


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


def emit_material(name, color, strength):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = 0.4
    # 自发光：判据 E8 要求四束均为 emission
    if "Emission Color" in b.inputs:
        b.inputs["Emission Color"].default_value = (*color, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    else:                                   # 旧版接口兜底
        b.inputs["Emission"].default_value = (*color, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    m.diffuse_color = (*color, 1.0)
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


# ============================================================ 构件
def build_spacecraft():
    """三器：逐字复用单体 build()，各自成组；返回 {tag: {holder, objs}}。

    objs 的键是**改名后**的名字（colA_bus 等），供后续按名查找筒口/窗口。
    """
    half = BASELINE_DISPLAY / 2
    specs = (("colA", C.build(purge=False), (0.0, -half, 0.0), 0.0),    # A 在 −Y：窗口朝 +Y
             ("colB", C.build(purge=False), (0.0, +half, 0.0), 180.0),  # B 在 +Y：窗口朝 −Y
             ("cmb", M.build(purge=False), (0.0, 0.0, 0.0), 0.0))
    out = {}
    for tag, objs, loc, rot in specs:
        holder = group(tag, loc, rot, list(objs.values()))
        out[tag] = {"holder": holder,
                    "objs": {o.name: o for o in objs.values()}}
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


def build_star_beams(col_objs, mat):
    """粉色粗光束 ×2：自 +Z 垂直入射各自镜筒口。"""
    beams = []
    for tag in ("colA", "colB"):
        tube = next(v for k, v in col_objs[tag]["objs"].items() if k.endswith("_tube"))
        lo, hi = vbounds([tube])
        cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
        mouth_z = hi[2]
        p0 = (cx, cy, mouth_z - STAR_BOTTOM_EMBED)      # 下端没入筒口
        p1 = (cx, cy, mouth_z + STAR_ABOVE)             # 上端出画
        beams.append(cylinder_between("BEAM_star_%s" % tag, p0, p1, D_STAR / 2, mat, verts=32))
    return beams


def build_link_beams(col_objs, ports, mat):
    """红色细光束 ×2：集光器出光窗口 → 合束器对应收光口，两端各内嵌 EMBED。"""
    beams = []
    for tag, port_tag in (("colA", "negY"), ("colB", "posY")):
        win = next(v for k, v in col_objs[tag]["objs"].items() if k.endswith("_window_out"))
        wlo, whi = vbounds([win])
        wc = [(wlo[i] + whi[i]) / 2 for i in range(3)]
        # 朝向合束器的一侧：colA 在 −Y → 朝 +Y；colB 在 +Y → 朝 −Y
        sgn = 1.0 if tag == "colA" else -1.0
        face_y = whi[1] if sgn > 0 else wlo[1]
        p0 = (wc[0], face_y - sgn * EMBED, wc[2])       # 起点：窗口面内嵌
        p = ports[port_tag]
        plo, phi = vbounds([p])
        pc = [(plo[i] + phi[i]) / 2 for i in range(3)]
        pface = phi[1] if sgn < 0 else plo[1]           # 收光口朝集光器的那一面
        p1 = (pc[0], pface + sgn * EMBED, pc[2])        # 终点：收光口内嵌
        beams.append(cylinder_between("BEAM_link_%s" % tag, p0, p1, D_LINK / 2, mat, verts=16))
    return beams


# ============================================================ 场景
def _set_glare(gl):
    """跨版本设 Glare 参数：≤4.x 是节点属性，5.x 移到了输入插槽。"""
    def put(attr, sock, val):
        if hasattr(gl, attr):
            try:
                setattr(gl, attr, val)
                return
            except Exception:
                pass
        if sock in gl.inputs:
            try:
                gl.inputs[sock].default_value = val
            except Exception:
                pass
    put("glare_type", "Type", "FOG_GLOW")
    put("quality", "Quality", "HIGH")
    put("threshold", "Threshold", GLARE_THRESHOLD)
    put("size", "Size", 8)


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

    for name, energy, rot in (("LGT_Key", 3.0, (58, 0, 38)), ("LGT_Fill", 1.6, (70, 0, -115)),
                              ("LGT_Fill2", 2.2, (60, 0, -40)), ("LGT_Rim", 2.0, (108, 0, 200))):
        d = bpy.data.lights.new(name, "SUN")
        d.energy = energy
        d.angle = math.radians(20 if name == "LGT_Fill2" else 6)
        ob = bpy.data.objects.new(name, d)
        ob.rotation_euler = tuple(math.radians(v) for v in rot)
        bpy.context.collection.objects.link(ob)

    # 色彩管理 AgX / Medium High Contrast
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception as exc:
        print("[formation] 色彩管理设置失败：%s" % exc, flush=True)

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


def add_camera():
    cam_data = bpy.data.cameras.new("RIG_Cam")
    cam_data.lens = 40.0
    cam = bpy.data.objects.new("RIG_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam


def point(cam, location, target, ortho=None, lens=40.0):
    cam.location = Vector(location)
    d = Vector(target) - Vector(location)
    if abs(d.normalized().dot(Vector((0, 0, 1)))) > 0.9995:
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    if ortho:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = "PERSP"
        cam.data.lens = lens


def render_views(final=False):
    scene = bpy.context.scene
    os.makedirs(OUT_DIR, exist_ok=True)
    cam = add_camera()

    layout = bpy.data.objects["EMPTY_LAYOUT"]
    colA_bus = bpy.data.objects["colA_bus"]
    half = BASELINE_DISPLAY / 2
    zc = vcenter(colA_bus)[2]
    wide = BASELINE_DISPLAY + 3.0
    views = (
        # front：斜视（与 F4 同观感——纯沿 ±X 看会把集光器翼看成一条线，F4 是斜视才同时
        # 看得到"三器成排"与"翼展"）；side/top：沿 ±Y 与俯视
        ("front", dict(location=(wide * 1.55, -wide * 0.55, zc + 2.6),
                       target=(0, 0, zc + 0.9), ortho=None, lens=40.0)),
        ("side", dict(location=(0, wide * 2.2, zc), target=(0, 0, zc), ortho=wide * 0.5)),
        ("top", dict(location=(0, 0, wide * 2.2), target=(0, 0, zc), ortho=wide)),
        # iso：colA 与光束的近景（对 F3 分布式_集光器特写）
        ("iso", dict(location=Vector((4.2, -half - 4.0, 2.6)), target=(0.0, -half, zc + 0.35),
                     ortho=None, lens=50.0)),
        # wide：编队全景（对 F6）
        ("wide", dict(location=Vector((wide * 1.1, -wide * 0.95, wide * 0.45)),
                      target=(0, 0, zc), ortho=None, lens=35.0)),
    )
    for name, kw in views:
        point(cam, **kw)
        scene.render.filepath = os.path.join(OUT_DIR, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)

    if final:      # 正式档另留一张 EXR 底片（规格 §五）
        scene.render.image_settings.file_format = "OPEN_EXR"
        point(cam, **dict(views[4][1]))
        scene.render.filepath = os.path.join(OUT_DIR, "wide.exr")
        bpy.ops.render.render(write_still=True)
        print("[formation] 出图 %s" % scene.render.filepath, flush=True)


def build():
    """生成分布式编队场景。返回对象字典。"""
    C.purge_scene()
    C.refresh()
    layout = bpy.data.objects.new("EMPTY_LAYOUT", None)
    bpy.context.collection.objects.link(layout)
    layout["baseline_display_m"] = BASELINE_DISPLAY
    layout["baseline_real_m"] = "%g–%g" % BASELINE_REAL_M
    layout["beam_d_star_m"] = D_STAR
    layout["beam_d_link_m"] = D_LINK
    layout["beam_ratio"] = D_STAR / D_LINK
    layout["wing_state"] = "集光器沿用单体 ±X 构型（T1 默认，未改）"
    layout["beam_material"] = "MAT_beam_star 粉 %s / MAT_beam_link 红 %s" % (COL_STAR, COL_LINK)

    craft = build_spacecraft()
    for tag, rec in craft.items():
        holder = rec["holder"]
        holder.parent = layout
        holder["tilt_deg"] = 0.0          # E4：倾角记录
        holder["posture"] = "单体默认：镜筒 +Z" + ("，窗口朝 +Y" if tag == "colA"
                                                else "，窗口朝 −Y" if tag == "colB" else "")
    C.refresh()

    mat_star = emit_material("MAT_beam_star", COL_STAR, STAR_STRENGTH)
    mat_link = emit_material("MAT_beam_link", COL_LINK, LINK_STRENGTH)

    ports = build_ports(craft["cmb"]["objs"])
    beams = build_star_beams(craft, mat_star) + build_link_beams(craft, ports, mat_link)
    for ob in list(ports.values()) + beams:
        ob.parent = layout                # 三器与四束均挂布局父级（E9）
    C.refresh()

    objs = {o.name: o for o in bpy.context.scene.objects}
    report(objs, beams)
    return objs


def report(objs, beams):
    lo, hi = vbounds([o for o in objs.values() if o.type == "MESH"])
    cA, cB = vcenter(objs["colA_bus"]), vcenter(objs["colB_bus"])
    print("[formation] 编队：显示基线 %.1f m（真实 %g–%g m，不成比例）"
          % (BASELINE_DISPLAY, *BASELINE_REAL_M), flush=True)
    print("[formation] 实测基线 |y_colA−y_colB| = %.3f m；合束器 y = %+.3f"
          % (abs(cA[1] - cB[1]), vcenter(objs["cmb_bus"])[1]), flush=True)
    print("[formation] 光束：星光 Φ%.2f（粉）/ 器间 Φ%.2f（红），束径比 %.1f:1"
          % (D_STAR, D_LINK, D_STAR / D_LINK), flush=True)
    for b in beams:
        bl, bh = vbounds([b])
        length = max(bh[i] - bl[i] for i in range(3))     # 束沿不同轴，取其最大跨度
        print("[formation]   %-18s 长 %6.3f m  bbox %.3f×%.3f×%.3f"
              % (b.name, length, bh[0] - bl[0], bh[1] - bl[1], bh[2] - bl[2]), flush=True)
    print("[formation] 包围盒 %.2f × %.2f × %.2f m；对象 %d"
          % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], len(objs)), flush=True)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    final = "--final" in argv
    build()
    build_render_env(final=final)
    render_views(final=final)
    print("[formation] 完成：%d 个对象（%s）" % (len(bpy.context.scene.objects),
                                              "正式" if final else "预览"), flush=True)
