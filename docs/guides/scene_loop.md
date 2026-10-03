# primer 会话舱环路（scene loop）

把「机器重建 → 人目检 → 机器重建」的决策环路收进 primer：人在浏览器会话舱里发消息、
看渲染图、点选问题卡；`primer.scene.loop` 这个进程盯着同一个会话目录，把新消息连同
**磁盘上的全部上下文**交给 `primer.config` 所配的 LLM，解析回信里的动作块，执行白名单
动作，再把结果与回信写回会话。**不使用任何外部 agent**。

界面只是视图：状态全在磁盘上，换模型随时可换（对话面板的下拉框里选 role，下一轮生效）。

## 运行方式

```bash
cd <primer 仓库根>
# 先开会话舱服务（另开一个终端）
PYTHONPATH=src python3 -m primer.review.server --session <会话目录> --port 8766

# 再开环路驱动
set -a && . .env && set +a        # 密钥只从环境变量进进程；不要 cat/复制 .env
PYTHONPATH=src python3 -m primer.scene.loop \
  --session <会话目录> --task <任务根> [--role scene] [--interval 2] \
  [--no-actions] [--once] [--vision auto|off]
```

- `--session` 会话目录（会话舱服务的那个），`--task` 任务根（`loop.yaml` 所在目录）。
- `--role` 默认模型（默认 `scene`）；人在这条消息上选的模型（界面下拉框）优先于它。
- `--interval` 轮询间隔秒数（默认 2）；`--once` 只处理当前积压的消息然后退出。
- `--no-actions` 只解析动作块、不执行（回信里注明未执行）。
- `--vision auto|off`：`auto` 时随信附图——用户消息引用过的图优先（问题卡号按卡面标题号优先解析、图片文件名、画布项 id，见「引用附图」一节），再用 `loop.yaml` 的 `images.glob` 按时间补足到 `images.max`。
- `--project-root`／`--config` 通常不必给：默认从 `--task` 向上找带 `_primer/config.yaml`
  的那一层。

`pyproject.toml` 里的 `primer-sceneloop` 短名仅安装后存在，**本工程不使用**，一律按
`python3 -m` 调用。

## 引用附图（「看清再答」）

用户在消息里引用的图会被优先附进本轮上下文：

- 问题卡号：`第 14 个问题卡`／`卡14`／`q3` → 解析到卡后附其 `attach.image`（**卡面标题号优先于列表序**——列表顺序不是给用户看的编号）；
- 文件与画布项：`r2_midstage.png` 这类文件名，或画布项 id，按会话 `pages/`、`renders/` 与任务 `out/still/**` 搜索；
- 定位不到的引用不附任何替代图；系统提示词含纪律条款：回答前先按「随信图片」清单声明看到了什么，引用图不在清单里时必须明说看不到，不得用其他图替代作答。

## 会话目录协议

```
<session>/
  chat.jsonl       消息流：人写 {ts, role:"user", text, model}；驱动写 {role:"agent", ...}
                   与过程状态 {role:"system", text:"开始执行命令：…"}
  chat_meta.json   驱动写：{heartbeat_ts, models:[{id,label}], default_role, vision}（GUI 读）
  loop_state.json  驱动写：游标 {processed_user_messages, ...}（断点续跑）
  loop_log.jsonl   驱动写：全动作 append-only 日志（编辑 diff、命令输出末尾、备份名）
  loop.lock        驱动持有：心跳 <30s 的锁文件；已有驱动且心跳新鲜时报错退出，过期则接管
  ESCALATION.md    升级标记：需要改 primer 代码时写入（追加式）
  _backup/         会话 JSON 写前备份（每份保留最近 10 个）
  canvas.json / cards.json / record.json   会话状态（session_update 按 id 合并）
```

驱动每轮刷新 `loop.lock` 的 mtime 与 `chat_meta.json` 的 `heartbeat_ts`；界面在心跳超过
60 秒旧时把模型下拉框置为「驱动未运行」禁用态。

## 任务侧 `loop.yaml`

```yaml
task: 觅音 2034「1+4」系统建模
params_file: 参数库_2034.yaml          # 当前参数源；全文进上下文
editable: [参数库_2034.yaml]           # edit_params 的白名单（相对任务根的路径或文件名）
commands:                              # run 动作的白名单；cwd＝任务根，继承环境
  build:  [python3, array2034.py]
  verify: [/Applications/Blender.app/Contents/MacOS/Blender, --background, --python, verify_array2034.py]
  render: [/Applications/Blender.app/Contents/MacOS/Blender, --background, --python, fig3match2034.py]
images: {glob: "out/still/match/*_match.png", max: 2}   # 可选：附进上下文／回信附件的图
system_extra: |                        # 可选：追加在纪律模板后面的任务补充说明
  任务背景：……
```

## 模型清单（下拉框的数据源）

下拉框内容由驱动写入会话 `chat_meta.json` 的 `models`，两个来源按优先级：

1. **`loop.yaml` 的 `models:`（显式清单，推荐）**——按清单顺序原样呈现，支持字符串或 `{role, label}`；未配置的 role 会显示为「`<role>（未配置）`」。例：
   ```yaml
   models: [scene, {role: distill, label: '强模型（订阅）'}]
   ```
2. 缺省（未写 `models:`）＝config 全部 roles **按 provider/model 端点去重**（同一端点的多个 role 名折叠为一项，取排序靠前者；默认 role 若被折叠则取其名）。

页面里把鼠标悬停在下拉框上会显示当前来源（`models_source`）。换模型下一轮生效；模型名与端点仍以 `_primer/config.yaml` 的 roles 表为准（清单只选 role，不定义端点）。

## 动作块语法

回信正文之后放一个 ```actions 围栏，内容是 YAML（JSON 也认，```yaml／```json 围栏也
收）。没有动作就整块省略。解析容错：围栏标签大小写不敏感；内容先按原样解析，失败再按
全角标点（`：，""（）【】` 等）归一化重试一次。**解析不出来的动作块不执行**，回信里如实
注明。

```yaml
actions:
  - kind: edit_params
    file: 参数库_2034.yaml          # 必须命中 loop.yaml 的 editable 白名单
    edits:
      - before: "length_m: 4.42"   # 原文件中精确出现一次的原文子串，否则该条失败
        after:  "length_m: 4.8"
        note: "裁决 q8=A"
  - kind: run
    name: build                    # 只能是 loop.yaml commands 里的名字
  - kind: session_update
    record_rows: [...]             # 与 review record.json 行同构；同 id 替换、新 id 追加
    canvas_items: [...]            # 与 canvas.json 项同构
    cards: [...]                   # 与 cards.json 卡同构
  - kind: escalate
    reason: "需要修改 primer.scene 的 X"
```

各类动作的行为：

| 动作 | 白名单 | 落盘 | 失败时 |
|:--|:--|:--|:--|
| `edit_params` | `editable` | 写回文件＋`loop_log.jsonl` 记 diff；**不自动跑 verify**（要跑由模型显式 `run`） | `before` 命中 0 或 >1 次、文件不在白名单：该条失败并如实回报 |
| `run` | `commands` | `loop_log.jsonl` 记 exit code、stdout/stderr 末尾各 120 行、耗时；成功后把 `images.glob` 里最新的图复制进会话 `renders/` 并作为回信 `refs` | 名字不在白名单直接拒绝；非零退出照实回报 |
| `session_update` | — | `canvas.json`／`cards.json`／`record.json` 按 id 合并；写前备份到 `_backup/` | `id` 缺失即追加；非列表报错 |
| `escalate` | — | 追加 `ESCALATION.md`；`record.json` 加一行 `⚠ 已升级 kimi code 回路`；回信明确标注 | — |

重建是长任务：驱动在 `run` 的整段时间里持有锁并刷新心跳，过程写 `loop_log.jsonl`，
回信在命令结束后写回会话。

## 升级边界

**只有「需要改 primer 代码」才置 `escalate` 标记**（回给 kimi code 回路）：

- 需要修改 `src/primer/**` 才能推进；
- 环路内的手段（改参数、跑构建／渲染、更新会话、出问题卡）都无法解决。

其余一律在 primer 内闭环。模型被明确要求**不得自行修改 primer 源码**，只能 `escalate`。
界面在 `ESCALATION.md` 存在时显示升级警示条，台账里也有一行醒目记录。

## 一轮里发生了什么

1. 读 `chat.jsonl`，按 `loop_state.json` 的游标取出**最后一条未处理的**用户消息（多条
   积压则逐条顺序处理、各自回复）。
2. 组装上下文：内置纪律模板（＋`system_extra`）作 system；user 侧放 `loop.yaml` 全文、
   参数库全文、`record.json`／`cards.json` 摘要、近 40 条聊天记录、本轮用户消息；可选附
   最近 N 张渲染图（JPEG q80、单张 ≤4 MB 的 data-URI）。端点若以 HTTP 4xx 拒绝图片，
   自动降级为纯文本重试一次，并在 `loop_log.jsonl` 记一笔。
3. 调所配 LLM，取回正文、usage（含 `reasoning_tokens`）、耗时、模型名。
4. 解析动作块 → 执行（`--no-actions` 时跳过）。有 `run` 动作时**先回一条过程状态**
   （`role:"system"`：「开始执行命令：…」），命令跑完再写最终回信——重建是长任务，界面
   不必等它跑完才知道在动。
5. 回信追加进 `chat.jsonl`：`{ts, role:"agent", text, model, provider, actions_executed,
   refs, usage, seconds}`，并刷新 `chat_meta.json`（心跳、模型表、当前默认）。
6. stdout 每轮一行中文简报（时间／模型／动作数／耗时）；诊断走 stderr（英文）。

## 语言与密钥纪律

stdout 中文人读简报；stderr／异常消息／选项名英文；JSON 键英文——照
`docs/guides/CODING_STANDARDS.md` v1.1 §2.2。密钥只从环境变量读
（`PRIMER_<PROVIDER>_API_KEY`，见 `primer.config`），报错只点名变量名，绝不回显密钥值，
也不写进任何产物或日志。
