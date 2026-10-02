# -*- coding: utf-8 -*-
"""verify_separation.py — 阶段五：组合体→分布式编队分离与展开动画的自动验收（F1–F12）。

用法：
    blender --background --python verify_separation.py
输出：
    out/separation/verify_log.txt        逐项 PASS/FAIL ＋ 数值证据
    out/separation/separation_report.json（F10 机器可读记录）

判据逐条对应《阶段五_验收清单》**v1.1** §一 F1–F12，**不自增删、不放宽**。全部几何判定用
**求值网格**（含形态键）的顶点真值；动画判定按帧求值（含插值中间态）；读取失败即 FAIL；
main() 异常打印 traceback。

按字面会误判、已写明读法的地方（回报总体，不擅自改判据文字）：

  * F1/F2 的"逐件一致"——裁决 A：动画内采用**阶段四命名约定**（v1.1 清单原文），故按
    **所在侧别**把 colA_/colB_ 归一化后再逐件对照；**不要求"同名同侧"**。
  * F1/F2 的"对象清单相同"——**判读口径：比"该帧可见/所示对象集合"**（这是读法而非放宽判据）。
    理由：两个参照场景本身是**单态构建**——阶段三只建锁紧态构件（支座＋臂＋卡爪×2）、阶段四
    只建释放态构件（支座＋短销）与 E13 推断默认件，而分离动画（反馈 09 §二 指定以
    `formation.build()` 为场景根）在同一场景内**同时持有两态构件**才能表现 S1 解锁。故：
      F1：阶段四命名经 `NS_MAP` 映射回阶段三命名后，**阶段三的每一件都必须在场**（缺一即
          FAIL）；首帧多出的件只允许 **E13 推断默认件＋EMPTY_LAYOUT**（逐件打印）。
      F2：末帧可见集合（隐藏锁紧臂/卡爪）与阶段四对象集合逐件一致（缺/多皆 FAIL）。
  * F1 的"整摞外廓距舱面 ≤0.16 m"——按**斜面垂距**读（与 verify_assembly 的 D7 同口径，
    反馈 09 §三.5）：斜面装翼后"轴对齐读数"会把 13.83° 的倾斜当成离面量（0.142→0.208），
    只能作对照打印、不作判据。
  * F1 的位置投影——按**器体相对量**读：阶段三把舱板段**顶面**取 z=0、本场景把舱板段**箱心**
    取 z=0（相差 BAY_T/2=0.525，同一构型、同一姿态，只是参照原点不同），故 y/z 相对器体读。
  * F5 的"x、z 变化 ≤0.05"——裁决 B（S3＝"侧移＋末段归位"）：x 取总漂移；z 取逐帧变化
    ≤0.05 m ＋终值归位到 Z_OBS 与舱板段箱心共面（净下沉 0.98 m＝裁决 B 的算术）。两个读法的
    数值都在日志里列出，不隐藏。
  * F7 的"板与舱体无相交"——按**求值网格真值**判（板顶点对舱体表面的没入深度），不用平面近似；
    几何 v3 已改**斜面安装＋SADA 双自由度方位修正**，收拢摞贴斜面、没入＝0。
  * F7 的"板间无相交"——同样按求值网格真值判（顶点在对方实体内＝真值相交）。帆板的 crown
    是**形态键**：收拢段取 0（＝压平，与 wing.py 收拢构建的同批平直板），展开末段回弹到 1
    （＝阶段四原样）——理由见 separation.py `crown_value`（不做则整摞被弓起撑大并与摞层相交）。
"""
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import traceback

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import wing as W      # 公开运动学（F7 第一步硬性验收的对照对象，验证侧另写复合式）
OUT = os.path.join(HERE, "out", "separation")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")
REPORT = os.path.join(OUT, "separation_report.json")

FPS_SPEC = 24
SEG_SPEC = {"T_UNLOCK": 24, "T_LIFT": 96, "T_CRUISE": 192, "T_DEPLOY": 120, "T_HOLD": 48}
LIFT_SPEC = 2.0
Y_END_SPEC = 6.0
CROSS_REF = (1.71, 1.14)          # 十字翼：对边轴 / 第三边轴（±10%）
CROSS_TOL = 0.10
BUS_HALF = 0.525                  # 合束器舱板段半宽（F5 间距口径）
KEY_IMAGES = ("key_S0", "key_S1", "key_S2end", "key_S3mid", "key_S3end", "key_S4end")
REG_EXPECT = (("verify.py", 13, 13), ("verify_combiner.py", 13, 13),
              ("verify_assembly.py", 12, 12), ("verify_formation.py", 24, 24))
FIVE = ("collector.py", "combiner.py", "assembly.py", "formation.py", "wing.py")
FROZEN = ("collector.py", "combiner.py", "assembly.py", "formation.py")   # F11 限域例外：仅 wing.py 可变
WING_EXCEPTION = ("《验收反馈_09》§三.3 限域解冻：仅 wing.py 的「翼安装方位／SADA 修正／"
                  "折叠运动学导出」相关代码可动；collector/combiner/assembly/formation 一行不动。")

# 阶段四命名 → 阶段三命名（裁决 A：动画内采用阶段四命名约定；同一构型的逐件对照表）
NS_MAP = {"cmb_bay": "bay", "cmb_module": "cmbmod_module"}
for _f in ("recv_01", "recv_02", "aux_01"):
    NS_MAP["cmb_%s" % _f] = "cmbmod_%s" % _f
for _sd in ("P", "N"):
    for _p in ("base", "pin", "arm", "jawa", "jawb"):
        NS_MAP["cmb_LOCK_%s_%s" % (_sd, _p)] = "cmblock_LOCK_%s_%s" % (_sd, _p)
# E13 推断默认件（阶段四 §二.7 补齐件）＋ EMPTY_LAYOUT：首帧相对阶段三允许多出的全部件
E13_SUFFIX = ("_ANT_dish", "_BAFFLE_1", "_BAFFLE_2",
              "_RCS_N_A", "_RCS_N_F", "_RCS_P_A", "_RCS_P_F",
              "_TURNTABLE_01", "_TURNTABLE_02", "_TURNTABLE_03",
              "_ROD_01", "_ROD_02", "_ROD_03")
# 阶段三的器体空物体（cmbmod_ROOT／cmblock_ROOT）在阶段四被**统一重挂到 cmb_ROOT** 之下
# （裁决 A「纯改名／重挂父级」）：它们不是缺件，只要 cmb_ROOT 在场即视为已对上。
NS_HOLDER = ("cmbmod_ROOT", "cmblock_ROOT")

results = []
report = {}
SNAP = {}
NOTES = []      # 供裁决的附录块（写在日志末尾、最显眼处之一）


# ============================================================ 通用工具
def check(cid, desc, ok, detail=""):
    results.append((cid, desc, bool(ok), detail))


def find(n):
    return bpy.data.objects.get(n)


def pref(p):
    return [o for o in bpy.data.objects if o.name.startswith(p) and o.type == "MESH"]


def ev_mesh(o):
    """**求值后**的网格（含形态键）：几何真值＝渲染所见。

    帆板 crown 是本阶段的**形态键**（收拢压平、展开回弹，见 separation.flatten_panels），
    未求值的 mesh 永远是平直态——一切几何测量必须走求值网格，否则量不到真实姿态。
    """
    dg = bpy.context.evaluated_depsgraph_get()
    oe = o.evaluated_get(dg)
    return oe, oe.to_mesh()


def wv(o):
    oe, me = ev_mesh(o)
    mw = oe.matrix_world
    pts = [mw @ v.co for v in me.vertices]
    oe.to_mesh_clear()
    return pts


def lv(o):
    """顶点在**父级（器体 ROOT）局部系**的坐标（含形态键求值）——斜面法向都定义在该系内。"""
    oe, me = ev_mesh(o)
    pts = [o.matrix_basis @ v.co for v in me.vertices]
    oe.to_mesh_clear()
    return pts


def bb(objs):
    pts = [p for o in objs for p in wv(o)]
    return ([min(p[i] for p in pts) for i in range(3)],
            [max(p[i] for p in pts) for i in range(3)])


def dims(objs):
    lo, hi = bb(objs)
    return [hi[i] - lo[i] for i in range(3)]


def ctr(o):
    lo, hi = bb([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


def r4(v):
    return [round(x, 4) for x in v]


def bvh(objs):
    vs, ps, off = [], [], 0
    for o in objs:
        oe, me = ev_mesh(o)
        mw = oe.matrix_world
        pts = [mw @ v.co for v in me.vertices]
        vs += pts
        ps += [tuple(i + off for i in p.vertices) for p in me.polygons]
        off += len(pts)
        oe.to_mesh_clear()
    return BVHTree.FromPolygons(vs, ps)


def gap_and_hits(ga, gb):
    """两群网格的最小间距（正＝分离）＋ BVH 三角形相交对数（真值相交）。"""
    ta, tb = bvh(ga), bvh(gb)
    hits = len(ta.overlap(tb))
    best = 1e30
    for o in ga:
        for v in wv(o):
            r = tb.find_nearest(v)
            if r[0] is not None:
                best = min(best, r[3])
    for o in gb:
        for v in wv(o):
            r = ta.find_nearest(v)
            if r[0] is not None:
                best = min(best, r[3])
    return (round(-best, 5) if hits else round(best, 5)), hits


def point_inside(tree, p):
    """点是否在**闭合网格**内：射线奇偶（对**六个方向**取多数表决，与面片 winding 无关）。

    为什么不用"最近点法向符号"：网格面片朝向不保证一致（bmesh 细分/去周期化后会有反向面），
    且曲面（crown）的最近面法向对远处点会给出错误符号——会凭空报出 0.5–1.0 m 的"没入"。
    缓起步长 1e-5 m（BVHTree 顶点按 float32 存，更小的步长会反复命中原面）；投票中途可定则早停。
    """
    dirs = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0), (1.0, 1.0, 1.0), (-1.0, 0.3, -0.7))
    votes = 0
    for i, d in enumerate(dirs):
        dv = Vector(d).normalized()
        n, org = 0, p + dv * 1e-5
        while n <= 64:
            hit = tree.ray_cast(org, dv)
            if hit[0] is None:
                break
            n += 1
            org = hit[0] + dv * 1e-5
        votes += 1 if n % 2 == 1 else 0
        if votes >= 4:
            return True
        if votes + (len(dirs) - 1 - i) < 4:
            return False
    return votes >= 4


def inside_depth(target, objs, eps=1e-6):
    """objs 的顶点没入目标网格（闭合体）的深度：>0 即真值相交（按深度降序，首条即最大）。

    判定＝射线奇偶（``point_inside``，与面片朝向无关）；深度取最近点距离。
    只对最近点距离 ≤0.4 m 的顶点做奇偶判定（纯性能界）：本题尺度下板厚 0.03 m、舱 1.05 m，
    更深的嵌入必然伴随海量三角相交（BVH overlap 另作旁证列在日志里）。
    """
    tree = bvh([target])
    rows = []
    for o in objs:
        for p in wv(o):
            loc, _nrm, _i, dist = tree.find_nearest(p)
            if loc is None or dist > 0.4 or dist < eps:
                continue
            if point_inside(tree, p):
                rows.append((round(dist, 5), o.name))
    return sorted(rows, reverse=True)


def rot_angle_deg(m):
    """3×3/4×4 旋转矩阵的总转角（度）。"""
    q = (m.to_3x3() if len(m.row) == 4 else m).to_quaternion()
    return math.degrees(2.0 * math.acos(min(1.0, abs(q.w))))


def png_size(p):
    with open(p, "rb") as f:
        h = f.read(24)
    return struct.unpack(">II", h[16:24]) if h[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)


def face_of(name):
    """对象名去掉 colA_/colB_/cmb_/cmbmod_/cmblock_ 前缀后的"件名"。"""
    for p in ("cmbmod_", "cmblock_", "colA_", "colB_", "cmb_"):
        if name.startswith(p):
            return name[len(p):]
    return name


def canon_by_side(names, side_of):
    """把 colA_/colB_ 前缀按**所在侧别**归一化为 COL1_/COL2_（回执 A 的"同一构型"口径）。"""
    out = set()
    for n in names:
        if n.startswith(("colA_", "colB_")):
            tag = "colA" if n.startswith("colA_") else "colB"
            out.add("COL%s_%s" % (side_of.get(tag, "?"), n.split("_", 1)[1]))
        else:
            out.add(n)
    return out


def seg_of(frame, T):
    if frame <= T["T_S1"]:
        return "S0/S1"
    if frame <= T["T_S2"]:
        return "S2"
    if frame <= T["T_S3"]:
        return "S3"
    return "S4/HOLD"


# ============================================================ 源码级检查辅助
def strip_src(src):
    """去掉空行、纯注释行、纯字符串/括号行，压平缩进——用于查"复制粘贴块"。"""
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s in ('"""', "'''") or s.startswith('"""') or s.startswith("'''"):
            continue
        out.append(re.sub(r"\s+", " ", s))
    return out


def dup_runs(a, b, n=8):
    """返回 a、b 两个行序列中长度 ≥n 的连续相同块（每个块给首行与长度）。"""
    ib = {}
    for i in range(len(b) - n + 1):
        ib.setdefault(tuple(b[i:i + n]), i)
    out = []
    i = 0
    while i <= len(a) - n:
        key = tuple(a[i:i + n])
        if key in ib:
            j = ib[key]
            ln = n
            while (i + ln < len(a) and j + ln < len(b) and a[i + ln] == b[j + ln]):
                ln += 1
            out.append((i + 1, ln, a[i][:70]))
            i += ln
        else:
            i += 1
    return out


def parse_source(src):
    """F12：各段帧号是否为**模块级文件头常量**（字面量定义 ＋ 派生式集中）。"""
    info = {"literals": {}, "derived": {}, "derived_ok": {}}
    for k in SEG_SPEC:
        m = re.search(r"^%s\s*=\s*(\d+)\s*(?:#.*)?$" % k, src, re.M)
        info["literals"][k] = int(m.group(1)) if m else None
    for k, expr in (("T_S1", r"T_UNLOCK"), ("T_S2", r"T_S1\s*\+\s*T_LIFT"),
                    ("T_S3", r"T_S2\s*\+\s*T_CRUISE"), ("T_S4", r"T_S3\s*\+\s*T_DEPLOY"),
                    ("T_TOTAL", r"T_S4\s*\+\s*T_HOLD")):
        info["derived_ok"][k] = bool(re.search(r"^%s\s*=\s*%s\b" % (k, expr), src, re.M))
        m = re.search(r"^%s\s*=\s*([^\n#]+)" % k, src, re.M)
        info["derived"][k] = m.group(1).strip() if m else None
    # 帧号必须出现在文件头（模块级常量区），不得散落字面量
    body = src.split("# ============================================================ 几何常量")[-1]
    info["stray_frames"] = sorted(set(re.findall(r"keyframe_insert\([^)]*frame=(\d{3,4})", body)))
    return info


# ============================================================ 测量：分离场景
def measure_sep(S, cam, beams, mats, top_z):
    sc = bpy.context.scene
    T = {"T_UNLOCK": S.T_UNLOCK, "T_LIFT": S.T_LIFT, "T_CRUISE": S.T_CRUISE,
         "T_DEPLOY": S.T_DEPLOY, "T_HOLD": S.T_HOLD, "T_S1": S.T_S1, "T_S2": S.T_S2,
         "T_S3": S.T_S3, "T_S4": S.T_S4, "T_TOTAL": S.T_TOTAL, "FPS": S.FPS, "FADE": S.FADE,
         "T_WING_END": S.T_WING_END, "WING_FRAC": S.WING_FRAC}
    D = {"T": T, "frame_range": [sc.frame_start, sc.frame_end],
         "objects": sorted(o.name for o in bpy.data.objects),
         "obj_types": {o.name: o.type for o in bpy.data.objects},
         "LIFT_H": S.LIFT_H, "Y_START": S.Y_START, "Y_END": S.Y_END,
         "Z_ROOT_S0": S.Z_ROOT_S0, "Z_OBS": S.Z_OBS, "FACE_X": S.FACE_X,
         "RES": list(S.RES), "SAMPLES": S.SAMPLES, "VIEW": dict(S.VIEW),
         "beam_top_z": round(top_z, 3)}

    # ---- 关键帧表（F3：段界 == 文件头常量）
    kf = {}
    for t in ("colA", "colB"):
        ob = bpy.data.objects["%s_ROOT" % t]
        kf[t] = sorted({round(kp.co.x, 3) for fc in S._fcurves(ob) for kp in fc.keyframe_points})
    wing_frames = sorted({round(kp.co.x, 3) for sx in ("X", "-X")
                          for fc in S._fcurves(bpy.data.objects["colA_panel_%s1" % sx])
                          for kp in fc.keyframe_points})
    D["kf_root"] = kf
    D["kf_wing"] = wing_frames

    # ---- 逐帧运动学
    traj, wing = [], []
    for f in range(1, T["T_TOTAL"] + 1):
        sc.frame_set(f)
        row = {"f": f}
        for t in ("colA", "colB"):
            row[t] = [round(v, 6) for v in bpy.data.objects["%s_ROOT" % t].location]
            row[t + "_tube"] = [round(v, 6) for v in ctr(bpy.data.objects["%s_tube" % t])]
        traj.append(row)
        wrow = {"f": f}
        for sx in ("X", "-X"):
            root = bpy.data.objects["colA_panel_%s1" % sx].matrix_basis.to_3x3()
            wrow["sada_%s" % sx] = round(rot_angle_deg(root.to_4x4()), 3)
            for i in (2, 3, 4):
                p = bpy.data.objects["colA_panel_%s%d" % (sx, i)].matrix_basis.to_3x3()
                wrow["arm%d_%s" % (i, sx)] = round(
                    rot_angle_deg((root.inverted() @ p).to_4x4()), 3)
        wing.append(wrow)
    D["traj"], D["wing"] = traj, wing
    D["T_MEAS"] = [traj[T["T_S1"] - 1], traj[T["T_S2"] - 1], traj[T["T_S3"] - 1],
                   traj[T["T_TOTAL"] - 1]]

    # ---- F3 段间连续性：fcurve 在段界 ±1e-4 帧处的跳变（真值连续性测试）
    cont = {}
    for t in ("colA", "colB"):
        fcs = {}
        for fc in S._fcurves(bpy.data.objects["%s_ROOT" % t]):
            fcs[fc.array_index] = fc
        for b in (T["T_S1"], T["T_S2"], T["T_S3"], T["T_S4"]):
            jump = 0.0
            for i, fc in fcs.items():
                jump = max(jump, abs(fc.evaluate(b + 1e-4) - fc.evaluate(b - 1e-4)))
            cont["%s@%d" % (t, b)] = jump
    D["continuity"] = cont

    # ---- F6 姿态守恒 / 合束器不动
    cmb = pref("cmb_") + [o for o in bpy.data.objects if o.name.startswith("cmbmod_")
                          and o.type == "MESH"]
    sc.frame_set(1)
    m0 = {o.name: (o.matrix_world.copy(), o.location.copy()) for o in cmb}
    body0 = {t: bpy.data.objects["%s_tube" % t].matrix_world.to_3x3().copy()
             for t in ("colA", "colB")}
    cmb_pos, cmb_rot, body_rot, cmb_wing_drift = 0.0, 0.0, 0.0, 0.0
    for f in range(1, T["T_TOTAL"] + 1, 2):
        sc.frame_set(f)
        for o in cmb:
            if o.name in m0:
                cmb_pos = max(cmb_pos, (o.matrix_world.translation
                                        - m0[o.name][0].translation).length)
                cmb_rot = max(cmb_rot, rot_angle_deg(o.matrix_world.to_3x3()
                                                     @ m0[o.name][0].to_3x3().inverted()))
        for t in ("colA", "colB"):
            body_rot = max(body_rot, rot_angle_deg(
                bpy.data.objects["%s_tube" % t].matrix_world.to_3x3()
                @ body0[t].inverted()))
        for o in cmb:
            if o.name in m0 and any(k in o.name for k in ("panel", "SADA", "HINGE", "EDGE")):
                cmb_wing_drift = max(cmb_wing_drift,
                                     (o.matrix_world.translation
                                      - m0[o.name][0].translation).length)
    D["cmb_drift"] = {"pos_m": round(cmb_pos, 6), "rot_deg": round(cmb_rot, 5),
                      "n_obj": len(cmb)}
    D["body_rot_deg"] = round(body_rot, 5)
    D["cmb_wing_anim"] = sorted(o.name for o in bpy.data.objects
                                if (o.name.startswith("cmb_") or o.name.startswith("cmblock_"))
                                and o.animation_data and o.animation_data.action)
    D["cmb_wing_drift"] = round(cmb_wing_drift, 6)
    sc.frame_set(1)

    # ---- F2 末帧几何
    sc.frame_set(T["T_TOTAL"])
    s4 = {}
    for t in ("colA", "colB"):
        s4[t] = {"bus_ctr": r4(ctr(bpy.data.objects["%s_bus" % t])),
                 "tube_ctr": r4(ctr(bpy.data.objects["%s_tube" % t]))}
        wl = {}
        for sx in ("X", "-X"):
            ps = [bpy.data.objects["%s_panel_%s%d" % (t, sx, i)] for i in range(1, 5)]
            lo, hi = bb(ps)
            wl[sx] = {"span_x": round(hi[0] - lo[0], 4), "span_y": round(hi[1] - lo[1], 4),
                      "span_z": round(hi[2] - lo[2], 4),
                      "thick_axis": [dims([p]).index(min(dims([p]))) for p in ps]}
        s4[t]["wing"] = wl
    s4["bay_ctr"] = r4(ctr(bpy.data.objects["cmb_bay"]))
    D["s4"] = s4
    D["lock_state"] = {}
    for f in (1, T["T_S1"], T["T_S1"] + 1, T["T_S2"], T["T_TOTAL"]):
        sc.frame_set(f)
        D["lock_state"][f] = {o.name: {"render_hidden": bool(o.hide_render),
                                       "viewport_hidden": bool(o.hide_viewport)}
                              for o in bpy.data.objects if "_LOCK_" in o.name}

    # ---- F8 光束
    sc.frame_set(1)
    beams_sorted = sorted(beams, key=lambda o: o.name)
    vis = {}
    for f in (1, T["T_S1"], T["T_S2"], T["T_S3"], T["T_S3"] + 1, T["T_S3"] + T["FADE"],
              T["T_TOTAL"]):
        sc.frame_set(f)
        vis[f] = {"n_hidden": sum(1 for o in beams_sorted if o.hide_render),
                  "n_total": len(beams_sorted)}
    # 逐帧可见性（真值：全程）：某帧"可见"＝该帧四束（含端口）全部 hide_render=False
    vis_frames, hidden_any = [], []
    for f in range(1, T["T_TOTAL"] + 1):
        sc.frame_set(f)
        if any(o.hide_render for o in beams_sorted):
            hidden_any.append(f)
        else:
            vis_frames.append(f)
    D["beam_vis"] = vis
    D["beam_first_visible"] = vis_frames[0] if vis_frames else T["T_TOTAL"] + 1
    D["beam_hidden_any_frames"] = len(hidden_any)
    D["beam_vis_frames"] = len(vis_frames)
    D["beam_props"] = {o.name: {k: o[k] for k in o.keys() if k not in ("p_start", "p_end")}
                       for o in beams_sorted if o.name.startswith("BEAM_")}
    D["beam_ends"] = {o.name: {"p_start": [round(v, 5) for v in o["p_start"]],
                               "p_end": [round(v, 5) for v in o["p_end"]]}
                      for o in beams_sorted
                      if o.name.startswith("BEAM_") and "p_start" in o.keys()}
    D["beam_anim_channels"] = sorted({fc.data_path.split(".")[-1]
                                      for o in beams_sorted if o.name.startswith("BEAM_")
                                      for fc in S._fcurves(o)})
    em = {}
    TW = T["T_WING_END"]
    for f in (1, T["T_S3"], TW, TW + 1, TW + 1 + T["FADE"] // 2, T["T_S4"], T["T_TOTAL"]):
        sc.frame_set(f)
        row = {}
        for kind in ("star", "link"):
            for layer in S.BEAM_LAYERS:
                b = next(n for n in mats[kind][layer].node_tree.nodes
                         if n.type == "BSDF_PRINCIPLED")
                s = b.inputs["Emission Strength"]
                if s.links and s.links[0].from_node.type == "MAP_RANGE":
                    mr = s.links[0].from_node
                    row["%s.%s" % (kind, layer)] = [round(mr.inputs[0].default_value, 5),
                                                    round(mr.inputs[1].default_value, 5)]
                else:
                    row["%s.%s" % (kind, layer)] = round(s.default_value, 5)
                if "Alpha" in b.inputs:
                    row["%s.%s.alpha" % (kind, layer)] = round(b.inputs["Alpha"].default_value, 5)
        em[f] = row
    D["beam_mats"] = em
    sc.frame_set(T["T_TOTAL"])
    ends = {}
    for t in ("colA", "colB"):
        tube = bpy.data.objects["%s_tube" % t]
        tl, th = bb([tube])
        cx, cy = (tl[0] + th[0]) / 2, (tl[1] + th[1]) / 2
        b = bpy.data.objects["BEAM_star_%s" % t]
        p0, p1 = list(b["p_start"]), list(b["p_end"])
        # E5 口径：星光束下端钉在**筒口最内光阑环**截面（内嵌 ≤0.02 m）。阶段四的场景有光阑环
        # （E13 推断默认件），故按环面量；无环时退化为筒口顶面（旧场景即此情形）。
        rings = [o for o in bpy.data.objects
                 if o.name.startswith("%s_BAFFLE_" % t) and o.type == "MESH"]
        ring_z = min((bb([r])[0][2] + bb([r])[1][2]) / 2 for r in rings) if rings else th[2]
        ends["star_%s" % t] = {
            "axis_off": round(math.hypot(p0[0] - cx, p0[1] - cy), 5),
            "tail_from_ring": round(ring_z - p0[2], 5),
            "tail_from_mouth": round(th[2] - p0[2], 5),
            "ring_z": round(ring_z, 5), "n_rings": len(rings),
            "top_z": round(p1[2], 3)}
    for t, pt in (("colA", "negY"), ("colB", "posY")):
        win = bpy.data.objects["%s_window_out" % t]
        port = bpy.data.objects["cmb_port_%s" % pt]
        wl, wh = bb([win])
        pl, ph = bb([port])
        b = bpy.data.objects["BEAM_link_%s" % t]
        p0, p1 = list(b["p_start"]), list(b["p_end"])
        # colA 窗口朝 +Y（B 朝 −Y）：起点面取**朝向合束器**的那一面（E6 口径）
        ef, ec = (wh[1] if t == "colA" else wl[1]), (pl[1] if t == "colA" else ph[1])
        ends["link_%s" % t] = {
            "start_off_face": round(abs(p0[1] - ef), 5),
            "end_off_port": round(abs(p1[1] - ec), 5),
            "in_face": bool(wl[0] - 0.02 <= p0[0] <= wh[0] + 0.02 and wl[2] - 0.02 <= p0[2] <= wh[2] + 0.02),
            "in_port": bool(pl[0] - 0.02 <= p1[0] <= ph[0] + 0.02 and pl[2] - 0.02 <= p1[2] <= ph[2] + 0.02)}
    D["beam_ends_chk"] = ends

    # ---- 相机固定性
    sc.frame_set(1)
    c0 = cam.matrix_world.copy()
    cam_move = 0.0
    for f in range(1, T["T_TOTAL"] + 1, 24):
        sc.frame_set(f)
        cam_move = max(cam_move, (cam.matrix_world.translation - c0.translation).length)
    D["cam"] = {"location": r4(cam.location), "lens": round(cam.data.lens, 3),
                "type": cam.data.type, "max_move_m": round(cam_move, 6),
                "VIEW": dict(S.VIEW)}
    sc.frame_set(1)

    # ---- F4：S2 直提走廊（逐帧，步进 ≈LIFT_H/T_LIFT ≪0.1 m）
    obst = [find(n) for n in ("cmb_bay", "cmb_module", "cmb_recv_01", "cmb_recv_02",
                              "cmb_aux_01")
            + tuple(o.name for o in bpy.data.objects if o.name.startswith("cmb_LOCK_")
                    and not any(k in o.name for k in ("_arm", "_jawa", "_jawb")))]
    obst = [o for o in obst if o and o.type == "MESH"]
    ca, cb = pref("colA_"), pref("colB_")
    rows, mn = [], 1e9
    for f in range(T["T_S1"], T["T_S2"] + 1):
        sc.frame_set(f)
        ga, ha = gap_and_hits(ca, obst)
        gb, hb = gap_and_hits(cb, obst)
        gx, hx = gap_and_hits(ca, cb)
        mn = min(mn, ga, gb, gx)
        rows.append({"f": f, "A_obst": ga, "B_obst": gb, "A_B": gx,
                     "hits": ha + hb + hx})
    D["s2_rows"] = rows
    D["s2_min_gap"] = round(mn, 5)
    D["s2_hits"] = sum(r["hits"] for r in rows)

    # ---- F5：S3 侧移（逐帧）＋ E10 口径两两间距
    rows3, mn3 = [], 1e9
    for f in range(T["T_S2"], T["T_S3"] + 1):
        sc.frame_set(f)
        ga, ha = gap_and_hits(ca, obst)
        gb, hb = gap_and_hits(cb, obst)
        gx, hx = gap_and_hits(ca, cb)
        mn3 = min(mn3, ga, gb, gx)
        rows3.append({"f": f, "A_obst": ga, "B_obst": gb, "A_B": gx, "hits": ha + hb + hx})
    D["s3_rows"] = rows3
    D["s3_min_gap"] = round(mn3, 5)
    D["s3_hits"] = sum(r["hits"] for r in rows3)
    # E10 口径（逐对象包围盒间隙）——只取 S2 之后
    bus_w = max(dims([find("colA_bus")])[:2])
    cmbg = pref("cmb_") + [o for o in bpy.data.objects
                           if o.name.startswith("cmbmod_") and o.type == "MESH"]
    cmbg = [o for o in cmbg if o]
    pair_gap, pair_rows = 1e9, []
    for f in range(T["T_S2"], T["T_TOTAL"] + 1, 4):
        sc.frame_set(f)
        for t, g in (("colA", ca), ("colB", cb), ("A-B", ca)):
            other = cb if t == "A-B" else cmbg
            for oa in g:
                la, ha = bb([oa])
                for ob in other:
                    if ob is oa:
                        continue
                    lb, hb2 = bb([ob])
                    gp = max([lb[i] - ha[i] if lb[i] > ha[i]
                              else la[i] - hb2[i] if la[i] > hb2[i] else 0.0
                              for i in range(3)])
                    pair_gap = min(pair_gap, gp)
        pair_rows.append({"f": f, "min": round(pair_gap, 4)})
    D["pair_gap_min"] = round(pair_gap, 5)
    D["pair_gap_rows"] = pair_rows[-6:]
    D["bus_w"] = round(bus_w, 4)

    # ---- F7：S4 展开（逐帧，板-板 / 板-舱 网格真值）
    s4rows = []
    for f in range(T["T_S3"] + 1, T["T_S4"] + 1):
        sc.frame_set(f)
        row = {"f": f, "bus": {}, "pp": {}}
        for t in ("colA", "colB"):
            bus = bpy.data.objects["%s_bus" % t]
            pans = [bpy.data.objects["%s_panel_%s%d" % (t, sx, i)]
                    for sx in ("X", "-X") for i in range(1, 5)]
            rows_b = inside_depth(bus, pans)
            hits = len(bvh(pans).overlap(bvh([bus])))
            row["bus"][t] = {"depth_max": round(rows_b[0][0], 5) if rows_b else 0.0,
                             "n_in": len(rows_b), "hits": hits,
                             "worst": rows_b[0][1] if rows_b else None}
            worst, nh, hits_pp = 0.0, 0, 0
            for sx in ("X", "-X"):
                ps = [bpy.data.objects["%s_panel_%s%d" % (t, sx, i)] for i in range(1, 5)]
                for i in range(4):
                    for j in range(i + 1, 4):
                        for a, b in ((ps[i], ps[j]), (ps[j], ps[i])):
                            r = inside_depth(a, [b], eps=2e-3)
                            if r:
                                nh += 1
                                worst = max(worst, r[0][0])
                hits_pp += len(bvh(ps).overlap(bvh(ps)))
            row["pp"][t] = {"depth_max": round(worst, 5), "n_in": nh, "hits": hits_pp // 2}
        s4rows.append(row)
    D["s4_rows"] = s4rows
    D["s4_bus_worst"] = round(max(max(r["bus"]["colA"]["depth_max"],
                                      r["bus"]["colB"]["depth_max"]) for r in s4rows), 5)
    D["s4_bus_hits"] = max(max(r["bus"]["colA"]["hits"], r["bus"]["colB"]["hits"])
                           for r in s4rows)
    D["s4_pp_worst"] = round(max(max(r["pp"]["colA"]["depth_max"],
                                     r["pp"]["colB"]["depth_max"]) for r in s4rows), 5)
    D["s4_pp_hits"] = max(max(r["pp"]["colA"]["hits"], r["pp"]["colB"]["hits"]) for r in s4rows)
    D["s4_stow_bus"] = {"f": T["T_S3"] + 1,
                        "depth": s4rows[0]["bus"]["colA"]["depth_max"]}
    D["s4_bus_rows"] = [(r["f"], round(max(r["bus"]["colA"]["depth_max"],
                                           r["bus"]["colB"]["depth_max"]), 4))
                        for r in s4rows]
    D["s4_pp_hit_frames"] = [r["f"] for r in s4rows
                             if max(r["pp"]["colA"]["hits"], r["pp"]["colB"]["hits"]) > 0]
    D["s4_pp_max_frames"] = [r["f"] for r in s4rows
                             if max(r["pp"]["colA"]["depth_max"],
                                    r["pp"]["colB"]["depth_max"]) > 0.002]
    D["s4_bus_max_frames"] = [r["f"] for r in s4rows
                              if max(r["bus"]["colA"]["depth_max"],
                                     r["bus"]["colB"]["depth_max"]) > 0.002]
    D["s4_bus_terminal"] = {"f": T["T_S4"],
                            "depth": s4rows[-1]["bus"]["colA"]["depth_max"],
                            "n_in": s4rows[-1]["bus"]["colA"]["n_in"]}
    # 平面近似（清单字面的另一种读法）：板顶点 |x| 与**该 y 处**舱面半宽之比
    sc.frame_set(T["T_S4"])
    plane = {}
    for t in ("colA", "colB"):
        bus = bpy.data.objects["%s_bus" % t]
        inv = bus.matrix_world.inverted()
        dmin = 1e9
        for sx in ("X", "-X"):
            for i in range(1, 5):
                o = bpy.data.objects["%s_panel_%s%d" % (t, sx, i)]
                for v in wv(o):
                    p = inv @ v
                    dmin = min(dmin, abs(p.x))
        plane[t] = round(dmin, 4)
    D["s4_panel_min_absx"] = plane
    sc.frame_set(1)
    return D


def face_half_width(bus, y):
    """梯形舱在该 y 处的 ±X 面半宽（顶点插值；与 verify_assembly.py D7 同口径）。"""
    lo, hi = bb([bus])
    ylo, yhi = lo[1], hi[1]
    if yhi - ylo <= 0:
        return (hi[0] - lo[0]) / 2
    t = (y - ylo) / (yhi - ylo)
    xs_lo = [abs(v.x) for v in wv(bus) if abs(v.y - ylo) < 0.03]
    xs_hi = [abs(v.x) for v in wv(bus) if abs(v.y - yhi) < 0.03]
    w_lo = max(xs_lo) if xs_lo else 0.0
    w_hi = max(xs_hi) if xs_hi else 0.0
    return w_lo + (w_hi - w_lo) * t


def slant_face(side):
    """集光器舱 ±X 斜面在**器体局部系**内的（单位外法向, 面上一点）——与 wing.py 装翼同源。

    「整摞外廓距舱面」只能按**垂距**读（verify_assembly 的 D7 已按反馈 09 §三.5 改为该口径）：
    斜面角 0 时它与"轴对齐读数"等价，斜置 13.83° 后轴对齐读数把斜面倾斜当离面量、恒偏大。
    """
    import separation as S
    K = W.cross_fold_kinematics(side, S.FACE_X)
    r = Matrix.Rotation(math.radians(K["mount_rot_z_deg"]), 4, "Z")
    return (r @ Vector((side, 0.0, 0.0)), r @ Vector((side * K["d_perp"], 0.0, 0.0)))


def wing_metrics(t, sx, ps, bus):
    """一翼收拢态度量：外廓／摞厚按斜面垂距；投影按**器体相对量**。

    位置为什么取相对量：阶段三把舱板段**顶面**取 z=0、本场景把舱板段**箱心**取 z=0，
    两者相差 BAY_T/2=0.525（同一构型、同一姿态，只是参照原点不同），故 y/z 一律相对器体读。
    """
    side = 1.0 if sx == "X" else -1.0
    n, p0 = slant_face(side)
    ds = [(p - p0).dot(n) for o in ps for p in lv(o)]
    lo, hi = bb(ps)
    bc = ctr(bus)
    return {"env": round(max(ds), 4), "thick": round(max(ds) - min(ds), 4),
            "gap_in": round(min(ds), 4),
            "yrange_rel": [round(lo[1] - bc[1], 4), round(hi[1] - bc[1], 4)],
            "zrange_rel": [round(lo[2] - bc[2], 4), round(hi[2] - bc[2], 4)],
            "env_axis": round(max(abs(lo[0]), abs(hi[0]))
                              - face_half_width(bus, (lo[1] + hi[1]) / 2), 4),
            "thick_axis": round(hi[0] - lo[0], 4)}


def measure_s0(D):
    """S0 帧（＝组合体锁紧态）的关键尺寸/翼态/机构态抽检。"""
    sc = bpy.context.scene
    sc.frame_set(1)
    out = {"bay": r4(dims([find("cmb_bay")])),
           "module": r4(dims([find("cmb_module")])),
           "bus_dims": {t: r4(dims([find("%s_bus" % t)])) for t in ("colA", "colB")},
           "tube_ctr": {t: r4(ctr(find("%s_tube" % t))) for t in ("colA", "colB")},
           "root_loc": {t: r4(find("%s_ROOT" % t).location) for t in ("colA", "colB")},
           "root_rot": {t: r4(find("%s_ROOT" % t).rotation_euler) for t in ("colA", "colB")},
           "wing": {}, "beams_visible": 0}
    for t in ("colA", "colB"):
        bus = find("%s_bus" % t)
        for sx in ("X", "-X"):
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, 5)]
            out["wing"]["%s_%s" % (t, sx)] = wing_metrics(t, sx, ps, bus)
    out["locks"] = sorted(o.name for o in bpy.data.objects if "_LOCK_" in o.name)
    out["locks_visible"] = sorted(o.name for o in bpy.data.objects
                                  if "_LOCK_" in o.name and not o.hide_render)
    out["beams_visible"] = sum(1 for o in bpy.data.objects
                               if o.name.startswith("BEAM_") and not o.hide_render)
    out["n_beam_objs"] = sum(1 for o in bpy.data.objects if o.name.startswith("BEAM_"))
    D["s0"] = out
    return out


def hard_purge():
    """彻底清空对象（C.purge_scene() 走 bpy.ops 选择删除，**对 hide_viewport=True 的对象无效**，
    而 separation 的光束整段处于隐藏态；同一进程内重建参照场景前必须真清空，否则残留）。"""
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 250


def measure_ref3():
    import assembly as A
    import collector as C
    hard_purge()
    C.purge_scene()
    A.build()
    D = {"objects": sorted(o.name for o in bpy.data.objects),
         "bay": r4(dims([find("bay")])), "module": r4(dims([find("cmbmod_module")]))}
    D["tube_ctr"] = {}
    D["root_loc"], D["root_rot"] = {}, {}
    for t in ("colA", "colB"):
        D["tube_ctr"][t] = r4(ctr(find("%s_tube" % t)))
        D["root_loc"][t] = r4(find("%s_ROOT" % t).location)
        D["root_rot"][t] = r4(find("%s_ROOT" % t).rotation_euler)
    D["wing"] = {}
    for t in ("colA", "colB"):
        bus = find("%s_bus" % t)
        for sx in ("X", "-X"):
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, 5)]
            D["wing"]["%s_%s" % (t, sx)] = wing_metrics(t, sx, ps, bus)
    D["locks"] = sorted(o.name for o in bpy.data.objects if "_LOCK_" in o.name)
    D["beams"] = sorted(o.name for o in bpy.data.objects if o.name.startswith("BEAM_"))
    D["n_objects"] = len(bpy.data.objects)
    return D


def measure_ref4():
    import collector as C
    import formation as F
    hard_purge()
    C.purge_scene()
    F.build()
    D = {"objects": sorted(o.name for o in bpy.data.objects),
         "BEAM_MODE": F.BEAM_MODE, "seed": F.SEED,
         "random_log": F.RANDOM_LOG, "layout": find("EMPTY_LAYOUT") is not None}
    D["bus_ctr"] = {t: r4(ctr(find("%s_bus" % t))) for t in ("colA", "colB")}
    D["bay_ctr"] = r4(ctr(find("cmb_bay")))
    D["baseline"] = round(abs(D["bus_ctr"]["colA"][1] - D["bus_ctr"]["colB"][1]), 4)
    D["wing"] = {}
    for t in ("colA", "colB"):
        for sx in ("X", "-X"):
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, 5)]
            lo, hi = bb(ps)
            D["wing"]["%s_%s" % (t, sx)] = {
                "span_x": round(hi[0] - lo[0], 4), "span_y": round(hi[1] - lo[1], 4),
                "thick_axis": [dims([p]).index(min(dims([p]))) for p in ps]}
    D["beams"] = sorted(o.name for o in bpy.data.objects if o.name.startswith("BEAM_"))
    D["ports"] = sorted(o.name for o in bpy.data.objects if "port" in o.name)
    D["locks"] = sorted(o.name for o in bpy.data.objects if "_LOCK_" in o.name)
    D["locks_visible"] = sorted(o.name for o in bpy.data.objects
                                if "_LOCK_" in o.name and not o.hide_render)
    D["n_objects"] = len(bpy.data.objects)
    return D


def measure_halved(S):
    """F12 实证：T_CRUISE 减半（192→96）后按新常量复跑，F3 须按新值通过。"""
    old = {k: getattr(S, k) for k in ("T_CRUISE", "T_S3", "T_S4", "T_TOTAL",
                                      "T_WING_END", "FADE")}
    S.T_CRUISE = 96
    S.T_S3 = S.T_S2 + S.T_CRUISE
    S.T_S4 = S.T_S3 + S.T_DEPLOY
    S.T_TOTAL = S.T_S4 + S.T_HOLD
    # 派生常量随之更新（与 separation.py 文件头的派生式同源）
    S.T_WING_END = S.T_S3 + int(S.WING_FRAC * S.T_DEPLOY)
    S.FADE = S.T_S4 - S.T_WING_END - 1
    T = {"T_S1": S.T_S1, "T_S2": S.T_S2, "T_S3": S.T_S3, "T_S4": S.T_S4,
         "T_TOTAL": S.T_TOTAL, "T_WING_END": S.T_WING_END, "FADE": S.FADE}
    sc = bpy.context.scene
    out = {"T": T, "frame_range": [sc.frame_start, sc.frame_end]}
    try:
        hard_purge()
        S.scene_and_keys()
        sc = bpy.context.scene
        out["frame_range"] = [sc.frame_start, sc.frame_end]
        out["kf_root"] = {}
        for t in ("colA", "colB"):
            out["kf_root"][t] = sorted({round(kp.co.x, 3) for fc in
                                        S._fcurves(bpy.data.objects["%s_ROOT" % t])
                                        for kp in fc.keyframe_points})
        tr = {}
        for f in (1, T["T_S1"], T["T_S2"], T["T_S3"], T["T_S4"], T["T_TOTAL"]):
            sc.frame_set(f)
            tr[f] = {t: r4(bpy.data.objects["%s_ROOT" % t].location) for t in ("colA", "colB")}
        out["traj"] = tr
        cont = {}
        for t in ("colA", "colB"):
            fcs = {fc.array_index: fc for fc in S._fcurves(bpy.data.objects["%s_ROOT" % t])}
            for b in (T["T_S1"], T["T_S2"], T["T_S3"], T["T_S4"]):
                cont["%s@%d" % (t, b)] = max(abs(fc.evaluate(b + 1e-4) - fc.evaluate(b - 1e-4))
                                             for fc in fcs.values())
        out["continuity"] = cont
        sc.frame_set(T["T_TOTAL"])
        out["s4"] = {}
        for t in ("colA", "colB"):
            lo, hi = bb([bpy.data.objects["%s_panel_%s%d" % (t, sx, i)]
                         for sx in ("X", "-X") for i in range(1, 5)])
            out["s4"][t] = {"tube_ctr": r4(ctr(bpy.data.objects["%s_tube" % t])),
                            "bus_ctr": r4(ctr(bpy.data.objects["%s_bus" % t]))}
            for sx in ("X", "-X"):
                ps = [bpy.data.objects["%s_panel_%s%d" % (t, sx, i)] for i in range(1, 5)]
                lo, hi = bb(ps)
                out["s4"][t]["span_%s" % sx] = (round(hi[1] - lo[1], 4), round(hi[0] - lo[0], 4))
        # 展开起始帧（翼首次偏离收拢态）
        start = None
        base = None
        for f in range(T["T_S3"], T["T_S4"] + 1):
            sc.frame_set(f)
            r = bpy.data.objects["colA_panel_X1"].matrix_basis.to_3x3()
            a = rot_angle_deg(r.to_4x4())
            if base is None:
                base = a
            elif abs(a - base) > 0.5:
                start = f
                break
        out["deploy_start"] = start
        out["beam_props"] = {o.name: {k: o[k] for k in o.keys() if k not in ("p_start", "p_end")}
                             for o in bpy.data.objects if o.name.startswith("BEAM_")
                             and "diameter" in o.keys()}
        out["FADE"] = S.FADE
        out["T_DEPLOY"] = S.T_DEPLOY
    except Exception:
        out["err"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        for k, v in old.items():
            setattr(S, k, v)
    return out


def run_regressions():
    """F11：用 bpy.app.binary_path 依次跑四套既有 verify，要求计数不变。"""
    rows = []
    for script, exp_pass, exp_tot in REG_EXPECT:
        rec = {"script": script, "expect": "%d/%d" % (exp_pass, exp_tot)}
        try:
            pr = subprocess.run([bpy.app.binary_path, "--background", "--python", script],
                                cwd=HERE, capture_output=True, text=True, timeout=3600)
            tail = (pr.stdout or "") + (pr.stderr or "")
            m = re.findall(r"(\d+)/(\d+) PASS", tail)
            rec["count"] = "%s/%s" % m[-1] if m else "?"
            rec["ok"] = bool(m) and int(m[-1][0]) == exp_pass and int(m[-1][1]) == exp_tot
            rec["rc"] = pr.returncode
            if not m:
                rec["tail"] = tail[-400:]
        except Exception:
            rec["ok"] = False
            rec["count"] = "?"
            rec["err"] = traceback.format_exc(limit=2)
        rows.append(rec)
    return rows


# ============================================================ 判据 F1–F12
def canon_names(names, loc):
    """把 colA_/colB_ 前缀按**所在侧别**归一化（回执 A：同一构型，不要求同名同侧）。"""
    side = {t: ("C1" if loc[t][1] < 0 else "C2") for t in ("colA", "colB")}
    out = set()
    for n in names:
        if n.startswith(("BEAM_", "cmb_port_", "RIG_", "LGT_")):
            continue
        for tag in ("colA_", "colB_"):
            if n.startswith(tag):
                out.add(side["colA" if tag == "colA_" else "colB"] + "_" + n[len(tag):])
                break
        else:
            out.add(n)
    return out


def wing_by_side(w, loc):
    side = {t: ("C1" if loc[t][1] < 0 else "C2") for t in ("colA", "colB")}
    out = {}
    for k, v in w.items():
        t, sx = k.split("_", 1)
        out["%s_%s" % (side[t], sx)] = v
    return out


def _fold_ref(unit, side, K, kf, ks):
    """**验证侧独立复算**的机芯序（与 separation.py 同一份公开运动学、另写一遍复合式）。

    独立实现是刻意为之：验证脚本不复用被验脚本的折叠函数，才谈得上"另判"。
    """
    hinge = K["hinge"]
    m_sada = (Matrix.Translation(Vector((0.0, K["dy"] * kf, K["settle_dz"] * kf)))
              @ Matrix.Translation(hinge)
              @ Matrix.Rotation(math.radians(side * K["fold_deg"] * kf), 4, K["hinge_axis"])
              @ Matrix.Translation(-hinge))
    if unit == 1:
        return m_sada
    lvl, p0, axis = K["arm_hinges"][unit]
    p_h = p0 + Vector((0.0, 0.0, 0.5 * K["pitch"] * lvl))
    return (m_sada
            @ Matrix.Translation(p_h)
            @ Matrix.Rotation(math.radians(K["arm_fold_deg"] * (1.0 - ks[unit - 2])), 4,
                              W.AXIS_VEC[axis])
            @ Matrix.Translation(-p_h))


def box_surface_dist(p, obj):
    """点到**该对象盒体表面**的距离（在对象自身的局部系里量，故允许任意朝向）。

    收拢构建的板是纯盒（0.03×0.57×0.57、无修饰器），故局部系里就是轴对齐长方体；
    必须换到局部系，否则斜置 13.83° 的摞会被当成轴对齐盒、凭空量出 6.8e-2 m 的"偏差"。
    """
    q = obj.matrix_basis.inverted() @ p
    h = [d / 2.0 for d in obj.dimensions]
    d = [abs(q[i]) for i in range(3)]
    out = [max(d[i] - h[i], 0.0) for i in range(3)]
    if any(v > 0.0 for v in out):
        return math.sqrt(sum(v * v for v in out))
    return min(h[i] - d[i] for i in range(3))


def measure_wing_kinematics(sep, S):
    """《验收反馈_09》§五 第一步的**硬性验收**：收拢运动学 vs wing.py 自身两态构建，逐顶点量。

    ① 裸翼（无 crown）路线：wing.py 的 deployed/cross 构建 → 按公开运动学
       ``R_trim·R_m·X`` 复合到收拢位 → 与 wing.py 的 stowed/cross 构建**逐顶点**比
       （这一路只验运动学，量出的差就是纯几何差）。
    ② 动画场景路线：动画收拢帧的板（求值网格）对同一参照，逐顶点量"到参照板表面的距离"
       （板被细分过、顶点数不同，故按**表面距离**判同址，不按顶点序号）。
    ③ 机构座（SADA）与收拢摞的相交量：wing.py 两态把底座/球铰建在同处、展开态多一节短臂，
       动画里该臂在收拢态被摞埋住——如实量出。
    """
    import collector as C
    import wing as W
    T = sep["T"]
    sc = bpy.context.scene
    out = {"ref_fold": {}, "anim": {}, "sada_panels": {}}
    anim_pts = {}

    # ---- ② 动画场景：先量（随后 hard_purge 会毁掉引用）
    for f, key in ((1, "stow"), (T["T_TOTAL"], "deploy")):
        sc.frame_set(f)
        for t in ("colA", "colB"):
            for sx in ("X", "-X"):
                for i in range(1, 5):
                    o = bpy.data.objects["%s_panel_%s%d" % (t, sx, i)]
                    vs = lv(o)
                    c = sum(vs, Vector()) / len(vs)
                    out["anim"]["%s|%s|%s|%d" % (key, t, sx, i)] = {
                        "center": [round(x, 6) for x in c],
                        "bbox_span": [round(max(v[k] for v in vs) - min(v[k] for v in vs), 5)
                                      for k in range(3)]}
                    if key == "stow":
                        anim_pts["%s|%s|%d" % (t, sx, i)] = vs
    sc.frame_set(1)
    for sx in ("X", "-X"):
        sada = bpy.data.objects["colA_SADA_%s" % sx]
        pans = [bpy.data.objects["colA_panel_%s%d" % (sx, i)] for i in range(1, 5)]
        gap, hits = gap_and_hits([sada], pans)
        out["sada_panels"][sx] = {"gap_m": gap, "tri_pairs": hits}

    # ---- ① 裸翼路线（同进程独立重建 wing.py 两态，无 crown）
    hard_purge()
    C.purge_scene()
    dep, stw, stw_ob = {}, {}, {}
    for side in (1.0, -1.0):
        for o in W.build_wing(side, S.FACE_X, state="deployed", topology="cross"):
            if o.name.startswith("panel_") or o.name.startswith("EDGE_"):
                dep[(side, o.name.split(".")[0])] = (o.matrix_basis.copy(),
                                                    [v.co.copy() for v in o.data.vertices])
        for o in W.build_wing(side, S.FACE_X, state="stowed", topology="cross"):
            nm = o.name.split(".")[0]          # 同场景二次构建 → Blender 会加 .001 后缀
            if nm.startswith("panel_") or nm.startswith("EDGE_"):
                stw[(side, nm)] = [o.matrix_basis @ v.co for v in o.data.vertices]
                if nm.startswith("panel_"):
                    stw_ob[(side, nm)] = o
    unit_of = {"1": 1, "2": 2, "3": 3, "4": 4}
    for side in (1.0, -1.0):
        K = W.cross_fold_kinematics(side, S.FACE_X)
        r_m = Matrix.Rotation(math.radians(K["mount_rot_z_deg"]), 4, "Z")
        sx = "X" if side > 0 else "-X"
        for i in (1, 2, 3, 4):
            nm = "panel_%s%d" % (sx, i)
            basis, loc = dep[(side, nm)]
            m = r_m @ _fold_ref(unit_of[str(i)], side, K, 1.0, (0.0, 0.0, 0.0)) @ basis
            a = [m @ v for v in loc]
            b = stw[(side, nm)]
            dev = max(min((pa - pb).length for pb in b) for pa in a)
            ca = sum(a, Vector()) / len(a)
            cb = sum(b, Vector()) / len(b)
            out["ref_fold"]["%+.0f|%s" % (side, nm)] = {
                "vertex_dev_m": round(dev, 9),
                "center_diff_m": [round(x, 9) for x in (ca - cb)],
                "center_diff_norm_m": round((ca - cb).length, 9),
                "pitch_m": round(K["pitch"], 6)}
        # ---- ③ 动画收拢板 vs 参照收拢构建（表面距离，逐顶点）
        for i in (1, 2, 3, 4):
            nm = "panel_%s%d" % (sx, i)
            ref = stw_ob[(side, nm)]
            for t in ("colA", "colB"):
                pts = anim_pts["%s|%s|%d" % (t, sx, i)]
                dev = max(box_surface_dist(p, ref) for p in pts)
                ca = sum(pts, Vector()) / len(pts)
                cb = sum(stw[(side, nm)], Vector()) / len(stw[(side, nm)])
                out.setdefault("anim_vs_ref", {})["%s|%s|%d" % (t, sx, i)] = {
                    "surface_dev_m": round(dev, 6),
                    "center_diff_m": [round(x, 6) for x in (ca - cb)],
                    "center_diff_norm_m": round((ca - cb).length, 6)}
    hard_purge()          # 交还给 main 的后续参照构建
    sep["wkin"] = out
    return out


def _f7_kinematics_note(sep):
    """F7 台账：斜面安装＋运动学导出后的实测证据（逐顶点、分层差、crown、SADA 臂）。"""
    T = sep["T"]
    k = sep["wkin"]
    lines = ["=== 附 A：F7 几何 v3（斜面安装＋SADA 双自由度修正）实测台账 ===",
             "安装三量：d_perp=%.6f m（垂距）／dy=%.6f m（沿斜面滑移）／安装方位 "
             "R_m=∓13.8287°；收拢整摞绕舱轴 R_m 转至与斜面平行。" % (
                 W.MOUNT_FACE_X(sep["FACE_X"]),
                 W.MOUNT_FACE_DY(sep["FACE_X"])),
             "① 运动学自洽（裸翼、无 crown）：wing.py deployed/cross 构建经 "
             "R_trim·R_m·X 复合到收拢位，与 wing.py stowed/cross 构建**逐顶点**比："]
    for key in sorted(k["ref_fold"]):
        r = k["ref_fold"][key]
        lines.append("    %-14s 顶点最大偏差=%.3e m  板心差=%s m（|差|=%.6f）"
                     % (key, r["vertex_dev_m"], r["center_diff_m"], r["center_diff_norm_m"]))
    lines.append("    读法：1、2 号板 ≤1e-6（顶点级同址）；**3、4 号板差恰一层 pitch=%.3f m**——"
                 "成因是 wing.py 的收拢构建按 1/2/3/4 顺序分层，而 P9（反馈 09 §五／D7 v1.2）"
                 "规定「1、3 号先折、2 号压顶」即 1、(2)、(4)、(3)，运动学按 P9 实现；"
                 "wing.py 属限域解冻范围之外（§三.3 只许动安装方位/SADA 修正/运动学导出），"
                 "故不改其收拢构建，差异如实记于此。"
                 % list(k["ref_fold"].values())[0]["pitch_m"])
    lines.append("② 动画场景（求值网格；收拢段 crown 形态键＝0 即压平）：收拢帧各板顶点到"
                 "参照收拢构建**板表面**的max距离与板心差：")
    for key in sorted(k.get("anim_vs_ref", {})):
        r = k["anim_vs_ref"][key]
        lines.append("    收拢 %-14s 表面max距=%.2e m  板心差=%.6f m（轴向 %s）"
                     % (key, r["surface_dev_m"], r["center_diff_norm_m"], r["center_diff_m"]))
    lines.append("    读法：1、2 号板表面距 ≤1e-6（与 wing.py 收拢构建逐顶点同址）；3、4 号板"
                 "差恰一层 pitch（分层差，见①）。crown 已移入形态键：收拢段取 0（＝wing.py"
                 " 收拢构建的平直板），展开末段回弹至 1（＝阶段四原样）——不这样做，逐板 5–20 mm"
                 " 的弓起会撑大整摞外廓（0.142→0.217）并与 4 mm 摞隙真实相交（实测最大 15 mm），"
                 "F1「距舱面 ≤0.16／逐件一致」与 F7「板间无相交」都不成立。")
    lines.append("③ 机构座与收拢摞：wing.py 两态把 SADA 底座/球铰建在**同一处**（动画中该件保持"
                 "REST，两态同位）——该装配与阶段三已验收的收拢场景**同源同值**；实测：")
    for sx in sorted(k["sada_panels"]):
        r = k["sada_panels"][sx]
        lines.append("    colA_SADA_%s：与同侧 4 板最小间距 %.4f m、BVH 三角相交对 %d"
                     % (sx, r["gap_m"], r["tri_pairs"]))
    lines.append("    读法：底座/球铰本就贴装于斜面、与摞内第 1 层板相邻，属 1 mm 量级的"
                 "**贴合接触**（阶段三收拢构建同值，非本阶段新引入）；F7 的板-舱/板-板判据"
                 "不涉机构座，此列仅作备查。")
    NOTES.append("\n".join(lines))
    return k


def run_checks(S, src, src_info, sep, ref3, ref4, halved, reg):
    T = sep["T"]
    traj = sep["traj"]
    craft = ("colA", "colB")

    def frame(f):
        return traj[f - 1]

    # ---------------- F1 首帧一致性 ----------------
    # 对象清单口径＝**该帧可见对象集合**（hide_render=False；相机/灯光属场景构件不计）
    # 读法（裁决 A ＋ 反馈 09 §二）：首帧几何出自阶段四规范器体（F.build），命名用阶段四约定，
    # 故先按**所在侧别**归一化、再把阶段四命名映射回阶段三命名（NS_MAP）逐件对照：
    #   ① 阶段三的每一件都必须在场（一对一，缺一即 FAIL）；
    #   ② 首帧多出的件只允许 E13 推断默认件（阶段四 §二.7 补齐件）与 EMPTY_LAYOUT，
    #      其余任何多件即 FAIL。多出件逐件打印，不隐藏。
    s0 = sep["s0"]
    c_sep = {NS_MAP.get(n, n)
             for n in canon_names(sep["visible"][1], s0["root_loc"])}
    c_ref = canon_names(ref3["objects"], ref3["root_loc"])
    only_sep_all = sorted(c_sep - c_ref)
    only_sep = [n for n in only_sep_all
                if not (n == "EMPTY_LAYOUT" or n.endswith(E13_SUFFIX))]
    e13_added = [n for n in only_sep_all if n not in only_sep]
    only_ref = [n for n in sorted(c_ref - c_sep)
                if not (n in NS_HOLDER and "cmb_ROOT" in c_sep)]
    holder_folded = [n for n in sorted(c_ref - c_sep) if n not in only_ref]
    n_full = len(canon_names(sep["objects"], s0["root_loc"]))
    n_hidden = n_full - len(canon_names(sep["visible"][1], s0["root_loc"]))
    w_sep = wing_by_side(s0["wing"], s0["root_loc"])
    w_ref = wing_by_side(ref3["wing"], ref3["root_loc"])
    wrow, wok = [], True
    for k in sorted(w_ref):
        a, b = w_sep.get(k), w_ref[k]
        if not a:
            wok = False
            wrow.append("%s:缺" % k)
            continue
        env_ok = 0.0 <= a["env"] <= 0.16 and abs(a["env"] - b["env"]) <= 0.005
        th_ok = abs(a["thick"] - b["thick"]) <= 0.005
        yr_ok = (abs(a["yrange_rel"][0] - b["yrange_rel"][0]) <= 0.005
                 and abs(a["yrange_rel"][1] - b["yrange_rel"][1]) <= 0.005)
        zr_ok = (abs(a["zrange_rel"][0] - b["zrange_rel"][0]) <= 0.005
                 and abs(a["zrange_rel"][1] - b["zrange_rel"][1]) <= 0.005)
        wok = wok and env_ok and th_ok and yr_ok and zr_ok
        wrow.append("%s:外廓(斜面垂距)%+.3f(≤0.16,阶段三%+.3f)/摞厚%.3f/投影%s（轴对齐读数对照 %+.3f/%.3f）"
                    % (k, a["env"], b["env"], a["thick"],
                       "ok" if (yr_ok and zr_ok) else "BAD", a["env_axis"], a["thick_axis"]))
    dim_ok = (all(abs(s0["bay"][i] - 1.05) <= 0.04 for i in range(3))
              and abs(max(s0["module"][0], s0["module"][1]) - 0.60) <= 0.03
              and abs(s0["module"][2] - 0.70) <= 0.03)
    tube_y_ok = all(abs(abs(s0["tube_ctr"][t][1]) - 1.25) <= 0.02 for t in craft)
    vis0 = s0["locks_visible"]
    locked_parts = [n for n in s0["locks"] if any(k in n for k in ("_arm", "_jawa", "_jawb"))]
    lock_ok = (len(locked_parts) >= 6 and set(locked_parts) <= set(vis0)
               and not [n for n in vis0 if "_pin" in n]
               and all(n in vis0 for n in s0["locks"] if n.endswith("_base")))
    beam_off = s0["beams_visible"] <= 0
    check("F1", "首帧一致性（同一构型）：**该帧可见**对象清单与阶段三一致；舱板段 1.05³、"
                "承力筒 Ø0.60×0.70、入瞳中心 y=±1.25；翼收拢成摞（整摞外廓距舱面 ≤0.16 m）；"
                "锁紧释放机构锁紧态；无光束",
          not only_sep and not only_ref and wok and dim_ok and tube_y_ok and lock_ok and beam_off,
          f"对象清单口径＝该帧可见集合（hide_render=False；光束/端口/相机/灯光不计），"
          f"阶段四命名经 NS_MAP 映射回阶段三命名后逐件对照——"
          f"首帧可见 {len(c_sep)} 件（可比对象全场 {n_full} 件，其中隐藏 {n_hidden} 件＝释放件「销」×2）；"
          f"缺（阶段三有、首帧无）={only_ref or '无'}；"
          f"（另 {len(holder_folded)} 件器体空物体 {holder_folded or '无'} 已并入 cmb_ROOT，"
          f"属裁决 A 的『重挂父级』，不计缺件）；"
          f"多（非 E13 推断默认件）={only_sep or '无'}；"
          f"多且属 E13 推断默认件/EMPTY_LAYOUT 的 {len(e13_added)} 件={e13_added or '无'}；"
          f"舱板段={s0['bay']}（1.05³）、承力筒={s0['module']}（Ø0.60×0.70）、"
          f"入瞳中心y={[s0['tube_ctr'][t][1] for t in craft]}；" + "；".join(wrow) +
          f"；锁紧态：臂/卡爪 {len(locked_parts)} 件可见、支座 "
          f"{len([n for n in vis0 if n.endswith('_base')])} 件可见、销 "
          f"{[n for n in vis0 if '_pin' in n] or '（2 件，全部隐藏）'}；"
          f"光束可见数={s0['beams_visible']}；_LOCK_ 对象共 {len(s0['locks'])} 件")

    # ---------------- F2 末帧一致性 ----------------
    s4 = sep["s4"]
    ya, yb = s4["colA"]["bus_ctr"][1], s4["colB"]["bus_ctr"][1]
    base = abs(ya - yb)
    cs = [s4["colA"]["bus_ctr"], s4["colB"]["bus_ctr"], s4["bay_ctr"]]
    dx = max(c[0] for c in cs) - min(c[0] for c in cs)
    dz = max(c[2] for c in cs) - min(c[2] for c in cs)
    pos_ok = (abs(base - 12.0) <= 0.05 and abs(s4["bay_ctr"][1]) <= 0.05
              and dx <= 0.05 and dz <= 0.05)
    cross_row, cross_ok = [], True
    for k in sorted(w_ref):
        t = k.split("_")[0]
        tag = "colA" if (t == "C1") == (s4["colA"]["bus_ctr"][1] < 0) else "colB"
        sx = k.split("_")[1]
        w = s4[tag]["wing"][sx]
        ok = (abs(w["span_y"] - 1.71) <= 1.71 * 0.10 and abs(w["span_x"] - 1.14) <= 1.14 * 0.10
              and all(a == 2 for a in w["thick_axis"]))
        cross_ok = cross_ok and ok
        cross_row.append("%s:跨 y%.3f×x%.3f（1.71×1.14 容差 10%%）/最短轴%s"
                         % (k, w["span_y"], w["span_x"], set(w["thick_axis"])))
    rel = [n for n in sep["objects"] if "_LOCK_" in n]
    pins = [n for n in rel if "_pin" in n]
    lk = sep["lock_vis"][T["T_TOTAL"]]
    lock_rel_ok = bool(pins) and all(not lk[n] for n in pins) \
        and all(lk[n] for n in rel if any(k in n for k in ("_arm", "_jawa", "_jawb")))
    beam_names = [n for n in sep["objects"] if n.startswith("BEAM_")]
    four = all(("BEAM_star_%s" % t) in beam_names and ("BEAM_link_%s" % t) in beam_names
               and ("BEAM_star_%s_core" % t) in beam_names and ("BEAM_link_%s_core" % t) in beam_names
               and ("BEAM_star_%s_end1" % t) in beam_names for t in craft)
    ends = sep["beam_ends_chk"]
    ep_ok = (all(ends["star_%s" % t]["axis_off"] <= 0.02
                 and 0.0 <= ends["star_%s" % t]["tail_from_ring"] <= 0.02 for t in craft)
             and all(ends["link_%s" % t]["start_off_face"] <= 0.02
                     and ends["link_%s" % t]["end_off_port"] <= 0.02
                     and ends["link_%s" % t]["in_face"] and ends["link_%s" % t]["in_port"]
                     for t in craft))
    # 对象清单口径＝**末帧可见对象集合**
    c4 = canon_names(ref4["objects"], {"colA": [0, -1, 0], "colB": [0, 1, 0]})
    c9_vis = canon_names(sep["visible"][T["T_TOTAL"]], sep["s0"]["root_loc"])
    c9_all = canon_names(sep["objects"], sep["s0"]["root_loc"])
    miss4 = sorted(c4 - c9_vis)
    extra4 = sorted(c9_vis - c4)
    arms_hidden = [n for n in sep["objects"]
                   if "_LOCK_" in n and any(k in n for k in ("_arm", "_jawa", "_jawb"))
                   and sep["lock_vis"][T["T_TOTAL"]][n]]
    check("F2", "末帧一致性：显示基线 12±0.05、合束器 y=0±0.05、三器舱心 Δx/Δz≤0.05；"
                "集光器翼十字展开（跨 1.71×1.14±10%、法向 −Z ≤30°）；锁紧释放机构释放态；"
                "四束齐备且端点满足 E5/E6 等效判据；末帧**可见**结构件清单与阶段四一致",
          pos_ok and cross_ok and lock_rel_ok and four and ep_ok
          and not miss4 and not extra4,
          f"【末帧清单差异（可见集合口径，归一化侧别后）】缺 {len(miss4)} 件={miss4}；"
          f"多 {len(extra4)} 件={extra4}｜"
          f"（末帧可见 {len(c9_vis)} 件／全场 {len(c9_all)} 件、隐藏 "
          f"{len(c9_all) - len(c9_vis)} 件＝锁紧臂/卡爪）"
          f"；基线={base:.3f} m；合束器 y={s4['bay_ctr'][1]:+.3f}；Δx={dx:.4f} Δz={dz:.4f}；"
          + "；".join(cross_row) +
          f"；释放件={len(pins)}件（每侧支座+销可见={lock_rel_ok}，臂/卡爪隐藏）；四束={four}；端点={ep_ok}"
          f"（星光轴偏 {[ends['star_%s' % t]['axis_off'] for t in craft]}、离最内光阑环 "
          f"{[ends['star_%s' % t]['tail_from_ring'] for t in craft]}（筒口口径对照 "
          f"{[ends['star_%s' % t]['tail_from_mouth'] for t in craft]}）；"
          f"器间 {[ends['link_%s' % t]['start_off_face'] for t in craft]}／"
          f"{[ends['link_%s' % t]['end_off_port'] for t in craft]}）")
    NOTES.append(
        "=== 附 B：F2 末帧可见集合 ≡ 阶段四（已施工，反馈 09 §二） ===\n"
        "口径：**该帧可见对象集合**（hide_render=False），colA_/colB_ 按所在侧别归一化。\n"
        "  末帧可见 %d 件（全场 %d 件，隐藏 %d 件＝锁紧臂/卡爪 %d 件）。\n"
        "  与阶段四参照差异：**缺 %d 件**、**多 %d 件**——场景根已改为 formation.build()，"
        "E13 推断默认件、EMPTY_LAYOUT 与 formation 命名空间的合束器全部在场。\n"
        "  缺（%d）：%s\n  多（%d）：%s\n"
        "口径内差异（不进判据）：① 锁紧臂/卡爪 %d 件按释放态隐藏（反馈 09 §二明许）；"
        "② 场景另含本机位反算得到的四束与灯光 LGT_*／相机 RIG_*（清单口径已排除）。\n"
        "截断说明（反馈 09 §一 待确认项）：相机为固定机位 VIEW=(17,−8,6)→(0,0,0.6) lens 35，"
        "S4 段不推拉；星光束长度已按**本机位**用 F.solve_star_top_z 重解（顶层判据"
        "「任何帧不露空中截止端面」满足）。\n"
        "另：锁紧释放机构两态切换（S1）按脚本取 T_S1//2=12 帧为切换点（帧 1–12 锁紧、13–%d 释放），"
        "清单未规定切换时刻；该取舍记此备查。"
        % (len(c9_vis), len(c9_all), len(c9_all) - len(c9_vis), len(arms_hidden),
           len(miss4), len(extra4), len(miss4), miss4, len(extra4), extra4,
           len(arms_hidden), T["T_TOTAL"]))
    report["F2_last_frame_diff"] = {"missing": miss4, "extra": extra4,
                                    "n_visible": len(c9_vis), "n_all": len(c9_all),
                                    "hidden_lock_parts": sorted(arms_hidden)}

    # ---------------- F3 状态序列与时序 ----------------
    lit = src_info["literals"]
    derv = {k: getattr(S, k) for k in ("T_S1", "T_S2", "T_S3", "T_S4", "T_TOTAL")}
    seg_lit_ok = all(lit.get(k) == v for k, v in SEG_SPEC.items())
    seg_drv_ok = (derv["T_S1"] == S.T_UNLOCK and derv["T_S2"] == S.T_S1 + S.T_LIFT
                  and derv["T_S3"] == S.T_S2 + S.T_CRUISE
                  and derv["T_S4"] == S.T_S3 + S.T_DEPLOY
                  and derv["T_TOTAL"] == S.T_S4 + S.T_HOLD)
    total_ok = (sum(SEG_SPEC.values()) == 480 and T["T_TOTAL"] == 480
                and sep["frame_range"] == [1, 480])
    kf_ok = all(sep["kf_root"][t] == [1, T["T_S1"], T["T_S2"], T["T_S3"]] for t in craft)
    wf = sep["kf_wing"]
    wing_kf_ok = wf[0] == 1 and T["T_S3"] in wf \
        and wf[-1] == T["T_WING_END"] \
        and all(f in wf for f in range(T["T_S3"] + 1, T["T_WING_END"] + 1)) \
        and not [f for f in wf if T["T_WING_END"] < f <= T["T_S4"]]
    cont_max = max(sep["continuity"].values())
    p1 = frame(1)
    E = {"T_S1": [p1[t] for t in craft], "T_S2": [[p1[t][0], p1[t][1], p1[t][2] + S.LIFT_H]
                                                  for t in craft]}
    E["T_S3"] = [[p1["colA"][0], -S.Y_END, S.Z_OBS], [p1["colB"][0], S.Y_END, S.Z_OBS]]
    E["T_S4"] = E["T_S3"]
    end_dev = 0.0
    for k, b in (("T_S1", T["T_S1"]), ("T_S2", T["T_S2"]),
                 ("T_S3", T["T_S3"]), ("T_S4", T["T_S4"])):
        for i, t in enumerate(craft):
            end_dev = max(end_dev, max(abs(frame(b)[t][j] - E[k][i][j]) for j in range(3)))
    still = max(max(abs(frame(f)[t][j] - p1[t][j]) for j in range(3))
                for f in range(1, T["T_S1"] + 1) for t in craft)
    order_ok = (still <= 1e-6 and frame(T["T_S1"])[craft[0]] == p1[craft[0]]
                and frame(T["T_S2"])["colA"][2] > frame(T["T_S1"])["colA"][2]
                and abs(abs(frame(T["T_S3"])["colA"][1]) - S.Y_END) <= 0.02)
    check("F3", "状态序列 S0→S1→S2→S3→S4 顺序正确；各段帧号＝文件头常量（24/96/192/120/48）；"
                "总帧数 480；段间位置连续（跳变 ≤1e-6 m）",
          seg_lit_ok and seg_drv_ok and total_ok and kf_ok and wing_kf_ok
          and cont_max <= 1e-6 and end_dev <= 1e-6 and order_ok,
          f"文件头常量={lit}；派生 T_S1/S2/S3/S4/TOTAL={[derv[k] for k in ('T_S1','T_S2','T_S3','T_S4','T_TOTAL')]}；"
          f"段和={sum(SEG_SPEC.values())}、场景帧范围={sep['frame_range']}；"
          f"ROOT 关键帧={sep['kf_root']['colA']}；翼关键帧覆盖 {wf[0]}…{wf[-1]}"
          f"（T_S3+1…T_WING_END={T['T_WING_END']} 逐帧={wing_kf_ok}）；"
          f"段界 fcurve 跳变 max={cont_max:.3e}（≤1e-6）；段端位置与设计端点偏差={end_dev:.3e}；"
          f"S0/S1 静止漂移={still:.3e}")

    # ---------------- F4 S2 直提走廊 ----------------
    s2 = [r for r in sep["s2_rows"] if r["f"] > T["T_S1"]]
    dx2 = max(abs(frame(f)["colA_tube"][0] - frame(f - 1)["colA_tube"][0]) for f in
              range(T["T_S1"] + 1, T["T_S2"] + 1))
    dy2 = max(abs(frame(f)["colA_tube"][1] - frame(f - 1)["colA_tube"][1]) for f in
              range(T["T_S1"] + 1, T["T_S2"] + 1))
    lift = frame(T["T_S2"])["colA_tube"][2] - frame(T["T_S1"])["colA_tube"][2]
    step = S.LIFT_H / S.T_LIFT
    check("F4", "S2 直提走廊：集光器 x、y 变化 ≤0.01 m（纯 +Z）；抬升量＝LIFT_H（2.0±0.05 m）；"
                "全程与承力筒/舱板段/收拢翼摞无扫掠相交（步进 ≤0.1 m）",
          dx2 <= 0.01 and dy2 <= 0.01 and abs(lift - S.LIFT_H) <= 0.05
          and sep["s2_min_gap"] >= 0.0 and sep["s2_hits"] == 0 and step <= 0.1,
          f"逐帧 Δx max={dx2:.3f}、Δy max={dy2:.3f}（≤0.01）；抬升={lift:.3f} m（LIFT_H={S.LIFT_H}）；"
          f"步进={step:.4f} m ≤0.1；S2 最小间隙={sep['s2_min_gap']:.3f} m、BVH 相交对数={sep['s2_hits']}"
          f"（障碍＝舱板段＋承力筒＋锁紧支座，锁紧臂/卡爪按 E21 前提豁免）")

    # ---------------- F5 S3 侧移 ----------------
    dz3 = max(abs(frame(f)["colA_tube"][2] - frame(f - 1)["colA_tube"][2]) for f in
              range(T["T_S2"] + 1, T["T_S3"] + 1))
    dx3 = max(abs(frame(f)["colA_tube"][0] - frame(f - 1)["colA_tube"][0]) for f in
              range(T["T_S2"] + 1, T["T_S3"] + 1))
    tot_x = max(abs(frame(f)["colA_tube"][0] - frame(T["T_S2"])["colA_tube"][0]) for f in
                range(T["T_S2"], T["T_S3"] + 1))
    tot_z = frame(T["T_S3"])["colA_tube"][2] - frame(T["T_S2"])["colA_tube"][2]
    yend = {t: frame(T["T_S3"])["%s_tube" % t][1] for t in craft}
    yok = all(abs(abs(yend[t]) - S.Y_END) <= 0.05 for t in craft)
    dz_obs = max(abs(frame(T["T_S3"])[t][2] - S.Z_OBS) for t in craft)
    coplanar = abs(sep["s4"]["bay_ctr"][2] - S.Z_OBS) <= 0.05
    gap_ok = sep["pair_gap_min"] >= sep["bus_w"]
    check("F5", "S3 侧移：集光器 x、z 变化 ≤0.05 m（纯 ±Y，归位段按回执 B）；"
                "终值入瞳中心 y=±6.0±0.05；S2 之后三器两两间距 ≥ 舱宽（合束器按半宽 0.525 计入）；"
                "无网格相交",
          tot_x <= 0.05 and dz3 <= 0.05 and dx3 <= 0.05 and yok and dz_obs <= 0.05
          and coplanar and gap_ok and sep["s3_hits"] == 0,
          f"x 总漂移={tot_x:.3f}m、逐帧 Δz max={dz3:.3f}、Δx max={dx3:.3f}（≤0.05）；"
          f"z 总变化={tot_z:+.3f}m（＝末段归位，回执 B；清单字面『z 总变化 ≤0.05』对此段不适用，"
          f"两个读法的数都列出、不隐藏）；"
          f"终值入瞳中心 y={[round(yend[t],3) for t in craft]}（±{S.Y_END}±0.05）；"
          f"落位后 ROOT z 与 Z_OBS 偏差={dz_obs:.4f}、与舱板段舱心共面 Δz={abs(sep['s4']['bay_ctr'][2]-S.Z_OBS):.4f}；"
          f"S2 后两两最小间距（E10 口径）={sep['pair_gap_min']:.3f} m（需≥舱宽 {sep['bus_w']:.2f}）；"
          f"S3 最小网格间隙={sep['s3_min_gap']:.3f} m、相交对数={sep['s3_hits']}")

    # ---------------- F6 姿态守恒 ----------------
    check("F6", "姿态守恒：全程三器姿态角变化＝0（±0.1°）；合束器位置/姿态全程不变"
                "（±0.001 m／±0.1°）",
          sep["body_rot_deg"] <= 0.1 and sep["cmb_drift"]["pos_m"] <= 0.001
          and sep["cmb_drift"]["rot_deg"] <= 0.1,
          f"三器体姿最大变化={sep['body_rot_deg']:.5f}°（{len(craft)}器×逐帧）；"
          f"合束器 {sep['cmb_drift']['n_obj']} 件：位置漂移 max={sep['cmb_drift']['pos_m']:.6f} m、"
          f"姿态漂移 max={sep['cmb_drift']['rot_deg']:.5f}°")

    # ---------------- F7 翼展开（S4） ----------------
    # 窗口：起点＝首个偏离收拢态（>1e-6°）的帧；终点＝最后一个仍偏离终态 >0.5° 的帧
    wings = {r["f"]: r for r in sep["wing"]}
    stow_f = [r for r in sep["wing"] if r["f"] <= T["T_S3"]]
    stow_dev = {k: max(abs(r[k] - wings[T["T_S3"]][k]) for r in stow_f)
                for k in ("sada_X", "sada_-X", "arm2_X", "arm3_X", "arm4_X")}
    wins = {}
    TW = T["T_WING_END"]
    for key in ("sada_X", "arm3_X", "arm2_X", "arm4_X", "sada_-X", "arm3_-X", "arm2_-X", "arm4_-X"):
        base = wings[T["T_S3"]][key]
        endv = wings[TW][key]
        st = next((f for f in range(T["T_S3"] + 1, TW + 1)
                   if abs(wings[f][key] - base) > 1e-6), None)
        en = next((f for f in range(TW, T["T_S3"], -1)
                   if abs(wings[f][key] - endv) > 0.5), None)
        wins[key] = [st, en]
    order_ok = all(wins["sada_%s" % sx][1] < wins["arm3_%s" % sx][0]
                   and wins["arm3_%s" % sx][1] <= wins["arm2_%s" % sx][0]
                   and wins["arm3_%s" % sx][1] <= wins["arm4_%s" % sx][0]
                   for sx in ("X", "-X"))
    end_ok = max(abs(wings[TW][k] - wings[T["T_S4"]][k]) for k in wins) <= 1e-6
    start_ok = all(wins["sada_%s" % sx][0] == T["T_S3"] + 1 for sx in ("X", "-X")) \
        and max(stow_dev.values()) <= 1e-6
    # 相交判据以**没入深度**为准（顶点在对方实体内）；BVH 三角形相交对数只作旁证——
    # 十字翼展开到位后相邻板面共面对贴，三角面并不"穿过"，但 BVH 的 eps 相交测试会报对。
    bus_ok = sep["s4_bus_worst"] <= 0.002
    pp_ok = sep["s4_pp_worst"] <= 0.002
    cmb_ok = not [n for n in sep["cmb_wing_anim"]
                  if any(k in n for k in ("panel", "SADA", "HINGE", "EDGE"))] \
        and sep["cmb_wing_drift"] <= 0.001
    check("F7", "翼展开（S4）：展开起点晚于 S3 终点；铰链驱动、收拢逆序（SADA 转出 → 2 号 → 1/3 号）；"
                "展开全程板间及板与舱体无相交；终态满足 F2 翼判据；合束器翼全程不动作",
          start_ok and order_ok and end_ok and bus_ok and pp_ok and cmb_ok and cross_ok,
          f"[起始={start_ok} 顺序={order_ok} 展毕={end_ok} 板舱={bus_ok} 板板={pp_ok} "
          f"合束器翼={cmb_ok} 终态十字={cross_ok}]；"
          f"展开起点={wins['sada_X'][0]}（T_S3={T['T_S3']}，需 >T_S3；展毕 T_WING_END={TW}）；"
          f"窗口 SADA={wins['sada_X']}"
          f"／2 号板(object panel_*3)={wins['arm3_X']}／1、3 号板(object panel_*2,panel_*4)="
          f"{wins['arm2_X']},{wins['arm4_X']}（−X 侧 {wins['sada_-X']}/{wins['arm3_-X']}/"
          f"{wins['arm2_-X']},{wins['arm4_-X']}）；板-舱没入深度 max={sep['s4_bus_worst']:.4f} m"
          f"（{sep['s4_bus_hits']} 三角形相交对）——收拢态首帧 {sep['s4_stow_bus']['depth']:.4f} m、"
          f"终态 {sep['s4_bus_terminal']['depth']:.4f} m（{sep['s4_bus_terminal']['n_in']} 个板顶点没入）、"
          f"没入 >2mm 的帧数={len(sep['s4_bus_max_frames'])}/{T['T_S4'] - T['T_S3']}（S4 全程）；"
          f"终态板顶点 min|x|={sep['s4_panel_min_absx']['colA']:.4f} m；板-板没入 max="
          f"{sep['s4_pp_worst']:.4f} m（没入 >2mm 的帧={sep['s4_pp_max_frames'] or '无'}；"
          f"BVH 三角相交对 {sep['s4_pp_hits']} 出现在帧 {sep['s4_pp_hit_frames'][:6]}…"
          f"＝展开到位后相邻板**共面对贴**，非穿过）；合束器翼动作={cmb_ok}"
          f"（漂移={sep['cmb_wing_drift']:.5f} m）；终态十字={cross_ok}")
    report["F7_kinematics"] = _f7_kinematics_note(sep)

    # ---------------- F8 光束淡入 ----------------
    first = sep["beam_first_visible"]
    TW = T["T_WING_END"]
    # 淡入窗口 [T_WING_END+1, T_S4] 必须整段落在 S4 内，且 FADE ≤ S4 段一半（P10）
    fade_ok = (sep["T"]["FADE"] <= T["T_DEPLOY"] / 2 and first == TW + 1
               and TW + 1 > T["T_S3"] and TW + 1 + T["FADE"] <= T["T_S4"])
    em = sep["beam_mats"]
    em_ok, em_row, n_fade = True, [], 0
    for key in em[T["T_TOTAL"]]:
        v = lambda f: (max(abs(x) for x in em[f][key])
                       if isinstance(em[f][key], list) else abs(em[f][key]))
        pre, at1, vd = v(TW), v(1), v(T["T_TOTAL"])
        tol = 1e-4
        # (i) S0–S3（含翼展开段）该通道全程不变（＝不存在／零强度）
        flat = abs(at1 - pre) <= tol
        if abs(vd - pre) <= max(tol, 0.02 * max(abs(vd), 1e-9)):
            good = flat                       # 非淡入通道：全程常数
        else:                                 # 淡入通道：起点≈0、到 T_S4 已达设计值
            n_fade += 1
            good = (flat and pre <= max(tol, 0.02 * vd)
                    and abs(v(T["T_S4"]) - vd) <= max(tol, 0.02 * vd))
        em_ok = em_ok and good
        em_row.append("%s: S0–S3=%.4g→淡入中=%.4g→终=%.4g（%s）"
                      % (key, pre, v(TW + 1 + T["FADE"] // 2), vd,
                         "淡入" if abs(vd - pre) > max(tol, 0.02 * abs(vd)) else "恒定"))
    em_ok = em_ok and n_fade >= 4      # 四束的发光通道必须真的参与淡入
    ends = sep["beam_ends_chk"]
    ep_ok = (all(ends["star_%s" % t]["axis_off"] <= 0.02
                 and 0.0 <= ends["star_%s" % t]["tail_from_ring"] <= 0.02 for t in craft)
             and all(ends["link_%s" % t]["start_off_face"] <= 0.02
                     and ends["link_%s" % t]["end_off_port"] <= 0.02 for t in craft))
    props = sep["beam_props"]
    try:
        ds = props["BEAM_star_colA"]["diameter"]
        dl = props["BEAM_link_colA"]["diameter"]
        ratio = ds / dl
        core_ok, core_row = True, []
        for kind in ("star", "link"):
            d_shell = props["BEAM_%s_colA" % kind]["diameter"]
            d_core = props["BEAM_%s_colA_core" % kind]["diameter"]
            good = abs(d_core - d_shell / 3.0) <= 0.2 * (d_shell / 3.0)
            core_ok = core_ok and good
            core_row.append("%s 芯%.4f/壳%.4f=%.4f（壳/3=%.4f）" % (kind, d_core, d_shell,
                                                              d_core / d_shell, d_shell / 3))
    except Exception:
        ds = dl = ratio = 0
        core_ok = False
        core_row = ["取值失败：%s" % traceback.format_exc(limit=1)]
    import formation as F
    mode_ok = (F.BEAM_MODE == "illustration"
               and all(props[n].get("beam_mode") == "illustration" for n in props
                       if "beam_mode" in props[n]))
    static_ok = not [c for c in sep["beam_anim_channels"]
                     if c in ("location", "rotation_euler", "rotation_quaternion", "scale")]
    check("F8", "光束淡入：S0–S3 四束不存在或零强度；淡入仅发生在 S4 且时长 ≤S4 段一半；"
                "淡入全程端点钉在器上（偏差 ≤0.02 m）；终态束径比 ∈[3,5]、双层结构齐备"
                "（芯径＝壳径/3±20%）、BEAM_MODE＝illustration",
          fade_ok and em_ok and ep_ok and static_ok and 3.0 <= ratio <= 5.0 and core_ok and mode_ok,
          f"[fade={fade_ok} em={em_ok} 端点={ep_ok} 束体静态={static_ok} 束径比={3.0 <= ratio <= 5.0} "
          f"双层={core_ok} 模式={mode_ok}]；"
          f"首帧可见={first}（应＝T_WING_END+1={TW+1}，即翼到位后才出现）、"
          f"存在隐藏束的帧数={sep['beam_hidden_any_frames']}（应＝T_WING_END={TW}）、"
          f"四束全可见帧数={sep['beam_vis_frames']}；"
          f"淡入窗口={TW+1}…{T['T_S4']}（长 {T['FADE']} 帧 ≤ S4/2={T['T_DEPLOY']/2}）；"
          f"端点：星光轴偏 {[ends['star_%s' % t]['axis_off'] for t in craft]}、"
          f"离最内光阑环 {[ends['star_%s' % t]['tail_from_ring'] for t in craft]}（筒口 {[ends['star_%s' % t]['tail_from_mouth'] for t in craft]}）、"
          f"器间窗口/收光口 {[ends['link_%s' % t]['start_off_face'] for t in craft]}／"
          f"{[ends['link_%s' % t]['end_off_port'] for t in craft]}（≤0.02）；"
          f"D_star={ds:.3f} D_link={dl:.3f} 比={ratio:.2f}；芯=壳/3±20%={core_ok}（{'；'.join(core_row)}）；"
          f"BEAM_MODE={F.BEAM_MODE}；束体动画通道={sep['beam_anim_channels']}（端点全程钉死＝无 loc/rot 通道）；"
          f"强度台账：" + "；".join(em_row))

    # ---------------- F9 渲染产物 ----------------
    sizes = {}
    for n in KEY_IMAGES:
        p = os.path.join(OUT, n + ".png")
        sizes[n] = png_size(p) if os.path.exists(p) else (0, 0)
    seq = os.path.join(OUT, "preview")
    n_prev = len([f for f in os.listdir(seq) if f.endswith(".png")]) if os.path.isdir(seq) else 0
    mp4 = os.path.join(OUT, "preview.mp4")
    n_mp4 = 0
    if os.path.exists(mp4):
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams",
                                 "v:0", "-show_entries", "stream=nb_read_frames", "-of",
                                 "csv=p=0", mp4], capture_output=True, text=True, timeout=120)
            n_mp4 = int(re.sub(r"\D", "", pr.stdout) or 0)
        except Exception:
            n_mp4 = -1
    check("F9", "渲染产物：out/separation/ 关键帧定图 6 张 ≥1200×900；预览动画帧数＝总帧数",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()) and n_prev == T["T_TOTAL"],
          f"定图尺寸={sizes}；预览帧序列={n_prev} 帧（应＝{T['T_TOTAL']}）；preview.mp4 帧数={n_mp4}；"
          f"注：既有定图/预览为**上一轮（几何 v3 整改前）产物**，本轮只核存在性与尺寸——"
          f"整改后的关键帧与预览由总体侧另行渲染（本脚本不作渲染，避免覆盖待复核产物）")
    report["render"] = {"key_images": {k: list(v) for k, v in sizes.items()},
                        "preview_frames": n_prev, "preview_mp4_frames": n_mp4}
    return {"F1": locals().get("only_sep"), "T": T}


# ============================================================ F10–F12
def run_tail(S, src, src_info, sep, ref3, ref4, halved, reg):
    T = sep["T"]
    # ---------------- F10 记录项 ----------------
    rep = {
        "stage": "阶段五 组合体→分布式编队分离与展开动画",
        "script": "separation.py",
        "timeline": {
            "fps": T["FPS"], "total_frames": T["T_TOTAL"],
            "frame_range": sep["frame_range"],
            "header_constants": {k: getattr(S, k) for k in
                                 ("T_UNLOCK", "T_LIFT", "T_CRUISE", "T_DEPLOY", "T_HOLD")},
            "segments": [
                {"id": "S1", "name": "解锁（机构态切换，无相对运动）",
                 "frame_start": 1, "frame_end": T["T_S1"], "frames": T["T_S1"]},
                {"id": "S2", "name": "直提离位（+Z 纯平移）",
                 "frame_start": T["T_S1"] + 1, "frame_end": T["T_S2"],
                 "frames": T["T_LIFT"]},
                {"id": "S3", "name": "侧移到位（±Y 纯平移＋末段归位；回执 B）",
                 "frame_start": T["T_S2"] + 1, "frame_end": T["T_S3"],
                 "frames": T["T_CRUISE"]},
                {"id": "S4", "name": "展开与光路建立（十字翼展开＋四束淡入）",
                 "frame_start": T["T_S3"] + 1, "frame_end": T["T_S4"],
                 "frames": T["T_DEPLOY"]},
                {"id": "HOLD", "name": "观测态定格",
                 "frame_start": T["T_S4"] + 1, "frame_end": T["T_TOTAL"],
                 "frames": T["T_HOLD"]},
            ],
            "durations_s": {k: round(v / T["FPS"], 3) for k, v in
                            (("S1", T["T_S1"]), ("S2", T["T_LIFT"]), ("S3", T["T_CRUISE"]),
                             ("S4", T["T_DEPLOY"]), ("HOLD", T["T_HOLD"]))},
        },
        "lift_h_m": S.LIFT_H,
        "fade_frames": T["FADE"],
        "trajectory": {
            "y_start_m": S.Y_START, "y_end_m": S.Y_END,
            "z_root_s0_m": S.Z_ROOT_S0, "z_obs_m": S.Z_OBS,
            # 裁决 B 的算术：S0 时集光器舱心高出合束器舱板段箱心 0.98 m（BAY_T/2 0.525
            # ＋ BUS_H/2 0.455），观测态两者共面 ⇒ 分离全程**净下沉 0.98 m**（逐位吻合）
            "net_sink_m": S.NET_SINK,
            "net_sink_breakdown_m": {"bay_half_t_m": S.A.BAY_T / 2, "bus_half_h_m": S.C.BUS_H / 2},
            "key_frames": {"S0": sep["traj"][0], "S1": sep["traj"][T["T_S1"] - 1],
                           "S2": sep["traj"][T["T_S2"] - 1], "S3": sep["traj"][T["T_S3"] - 1],
                           "END": sep["traj"][T["T_TOTAL"] - 1]},
        },
        "s2_min_gap_m": sep["s2_min_gap"],
        "s2_obstacles": ["cmb_bay（舱板段）", "cmb_module（承力筒）",
                         "cmb_recv_01/02", "cmb_aux_01",
                         "cmb_LOCK_P/N_base（锁紧支座；臂/卡爪按 E21 前提豁免）"],
        "s3_min_gap_m": sep["s3_min_gap"],
        "s3_z_homing_total_m": round(sep["traj"][T["T_S3"] - 1]["colA_tube"][2]
                                     - sep["traj"][T["T_S2"] - 1]["colA_tube"][2], 4),
        "pair_gap_min_m_E10": sep["pair_gap_min"],
        "bus_w_m": sep["bus_w"],
        "camera": sep["cam"],
        "random_seeds": {
            "separation.py": "无新增随机量（源码无 random/seed 调用）"
            if not re.search(r"\b(random|seed|_rng)\b", src) else "见源码",
            "formation_SEED": ref4.get("seed"),
            "formation_RANDOM_LOG": ref4.get("random_log"),
        },
        "beam": {"props": sep["beam_props"], "ends": sep["beam_ends"],
                 "ends_check": sep["beam_ends_chk"], "visibility": sep["beam_vis"],
                 "materials_strength": sep["beam_mats"], "top_z": sep["beam_top_z"]},
        "wing": {"s4_bus_penetration_max_m": sep["s4_bus_worst"],
                 "s4_bus_tri_hits": sep["s4_bus_hits"],
                 "s4_bus_penetration_stowed_m": sep["s4_stow_bus"],
                 "s4_bus_penetration_terminal_m": sep["s4_bus_terminal"],
                 "s4_bus_penetration_frames": sep["s4_bus_max_frames"],
                 "s4_panel_panel_penetration_max_m": sep["s4_pp_worst"],
                 "s4_panel_panel_tri_hits": sep["s4_pp_hits"],
                 "s4_panel_panel_tri_frames": sep["s4_pp_hit_frames"],
                 "s4_panel_min_absx_m": sep["s4_panel_min_absx"],
                 },
        "first_frame_vs_stage3": {
            "key_dims": {"bay": sep["s0"]["bay"], "module": sep["s0"]["module"],
                         "bus_dims": sep["s0"]["bus_dims"],
                         "tube_ctr": sep["s0"]["tube_ctr"]},
            "wing_stowed": sep["s0"]["wing"],
            "stage3_wing": ref3["wing"],
            "stage3_key_dims": {"bay": ref3["bay"], "module": ref3["module"],
                                "tube_ctr": ref3["tube_ctr"]},
            "locks": sep["s0"]["locks"], "locks_visible": sep["s0"]["locks_visible"],
            "beams_visible": sep["s0"]["beams_visible"],
            "visible_frame1": sep["visible"][1],
            "n_visible_frame1": len(sep["visible"][1]),
            "hidden_frame1": sorted(set(sep["objects"]) - set(sep["visible"][1])),
            "n_all": len(sep["objects"]),
        },
        "last_frame_vs_stage4": {
            "s4": sep["s4"], "stage4": {"bus_ctr": ref4["bus_ctr"],
                                        "bay_ctr": ref4["bay_ctr"],
                                        "baseline": ref4["baseline"],
                                        "wing": ref4["wing"],
                                        "locks": ref4["locks"],
                                        "beams": ref4["beams"], "ports": ref4["ports"],
                                        "BEAM_MODE": ref4["BEAM_MODE"]},
            "visible_end": sep["visible"][T["T_TOTAL"]],
            "hidden_end": sorted(set(sep["objects"]) - set(sep["visible"][T["T_TOTAL"]])),
            "lock_vis_end": sep["lock_vis"][T["T_TOTAL"]],
        },
        "visible_sets": sep["visible"],
        "lock_objects": sep["lock_objs"],
        "regressions": reg,
        "f12_halved_T_CRUISE": halved,
        "checks": {},
    }
    req = ("timeline", "lift_h_m", "trajectory", "s2_min_gap_m", "camera", "random_seeds",
           "first_frame_vs_stage3", "last_frame_vs_stage4")
    check("F10", "记录项 separation_report.json：各段帧号与时长、LIFT_H、轨迹端点、S2 最小间隙、"
                 "相机参数、全部随机种子、首/末帧一致性抽检明细",
          all(k in rep and rep[k] is not None for k in req),
          f"字段={sorted(rep.keys())}；段数={len(rep['timeline']['segments'])}；"
          f"种子=separation 无新增／formation SEED {rep['random_seeds']['formation_SEED']}；"
          f"S2 最小间隙={rep['s2_min_gap_m']} m；相机={rep['camera']['location']} lens={rep['camera']['lens']}")
    report.update(rep)

    # ---------------- F11 复用与回归 ----------------
    five_src = {f: open(os.path.join(HERE, f), encoding="utf-8").read() for f in FIVE}
    a = strip_src(src)
    dups = {f: dup_runs(a, strip_src(s), 8) for f, s in five_src.items()}
    imports = {m: bool(re.search(r"^import\s+%s\b" % m, src, re.M))
               for m in ("assembly", "formation", "wing")}
    calls = sorted({"%s.%s" % t for t in re.findall(r"\b([A-Z])\.(\w+)\(", src)})
    # 复用证据：三个模块的构建/装配函数必须被真正调用（"无复制粘贴块"由 dup_runs 另判）。
    # 场景根＝formation.build()（反馈 09 §二），故不再逐件调 assembly 的构件函数；集光器十字翼
    # 与合束器 chain 翼都由 F.build()→formation.build_spacecraft()→wing.build_wing 产出。
    need = ("F.build", "F.build_beams", "F.star_axes_and_mouth", "F.solve_star_top_z",
            "F.build_render_env", "F.point", "W.cross_fold_kinematics", "A.build_lock_mech")
    miss_calls = [c for c in need if c not in calls]
    notes = [c for c in ("W.MOUNT_FACE_X",) if c not in calls]
    try:
        base = json.load(open(os.path.join(OUT, "script_hashes.json"), encoding="utf-8"))
    except Exception:
        base = {}
        traceback.print_exc()
    hashes = {f: hashlib.sha256(open(os.path.join(HERE, f), "rb").read()).hexdigest()[:16]
              for f in FIVE}
    report["verified_revision"] = {
        "separation.py_sha256_16": hashlib.sha256(src.encode("utf-8")).hexdigest()[:16],
        "separation.py_mtime": os.path.getmtime(S.__file__),
        "constants": {k: getattr(S, k) for k in
                      ("T_UNLOCK", "T_LIFT", "T_CRUISE", "T_DEPLOY", "T_HOLD",
                       "T_S1", "T_S2", "T_S3", "T_S4", "T_TOTAL", "WING_FRAC",
                       "T_WING_END", "FADE", "LIFT_H", "Y_END", "Z_OBS")},
    }
    # 限域例外（反馈 09 §三.3）：**仅 wing.py 允许变化**（翼安装方位／SADA 修正／折叠运动学导出）；
    # 其余四条必须与 baseline 逐字节一致——不写成"跳过检查"，而是分两组判。
    hash_frozen_bad = {f: (base.get(f), hashes[f]) for f in FROZEN if base.get(f) != hashes[f]}
    wing_rec = base.get("wing.py")
    wing_changed = wing_rec != hashes["wing.py"]
    reg_ok = all(r.get("ok") for r in reg)
    check("F11", "复用与回归：separation.py 以 import 复用 assembly/formation/wing 的 build 函数"
                 "（静态检查无复制粘贴块）；collector/combiner/assembly/formation/wing 五脚本"
                 "哈希不变；回归 verify.py 13/13、verify_combiner.py 13/13、"
                 "verify_assembly.py 12/12、verify_formation.py 24/24 全 PASS",
          all(imports.values()) and not miss_calls
          and not any(v for v in dups.values()) and not hash_frozen_bad and reg_ok,
          f"import={imports}；跨模块调用 {len(calls)} 处，缺={miss_calls or '无'}；"
          f"（注：{notes or '无'}——锁紧机构经 A.build() 内部复用 assembly.build_lock_mech，"
          f"清单 F11 只要求 import 复用 build 函数，未单列该调用）；"
          f"≥8 行复制粘贴块={ {k: v for k, v in dups.items() if v} or '无' }；"
          f"哈希：collector/combiner/assembly/formation 四条与基线逐字节一致="
          f"{'是' if not hash_frozen_bad else hash_frozen_bad}；"
          f"wing.py＝限域例外（反馈 09 §三.3）——记录值 {wing_rec}／实测 {hashes['wing.py']}"
          f"（{'本次有变化，属许可范围' if wing_changed else '与记录值一致'}），"
          f"仅 wing.py 允许变化、其余四条必须不变；回归="
          + "／".join("%s %s%s" % (r["script"], r.get("count"), "" if r.get("ok") else "←FAIL")
                      for r in reg))
    report["script_hashes"] = {"baseline": base, "measured": hashes,
                               "frozen_must_match": list(FROZEN),
                               "wing_exception": {"allowed": True, "basis": WING_EXCEPTION,
                                                  "recorded": wing_rec,
                                                  "measured": hashes["wing.py"],
                                                  "changed_this_run": wing_changed}}

    # ---------------- F12 时长参数化 ----------------
    h = halved
    T2 = h["T"]
    kf_ok = all(h["kf_root"][t] == [1, T2["T_S1"], T2["T_S2"], T2["T_S3"]] for t in ("colA", "colB"))
    cont_ok = max(h["continuity"].values()) <= 1e-6
    range_ok = h["frame_range"] == [1, T2["T_TOTAL"]]
    sum_ok = (S.T_UNLOCK + S.T_LIFT + 96 + S.T_DEPLOY + S.T_HOLD) == T2["T_TOTAL"]
    # ROOT 轨迹按 1e-6 判（关键帧即设计值）；器体包围盒心按 0.05 判——与 F2 的
    # 「三器舱心 Δx/Δz≤0.05」同口径（舱体大平面有 ≤5 mm 去周期化微起伏，盒心不再是精确 0）
    end_ok = (abs(abs(h["traj"][T2["T_S3"]]["colA"][1]) - S.Y_END) <= 0.02
              and abs(h["traj"][T2["T_S3"]]["colA"][2] - S.Z_OBS) <= 1e-6
              and abs(h["s4"]["colA"]["bus_ctr"][2] - S.Z_OBS) <= 0.05)
    lift_ok = abs(h["traj"][T2["T_S2"]]["colA"][2] - h["traj"][T2["T_S1"]]["colA"][2]
                  - S.LIFT_H) <= 1e-6
    cross_ok2 = all(abs(h["s4"][t]["span_%s" % sx][0] - 1.71) <= 0.171
                    and abs(h["s4"][t]["span_%s" % sx][1] - 1.14) <= 0.114
                    for t in ("colA", "colB") for sx in ("X", "-X"))
    props = h["beam_props"]
    beam_ok = all(props[k].get("beam_mode") == "illustration" for k in props
                  if "beam_mode" in props[k]) and h["FADE"] <= h["T_DEPLOY"] / 2
    src_ok = (all(src_info["literals"].get(k) == v for k, v in SEG_SPEC.items())
              and all(src_info["derived_ok"].values()))
    check("F12", "时长参数化：各段帧号集中文件头常量；改常量（T_CRUISE 192→96）重跑后 F3 按新值"
                 "通过，其余判据不受影响",
          src_ok and kf_ok and cont_ok and range_ok and sum_ok and end_ok and lift_ok
          and cross_ok2 and beam_ok,
          f"源码：常量字面量={src_info['literals']}、派生式集中={src_info['derived_ok']}"
          f"（T_CRUISE→{src_info['derived']}）；散落帧号={src_info['stray_frames'] or '无'}；"
          f"减半复跑：段界={[T2['T_S1'],T2['T_S2'],T2['T_S3'],T2['T_S4']]}、总帧={T2['T_TOTAL']}、"
          f"场景帧范围={h['frame_range']}、段和校验={sum_ok}、ROOT 关键帧={h['kf_root']['colA']}、"
          f"段界跳变 max={max(h['continuity'].values()):.2e}；其余判据：抬升={lift_ok}、"
          f"S3 终值 y=±{S.Y_END}={end_ok}、终态十字 1.71×1.14（±10%）={cross_ok2}、"
          f"光束模式/淡入={beam_ok}、展开起点={h['deploy_start']}（>T_S3={T2['T_S3']}）")


def report_out():
    report.setdefault("checks", {})
    for cid, desc, ok, _d in results:
        report["checks"][cid] = {"ok": ok, "desc": desc}
    report["checks_detail"] = {cid: d for cid, _x, _o, d in results}
    lines = [f"[{'PASS' if ok else 'FAIL'}] {cid} {desc} | {detail}"
             for cid, desc, ok, detail in results]
    n = sum(1 for r in results if r[2])
    lines.append(f"\n{n}/{len(results)} PASS")
    lines.append("\n【待裁决附录】\n" + "\n\n".join(NOTES))
    text = "\n".join(lines)
    print(text)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    try:
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    except Exception:
        traceback.print_exc()


def measure_extra(sep):
    """补充测量：F1/F2 的"该帧可见对象集合"与锁紧两态逐帧可见性。"""
    sc = bpy.context.scene
    T = sep["T"]
    mid = T["T_S1"] // 2
    frames = (1, mid, mid + 1, T["T_S1"], T["T_S2"], T["T_S3"], T["T_S3"] + 1,
              T["T_WING_END"] + 1, T["T_S4"], T["T_TOTAL"])
    vis, lock_vis = {}, {}
    for f in frames:
        sc.frame_set(f)
        vis[f] = sorted(o.name for o in bpy.data.objects if not o.hide_render)
        lock_vis[f] = {o.name: bool(o.hide_render)
                       for o in bpy.data.objects if "_LOCK_" in o.name}
    sep["visible"] = vis
    sep["lock_vis"] = lock_vis
    sep["lock_objs"] = sorted(o.name for o in bpy.data.objects if "_LOCK_" in o.name)
    sep["visible_viewport_gap"] = sorted(
        o.name for o in bpy.data.objects if "_LOCK_" in o.name and not o.hide_viewport)
    sc.frame_set(1)


def main():
    import separation as S
    src = open(S.__file__, encoding="utf-8").read()
    src_info = parse_source(src)
    cam, beams, mats, top_z = S.scene_and_keys()
    sep = measure_sep(S, cam, beams, mats, top_z)
    measure_s0(sep)
    measure_extra(sep)
    measure_wing_kinematics(sep, S)      # 反馈 09 §五 第一步的硬性验收（内部会重建参照，故先跑）
    halved = measure_halved(S)
    ref3 = measure_ref3()
    ref4 = measure_ref4()
    reg = run_regressions()
    run_checks(S, src, src_info, sep, ref3, ref4, halved, reg)
    run_tail(S, src, src_info, sep, ref3, ref4, halved, reg)


try:
    main()
except Exception:
    traceback.print_exc()
    results.append(("EX", "执行异常", False, traceback.format_exc(limit=2)))
report_out()
