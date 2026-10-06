# PRIMER: Prompt-Ready Interface for Multimodal Extraction and Representation

> **S2AI（Science to AI）的"底漆"工程：把复杂科学数据转化为大模型可读、可用的语义表征。**

## 1. 愿景：为什么需要 PRIMER

当今主流的文本大模型（LLM）擅长处理自然语言，但在面对"硬核科学"（Hard Science）数据时力不从心：原始研究形态往往是复杂的目录结构、包含高维图表的学术 PDF，以及非人类可读的二进制仿真结果。

PRIMER 不是要创造一个新的世界模型，而是充当科学数据与 LLM 之间的"**底漆**"——通过对原始数据解构、提取和重新表征，抹平异构数据间的粗糙纹理，为上层 LLM 提供标准化、高附着力的"提示词就绪（Prompt-Ready）"界面。

**我们要做的不是 AI4S，而是先完成 S2AI（Science to AI）。**

## 2. 当前形态（2026-10）

愿景已落成一套**模块化工具链**：`src/primer/` 下 9 个功能模块、13 个命令行入口；`tasks/` 两个长期任务包；67 个测试文件、全量 1700+ 例通过。

对照最初设想的三层能力：① 项目文件夹语义摘要 → `digest`；② 科学文献多维解析 → `literature`（及其上下游 `references`／`claims`／`book`／`slides`）；③ 二进制数据转译尚无对应模块，仍属研究性设想。

### 2.1 文献与数据线

| 模块 | 职责 | 入口 |
| --- | --- | --- |
| `literature` | **文献库**：数据层＋本地 WebUI。导入（CSV／JSON／BibTeX／Markdown，含 AI 精析）、联网补全与全集比对（OpenAlex→NASA ADS→Crossref）、文件记录与 MinerU 解析队列（批量、并行、断点续跑）、批量自动关联（文件×文献）、批量下载 OA 原文（内容级校验、自动登记挂链）、按备注推断项目（LLM 预演）、从清单 md 恢复项目归属、导出（BibLaTeX／RIS／图书馆人工获取清单／库 JSON） | `primer-literature` |
| `digest` | 项目目录语义摘要：文件树层级、编码处理与语义路由 | `primer-digest` |
| `references` | 参考文献审计：解析 LLM 产出的文献清单 → 核对本地原文 → 逐条判定（结论＋置信度＋证据）与索取清单 | `primer-references` |
| `claims` | 论断—引用链路：把书稿里的引用落到原文，判定论断是否真被支撑（前半程确定性、不联网） | `primer-claims` |
| `book` | 清单驱动的"markdown 装配 → LaTeX → PDF"成书构建器 | `primer-book` |
| `slides` | 把书稿变成可演示幻灯片（确定性步骤＋可选模型辅助＋版面质检） | `primer-slides` |

### 2.2 场景与会话线

| 模块 | 职责 | 入口 |
| --- | --- | --- |
| `scene` | 声明式场景规格（`mission_layout.yaml`）→ Blender 场景 → 静默渲染；`scene.loop` 是长跑评审循环（模型作答、动作回执、每轮上下文） | `primer-scene`／`primer-sceneloop` |
| `review` | 会话舱：把"参考输入 → 参数台账 → 模型"的决策环路摆进浏览器（三栏：资产树｜自适应可视化｜对话） | `primer-review` |
| `imgcmp` | 参考图／渲染图对照工具链：切图、组件特征表、差异分诊（姿态反求 T2b 已注销） | `primer-imgrefgen`／`primer-imgtile`／`primer-imgfeat`／`primer-imgtriage` |

### 2.3 公共底座

- `config`／`envfile`：工作区端点与角色表（`_primer/config.yaml`），密钥只从环境变量（`.env`）读；
- `llm`：通用聊天客户端（多轮消息、文本与图片分片、按角色选端点）；
- `paths`：输出边界与"相对工程根"的路径换算（全项目唯一实现）。

## 3. 仓库结构

```text
src/primer/      9 个功能模块（见 §2）
tasks/           长期任务包：literature-webui（文献库 WebUI 的设计规格／方案／进展记录／验收清单）、
                 interferometer-pathfinder（觅音计划干涉探测任务的三器建模与分阶段自动验收）
docs/guides/     方法性文档：CODING_STANDARDS、scene_loop、review、imgcmp、imgcmp_triage
tests/           67 个测试文件（pytest）
```

## 4. 开发与测试

```bash
python3 -m pytest -q                        # 全量测试（1700+ 例）
uvx ruff check src/primer                   # 静态检查（可选）
primer-literature web --db <库文件.json>     # 启动文献库 WebUI（本机 127.0.0.1）
```

编码与文档纪律见 `docs/guides/CODING_STANDARDS.md`；各任务包的现状与时间线见其 `README.md` 与《进展记录.md》。

## 开发者箴言

> "底漆是结构与功能之间的界面。PRIMER 的目标是让 AI 能够站在人类数百年积累的科学物理基底之上，而不仅仅是漂浮在自然语言的泡沫之上。"
