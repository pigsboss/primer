# -*- coding: utf-8 -*-
"""verify.py — 集光器模型自动验收（在 collector.py 之后运行）。

用法：blender --background --python verify.py
本版对齐《验收清单》2026-09-30 修订：tank_* 改名、A9 加 +Y 符号校验、A7 措辞、
新增 A13 平面梯形判据；A4/A11/A12 改用**网格顶点**求真实包围盒（摆脱"对象原点必须
在几何中心"的隐含契约）。A11/A12 为上一轮返工引入的硬伤判据，2026-09-30 清单漏列，
此处保留。
"""
import os
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")

results = []


def check(cid, desc, ok, detail=""):
    results.append((cid, desc, ok, detail))


def wverts(o):
    mw = o.matrix_world
    return [mw @ v.co for v in o.data.vertices]


def vbounds(objs):
    """按网格顶点求世界包围盒（不依赖对象原点与 dimensions）。"""
    pts = [p for o in objs for p in wverts(o)]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def vdims(objs):
    lo, hi = vbounds(objs)
    return [hi[i] - lo[i] for i in range(3)]


def top_verts(o, tol=0.02):
    vs = wverts(o)
    zmax = max(v.z for v in vs)
    return [v for v in vs if abs(v.z - zmax) < tol]


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


def find(name):
    return bpy.data.objects.get(name)


def main():
    # 1) 构建场景；**导入失败必须报出来**——否则会在空场景上跑出一串误导性 FAIL
    try:
        sys.path.insert(0, HERE)
        import collector
        if hasattr(collector, "build"):
            collector.build()
    except Exception:
        traceback.print_exc()
        check("EX", "collector 导入/构建异常", False, traceback.format_exc(limit=3))

    bus, tube = find("bus"), find("tube")
    gimbals = [o for o in bpy.data.objects if o.name.startswith("gimbal")]
    tanks = [o for o in bpy.data.objects if o.name.startswith("tank")]
    panels = {n: find(n) for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")}
    window = find("window_out")

    # A1 对象齐备
    ok = (bus and tube and 3 <= len(gimbals) <= 4 and len(tanks) >= 2
          and all(panels.values()) and window)
    check("A1", "场景对象齐备", bool(ok),
          f"bus={bool(bus)}, tube={bool(tube)}, gimbals={len(gimbals)}, tanks={len(tanks)}, "
          f"panels={all(panels.values())}, window={bool(window)}")
    if not (bus and tube):
        raise SystemExit("bus/tube 缺失，终止后续检查")

    bus_dims = vdims([bus])
    bus_w = max(bus_dims[0], bus_dims[1])
    bus_h = bus_dims[2]
    bus_lo, bus_hi = vbounds([bus])
    tube_dims = vdims([tube])
    tube_d = max(tube_dims[0], tube_dims[1])
    tube_lo, tube_hi = vbounds([tube])

    # A2 镜筒口径
    check("A2", "镜筒外径 0.55–0.65 m", 0.55 <= tube_d <= 0.65, f"d={tube_d:.3f}")
    # A3 比例①
    r1 = tube_d / bus_w
    check("A3", "镜筒外径/舱宽 ∈ [0.40,0.55]", 0.40 <= r1 <= 0.55, f"{r1:.3f}")
    # A4 比例②（顶点法）
    r2 = (tube_hi[2] - bus_hi[2]) / bus_h
    check("A4", "镜筒顶面高出舱顶/舱高 ∈ [0.7,1.1]", 0.7 <= r2 <= 1.1, f"{r2:.3f}")
    # A5 比例③
    pt = [p for n, p in panels.items() if p]
    plo, phi = vbounds(pt)
    r3 = (phi[0] - plo[0]) / bus_w
    check("A5", "太阳翼展开总宽/舱宽 ∈ [3.0,4.5]", 3.0 <= r3 <= 4.5,
          f"{r3:.3f}（翼展 {phi[0] - plo[0]:.3f} m）")
    # A6 材质色
    cb, ct = max_color(bus), max_color(tube)
    ck = max_color(tanks[0]) if tanks else None
    cp = max_color(panels["panel_X1"])
    ok = (cb and cb[0] > cb[1] > cb[2]) and (ct and all(v < 0.1 for v in ct)) \
        and (ck and all(v > 0.8 for v in ck)) and (cp and cp[2] > cp[0])
    check("A6", "材质色：舱金/筒黑/储箱乳白/翼蓝", bool(ok),
          f"bus={cb}, tube={ct}, tank={ck}, panel={cp}")
    # A7 太阳翼：±X 两侧、不沿 Y 偏置；板面竖立（弦向沿 Z、法线 ±Y）
    ys = [(p.location.y) for p in pt]
    vertical = all(vdims([p])[2] > vdims([p])[1] and vdims([p])[1] <= 0.08 for p in pt)
    check("A7", "四板分居 ±X、不沿 Y 偏置；板面竖立（弦向沿 Z）",
          all(abs(y) < 0.3 * bus_w for y in ys) and vertical,
          f"ys={[round(y, 2) for y in ys]}, 竖立={vertical}")
    # A8 储箱贴 +Y 侧面微凸
    tstat = []
    for t in tanks:
        tlo, thi = vbounds([t])
        tstat.append((round((tlo[1] + thi[1]) / 2, 3), round(thi[1] - bus_hi[1], 3)))
    check("A8", "两只储箱贴 +Y 侧面（长边、朝相邻器）微凸",
          len(tanks) == 2 and all(cy > 0 and abs(cy - bus_hi[1]) <= 0.15 for cy, _ in tstat),
          f"(中心y, 凸出舱面)={tstat}；舱 +Y 面 y={bus_hi[1]:.2f}")
    # A9 出光窗口：镜筒根部侧面，朝 +Y（符号校验）
    if window:
        wlo, whi = vbounds([window])
        wy = (wlo[1] + whi[1]) / 2
        wz = (wlo[2] + whi[2]) / 2
        tc = (tube_lo[2] + tube_hi[2]) / 2
        ok = wy > 0.3 * tube_d and wz < tc
        check("A9", "出光窗口位于镜筒根部侧面、朝 +Y（符号校验）", bool(ok),
              f"窗口 y={wy:.3f}（需 >{0.3 * tube_d:.3f}）、z={wz:.3f} < 镜筒中心 z={tc:.3f}")
    else:
        check("A9", "出光窗口位于镜筒根部侧面、朝 +Y", False, "window_out 缺失")
    # A10 自检渲染
    imgs = [f for f in ("side.png", "front.png", "iso.png", "top.png")
            if os.path.exists(os.path.join(OUT, f))]
    check("A10", "out/ 四视角渲染图", len(imgs) == 4, f"found={imgs}")
    # A11 镜筒落在舱顶（不悬空）
    gap = tube_lo[2] - bus_hi[2]
    check("A11", "镜筒落在舱顶上（不悬空）", -0.3 <= gap <= 0.05, f"gap={gap:.3f} m")
    # A12 舱体顶面为完整平面
    top = top_verts(bus)
    if len(top) >= 4:
        sx = max(v.x for v in top) - min(v.x for v in top)
        sy = max(v.y for v in top) - min(v.y for v in top)
        check("A12", "舱体顶面为完整平面（非楔形）",
              sx >= 0.7 * bus_dims[0] and sy >= 0.7 * bus_dims[1],
              f"top_verts={len(top)}, span=({sx:.2f},{sy:.2f}) vs bus=({bus_dims[0]:.2f},{bus_dims[1]:.2f})")
    else:
        check("A12", "舱体顶面为完整平面（非楔形）", False, f"top_verts={len(top)}")
    # A13 平面梯形：顶面按 y 正负分组比 X 跨度，短边/长边 ∈[0.5,0.8]，长边在 +Y
    if len(top) >= 4:
        pos = [v for v in top if v.y > 0]
        neg = [v for v in top if v.y < 0]
        sx_pos = max(v.x for v in pos) - min(v.x for v in pos) if pos else 0.0
        sx_neg = max(v.x for v in neg) - min(v.x for v in neg) if neg else 0.0
        lo_w, hi_w = sorted((sx_pos, sx_neg))
        ratio = lo_w / hi_w if hi_w > 0 else 0.0
        check("A13", "平面为梯形：短边/长边 ∈[0.5,0.8] 且长边在 +Y（朝相邻器）",
              0.5 <= ratio <= 0.8 and sx_pos > sx_neg,
              f"+Y 侧跨 {sx_pos:.3f}、−Y 侧跨 {sx_neg:.3f}，短/长={ratio:.3f}")
    else:
        check("A13", "平面为梯形", False, f"top_verts={len(top)}")


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
