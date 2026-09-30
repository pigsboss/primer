# -*- coding: utf-8 -*-
"""verify_assembly.py — 三器组合体装配的自动验收（在 assembly.py 之后运行）。

用法：blender --background --python verify_assembly.py，日志写 out/assembly/verify_log.txt。
判据逐条对应《阶段三_验收清单》（2026-09-30）D1–D12，**不自行增删**。

两处按字面会误判、已在代码里显式写明读法（并在交付说明中提请 kimi work 确认）：
  * D4 与 D5 需同时成立 ⇒ 载荷舱露高须 ≥0.60 m（combiner.MODULE_H 已由 0.54 调到 0.70）；
  * D7 按字面会把乳白储箱判 FAIL，故把 `tank` 列入排除项（D7 的意图是"不得有外露深灰梁体"）；
  * D9 "储箱 y 符号与所属舱相反（朝内）"取**相对舱心的偏移方向**读法——按绝对坐标符号读
    会要求储箱距舱心 1.74 m，几何上不可能。
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


def center(o):
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


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    w, h = struct.unpack(">II", head[16:24])
    return (w, h)


def main():
    try:
        import assembly
        if hasattr(assembly, "build"):
            assembly.build()
    except Exception:
        traceback.print_exc()
        check("EX", "assembly 导入/构建异常", False, traceback.format_exc(limit=3))

    deck = find("deck")
    buses = {tag: find("%s_bus" % tag) for tag in ("colA", "colB", "cmblow")}
    tubes = {tag: find("%s_tube" % tag) for tag in ("colA", "colB")}
    module = find("cmbmod_module")

    # ---- D1 对象齐备 ----
    need_col = ("bus", "tube", "panel_X1", "panel_X2", "panel_-X1", "panel_-X2", "window_out",
                "tank_01", "tank_02", "gimbal_01", "gimbal_02", "gimbal_03")
    miss = [f"{t}_{n}" for t in ("colA", "colB") for n in need_col if not find(f"{t}_{n}")]
    cmb_need = ("cmblow_bus", "cmblow_panel_X1", "cmblow_panel_X2", "cmblow_panel_-X1",
                "cmblow_panel_-X2", "cmblow_tank_01", "cmblow_tank_02",
                "cmbmod_module", "cmbmod_recv_01", "cmbmod_recv_02")
    miss += [n for n in cmb_need if not find(n)]
    check("D1", "对象齐备（集光器×2、合束器、共机架 deck）",
          bool(deck) and not miss, f"缺件={miss or '无'}")

    if not (deck and all(buses.values()) and all(tubes.values()) and module):
        raise SystemExit("关键对象缺失，终止后续检查")

    dlo, dhi = vbounds([deck])
    bus_v = {t: (vbounds([b]), vbounds([b])) for t, b in buses.items()}
    bus_lo = {t: vbounds([b])[0] for t, b in buses.items()}
    bus_hi = {t: vbounds([b])[1] for t, b in buses.items()}

    # ---- D2 基线 ----
    ya, yb = center(tubes["colA"])[1], center(tubes["colB"])[1]
    check("D2", "基线（两 tube 轴线 y 之差）∈ [2.3,2.6] m", 2.3 <= abs(ya - yb) <= 2.6,
          f"baseline={abs(ya - yb):.3f} m")

    # ---- D3 对称居中 ----
    mc = center(module)
    check("D3", "对称居中：|y_A+y_B|≤0.1；载荷舱 |y|≤0.3、|x|≤0.15",
          abs(ya + yb) <= 0.1 and abs(mc[1]) <= 0.3 and abs(mc[0]) <= 0.15,
          f"y_A+y_B={ya + yb:+.3f}；载荷舱 y={mc[1]:+.3f}, x={mc[0]:+.3f}")

    # ---- D4 支撑关系（两台集光器舱、载荷舱都落在甲板上；合束器舱顶贴甲板底面）----
    g_col = [bus_lo[t][2] - dhi[2] for t in ("colA", "colB")]
    g_mod = vbounds([module])[0][2] - dhi[2]
    g_low = bus_hi["cmblow"][2] - dlo[2]
    ok = all(-0.05 <= g <= 0.05 for g in g_col) and -0.05 <= g_mod <= 0.05 and -0.05 <= g_low <= 0.05
    check("D4", "支撑关系：集光器舱底/载荷舱底贴甲板顶面，合束器舱顶贴甲板底面", ok,
          f"两集光器舱底−甲板顶={[round(g, 3) for g in g_col]}；载荷舱={g_mod:+.3f}；合束器舱顶−甲板底={g_low:+.3f}")

    # ---- D5 上层同高 ----
    dh = abs(vbounds([module])[1][2] - max(bus_hi[t][2] for t in ("colA", "colB")))
    check("D5", "载荷舱顶面与集光器舱顶面高差 ≤0.3 m（顶视三者同层成排）", dh <= 0.3, f"高差={dh:.3f} m")

    # ---- D6 共机架 ----
    cdk = max_color(deck)
    deck_ok = cdk and cdk[0] > cdk[1] > cdk[2]
    span_ok = (dhi[1] - dlo[1]) >= abs(ya - yb) + 0.9
    inside = all(dlo[0] <= bus_lo[t][0] and bus_hi[t][0] <= dhi[0]
                 and dlo[1] <= bus_lo[t][1] and bus_hi[t][1] <= dhi[1] for t in ("colA", "colB"))
    check("D6", "共机架：deck 金色、Y 向跨度≥基线+0.9、两台集光器舱底面落在甲板顶面范围内",
          bool(deck_ok and span_ok and inside),
          f"deck 色={cdk}；Y 跨度={dhi[1] - dlo[1]:.2f}（需≥{abs(ya - yb) + 0.9:.2f}）；投影内={inside}")

    # ---- D7 无外露裸结构（除功用件外，结构件一律金色）----
    # 读法：tank 亦列入排除项——D10 规定储箱为乳白，按字面 D7 会误判（见文件头说明）
    excl = ("tube", "module", "gimbal", "recv", "aux", "panel", "window", "tank")
    bad = []
    for o in bpy.data.objects:
        if o.type != "MESH" or any(k in o.name for k in excl):
            continue
        c = max_color(o)
        if not (c and c[0] > c[1] > c[2]):
            bad.append((o.name, c))
    check("D7", "无外露裸结构：除筒/载荷舱/机构/翼/窗口/储箱外，结构件一律金色", not bad,
          f"非金色结构件={bad or '无'}")

    # ---- D8 太阳翼展开 ----
    allm = [o for o in bpy.data.objects if o.type == "MESH"]
    lo, hi = vbounds(allm)
    bus_w = max(vdims([buses["colA"]])[0], vdims([buses["colA"]])[1])
    check("D8", "三器太阳翼均展开（全组合体 X 跨度 ≥3×舱宽）",
          (hi[0] - lo[0]) >= 3 * bus_w, f"X 跨度={hi[0] - lo[0]:.3f} m，3×舱宽={3 * bus_w:.3f}")

    # ---- D9 储箱朝向 ----
    detail, ok = [], True
    for t in ("colA", "colB"):
        by = center(buses[t])[1]
        for k in (1, 2):
            tk = find("%s_tank_%02d" % (t, k))
            ty = center(tk)[1]
            # "符号相反（朝内）"取**相对舱心的偏移方向**：储箱须位于舱心与组合体原点之间。
            # 按绝对坐标符号读则要求储箱距舱心 1.74 m，几何上不可能。
            off = ty - by
            good = (off > 0) != (by > 0) and abs(off) < 1.0
            ok = ok and good
            detail.append(f"{t}{k}: tank_y={ty:+.2f}/bus_y={by:+.2f}/偏移={off:+.2f}")
    for k in (1, 2):
        tk = find("cmblow_tank_%02d" % k)
        tx = center(tk)[0]
        bw = vdims([buses["cmblow"]])[0] / 2
        good = abs(abs(tx) - bw) <= 0.15
        ok = ok and good
        detail.append(f"cmb{k}: |x|={abs(tx):.2f}/半宽={bw:.2f}")
    check("D9", "储箱朝向：集光器朝内（符号相反）、合束器在 ±X 侧面", ok, "；".join(detail))

    # ---- D10 材质色 ----
    cb, ct, cm = max_color(buses["colA"]), max_color(tubes["colA"]), max_color(module)
    ck, cp = max_color(find("colA_tank_01")), max_color(find("colA_panel_X1"))
    ok = (cb and cb[0] > cb[1] > cb[2]) and (ct and all(v < 0.1 for v in ct)) \
        and (cm and all(v < 0.35 for v in cm)) and (ck and all(v > 0.8 for v in ck)) \
        and (cp and cp[2] > cp[0])
    check("D10", "材质色：舱/架金、筒与载荷舱近黑、储箱乳白、翼蓝", bool(ok),
          f"bus={cb}, tube={ct}, module={cm}, tank={ck}, panel={cp}")

    # ---- D11 包络记录 ----
    ymax = 0.0
    for o in [buses["colA"], buses["colB"], buses["cmblow"], deck]:
        l, h = vbounds([o])
        ymax = max(ymax, abs(l[1]), abs(h[1]))
    check("D11", "包络记录：三器舱体外缘 max|y|≤1.85 m、总高≤4.61 m",
          ymax <= 1.85 and (hi[2] - lo[2]) <= ENV_H,
          f"max|y|={ymax:.3f} / 1.85；总高={hi[2] - lo[2]:.3f} / {ENV_H}")

    # ---- D12 自检渲染 ----
    sizes = {}
    for f in ("side.png", "front.png", "iso.png", "top.png"):
        p = os.path.join(OUT, f)
        sizes[f] = png_size(p) if os.path.exists(p) else (0, 0)
    check("D12", "out/assembly/ 四视角渲染图 ≥1200×900",
          all(w >= 1200 and h >= 900 for w, h in sizes.values()),
          f"{sizes}")


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
