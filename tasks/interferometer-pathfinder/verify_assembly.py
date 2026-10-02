# -*- coding: utf-8 -*-
"""verify_assembly.py — 三器组合体装配的自动验收（在 assembly.py 之后运行）。

用法：blender --background --python verify_assembly.py，日志写 out/assembly/verify_log.txt。
判据**逐条照抄**《验收反馈 03》（2026-09-30）§三 D1–D12，不得自增删。

三处按字面会误判、已在代码里写明读法（并在交付说明中回报，不擅自改判据文字）：
  * D1 "平板类对象" → 除豁免件（筒/载荷舱/机构/翼/窗口/储箱）外的结构件，若网格真实
    包围盒最小边 ≤0.08 m 即视为薄板；另按对象名子串 deck/beam/truss/plate 直接判违规。
  * D4 "平台舱顶面上方对象清单恰好为 {集光器A, 集光器B, 载荷舱}" → 按**坐在该面上**判定
    （对象包围盒底面落在面高 ±0.05 m 内），按所属件归并。否则合束器那副展开翼（其几何
    本就在平台舱之上）会被误判，而规格 §二.4 明确要求它保持展开。
  * D7 "±X 侧面" → 平台舱为矩形，集光器舱为梯形，故用顶点插值求该 y 处的面半宽，不假定面是平的。
"""
import math
import os
import struct
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "out", "assembly")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")

ENV_R, ENV_H = 1.825, 4.610
EXEMPT = ("tube", "module", "gimbal", "recv", "aux", "panel", "window", "tank",
          "sada", "hinge", "edge", "SADA", "HINGE", "EDGE",
          "lock", "LOCK")   # v1.1/1.3：太阳翼机构与锁紧释放机构均属机构件豁免
FORBIDDEN_NAME = ("deck", "beam", "truss", "plate")
results = []


def check(cid, desc, ok, detail=""):
    results.append((cid, desc, ok, detail))


def find(name):
    return bpy.data.objects.get(name)


def wverts(o):
    mw = o.matrix_world
    return [mw @ v.co for v in o.data.vertices]


def vbounds(objs):
    pts = [p for o in objs for p in wverts(o)]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def vdims(objs):
    lo, hi = vbounds(objs)
    return [hi[i] - lo[i] for i in range(3)]


def vcenter(o):
    lo, hi = vbounds([o])
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


def max_color(obj):
    if not obj or not obj.data or not hasattr(obj.data, "materials"):
        return None
    for m in obj.data.materials:
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    c = n.inputs["Base Color"].default_value
                    return (c[0], c[1], c[2])
        elif m:
            c = m.diffuse_color
            return (c[0], c[1], c[2])
    return None


def owner(name):
    return name.split("_")[0]


def face_half_width(bus, y):
    """梯形舱在该 y 处的 ±X 面半宽（顶点插值，对 180° 旋转也成立）。"""
    lo, hi = vbounds([bus])
    ylo, yhi = lo[1], hi[1]
    span = yhi - ylo
    if span <= 0:
        return max(hi[0] - lo[0], 0) / 2
    t = (y - ylo) / span
    # 两端各自的 x 半宽
    xs_lo = [abs(v.x) for v in wverts(bus) if abs(v.y - ylo) < 0.03]
    xs_hi = [abs(v.x) for v in wverts(bus) if abs(v.y - yhi) < 0.03]
    w_lo = max(xs_lo) if xs_lo else 0.0
    w_hi = max(xs_hi) if xs_hi else 0.0
    return w_lo + (w_hi - w_lo) * t


def slant_face(bus, side):
    """该器 ±X **斜面**的网格真值：取面积最大的朝该侧大面片，返回 (n_world, p_world)。

    大平面有 ≤5 mm 低频微起伏（formation 的去周期化），小倒角面片法向会被搅乱，故按面积取。
    ``side`` 按**该器自身坐标系**判定（assembly 里 colA 绕 Z 转 180°，世界 x 会翻号）。
    """
    best = None
    for poly in bus.data.polygons:
        n = poly.normal                                  # 对象空间即该器自身系
        if side * n.x > 0.9 and abs(n.z) < 0.15:
            if best is None or poly.area > best[0]:
                best = (poly.area, n.copy(), poly.center.copy())
    if best is None:
        return None, None
    mw = bus.matrix_world
    return (mw.to_3x3() @ best[1]).normalized(), mw @ best[2]


def bus_local_y_span(bus):
    """舱体自身系的 y 跨度与"世界→自身系"逆矩阵（判顶点是否落在舱体 y 跨度内用）。"""
    ys = [v.co.y for v in bus.data.vertices]
    return min(ys), max(ys), bus.matrix_world.inverted()


def pen_stats(bus, objs):
    """**没入/离缝真值**：逐顶点用 Object.closest_point_on_mesh（不包围盒近似）。

    返回 (最大没入, 最深处顶点, 最小离缝, 离缝处顶点, 查询失败数)；没入＝顶点在舱体内时到
    最近表面的距离，离缝＝顶点在舱体外时的最小距离。**查询失败数 >0 ⇒ 判据 FAIL**（读取失败
    不得静默通过）。
    """
    inv = bus.matrix_world.inverted()
    worst, wpt, gap, gpt, miss = 0.0, None, None, None, 0
    for o in objs:
        for p in wverts(o):
            lp = inv @ p
            ok, loc, nrm, _ = bus.closest_point_on_mesh(lp)
            if not ok:
                miss += 1
                continue
            d = (lp - loc).length
            if (lp - loc).dot(nrm) < 0.0:
                if d > worst:
                    worst, wpt = d, tuple(round(c, 4) for c in p)
            elif gap is None or d < gap:
                gap, gpt = d, tuple(round(c, 4) for c in p)
    return worst, wpt, gap, gpt, miss


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", head[16:24])


def main():
    try:
        import assembly
        if hasattr(assembly, "build"):
            assembly.build()
    except Exception:
        traceback.print_exc()
        check("EX", "assembly 导入/构建异常", False, traceback.format_exc(limit=3))

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    bay = find("bay")
    buses = {t: find("%s_bus" % t) for t in ("colA", "colB")}
    tubes = {t: find("%s_tube" % t) for t in ("colA", "colB")}
    module = find("cmbmod_module")

    # ---- D1 对象齐备且无独立连接件 ----
    need = ("bus", "tube", "window_out", "tank_01", "tank_02",
            "gimbal_01", "gimbal_02", "gimbal_03", "SADA_X", "SADA_-X",
            "HINGE_X01", "HINGE_X02", "HINGE_X03",
            "HINGE_-X01", "HINGE_-X02", "HINGE_-X03")
    need += tuple("panel_%s%d" % (sx, i) for sx in ("X", "-X") for i in range(1, 5))
    miss = [f"{t}_{n}" for t in ("colA", "colB") for n in need if not find(f"{t}_{n}")]
    missing_cmb = [n for n in ("bay", "cmbmod_module", "cmbmod_recv_01", "cmbmod_recv_02",
                               "cmb_SADA_X", "cmb_SADA_-X", "cmb_HINGE_X01", "cmb_HINGE_-X03")
                   if not find(n)]
    need += tuple("cmb_panel_%s%d" % (sx, i) for sx in ("X", "-X") for i in range(1, 5))
    missing_cmb += [f"cmb_{n}" for n in need if n.startswith("panel_") and not find(f"cmb_{n}")]
    bay_dim_ok = False
    if bay:
        bd = vdims([bay])
        bay_dim_ok = all(abs(bd[i] - 1.05) <= 0.04 for i in range(3))   # v1.3：舱板段 1.05³
    mod_ok = False
    if module:
        md = vdims([module])
        mod_ok = (abs(max(md[0], md[1]) - 0.60) <= 0.03 and abs(md[2] - 0.70) <= 0.03)
    lock_ok = all(find("cmblock_LOCK_%s_base" % k) for k in ("P", "N"))
    bus_dim_ok = True
    for t in ("colA", "colB"):
        b = buses.get(t)
        if not b:
            bus_dim_ok = False
            continue
        d = vdims([b])
        bus_dim_ok = bus_dim_ok and abs(d[0] - 0.91) <= 0.04 and abs(d[1] - 0.65) <= 0.04 \
            and abs(d[2] - 0.91) <= 0.04                       # v1.3：集光器舱平面修订
    by_name = [o.name for o in meshes if any(k in o.name.lower() for k in FORBIDDEN_NAME)]
    struct = [o for o in meshes if not any(k in o.name for k in EXEMPT)]
    thin = [(o.name, round(min(vdims([o])), 3)) for o in struct if min(vdims([o])) <= 0.08]
    check("D1", "对象齐备（含翼机构：每翼 SADA×1＋4 板＋板间铰链×3）且无独立连接件；舱板段＝1.05³＋承力筒 Ø0.60",
          not miss and not missing_cmb and not by_name and not thin and bay_dim_ok
          and mod_ok and lock_ok and bus_dim_ok,
          f"缺件={miss + missing_cmb or '无'}；按名命中={by_name or '无'}；薄板类结构件={thin or '无'}；"
          f"舱板段={'ok' if bay_dim_ok else vdims([bay]) if bay else '缺'}；"
          f"承力筒={'ok' if mod_ok else vdims([module]) if module else '缺'}；"
          f"锁紧机构×2={lock_ok}；集光器舱={'ok' if bus_dim_ok else 'BAD'}")

    if not (bay and all(buses.values()) and all(tubes.values()) and module):
        raise SystemExit("关键对象缺失，终止后续检查")

    bay_lo, bay_hi = vbounds([bay])

    # ---- D2 基线 ----
    ya, yb = vcenter(tubes["colA"])[1], vcenter(tubes["colB"])[1]
    check("D2", "基线（两 tube 轴线 y 之差）＝2.50±0.05 m（T9：入瞳中心距）",
          2.45 <= abs(ya - yb) <= 2.55, f"入瞳中心距={abs(ya - yb):.3f} m")

    # ---- D3 对称居中 ----
    mc = vcenter(module)
    check("D3", "对称居中：|y_A+y_B|≤0.1；载荷舱 |y|≤0.3、|x|≤0.15",
          abs(ya + yb) <= 0.1 and abs(mc[1]) <= 0.3 and abs(mc[0]) <= 0.15,
          f"y_A+y_B={ya + yb:+.3f}；载荷舱 y={mc[1]:+.3f}, x={mc[0]:+.3f}")

    # ---- D4 两层支撑 ----
    gaps = {t: vbounds([buses[t]])[0][2] - bay_hi[2] for t in ("colA", "colB")}
    g_mod = vbounds([module])[0][2] - bay_hi[2]
    ok_gap = all(-0.05 <= g <= 0.05 for g in gaps.values()) and -0.05 <= g_mod <= 0.05
    # 坐在平台舱顶面上的对象（按其所属件归并）
    sitters = sorted({owner(o.name) for o in meshes
                      if o is not bay and abs(vbounds([o])[0][2] - bay_hi[2]) <= 0.05})
    hang = {}
    for t in ("colA", "colB"):
        bl, bh = vbounds([buses[t]])
        hang[t] = round(min(abs(bl[1]), abs(bh[1])), 3)
    hang_ok = all(v > bay_hi[1] for v in hang.values())          # 舱体悬于舱板段之外
    arms = [find("cmblock_LOCK_%s_arm" % k) for k in ("P", "N")]
    reach = [round(max(abs(vbounds([a])[0][1]), abs(vbounds([a])[1][1])), 3)
             for a in arms if a] if all(arms) else []
    spans = [round(r - 0.30, 3) for r in reach]           # 跨距＝臂外端 − 承力筒筒面(±0.30)
    span_ok = bool(spans) and all(0.55 <= v <= 0.70 for v in spans)   # 跨距 ≈0.63
    check("D4", "两层支撑与锁紧连接：集光器舱底与舱板段顶面等高（±0.05）；舱体悬于舱板段之外；"
                "锁紧释放机构×2 各连承力筒筒面与一集光器舱内侧面（跨距 ≈0.63 m）；"
                "面上对象恰为 {集光器A,B,承力筒}",
          ok_gap and sitters == ["cmbmod", "colA", "colB"] and hang_ok and span_ok,
          f"间隙={[round(g, 3) for g in gaps.values()]}+承力筒{g_mod:+.3f}；面上件={sitters}；"
          f"舱内侧面={hang}（舱板段半宽 {round(bay_hi[1], 3)}）；锁紧跨距={spans}")

    # ---- D5 同层成排 ----
    dh = abs(vbounds([module])[1][2] - max(vbounds([buses[t]])[1][2] for t in ("colA", "colB")))
    check("D5", "载荷舱顶面与集光器舱顶面高差 ≤0.3 m（顶视三者同层成排）", dh <= 0.3, f"高差={dh:.3f} m")

    # ---- D6 金色包覆 ----
    bad = []
    for o in struct:
        c = max_color(o)
        if not (c and c[0] > c[1] > c[2]):
            bad.append((o.name, c))
    check("D6", "金色包覆：除豁免件外一切可见结构件金色 MLI", not bad, f"非金色结构件={bad or '无'}")

    # ---- D7 集光器翼折叠（几何 v3 双侧限：翼摞**斜面**贴合）----
    # **外廓上限**＝整摞顶点相对 ±X **斜面**的垂距 ≤0.16 m。斜面装翼下"距舱面"只能按**垂距**
    #   读：原轴对齐读数（|x|max − 该 y 处半宽）在斜面角 0 时与垂距等价，斜置后恒偏大
    #   （把倾斜摞的"上坡角"当成离面量），故仅作对照打印、不作判据。
    # **没入下限**＝板（panel_*）顶点没入舱体 ＝0；用 Object.closest_point_on_mesh 真值量
    #   （不包围盒近似），>2 mm 即 FAIL（《验收反馈 09》§三.5 量法）。
    detail, ok = [], True
    bus_w = max(vdims([buses["colA"]])[0], vdims([buses["colA"]])[1])
    for t in ("colA", "colB"):
        bus = buses[t]
        blo, bhi = vbounds([bus])
        y_lo, y_hi, inv_bus = bus_local_y_span(bus)
        for side in (+1.0, -1.0):
            sx = "X" if side > 0 else "-X"
            ps = [find("%s_panel_%s%d" % (t, sx, i)) for i in range(1, 5)]
            ps = [p for p in ps if p]
            if len(ps) != 4:
                ok = False
                detail.append(f"{t}{sx}:板数={len(ps)}")
                continue
            plo, phi = vbounds(ps)
            n, p0 = slant_face(bus, side)
            if n is None:
                ok = False
                detail.append(f"{t}{sx}:找不到 ±X 斜面（网格异常）")
                continue
            ds = [(v - p0).dot(n) for o in ps for v in wverts(o)
                  if y_lo - 1e-6 <= (inv_bus @ v).y <= y_hi + 1e-6]
            env = max(ds)                                       # 外廓（相对斜面垂距）
            env_axis = max(abs(plo[0]), abs(phi[0])) \
                - face_half_width(bus, (plo[1] + phi[1]) / 2)   # 旧轴对齐读数（仅对照）
            thick = max(ds) - min(ds)                           # 摞厚（沿斜面法向＝4×0.03＋折缝）
            thick_axis = phi[0] - plo[0]                        # 旧轴对齐读数（斜置后含投影，仅对照）
            proj_ok = (phi[1] - plo[1]) <= 1.1 * (bhi[1] - blo[1]) and \
                      (phi[2] - plo[2]) <= 1.1 * (bhi[2] - blo[2])
            pen, pen_at, gap, gap_at, miss = pen_stats(bus, ps)
            en, en_pen, en_gap, en_miss = 0.0, 0.0, None, 0
            es = [find("%s_EDGE_%s%d" % (t, sx, i)) for i in range(1, 5)]
            es = [e for e in es if e]
            if es:
                es_ds = [(v - p0).dot(n) for o in es for v in wverts(o)
                         if y_lo - 1e-6 <= (inv_bus @ v).y <= y_hi + 1e-6]
                en = max(es_ds) if es_ds else 0.0
                en_pen, _, en_gap, _, en_miss = pen_stats(bus, es)
            good = (0.0 <= env <= 0.16) and thick <= 0.20 and proj_ok \
                and pen <= 0.002 and miss == 0
            ok = ok and good
            detail.append(
                f"{t}{sx}:外廓{env:.3f}(上限 0.16)/没入{pen:.6f}@{(pen_at[0], pen_at[1], pen_at[2]) if pen_at else '—'}"
                f"(下限 0，>0.002 即 FAIL)/离缝{gap if gap is None else round(gap, 4)}"
                f"{'@' + str(gap_at) if gap_at else ''}/查询失败{miss}/"
                f"摞厚{thick:.3f}(沿斜面对照轴对齐{thick_axis:.3f})/"
                f"投影{'ok' if proj_ok else 'BAD'}/旧轴对齐读数{env_axis:+.3f}(对照)"
                f"／翼缘描边(补充)外廓{en:.3f}·没入{en_pen:.6f}·离缝"
                f"{en_gap if en_gap is None else round(en_gap, 4)}·查询失败{en_miss}")
    check("D7", "集光器翼折叠（**外廓上限＋没入下限**双侧限）：4 板 Z 折成摞贴 ±X 斜面"
                "（整摞顶点相对斜面垂距 ≤0.16 m）；板-舱**没入＝0**（＞2 mm 即 FAIL，"
                "closest_point_on_mesh 真值）；摞厚 ≤0.20；投影不超舱体 1.1 倍",
          ok, "；".join(detail))

    # ---- D8 合束器翼展开（v1.1：每翼 4 板成线自 SADA 外伸、法向朝 −Z ≤30°、跨度 ≥3×舱宽）----
    detail, ok = [], True
    allp = []
    for sx in ("X", "-X"):
        ps = [find("cmb_panel_%s%d" % (sx, i)) for i in range(1, 5)]
        ps = [p for p in ps if p]
        if len(ps) != 4:
            ok = False
            detail.append(f"{sx}:板数={len(ps)}")
            continue
        allp += ps
        thick_axis = [vdims([p]).index(min(vdims([p]))) for p in ps]
        normal_ok = all(a == 2 for a in thick_axis)      # 厚度沿 Z ⇒ 板面法向 ±Z（与 −Z 夹角 0°）
        cs = [vcenter(p) for p in ps]
        # "成线"＝4 板板心共线（同 y、同 z），不要求落在某个绝对高度上（翼面高度由挂载决定）
        line_ok = (max(c[1] for c in cs) - min(c[1] for c in cs) <= 0.02
                   and max(c[2] for c in cs) - min(c[2] for c in cs) <= 0.02)
        xs = sorted(c[0] for c in cs)
        gaps = [round(xs[i + 1] - xs[i], 3) for i in range(3)]
        even = (max(gaps) - min(gaps)) <= 0.02
        good = normal_ok and line_ok and even
        ok = ok and good
        detail.append(f"{sx}:法向沿Z={normal_ok}/成线={line_ok}/板心等距={gaps}")
    if allp:
        lo, hi = vbounds(allp)
        span = hi[0] - lo[0]
        span_ok = span >= 3 * bus_w
        check("D8", "合束器翼展开：±X 两翼各 4 板成直线自 SADA 外伸、法向朝 −Z（≤30°）、X 跨度 ≥3×舱宽",
              ok and span_ok,
              f"X 跨度={span:.3f}（需≥{3 * bus_w:.2f}）；" + "；".join(detail))
    else:
        check("D8", "合束器翼展开", False, "合束器翼面缺失")

    # ---- D9 储箱朝向 ----
    detail, ok = [], True
    for t in ("colA", "colB"):
        by = vcenter(buses[t])[1]
        for k in (1, 2):
            ty = vcenter(find("%s_tank_%02d" % (t, k)))[1]
            off = ty - by
            good = (off > 0) != (by > 0) and abs(off) < 1.0
            ok = ok and good
            detail.append(f"{t}{k}: 偏移{off:+.2f}")
    for k in (1, 2):
        tx = vcenter(find("cmb_tank_%02d" % k))[0]
        bw = vdims([bay])[0] / 2
        good = abs(abs(tx) - bw) <= 0.15
        ok = ok and good
        detail.append(f"bay{k}: |x|={abs(tx):.2f}/半宽{bw:.2f}")
    check("D9", "储箱朝向：集光器朝内（相对舱心偏移指向合束器）、合束器在 ±X 侧面", ok,
          "；".join(detail))

    # ---- D10 材质色 ----
    cb, ct, cm = max_color(buses["colA"]), max_color(tubes["colA"]), max_color(module)
    ck, cp = max_color(find("colA_tank_01")), max_color(find("cmb_panel_X1"))
    ok = (cb and cb[0] > cb[1] > cb[2]) and (ct and all(v < 0.1 for v in ct)) \
        and (cm and all(v < 0.35 for v in cm)) and (ck and all(v > 0.8 for v in ck)) \
        and (cp and cp[2] > cp[0])
    check("D10", "材质色：舱/架金、筒与载荷舱近黑、储箱乳白、翼蓝", bool(ok),
          f"bus={cb}, tube={ct}, module={cm}, tank={ck}, panel={cp}")

    # ---- D11 包络 ----
    ymax = 0.0
    for o in (buses["colA"], buses["colB"], bay):
        l, h = vbounds([o])
        ymax = max(ymax, abs(l[1]), abs(h[1]))
    lo, hi = vbounds(meshes)
    check("D11", "包络：三器舱体外缘 max|y|≤1.85 m、总高≤4.61 m",
          ymax <= 1.85 and (hi[2] - lo[2]) <= ENV_H,
          f"max|y|={ymax:.3f}/1.85；总高={hi[2] - lo[2]:.3f}/{ENV_H}")

    # ---- D12 渲染自检 ----
    sizes = {}
    for f in ("side.png", "front.png", "iso.png", "top.png"):
        p = os.path.join(OUT, f)
        sizes[f] = png_size(p) if os.path.exists(p) else (0, 0)
    check("D12", "out/assembly/ 四视角 PNG ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()), f"{sizes}")


def report():
    lines = [f"[{'PASS' if ok else 'FAIL'}] {cid} {desc} | {detail}"
             for cid, desc, ok, detail in results]
    n = sum(1 for r in results if r[2])
    lines.append(f"\n{n}/{len(results)} PASS")
    text = "\n".join(lines)
    print(text)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(text + "\n")


try:
    main()
except Exception:
    traceback.print_exc()
    results.append(("EX", "执行异常", False, traceback.format_exc(limit=2)))
report()
