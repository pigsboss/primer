# -*- coding: utf-8 -*-
"""T2b 姿态反求（用户真正会敲的命令：``python3 -m primer.imgcmp.pose``）测试。

- **不依赖 Blender**：归一化不变性、旋转-IoU 单调性、粗→精收敛、`--top-k`、CLI 退出码、
  `--selftest`。收敛用"假渲染器"（在画布上按 el 拉伸、按 roll 做 2D 旋转）驱动，
  与真渲染器的差别只在"谁来出那张 PNG"。
- **依赖 Blender/任务侧**：观象台适配器真渲两张剪影、检查清单与严格双色——缺 Blender 或
  任务侧目录时整组 skip。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from primer.imgcmp import pose as P

BLENDER = os.environ.get("PRIMER_BLENDER") or "/Applications/Blender.app/Contents/MacOS/Blender"
TASK_ROOT = ("/Users/huo/Documents/kimi/Workspaces/行星探测工程/_primer/scene/"
             "interferometer/observatory")
ADAPTER_DIR = os.path.join(os.path.dirname(os.path.abspath(P.__file__)), "adapters")
ADAPTER = os.path.join(ADAPTER_DIR, "observatory_silhouette.py")
HAVE_BLENDER = os.path.isfile(BLENDER)


# ---------------------------------------------------------------- 剪影与归一化

def test_outline_keeps_largest_component_and_fills_holes():
    mask = np.zeros((60, 80), dtype=bool)
    mask[10:50, 20:60] = True
    mask[25:35, 35:45] = False          # 内部空洞
    mask[5:9, 70:75] = True             # 另有一小块
    out = P.outline_of(mask)
    assert int(out.sum()) == 40 * 40
    assert out[30, 40]


def test_normalise_is_translation_and_scale_invariant():
    base = P.normalize_mask(P._synth_silhouette(), 256)[0]
    moved = np.zeros((600, 700), dtype=bool)
    img = Image.fromarray(np.where(P._synth_silhouette(), 255, 0).astype(np.uint8), "L")
    img = img.resize((400, 400), Image.BILINEAR)
    moved[120:520, 150:550] = np.asarray(img) > 127
    assert P.iou(base, P.normalize_mask(moved, 256)[0]) > 0.97


def test_rotation_iou_is_monotone():
    base = P.normalize_mask(P._synth_silhouette(), 256)[0]
    padded = P.pad_for_rotation(P._synth_silhouette())
    seq = [P.iou(P.normalize_mask(P.rotate_mask(padded, a), 256)[0], base)
           for a in (0, 5, 10, 20, 40, 90)]
    assert abs(seq[0] - 1.0) < 1e-9
    assert all(seq[i] > seq[i + 1] for i in range(len(seq) - 1))


def test_pad_for_rotation_does_not_clip():
    mask = np.zeros((40, 120), dtype=bool)
    mask[18:22, 5:115] = True
    padded = P.pad_for_rotation(mask)
    assert padded.shape[0] == padded.shape[1]
    assert int(padded.sum()) == int(mask.sum())
    assert int(P.rotate_mask(padded, 45).sum()) > int(mask.sum())


def test_iou_and_bbox_edges():
    a = np.zeros((10, 10), dtype=bool)
    assert P.iou(a, a) == 0.0          # 两边都空：定义为零
    a[2:5, 2:5] = True
    b = a.copy()
    assert P.iou(a, b) == 1.0
    b[7:9, 7:9] = True
    assert 0.0 < P.iou(a, b) < 1.0
    with pytest.raises(P.ImgCmpError):
        P.silhouette_mask(np.zeros((8, 8, 3), dtype=np.uint8))


# ---------------------------------------------------------------- 反求收敛（假渲染器）

def _fake_render(mask, az, el, roll):
    """假渲染器：el 竖直拉伸（形变），roll 相机滚转（图像空间最后一步 2D 旋转）。"""
    canvas = np.zeros((320, 320), dtype=bool)
    canvas[60:260, 60:260] = mask
    img = Image.fromarray(np.where(canvas, 255, 0).astype(np.uint8), "L")
    w, h = img.size
    img = img.resize((w, max(1, int(round(h * (1.0 + 0.01 * float(el)))))), Image.BILINEAR)
    img = img.rotate(-float(roll), resample=Image.BILINEAR, fillcolor=0)
    return np.asarray(img) > 127


def _solve_stub(tmp_path, true_el, true_roll, **kw):
    os.makedirs(tmp_path, exist_ok=True)
    stub = P._StubRenderer(P._stub_renderer(P._synth_silhouette(), 256, _fake_render))
    target = tmp_path / "baseline.png"
    base_view = _fake_render(P._synth_silhouette(), 0.0, true_el, true_roll)
    Image.fromarray(np.where(base_view, 255, 0).astype(np.uint8), "L").convert("RGB") \
        .save(target)
    return P.solve(str(target), stub, str(tmp_path / "solve"),
                   az_range=(0.0, 0.0), el_range=(-60.0, 60.0), **kw)


@pytest.mark.parametrize("true_el,true_roll", [(25.0, 37.0), (-30.0, 200.0)])
def test_coarse_to_fine_converges(tmp_path, true_el, true_roll):
    result = _solve_stub(tmp_path, true_el, true_roll, coarse=10.0, fine=2.0, top_k=2,
                         roll_step=10.0)
    got = result["angles"]
    roll_err = min(abs(got["roll"] - true_roll), 360 - abs(got["roll"] - true_roll))
    assert abs(got["el"] - true_el) <= 3.0, result["angles"]
    assert roll_err <= 3.0, result["angles"]
    assert result["residual"]["iou"] > 0.90


def test_top_k_is_descending_and_respected(tmp_path):
    result = _solve_stub(tmp_path, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=3,
                         roll_step=10.0)
    ious = [row["iou"] for row in result["top_k"]]
    assert len(result["top_k"]) == 3
    assert ious == sorted(ious, reverse=True)


def test_solve_writes_camera_and_overlay(tmp_path):
    result = _solve_stub(tmp_path, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                         roll_step=10.0)
    solve_dir = tmp_path / "solve"
    for name in ("solve.json", "camera.json", "overlay.png"):
        assert (solve_dir / name).is_file()
    camera = json.loads((solve_dir / "camera.json").read_text(encoding="utf-8"))
    assert camera["angles"] == result["angles"]
    assert "{az}" not in camera["renderer_cmd_resolved"]
    saved = json.loads((solve_dir / "solve.json").read_text(encoding="utf-8"))
    assert saved["grid"]["coarse"] == 10.0 and saved["grid"]["fine"] == 2.0
    assert saved["grid"]["roll_range"] == [0.0, 355.0]
    assert saved["ambiguous"] in ("strong", "weak", "none")
    assert isinstance(saved["solvable"], bool) and saved["min_iou"] > 0
    assert saved["method"]["roll_convention"] and saved["method"]["ambiguity"]


def test_ambiguous_is_graded(tmp_path):
    """歧义分档：strong(<0.004) / weak(<0.02) / none；note 要点出歧义类型。"""
    result = _solve_stub(tmp_path, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                         roll_step=10.0)
    assert result["ambiguous"] in ("strong", "weak", "none")
    assert isinstance(result["ambiguity_gap"], float)
    assert isinstance(result["ambiguity_note"], str) and result["ambiguity_note"]
    if result["ambiguous"] == "strong":
        assert result["ambiguity_gap"] < P.AMBIGUITY_STRONG
    elif result["ambiguous"] == "weak":
        assert P.AMBIGUITY_STRONG <= result["ambiguity_gap"] < P.AMBIGUITY_WEAK
    # 手工造"翻面并列"：说明必须点出歧义类型与档位
    tight = P._ambiguity_note({"az": 0.0, "el": 30.0, "roll": 10.0, "iou": 0.90},
                              {"az": 180.0, "el": -30.0, "roll": 190.0, "iou": 0.899},
                              "strong")
    assert "歧义" in tight and "强" in tight
    assert "俯仰反号" in tight or "方位掉头" in tight


def test_solvable_gate(tmp_path):
    """有解性门限：基准侧最优 IoU 低于 --min-iou 时判 solvable=false。

    缺省门限＝0.90（第 5 轮由 0.80 上调）：T2b 实测同源可解例 0.968、收窄搜索的伪解
    0.849、论文 CAD 基准 0.737——0.80 会把 0.849 的伪解判成可解。下面同时钉住"缺省值
    落在 0.849 与 0.968 之间"这条设计约束，防止以后被改回去。
    """
    assert P.DEFAULT_MIN_IOU == 0.90
    assert 0.849 < P.DEFAULT_MIN_IOU <= 0.968
    ok = _solve_stub(tmp_path, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                     roll_step=10.0)
    assert ok["solvable"] is True and ok["min_iou"] == P.DEFAULT_MIN_IOU
    assert "足以定姿态" in ok["solvable_note"]
    bad = _solve_stub(tmp_path / "bad", 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                      roll_step=10.0, min_iou=0.999)
    assert bad["solvable"] is False
    assert "不足以定姿态" in bad["solvable_note"]


def test_refine_window_covers_the_coarse_roll_step(tmp_path):
    """回归：roll_step > coarse 时，真值可能落在粗筛网格之外，精修窗要够宽。

    粗筛只扫 roll 网格点，真值离最近网格点最多 roll_step/2；窗口若只开 ±coarse，
    真值会被窗口边缘截住（实测踩过：roll_step=30、coarse=5 时滚转误差被截成 ±5°）。
    """
    result = _solve_stub(tmp_path, 25.0, 10.0, coarse=5.0, fine=1.0, top_k=2,
                         roll_step=30.0)
    assert abs(result["angles"]["roll"] - 10.0) <= 2.0, result["angles"]


def test_roll_range_is_configurable(tmp_path):
    """roll 收窄后只在给定段里搜，且 grid.roll_range 如实记录。"""
    result = _solve_stub(tmp_path, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                         roll_step=10.0, roll_range=(30.0, 40.0))
    assert result["grid"]["roll_range"] == [30.0, 40.0]
    assert 30.0 <= result["angles"]["roll"] <= 40.0
    assert abs(result["angles"]["roll"] - 37.0) <= 3.0
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(P.ImgCmpError):
        _solve_stub(bad, 25.0, 37.0, coarse=10.0, fine=2.0, top_k=2,
                    roll_step=10.0, roll_range=(40.0, 30.0))


def test_solve_rejects_bad_parameters(tmp_path):
    stub = P._StubRenderer(P._stub_renderer(P._synth_silhouette(), 256, _fake_render))
    target = tmp_path / "b.png"
    Image.fromarray(np.where(P._synth_silhouette(), 255, 0).astype(np.uint8), "L") \
        .convert("RGB").save(target)
    with pytest.raises(P.ImgCmpError):
        P.solve(str(target), stub, str(tmp_path / "s1"), coarse=0.0)
    with pytest.raises(P.ImgCmpError):
        P.solve(str(target), stub, str(tmp_path / "s2"), top_k=0)


# ---------------------------------------------------------------- CLI

def test_renderer_template_guard(tmp_path):
    for tmpl in ("", "   ", "render --foo", "render {az} {el}"):
        with pytest.raises(P.ImgCmpError):
            P.Renderer(tmpl, str(tmp_path), 64, 5.0)
    ok = P.Renderer("render --az {az} --el {el} --roll {roll} --out {out}", str(tmp_path),
                    64, 5.0)
    assert ok.batch is False
    assert P.Renderer("render --jobs {jobs}", str(tmp_path), 64, 5.0).batch is True


def _cli(argv):
    try:
        return int(P.main(argv))
    except SystemExit as exc:                      # argparse 用法错误
        return int(exc.code) if isinstance(exc.code, int) else 2


def test_cli_exit_codes(tmp_path, capsys):
    assert _cli(["--selftest"]) == 0
    capsys.readouterr()
    assert _cli([]) == 2
    assert _cli(["solve"]) == 2                                    # 缺 --renderer-cmd
    assert _cli(["solve", "x.png", "--renderer-cmd", "r {jobs}"]) == 2   # 缺 --out
    assert _cli(["solve", "x.png", "--out", str(tmp_path), "--renderer-cmd", "r {jobs}",
                 "--coarse", "0"]) == 2
    assert _cli(["solve", str(tmp_path / "missing.png"), "--out", str(tmp_path),
                 "--renderer-cmd", "r {jobs}"]) == 2
    assert _cli(["solve", "x.png", "--out", str(tmp_path), "--renderer-cmd", "r {jobs}",
                 "--az-range", "1"]) == 2
    assert _cli(["solve", "x.png", "--out", str(tmp_path), "--renderer-cmd", "r {jobs}",
                 "--roll-range", "nope"]) == 2


def test_cli_solve_end_to_end(tmp_path, capsys):
    """真跑一次 CLI（渲染器是一个把它自己当 python 跑的小脚本）。"""
    script = tmp_path / "renderer.py"
    script.write_text(
        "import json, sys\n"
        "from PIL import Image\n"
        "import numpy as np\n"
        "job=json.load(open(sys.argv[1]))\n"
        "for s in job['shots']:\n"
        "    a=np.zeros((job['res'][0],job['res'][1]),np.uint8)\n"
        "    a[40:200,60:200]=255\n"
        "    Image.fromarray(a,'L').save(s['out'])\n", encoding="utf-8")
    baseline = tmp_path / "base.png"
    a = np.zeros((240, 240), np.uint8)
    a[40:200, 60:200] = 255
    Image.fromarray(a, "L").convert("RGB").save(baseline)
    out = tmp_path / "out"
    tmpl = "%s %s {jobs}" % (os.sys.executable, script)
    assert _cli(["solve", str(baseline), "--renderer-cmd", tmpl, "--out", str(out),
                 "--coarse", "45", "--fine", "15", "--top-k", "1",
                 "--az-range", "0,0", "--el-range", "0,0", "--roll-step", "45"]) == 0
    report = capsys.readouterr().out
    assert "T2b 姿态反求" in report
    result = json.loads((out / "solve.json").read_text(encoding="utf-8"))
    assert result["residual"]["iou"] > 0.95
    assert result["renderer"]["mode"] == "batch"


def test_selftest_reports_every_check(capsys):
    assert P.main(["--selftest"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "FAIL" not in out
    assert "solve_converges" in out and "rotation_iou_monotone" in out


# ---------------------------------------------------------------- 需要 Blender 的部分

@pytest.mark.skipif(not HAVE_BLENDER, reason="Blender not available")
@pytest.mark.skipif(not os.path.isdir(TASK_ROOT), reason="task side not available")
def test_observatory_adapter_renders_two_colour_silhouettes(tmp_path):
    job = tmp_path / "jobs.json"
    shots = [{"az": 90.0, "el": 0.0, "roll": 0.0, "out": str(tmp_path / "a.png")},
             {"az": 90.0, "el": 0.0, "roll": 30.0, "out": str(tmp_path / "b.png")}]
    job.write_text(json.dumps({"res": [240, 240], "shots": shots}), encoding="utf-8")
    cmd = [BLENDER, "-noaudio", "--background", "--python", ADAPTER, "--",
           "--task-root", TASK_ROOT, "--jobs", str(job), "--outdir", str(tmp_path)]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-800:]
    manifest = json.loads((tmp_path / "render_manifest.json").read_text(encoding="utf-8"))
    assert manifest["view_transform"] == "Standard"
    assert manifest["aa"] == "OFF" and manifest["background"] == [0, 0, 0]
    for shot in shots:
        arr = np.asarray(Image.open(shot["out"]).convert("L"))
        values = set(np.unique(arr).tolist())
        assert values <= {0, 255}, values          # 严格双色
        assert (arr > 127).sum() > 100
    # 同一机位 roll=0 与 roll=30：把前者做 −30° 2D 旋转应基本重合（远场近似）
    base = P.outline_of(np.asarray(Image.open(shots[0]["out"]).convert("L")) > 127)
    turned = P.outline_of(np.asarray(Image.open(shots[1]["out"]).convert("L")) > 127)
    approx = P.rotate_mask(P.pad_for_rotation(base), -30.0)
    # 正交投影下"相机滚转 θ"与"对图像做 −θ 2D 旋转"理论上等价，残差只来自重采样与
    # 细杆在低分辨率下的连通性；240 px 小图上实测 ≈0.66，480 px 上更高（验收里给实测值）
    assert P.iou(P.normalize_mask(approx, 256)[0], P.normalize_mask(turned, 256)[0]) > 0.6


@pytest.mark.skipif(not shutil.which("blender") and not HAVE_BLENDER,
                    reason="Blender not available")
def test_model_adapter_rejects_missing_model(tmp_path):
    adapter = os.path.join(ADAPTER_DIR, "model_silhouette.py")
    if not os.path.isfile(adapter):
        pytest.skip("model adapter not shipped")
    job = tmp_path / "jobs.json"
    job.write_text(json.dumps({"res": [64, 64], "shots": [
        {"az": 0, "el": 0, "roll": 0, "out": str(tmp_path / "x.png")}]}), encoding="utf-8")
    proc = subprocess.run([BLENDER, "-noaudio", "--background", "--python", adapter, "--",
                           "--model", str(tmp_path / "nope.glb"), "--jobs", str(job)],
                          capture_output=True, timeout=300)
    assert proc.returncode != 0
