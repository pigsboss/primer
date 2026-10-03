# primer-review 会话舱

把"参考输入 → 参数台账 → 模型"的决策环路摆进浏览器的本地服务：人上传资料、看图、
圈选、作答；模型侧出图、圈注、提问、落账。它是**人机会话界面**——与 imgcmp（量具/
自检）的分工是：imgcmp 服务施工一致性，review 服务人与模型之间的往返。

## 调用约定

本工程**不安装发行包**，一律以模块方式调用（同 imgcmp）：

```bash
cd <primer 仓库根>
PYTHONPATH=src python3 -m primer.review.server --session <会话目录> [--port 8766] [--no-open]
```

- `--session` 目录不存在时会**自动创建**并铺好子目录（uploads/pages/answers/renders）。
- 默认自动打开浏览器；服务只绑回环地址（`127.0.0.1`）。
- `pyproject.toml` 里的 `primer-review` 短名仅安装后存在，**本工程不使用**。

## 会话目录即协议

```
<session>/
  uploads/       人上传的原件（PDF／图片）＋ uploads/index.jsonl 台账
  pages/         PDF 按页光栅化（pdftoppm -png -r 150）与图片副本
  canvas.json    画布（模型侧写）：图像项＋矢量覆盖层
  cards.json     问题卡（模型侧写）：问题、选项、证据锚点
  record.json    参数台账（模型侧写；页面只读呈现）
  answers/       人的提交（服务写）：<card_id>.json 每卡一份＋_log.jsonl 流水
  renders/       渲染回放区（Phase 2）
```

服务是**哑文件经纪人**：不做任何判断，只负责收/发与状态合并；所有智能在会话两端。
人的圈选/擦除一律以**原图像素坐标**回传，不做坐标折损。

## 覆盖层语法（canvas.json 的 overlays 元素）

```json
{"id": "oct1", "type": "ellipse", "center": [216, 91], "rx": 67, "ry": 63,
 "color": "#ff3b30", "fill": 0.10, "width": 3, "label": "集光器罩面：八边形"}
```

- `type`：`polygon`／`polyline`／`rect`（`xy`+`w`+`h`）／`ellipse`（`center`+`rx`+`ry`）
  ／`label`（`at`+`label`）；`dash: true` 出虚线；`label` 会被渲染在形状旁。
- 图像项需给 `w`/`h`（原图像素），页面据此布点与适配视图。

## 问题卡（cards.json）与台账（record.json）

```json
{"id": "q1", "title": "1. 题面", "text": "……",
 "options": [{"key": "A", "label": "……", "desc": "……"}],
 "attach": {"image": "pages/fig3c.png", "overlay": "oct_comb"}}
```

- 卡支持选项点选＋补充文字；「提交本卡」把选项、文字与当前圈选盘一起落盘。
- 台账行 `{"id","subject","value","status","evidence","highlight"}`，
  `status` ∈ `open／measured／adjudicated／inferred`（配色区分）；点击行即聚焦证据。

## 人的提交（answers/<card_id>.json）

```json
{"card_id": "q1", "choice": "A", "text": "……",
 "selections": [{"image": "pages/fig3c.png", "mode": "lasso",
                 "points": [[x, y], ...]}]}
```

`mode` ∈ `lasso`（新增圈选）／`erase`（擦除模型侧预选区）；擦除语义为"把预选区域
减去该笔画覆盖部分"，由模型侧在回读时执行布尔减。

## 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 静态页面 |
| GET | `/state` | 全量状态（canvas/cards/record/uploads/pages/answers） |
| GET | `/f/<rel>` | 会话文件（图像等；路径穿越防护） |
| GET | `/healthz` | 健康检查 |
| POST | `/answer` | JSON 提交 |
| POST | `/upload?name=&desc=` | 原始字节流；PDF 自动按页光栅化 |

## 例子：2034 首个负载

`_primer/scene/interferometer/observatory/review/`：编排脚本 `_tools/author_payload.py`
负责铺页、写三份 JSON、出覆盖层自检图（`_tools/_check/`）——**坐标改动后必须先看
自检图**再开服务。

## Phase 0 边界

已完成：上传、画布（缩放/平移/套索/橡皮）、问题卡、只读台账、自由输入。
未做（Phase 1/2）：台账双向编辑、量尺、渲染回放并排、标志点机位配准、工程包导出。
