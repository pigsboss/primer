# -*- coding: utf-8 -*-
"""verify_combiner.py — 合束器模型自动验收（在 combiner.py 之后运行）。

用法：blender --background --python verify_combiner.py
本版对齐《阶段二_验收清单》与 2026-09-30 修订：tank_* 改名、新增 C13 平面梯形判据、
C11 读取失败即 FAIL；C3/C4 改用**网格顶点**求真实包围盒（摆脱对象原点契约）。
"""
import os
import re
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "combiner")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "verify_log.txt")

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


def collector_bus_width():
    """读 collector.py 常量区，拿集光器舱宽；**读不到返回 None，由调用方判 FAIL**。"""
    try:
        src = open(os.path.join(HERE, "collector.py"), encoding="utf-8").read()
        m = re.search(r"BUS_(?:WIDTH|SIZE|W)\s*=\s*\(?\s*([0-9.]+)", src)
        if m:
            return float(m.group(1))
        m = re.search(r"BUS_\w*=\s*\(\s*([0-9.]+)\s*,", src)
        if m:
            return float(m.group(1))
    except Exception:
        traceback.print_exc()
    return None


def main():
    try:
        sys.path.insert(0, HERE)
        import combiner
        if hasattr(combiner, "build"):
            combiner.build()
    except Exception:
        traceback.print_exc()
        check("EX", "combiner 导入/构建异常", False, traceback.format_exc(limit=3))

    bus, module = find("bus"), find("module")
    recvs = [o for o in bpy.data.objects if o.name.startswith("recv")]
    tanks = [o for o in bpy.data.objects if o.name.startswith("tank")]
    panels = {n: find(n) for n in ("panel_X1", "panel_X2", "panel_-X1", "panel_-X2")}

    check("C1", "场景对象齐备",
          bool(bus and module and len(recvs) >= 2 and len(tanks) >= 2 and all(panels.values())),
          f"bus={bool(bus)}, module={bool(module)}, recvs={len(recvs)}, tanks={len(tanks)}, "
          f"panels={all(panels.values())}")
    if not (bus and module):
        raise SystemExit("bus/module 缺失，终止后续检查")

    bus_dims = vdims([bus])
    bus_w = max(bus_dims[0], bus_dims[1])
    bus_h = bus_dims[2]
    bus_lo, bus_hi = vbounds([bus])
    mod_dims = vdims([module])
    mod_d = max(mod_dims[0], mod_dims[1])
    mod_lo, mod_hi = vbounds([module])

    check("C2", "载荷舱外径/舱宽 ∈ [0.35,0.50]", 0.35 <= mod_d / bus_w <= 0.50, f"{mod_d / bus_w:.3f}")
    over = mod_hi[2] - bus_hi[2]
    check("C3", "载荷舱高出舱顶/舱高 ∈ [0.5,0.9]", 0.5 <= over / bus_h <= 0.9, f"{over / bus_h:.3f}")
    gap = mod_lo[2] - bus_hi[2]
    check("C4", "载荷舱落在舱顶（不悬空）", -0.3 <= gap <= 0.05, f"gap={gap:.3f} m")
    off = max(abs((mod_lo[0] + mod_hi[0]) / 2), abs((mod_lo[1] + mod_hi[1]) / 2))
    check("C5", "载荷舱居中", off <= 0.15 * bus_w, f"offset={off:.3f} m")

    top = top_verts(bus)
    if len(top) >= 4:
        sx = max(v.x for v in top) - min(v.x for v in top)
        sy = max(v.y for v in top) - min(v.y for v in top)
        check("C6", "舱体顶面为完整平面（非楔形）",
              sx >= 0.7 * bus_dims[0] and sy >= 0.7 * bus_dims[1],
              f"top_verts={len(top)}, span=({sx:.2f},{sy:.2f}) vs bus=({bus_dims[0]:.2f},{bus_dims[1]:.2f})")
    else:
        check("C6", "舱体顶面为完整平面（非楔形）", False, f"top_verts={len(top)}")

    cb, cm = max_color(bus), max_color(module)
    ck = max_color(tanks[0]) if tanks else None
    cp = max_color(panels["panel_X1"])
    ok = (cb and cb[0] > cb[1] > cb[2]) and (cm and all(v < 0.35 for v in cm)) \
        and (ck and all(v > 0.8 for v in ck)) and (cp and cp[2] > cp[0])
    check("C7", "材质色：舱金/载荷舱深灰/储箱乳白/翼蓝", bool(ok),
          f"bus={cb}, module={cm}, tank={ck}, panel={cp}")

    pt = [p for n, p in panels.items() if p]
    check("C8", "太阳翼沿 ±X 展开",
          all(abs(p.location.y) < 0.3 * bus_w for p in pt),
          f"ys={[round(p.location.y, 2) for p in pt]}")

    check("C9", "接收机构坐载荷舱顶",
          all(vbounds([r])[0][2] >= mod_hi[2] - 0.05 for r in recvs),
          f"recv 底面 z={[round(vbounds([r])[0][2], 2) for r in recvs]}, 载荷舱顶 z={mod_hi[2]:.2f}")
    if len(recvs) >= 2:
        xs = sorted((vbounds([r])[0][0] + vbounds([r])[1][0]) / 2 for r in recvs)
        mx = (mod_lo[0] + mod_hi[0]) / 2
        check("C10", "接收机构分处两侧（±X）", xs[0] < mx < xs[-1],
              f"recv_x={[round(v, 2) for v in xs]}, module_x={mx:.2f}")
    else:
        check("C10", "接收机构分处两侧（±X）", False, "recv 不足 2 台")

    cw = collector_bus_width()
    if cw is None:
        # 读取失败即 FAIL：静默 PASS 会让"同族"这条判据形同虚设
        check("C11", "与集光器同族（舱宽一致 ±0.05 m）", False,
              "collector.py 常量未识别——判据不得静默放行")
    else:
        check("C11", "与集光器同族（舱宽一致 ±0.05 m）", abs(bus_w - cw) <= 0.05,
              f"combiner={bus_w:.2f}, collector={cw:.2f}")

    imgs = [f for f in ("side.png", "front.png", "iso.png", "top.png")
            if os.path.exists(os.path.join(OUT, f))]
    check("C12", "out/combiner/ 四视角渲染图", len(imgs) == 4, f"found={imgs}")

    if len(top) >= 4:
        pos = [v for v in top if v.y > 0]
        neg = [v for v in top if v.y < 0]
        sx_pos = max(v.x for v in pos) - min(v.x for v in pos) if pos else 0.0
        sx_neg = max(v.x for v in neg) - min(v.x for v in neg) if neg else 0.0
        lo_w, hi_w = sorted((sx_pos, sx_neg))
        ratio = lo_w / hi_w if hi_w > 0 else 0.0
        check("C13", "平面为梯形：短边/长边 ∈[0.5,0.8] 且长边在 +Y（朝相邻器）",
              0.5 <= ratio <= 0.8 and sx_pos > sx_neg,
              f"+Y 侧跨 {sx_pos:.3f}、−Y 侧跨 {sx_neg:.3f}，短/长={ratio:.3f}")
    else:
        check("C13", "平面为梯形", False, f"top_verts={len(top)}")


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
