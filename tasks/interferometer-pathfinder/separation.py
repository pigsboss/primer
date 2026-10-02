# -*- coding: utf-8 -*-
"""阶段五：组合体→分布式编队分离与展开动画（S0→S4）。

用法：
    blender --background --python separation.py              # 预览：六张关键帧定图＋预览动画
    blender --background --python separation.py -- --final   # 正式档全帧（须经审查批准）

依《阶段五_建模规格》v1.1／《阶段五_验收清单》v1.1／《验收反馈_09》：
    S0 组合体锁紧态 → S1 解锁（机构态切换、无相对运动）→ S2 +Z 直提 LIFT_H
    → S3 ±Y 侧移到位＋末段归位（裁决 B）→ S4 集光器十字翼展开（收拢逆序，几何 v3 斜面安装）
    ＋光束淡入。

**场景根＝阶段四规范器体**（《验收反馈_09》§二 整改）：`F.build()` 一次给出三器（含 E13
推断默认件、`EMPTY_LAYOUT`、formation 命名空间的合束器）与四束，本脚本只做三件事——
把三器搬到 S0 组合体布局、补锁紧态构件、按本机位重解星光束并驱动翼与光束。末帧可见集合
因此与阶段四逐件一致（F2）。

**三器的侧别与命名（裁决 A）**：`formation` 本身就是 colA 在 −Y 不转、colB 在 +Y 转 180°，
与"观测态"完全一致，故**不需要**再交换两侧 ROOT 的位姿（原 `swap_collector_sides()` 已删）：
S0 起三器只做平移，姿态守恒（P8）天然成立。

**S0 布局与 z 向算术（裁决 B）**：集光器 holder 置 `(0, ∓1.25, BAY_T/2 + BUS_H/2 = 0.98)`
（舱底落舱板段顶面）；`cmb_ROOT` 保持原点（其局部原点＝舱板段箱心，顶面在 +BAY_T/2）。
观测态（S3/S4）置 `(0, ∓6.0, 0)` ⇒ 三器舱心共面（E3），**净下沉 0.98 m** 恰为裁决 B 的算术。
"""
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import assembly as A
import collector as C
import formation as F
import wing as W

OUT_DIR = os.path.join(HERE, "out", "separation")
REST = {}   # 翼各件的“建造静止姿态” matrix_basis 台账（＝阶段四展开位姿；姿态函数幂等的前提）

# ============================================================ 时序常量（一处改处处改，F12）
FPS = 24
T_UNLOCK = 24      # S1 解锁
T_LIFT = 96        # S2 直提
T_CRUISE = 192     # S3 侧移（含末段归位）
T_DEPLOY = 120     # S4 展开＋光束淡入
T_HOLD = 48        # 观测态定格
T_S1 = T_UNLOCK
T_S2 = T_S1 + T_LIFT
T_S3 = T_S2 + T_CRUISE
T_S4 = T_S3 + T_DEPLOY
T_TOTAL = T_S4 + T_HOLD                     # 480
# S4 段内再分两拍（规格状态表 S4："翼展开；**到位后**光束淡入"）：先展开、后点亮
WING_FRAC = 0.6                             # 翼展开占 S4 的比例，其余留给淡入
T_WING_END = T_S3 + int(WING_FRAC * T_DEPLOY)      # 翼到位帧 = 384
FADE = T_S4 - T_WING_END - 1                # 光束淡入时长 = 47 帧（≤S4 段一半 60，P10）

# ============================================================ 几何常量
LIFT_H = 2.0               # S2 直提高度（E21 走廊）
Y_START = A.BASELINE / 2   # 入瞳中心起点 y=±1.25（T9）
Y_END = 6.0                # 观测态入瞳中心 y=±6.000（P5）
Z_ROOT_S0 = A.BAY_T / 2 + C.BUS_H / 2      # 0.98：集光器舱底落舱板段顶面
Z_OBS = 0.0                                # 观测态：三器舱心共面（舱板段箱心在 cmb_ROOT 原点）
NET_SINK = Z_ROOT_S0 - Z_OBS               # 0.98＝裁决 B 的净下沉算术
FACE_X = (C.BUS_W + C.BUS_SHORT_W) / 4     # 集光器舱 ±X 面 y=0 处半宽（wing.py 内部换算垂距）
SIDE_SIGN = {"colA": -1.0, "colB": +1.0}   # formation 命名：colA 在 −Y 不转、colB 在 +Y 转 180°
WING_UNIT = {                              # 翼件 → 所属驱动单元（1＝0 号板随 SADA 折转）
    "panel_%s1": 1, "EDGE_%s1": 1,
    "panel_%s2": 2, "EDGE_%s2": 2, "HINGE_%s01": 2,
    "panel_%s3": 3, "EDGE_%s3": 3, "HINGE_%s02": 3,
    "panel_%s4": 4, "EDGE_%s4": 4, "HINGE_%s03": 4,
    "SADA_%s": 0,                          # 机构座：wing.py 两态同位 ⇒ 不参与折叠（见 wing_basis）
}
RES, SAMPLES = F.RES, F.SAMPLES
RES_PREVIEW, SAMPLES_PREVIEW = (960, 540), 8
RES_4K, SAMPLES_4K = (3840, 2160), 1024     # 4K 静帧（单帧成本可忽略，故采样给足）
VIEW = dict(location=(17.0, -8.0, 6.0), target=(0.0, 0.0, 0.6), ortho=None, lens=35.0)


def refresh():
    C.refresh()


def ease(t):
    """缓动 ease-in-out（段端速度为零）。"""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def units(side):
    """该侧翼各件（去前缀名 → 驱动单元号）。"""
    sx = "X" if side > 0 else "-X"
    return {k % sx: v for k, v in WING_UNIT.items()}, sx


# ============================================================ 翼运动学（几何 v3：斜面安装＋SADA 方位修正）
def fold_matrix(unit, side, K, kf, ks):
    """**机芯序** X（在"未施加安装方位旋转 R_m"的构建系内，＝ wing.py 收拢构建的坐标）。

    ``kf``＝折叠量（1＝收拢、0＝展开）；``ks``＝(k2,k3,k4) 三块臂板的折开度（0＝折、1＝展）。

        折序＝SADA 铰折（绕 ``hinge``／``hinge_axis`` 转 side·90°·kf）
              ＋沿斜面滑移 dy·kf ＋沿 +Z 落位 settle_dz·kf
              ＋板间 180°·(1−k) 折（铰点／铰轴取 wing.py 同一份 ``arm_hinges``，按 P9 分层）

    **折转方向**（本阶段实测修正）：板间折必须把板**抬离机翼平面（+Z）**翻过去——取
    ``d/dθ(A×r₀)·ẑ > 0`` 的那个方向（A＝铰轴、r₀＝板心相对铰心的面内偏置）。若反向折，
    180° 半程里板会从邻板**体内**扫过：实测板-板没入 15 mm（帧 338–382），判据「展开全程
    板间无相交」不成立。两端点的位姿与方向无关（±180° 同一变换），故收拢态同址不受影响。
    """
    hinge = K["hinge"]
    m_sada = (Matrix.Translation(Vector((0.0, K["dy"] * kf, K["settle_dz"] * kf)))
              @ Matrix.Translation(hinge)
              @ Matrix.Rotation(math.radians(side * K["fold_deg"] * kf), 4, K["hinge_axis"])
              @ Matrix.Translation(-hinge))
    if unit == 1:
        return m_sada                            # 0 号根板：无板间折，只随 SADA 折转
    lvl, p0, axis = K["arm_hinges"][unit]
    p_h = p0 + Vector((0.0, 0.0, 0.5 * K["pitch"] * lvl))
    av = Vector(W.AXIS_VEC[axis])
    sgn = 1.0 if av.cross(K["deployed_center"][unit] - p_h).z > 0.0 else -1.0
    m_arm = (Matrix.Translation(p_h)
             @ Matrix.Rotation(math.radians(sgn * K["arm_fold_deg"] * (1.0 - ks[unit - 2])),
                               4, av)
             @ Matrix.Translation(-p_h))
    return m_sada @ m_arm


def wing_basis(full_name, side, k_sada, ks):
    """翼件在**集光器局部系**内的 matrix_basis（逐帧复算，中间态即机构弧线）。

    复合式（《验收反馈_09》§五 第三步）：

        basis = R_trim(k_sada) @ R_m @ X(kf, ks) @ REST[name]

        R_m    = 安装方位（常量，绕舱轴 Z）＝`W.MOUNT_ROT_Z_DEG(side)`
        R_trim = SADA 方位修正 ＝ R_m⁻¹·k_sada（展开态反向抵消）
        X      = 机芯序（`fold_matrix`，在未施加 R_m 的构建系内）
        REST   = 该件在阶段四展开态下的建造位姿

    两端点（F7 台账逐顶点复算）：
      k_sada=1（展开）：R_trim·R_m = I 且 X = I ⇒ basis = REST —— 与阶段四几何逐位等价；
      k_sada=0（收拢）：basis = R_m·X(1)·REST —— 与 wing.py `state="stowed", topology="cross"`
                        的构建逐件同址（1、2 号板 ≤1e-6；3、4 号板差恰一层 `pitch`，
                        成因＝wing.py 收拢构建按 1/2/3/4 分层、本运动学按 P9 分层，见报告）。

    注：反馈 09 给的复合式写作 `R_trim @ R_m @ X @ R_m.inverted() @ REST`。其中
    `R_m.inverted()` 会把**展开端点**变成 R_m⁻¹·REST（实测：0 号板绕舱轴偏 −13.83°、
    十字不再正交，与"展开态与现几何逐位等价"冲突）。物理读法是"折叠在未施加安装旋转的构建系
    内完成 ⇒ 施加安装旋转 ⇒ 再做 SADA 方位修正"，即上式——两端点均与 wing.py 自身的两态构建
    逐顶点一致（verify_separation 的 F7 台账），故按此实现并把该修正写明。
    """
    base = full_name.split("_", 1)[1]
    unit = {k % ("X" if side > 0 else "-X"): v for k, v in WING_UNIT.items()}[base]
    rest = REST[full_name]
    if unit == 0:
        return rest.copy()          # SADA 机构座：wing.py 两态把底座/球铰建在**同一处**
    K = W.cross_fold_kinematics(side, FACE_X)
    r_m = Matrix.Rotation(math.radians(K["mount_rot_z_deg"]), 4, "Z")
    r_trim = Matrix.Rotation(math.radians(-K["mount_rot_z_deg"] * k_sada), 4, "Z")
    return r_trim @ r_m @ fold_matrix(unit, side, K, 1.0 - k_sada, ks) @ rest


def wing_pose(side, k_sada, ks, tags=("colA", "colB")):
    """把两台器的同侧翼摆到指定开合程度（**全程在集光器局部系内**，不动父子层级）。"""
    table, sx = units(side)
    for tag in tags:
        for base in table:
            ob = bpy.data.objects.get("%s_%s" % (tag, base))
            if ob:
                ob.matrix_basis = wing_basis("%s_%s" % (tag, base), side, k_sada, ks)
    refresh()


def collector_offset(tag, dy, dz):
    h = bpy.data.objects.get("%s_ROOT" % tag)
    if h:
        h.location = (h.location[0], h.location[1] + dy, h.location[2] + dz)
    refresh()


# ============================================================ 锁紧释放机构（S0 锁紧 / S1 起释放）
def add_lock_parts():
    """补锁紧释放机构的**锁紧态**构件（臂＋卡爪）。

    ``formation.build_cmb_parts()`` 只建**释放态**（支座＋短销）；S1「解锁」是机构态切换
    （规格 §二.3），两态构件必须同时在场、互斥显示。锁紧件按同一装配口径用
    ``A.build_lock_mech(side, "locked")`` 取 ``_arm/_jawa/_jawb``；**它顺带建的支座删除**
    （formation 已有同名支座），命名 ``cmb_LOCK_*``、挂到 ``cmb_ROOT``、局部 z 与既有
    ``cmb_LOCK_*_base`` 取齐。
    """
    holder = bpy.data.objects.get("cmb_ROOT")
    base = bpy.data.objects.get("cmb_LOCK_P_base")
    if holder is None or base is None:
        raise RuntimeError("缺 cmb_ROOT／cmb_LOCK_P_base，formation 结构变化？")
    z0 = base.location.z
    parts = []
    for side in (+1.0, -1.0):
        for o in A.build_lock_mech(side, state="locked"):
            me = o.data if o.type == "MESH" else None
            if o.name.endswith("_base"):
                bpy.data.objects.remove(o, do_unlink=True)
                if me is not None and me.users == 0:
                    bpy.data.meshes.remove(me)
                continue
            o.location = (o.location.x, o.location.y, z0 + o.location.z)
            o.parent = holder            # 不设 matrix_parent_inverse：与锁紧支座同一就位口径
            o.name = "cmb_%s" % o.name
            parts.append(o)
    refresh()
    print("[separation] 锁紧释放机构：补建锁紧态构件 %d 件（臂＋卡爪；支座沿用 formation 的）"
          % len(parts), flush=True)
    return parts


def set_locks(state):
    """锁紧释放机构两态切换（S1）：锁紧件（臂/卡爪）与释放件（销）互斥显示；支座两态皆在。"""
    for o in bpy.data.objects:
        if "_LOCK_" not in o.name:
            continue
        if any(k in o.name for k in ("_arm", "_jawa", "_jawb")):
            o.hide_render = o.hide_viewport = (state == "released")
        elif "_pin" in o.name:
            o.hide_render = o.hide_viewport = (state == "locked")
        else:
            o.hide_render = o.hide_viewport = False
    refresh()


# ============================================================ 时间轴
def _fcurves(ob):
    """取对象动画的所有 F-Curve：≤4.3 在 action.fcurves；4.4+ 改为分层 action
    （layers→strips→channelbags→fcurves）——版本敏感 API，取不到即回退，不静默。"""
    ad = ob.animation_data
    if not ad or not ad.action:
        return []
    act = ad.action
    if hasattr(act, "fcurves"):
        return list(act.fcurves)
    out = []
    for layer in getattr(act, "layers", []):
        for strip in getattr(layer, "strips", []):
            for cb in getattr(strip, "channelbags", []) or []:
                out += list(cb.fcurves)
    return out


def _ease_all(ob):
    for fc in _fcurves(ob):
        for kp in fc.keyframe_points:
            kp.interpolation = "SINE"
            kp.easing = "EASE_IN_OUT"
    refresh()


def key_lock_switch():
    """S1 解锁：锁紧态→释放态的**离散**切换（规格 §二.3「抽象处理——机构态切换」）。

    切换点取 S1 段中点（``T_S1 // 2``）：F1 只要求首帧为锁紧态、F2 只要求末帧为释放态，
    段内切换时刻未定；取中点使 S1 这 1 s 两侧各占半拍，观感上"解锁"成为一个可读的动作。
    """
    mid = T_S1 // 2
    for f, st in ((1, "locked"), (mid, "locked"),
                  (mid + 1, "released"), (T_TOTAL, "released")):
        set_locks(st)
        for o in bpy.data.objects:
            if "_LOCK_" in o.name:
                o.keyframe_insert("hide_render", frame=f)
                o.keyframe_insert("hide_viewport", frame=f)   # 视口与渲染两态一致
    refresh()
    print("[separation] 锁紧释放机构：锁紧态 1–%d 帧、释放态 %d–%d 帧（S1 段内切换）"
          % (mid, mid + 1, T_TOTAL), flush=True)


def key_timeline():
    """S0→S4 关键帧：S1 解锁（机构态）、S2 +Z 直提、S3 ±Y 侧移＋末段归位、S4 翼展开。"""
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end, sc.render.fps = 1, T_TOTAL, FPS
    for tag, sgn in SIDE_SIGN.items():
        ob = bpy.data.objects["%s_ROOT" % tag]
        ob.location = (0.0, sgn * Y_START, Z_ROOT_S0)
        ob.keyframe_insert("location", frame=1)
        ob.keyframe_insert("location", frame=T_S1)                     # S1：仍无相对运动
        ob.location = (0.0, sgn * Y_START, Z_ROOT_S0 + LIFT_H)          # S2：+Z 直提
        ob.keyframe_insert("location", frame=T_S2)
        # S3：±Y 侧移到位 + z 归位到观测高度（裁决 B：抬起—平移—落位，落位后与合束器共面 E3）
        ob.location = (0.0, sgn * Y_END, Z_OBS)
        ob.keyframe_insert("location", frame=T_S3)
        _ease_all(ob)
    stow = (0.0, (0.0, 0.0, 0.0))
    for side in (+1.0, -1.0):                                           # S0–S3：保持收拢
        wing_pose(side, *stow)
    for f in (1, T_S3):                                                 # 收拢段：压平
        key_crown(f, 0.0)
    for side in (+1.0, -1.0):
        for f in (1, T_S3):
            _key_wing(side, f)
    key_wing_deploy()                                                   # S4：逐帧烘焙展开
    key_lock_switch()
    sc.frame_set(1)
    refresh()
    print("[separation] 时间轴：S1=%d S2=%d S3=%d S4=%d 定格至 %d（总 %d 帧 @%dfps）；"
          "S0 z=%.3f → 观测 z=%.3f（净下沉 %.2f m）"
          % (T_S1, T_S2, T_S3, T_S4, T_TOTAL, T_TOTAL, FPS, Z_ROOT_S0, Z_OBS, NET_SINK),
          flush=True)


WING_KEYS = ("location", "rotation_euler", "scale")
CROWN_KEY = "crown"       # 帆板 crown（去周期化的微弯）形态键名：0＝压平、1＝阶段四原样
CROWN_FROM = 0.88         # 展开进度到此才开始回弹（此前摞层仍贴紧、或邻板尚在近旁，必须保持压平）


def crown_value(t):
    """crown 形态键取值：收拢／摞层未散开前为 0（压平），展开末段回弹到 1（阶段四原样）。

    为什么必须压平（本阶段实测到的硬冲突，见 verify_log 附 A）：
      crown 沿板**法向**弓起 5–20 mm；收拢折成摞后该方向正是**摞层方向**（层净隙仅 4 mm），
      于是 (a) 整摞外廓由 0.142/0.132 撑到 0.217/0.274 —— F1「距舱面 ≤0.16」与「与阶段三逐件
      一致」双双不成立；(b) 相邻摞层真实相交（实测最大 15 mm）—— F7「展开全程板间无相交」
      不成立。而 wing.py 的收拢构建是**平直板**（收拢态本就由压紧机构夹平）：故收拢段取 0，
      与 wing.py 收拢构建逐顶点同址；展开段回弹到 1，与阶段四几何逐位等价。
    """
    return 0.0 if t <= CROWN_FROM else ease((t - CROWN_FROM) / (1.0 - CROWN_FROM))


def flatten_panels():
    """把 F.build() 各帆板的 crown **移入形态键**，网格本体现为平直（见 ``crown_value``）。

    crown 由 formation.upgrade() 的 ``_crown_mesh`` 施加：沿板**最长轴**取 t，沿**最短轴**
    （板法向）位移 ``amount·(1−t²)``，amount/符号记在 formation.RANDOM_LOG（随机量台账）。
    这里按同一算式**逐顶点反解**，把平直坐标写回网格本体、把带 crown 的坐标写进形态键——
    两块帆板都不重建（沿用 F.build() 的划分与材质）。
    """
    made = 0
    for tag in ("colA", "colB"):
        for side in (+1.0, -1.0):
            sx = "X" if side > 0 else "-X"
            for i in range(1, 5):
                nm = "%s_panel_%s%d" % (tag, sx, i)
                ob = bpy.data.objects.get(nm)
                if ob is None:
                    raise RuntimeError("缺帆板 %s（formation 的十字翼构建变化？）" % nm)
                k_amt, k_sgn = "crown|%s|panel_%s%d" % (tag, sx, i), \
                    "crownsign|%s|panel_%s%d" % (tag, sx, i)
                if k_amt not in F.RANDOM_LOG or k_sgn not in F.RANDOM_LOG:
                    raise RuntimeError("缺 crown 台账 %s（formation 随机量台账变化？）" % k_amt)
                amount = F.RANDOM_LOG[k_amt] * (1.0 if F.RANDOM_LOG[k_sgn] > 0 else -1.0)
                me = ob.data
                vs = [v.co.copy() for v in me.vertices]
                ext = [max(v[k] for v in vs) - min(v[k] for v in vs) for k in range(3)]
                span_ax, norm_ax = ext.index(max(ext)), ext.index(min(ext))
                half = max(abs(v[span_ax]) for v in vs) or 1.0
                flat = []
                for v in vs:
                    t = v[span_ax] / half
                    c = v.copy()
                    c[norm_ax] -= amount * (1.0 - t * t)
                    flat.append(c)
                # 形态键：**Basis＝平直**、键「crown」＝阶段四原样（value 1）
                # （有形态键后 me.vertices 不可再写，必须写 key_blocks 的 data）
                ob.shape_key_add(name="Basis", from_mix=False)
                key = ob.shape_key_add(name=CROWN_KEY, from_mix=False)
                kbs = ob.data.shape_keys.key_blocks
                for n, co in enumerate(flat):
                    kbs["Basis"].data[n].co = co
                for n, co in enumerate(vs):
                    key.data[n].co = co
                key.value = 1.0
                made += 1
    C.refresh()
    print("[separation] 帆板 crown：%d 块移入形态键「%s」（收拢段压平、展开末段回弹；"
          "crown 幅度 0.005–0.020 m／块，台账＝formation.RANDOM_LOG）" % (made, CROWN_KEY),
          flush=True)
    return made


def crown_keys():
    """场景内全部帆板 crown 形态键（其余对象没有该键）。"""
    out = []
    for ob in bpy.data.objects:
        if ob.type != "MESH" or ob.data is None or ob.data.shape_keys is None:
            continue
        kb = ob.data.shape_keys.key_blocks.get(CROWN_KEY)
        if kb:
            out.append(kb)
    return out


def key_crown(frame, v):
    for kb in crown_keys():
        kb.value = v
        kb.keyframe_insert("value", frame=frame)
    refresh()


def wing_phase(t):
    """S4 展开相位（P9＝收拢逆序）：0–0.35 SADA 转出整摞；0.35–0.65 2 号板翻开；
    0.65–1 1/3 号板翻开。返回 ``(k_sada, (k2, k3, k4))``；各段内部缓动、段端速度为零。

    末段的两块对边板（对象 panel_*2 与 panel_*4）**按摞层次序自上而下依次翻开**（不是同时）：
    折叠时 panel_*4 压在 panel_*2 之上（P9 分层 1、(2)、(4)、(3)），逆序展开必须**顶层先走**，
    否则上升的板会从仍在摞中的邻板体内穿过（实测同时翻开时板-板没入 15 mm，帧 360–368）。
    两块板同属 P9 的「1/3 号板」一拍，先后不改变该顺序判据。
    """
    if t <= 0.35:
        return ease(t / 0.35), (0.0, 0.0, 0.0)
    if t <= 0.65:
        return 1.0, (0.0, ease((t - 0.35) / 0.30), 0.0)
    if t <= 0.825:                       # 上层先走（panel_*4，摞层 2）
        return 1.0, (0.0, 1.0, ease((t - 0.65) / 0.175))
    return 1.0, (ease((t - 0.825) / 0.175), 1.0, 1.0)


def key_wing_deploy():
    """S4 翼展开逐帧烘焙（T_S3+1 … T_WING_END）。

    逐帧打键（而非首尾两键）是为了让**中间态就是机构弧线**：Blender 对矩阵分解出的
    loc/rot 各自插值，若只打两端键，中间帧会退化成"翼板绕自身原点转 + 直线平移"，
    板角会扫入舱体；逐帧按 ``wing_phase`` 复算则中间态与端点同源（F7）。
    同一批帧里也对 crown 形态键打键：摞层未散开前压平（0），展开末段回弹到 1。
    """
    span = float(T_WING_END - T_S3)
    for f in range(T_S3 + 1, T_WING_END + 1):
        t = (f - T_S3) / span
        k_sada, ks = wing_phase(t)
        for side in (+1.0, -1.0):
            wing_pose(side, k_sada, ks)
            _key_wing(side, f, ease_keys=False)     # 逐帧已是机构弧线，无需再缓动
        key_crown(f, crown_value(t))
        refresh()


def _key_wing(side, frame, ease_keys=True):
    table, sx = units(side)
    for tag in ("colA", "colB"):
        for base in sorted(table):
            ob = bpy.data.objects.get("%s_%s" % (tag, base))
            if ob:
                for ch in WING_KEYS:
                    ob.keyframe_insert(ch, frame=frame)
                if ease_keys:
                    _ease_all(ob)


# ============================================================ 光束（S4 淡入）
BEAM_LAYERS = ("shell", "core", "end")


def beam_materials():
    """取 formation 已建的四束材质（阶段四材质管线一行不改）。"""
    return {kind: {lay: bpy.data.materials["MAT_beam_%s_%s" % (kind, lay)]
                   for lay in BEAM_LAYERS} for kind in ("star", "link")}


def rebuild_beams(cam):
    """按**本机位**重解星光束长度并重建四束。

    formation 的束长是按阶段四五视角反算的；阶段五相机是固定机位 ``VIEW``，必须用
    ``F.solve_star_top_z`` 为本机位重解束顶（F8：任何帧不露空中截止端面），再按同一
    ``F.build_beams`` 重建（端点由实测包围盒反算，E5/E6 口径不变）。
    """
    layout = bpy.data.objects["EMPTY_LAYOUT"]
    for o in [x for x in bpy.data.objects if x.name.startswith("BEAM_")]:
        bpy.data.objects.remove(o, do_unlink=True)
    craft = {t: {"objs": {o.name: o for o in bpy.data.objects if o.name.startswith(t + "_")}}
             for t in ("colA", "colB")}
    axes, mouth = F.star_axes_and_mouth(craft)
    top_z = F.solve_star_top_z(cam, axes, mouth + F.STAR_ABOVE_MIN, (("key", VIEW),))
    mats = beam_materials()
    ports = {"posY": bpy.data.objects["cmb_port_posY"],
             "negY": bpy.data.objects["cmb_port_negY"]}
    groups, _shells = F.build_beams(craft, ports, top_z, mats["star"], mats["link"])
    objs = [o for g in groups.values() for o in g] + list(ports.values())
    for o in objs:
        o.parent = layout
        o.hide_viewport = o.hide_render = True          # S0–S3：不存在（F8 允许）
    refresh()
    print("[separation] 星光束：筒口 z=%.2f → 束顶 z=%.2f（长 %.2f m，本机位反算）"
          % (mouth, top_z, top_z - mouth), flush=True)
    return objs, mats, top_z


def key_beam_visibility(objs):
    """S0–S3 四束不存在（hide_render=True）；**翼到位后**（T_WING_END+1）起显示。

    "到位后淡入"是规格状态表 S4 一行的字面要求，故光束出现晚于翼展开结束（F8）。
    """
    for o in objs:
        for f, hid in ((1, True), (T_WING_END, True), (T_WING_END + 1, False)):
            o.hide_render = o.hide_viewport = hid
            o.keyframe_insert("hide_render", frame=f)
            o.keyframe_insert("hide_viewport", frame=f)
    refresh()


def key_beam_fade(mats):
    """S4 段后半淡入：壳体 alpha/强度、芯与端点发光强度自 0 升至设计值（P10、F8）。"""
    start, end = T_WING_END + 1, T_S4
    for kind in ("star", "link"):
        for layer in BEAM_LAYERS:
            m = mats[kind][layer]
            nt = m.node_tree
            b = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
            s = b.inputs["Emission Strength"]
            target = s.default_value
            linked = bool(s.links)
            mr = s.links[0].from_node if linked else None
            tmin = mr.inputs[0].default_value if linked else None
            tmax = mr.inputs[1].default_value if linked else None
            alpha = b.inputs["Alpha"].default_value if "Alpha" in b.inputs else 1.0
            for f, k in ((start, 0.0), (end, 1.0)):
                if linked:
                    mr.inputs[0].default_value = tmin * k
                    mr.inputs[1].default_value = tmax * k
                    mr.inputs[0].keyframe_insert("default_value", frame=f)
                    mr.inputs[1].keyframe_insert("default_value", frame=f)
                else:
                    s.default_value = target * k
                    s.keyframe_insert("default_value", frame=f)
                if "Alpha" in b.inputs and alpha < 1.0:
                    b.inputs["Alpha"].default_value = alpha * k
                    b.inputs["Alpha"].keyframe_insert("default_value", frame=f)
    refresh()


def unhide_beams(objs, frame):
    for o in objs:
        o.hide_viewport = o.hide_render = False
    refresh()


# ============================================================ 相机与渲染
def setup_env(final=False):
    F.build_render_env(final=final)
    cam = bpy.data.objects.get("RIG_Cam") or F.add_camera()
    F.point(cam, **VIEW)
    return cam


def render_keyframes(cam=None, out_dir=None, final=False):
    """六张关键帧定图（S0／S1／S2 末／S3 中／S3 末／S4 末）。

    预览档 1200×900/64（`RES`/`SAMPLES`）；正式档 1920×1080/512（`RES_FINAL`/`SAMPLES_FINAL`，
    由 `F.build_render_env(final=True)` 设定）。``out_dir`` 给定时写到别处，便于预览档与正式档并存。
    """
    sc = bpy.context.scene
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    if not final:
        sc.render.resolution_x, sc.render.resolution_y = RES
        sc.cycles.samples = SAMPLES
    shots = (("key_S0", 1), ("key_S1", T_S1), ("key_S2end", T_S2),
             ("key_S3mid", (T_S2 + T_S3) // 2), ("key_S3end", T_S3), ("key_S4end", T_TOTAL))
    for name, f in shots:
        sc.frame_set(f)
        sc.render.filepath = os.path.join(out_dir, name + ".png")
        bpy.ops.render.render(write_still=True)
        print("[separation] 出图 %s（帧 %d）" % (sc.render.filepath, f), flush=True)


def render_final(cam=None):
    """**正式档全帧**（规格 §四；须经总体侧目检通过并签发后方可运行）。

    1920×1080 @512 samples、GPU、降噪、16 bit PNG，输出到 ``out/separation/final/``；
    同时按正式档质量重出六张定图。帧率/时长/时序与预览档同一套文件头常量。
    """
    sc = bpy.context.scene
    seq = os.path.join(OUT_DIR, "final")
    os.makedirs(seq, exist_ok=True)
    # 只做"正式档参数覆盖"，**不重跑** F.build_render_env——它会新建一套灯光/世界，
    # 在已建场景上再调用一次会把主光与补光各复制一份（照度翻倍）。
    sc.render.engine = "CYCLES"
    sc.render.resolution_x, sc.render.resolution_y = F.RES_FINAL
    sc.cycles.samples = F.SAMPLES_FINAL
    sc.cycles.use_denoising = True
    try:
        sc.cycles.device = "GPU"
    except Exception:
        pass
    sc.render.image_settings.file_format = "PNG"     # EXR 留给单帧底片，动画帧用 16bit PNG
    sc.render.image_settings.color_depth = "16"
    sc.frame_start, sc.frame_end = 1, T_TOTAL
    render_keyframes(cam, out_dir=seq, final=True)
    sc.render.filepath = os.path.join(seq, "f")
    print("[separation] 正式档：%d×%d @%d samples，%d 帧 → %s"
          % (sc.render.resolution_x, sc.render.resolution_y, sc.cycles.samples, T_TOTAL, seq),
          flush=True)
    bpy.ops.render.render(animation=True)
    n = len([f for f in os.listdir(seq) if f.startswith("f") and f.endswith(".png")])
    print("[separation] 正式档帧序列 %s（%d 帧 / 总帧数 %d）" % (seq, n, T_TOTAL), flush=True)


def render_preview(f0=None, f1=None):
    """预览动画：EEVEE，低分辨率，帧数＝T_TOTAL（规格 §四）。"""
    sc = bpy.context.scene
    if f0 is not None:
        sc.frame_start = f0
    if f1 is not None:
        sc.frame_end = f1
    sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in         [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]         else "BLENDER_EEVEE"
    sc.render.resolution_x, sc.render.resolution_y = RES_PREVIEW
    sc.render.image_settings.file_format = "PNG"
    seq = os.path.join(OUT_DIR, "preview")
    os.makedirs(seq, exist_ok=True)
    sc.render.filepath = os.path.join(seq, "f")
    bpy.ops.render.render(animation=True)
    n = len([f for f in os.listdir(seq) if f.endswith(".png")])
    print("[separation] 预览动画帧序列 %s（%d 帧，总帧数 %d）" % (seq, n, T_TOTAL), flush=True)


def register_rest():
    """登记两台器十字翼各件的建造位姿（＝阶段四展开态）——姿态函数幂等的前提。"""
    for tag in ("colA", "colB"):
        for side in (+1.0, -1.0):
            table, sx = units(side)
            for base in table:
                full = "%s_%s" % (tag, base)
                ob = bpy.data.objects.get(full)
                if ob is None:
                    raise RuntimeError("缺翼件 %s（formation 的十字翼构建变化？）" % full)
                REST[full] = ob.matrix_basis.copy()


def scene_and_keys():
    """建分离动画场景＋全部关键帧（**不打图**）。返回 (cam, beam_objs, mats, top_z)。

    verify_separation.py 直接调它复跑，避免重复构建逻辑（也保证"两状态几何同源"可核）。
    """
    C.purge_scene()
    F.build()                       # 阶段四规范器体：三器＋E13 推断默认件＋EMPTY_LAYOUT＋四束
    flatten_panels()                # 帆板 crown 移入形态键（收拢压平／展开回弹）
    register_rest()
    cam = setup_env()
    beam_objs, mats, top_z = rebuild_beams(cam)   # 此刻三器仍在观测位姿 ⇒ 端点在位
    for tag, sgn in SIDE_SIGN.items():            # 搬到 S0 组合体布局（只平移，不动姿态）
        bpy.data.objects["%s_ROOT" % tag].location = (0.0, sgn * Y_START, Z_ROOT_S0)
    bpy.data.objects["cmb_ROOT"].location = (0.0, 0.0, Z_OBS)
    refresh()
    add_lock_parts()
    for side in (+1.0, -1.0):
        wing_pose(side, 0.0, (0.0, 0.0, 0.0))     # S0：收拢成摞贴斜面
    key_timeline()
    key_beam_visibility(beam_objs)
    key_beam_fade(mats)
    print("[separation] S0 布局：集光器 y=±%.2f z=%.2f（舱底落舱板段顶面）；"
          "观测态 y=±%.1f z=%.2f（三器舱心共面）；斜面安装 d_perp=%.6f dy=%.6f"
          % (Y_START, Z_ROOT_S0, Y_END, Z_OBS,
             W.MOUNT_FACE_X(FACE_X), W.MOUNT_FACE_DY(FACE_X)), flush=True)
    return cam, beam_objs, mats, top_z


def render_still4k(cam=None, frame=None):
    """4K 静帧（默认观测态末帧）：3840×2160 @1024 samples、GPU、降噪。

    同时留 16 bit PNG（可看）与 EXR 底片（规格 §五 的正式档底片口径，与 formation 同）。
    与正式档动画同机位（``VIEW``），故它就是正式档末帧的 4K 版。
    """
    sc = bpy.context.scene
    frame = T_TOTAL if frame is None else frame
    sc.render.engine = "CYCLES"                      # 覆盖参数，不重跑 build_render_env（避免灯翻倍）
    sc.render.resolution_x, sc.render.resolution_y = RES_4K
    sc.cycles.samples = SAMPLES_4K
    sc.cycles.use_denoising = True
    try:
        sc.cycles.device = "GPU"
    except Exception:
        pass
    sc.frame_set(frame)
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_depth = "16"
    sc.render.filepath = os.path.join(OUT_DIR, "still4k_f%04d.png" % frame)
    bpy.ops.render.render(write_still=True)
    print("[separation] 4K 静帧 %s（%d×%d @%d，帧 %d）"
          % (sc.render.filepath, RES_4K[0], RES_4K[1], SAMPLES_4K, frame), flush=True)
    sc.render.image_settings.file_format = "OPEN_EXR"      # 正式档底片
    sc.render.filepath = os.path.join(OUT_DIR, "still4k_f%04d.exr" % frame)
    bpy.ops.render.render(write_still=True)
    print("[separation] 4K 底片 %s" % sc.render.filepath, flush=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    final = "--final" in argv
    cam, beam_objs, mats, top_z = scene_and_keys()
    if "--still4k" in argv:
        fr = next((int(a.split("=")[1]) for a in argv if a.startswith("--still4k=")), None)
        render_still4k(cam, fr)
    elif final:
        render_final(cam)               # 正式档 1920×1080 @512（须经签发）
    else:
        render_keyframes(cam)
        render_preview()
    print("[separation] 完成：%d 个对象，%d 帧" % (len(bpy.context.scene.objects), T_TOTAL),
          flush=True)


if __name__ == "__main__":
    main()
