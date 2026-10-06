# H1 `python3 -m primer.imgcmp.triage` —— 差异分诊与交付门禁

> 本文件是 H1（决策环插入）的独立说明，与 `imgcmp.md`（T1/T2/refgen 工具链）分开维护。
> 需求判据：`_primer/scene/interferometer/observatory/工具与harness需求书_2034.md` §三（A-H1-1..3）与 §五。
> 语言纪律按 `CODING_STANDARDS.md` v1.1：stdout 中文；stderr／异常／选项名／JSON 键英文。
> 调用约定见下文「CLI」节的说明——**本工程不安装发行包，命令一律写 `python3 -m` 口径**。

## 定位与插入点

H1 不是图像工具，而是**上下文工程**：把 T2 的**差异对照表**（`feat.compare_tables` 的
`diffs[]`）送进调用方给的**规则文件**，逐条判出**三档**，产出《差异分诊表》与《异议单》，
再用门禁把"交付说明缺《差异分诊表》"变成**非零退出**。

kimi code 建模工作流中的强制插入点（需求书 §三.1）：

```
渲染预览 ──▶ T2 特征级对照（基准图 vs 同视角渲染）──▶ 分诊（python3 -m primer.imgcmp.triage triage）
        ──▶ 《差异分诊表》（＋升级项的《异议单》）──▶ 交付说明（含强制章节）
        ──▶ 交付门禁 python3 -m primer.imgcmp.triage gate
```

**门禁口径**：交付物未附《差异分诊表》即视为未交付——`gate` 缺章节或章节内缺固定字段
一律**非零退出**（退出码 2）并在 stderr 给英文诊断。

## 三档与处置边界（保守，不许自我豁免）

| 档位（JSON 码） | 命中条件 | 依据字段 | 处置 |
|---|---|---|---|
| **允许差**（`allow`） | 命中调用方给的《允许差清单》条目 | `list_item_id`（清单条目号） | 维持设计，登记分诊表 |
| **执行自由度内差异**（`free`） | 仅命中**声明的自由度范围**规则（观感/工艺/未冻结细节） | `scope`（自由度范围名） | 可自行迭代，迭代记录入表 |
| **升级**（`escalate`） | 其余一律升级；触及构型/几何判据的差异也可由 `escalate[]` **显式声明** | `judgement`（判据号）＋`escalation_id`（异议单号） | 逐条出《异议单》，**禁止自行推理后维持** |

判定优先级（`triage.py:decide`）：**显式 `escalate[]` 声明 > `allow[]` > `free[]` > 默认档位**。

为什么构型级声明优先：需求书要求"触及构型/几何判据的差异只允许升级"，而 allow/free 都是
**豁免**。若豁免规则写得宽（例如按差异类批量允许），构型差异会被一并吸收——把显式构型级
声明放在最前，豁免再宽也压不住它，这是一条**结构性**的自豁免防线，不只靠规则作者的自觉。

默认档位来自 `escalation.default`（示例与判据都设 `escalate`）：清单外差异不得自我豁免，
所以"没命中任何规则"必须落到升级。工具不内嵌任何任务数值与档位偏好。

**规则失败即拒**（`triage.py:load_rules`，全部抛英文 `ImgCmpError` → 退出码 2）：

- 一条 allow/free/escalate 规则**必须至少给一个选择器**（`kind`／`pattern`／形状类／数值区间／`when`），
  否则整份规则被拒——"通配豁免"是最容易出事的写法；
- `allow[]` 必须给 `list_item_id`、`free[]` 必须给 `scope`、`escalate[]` 必须给 `judgement`；
- 未知顶层键、非法正则、非数值区间、`escalation.default` 不在三档之内，一律拒。

## 规则文件（英文键）

```yaml
version: 1

allow:                                   # 允许差清单
  - list_item_id: 2034-AL-01             # 清单条目号（必填）
    note: 基准无背景／我方星空            # 为什么允许
    disposition: 维持设计，登记分诊表      # 处置（可选，有缺省）
    kind: background_kind                # ← 选择器

free:                                    # 执行自由度范围
  - scope: rendering-style               # 自由度范围名（必填）
    note: 渲染风格差异（不触及构型与几何判据）
    kind: added

escalate:                                # 显式构型级声明（优先级最高）
  - judgement: CFG-01                    # 判据号（必填）
    note: 外置桅杆（差异描述）
    advice: 按 D2 重做为内置遮光筒（建议方案）
    kind: added
    ours_shape: rod
    ours_min: 10000

escalation:
  default: escalate                      # 清单外默认档位
  template:                              # 异议单模板字段
    title: 异议单
    fields: [差异描述, 特征证据, 建议方案]
```

### 选择器一览

| 选择器 | 作用 |
|---|---|
| `kind` | 差异类（字符串或列表）：`count`／`height_fraction`／`area_fraction`／`anchor_ratio`／`shape_class`／`hole_count`／`contact`／`merged`／`split`／`removed`／`added`／`background_kind` |
| `pattern` | 对**主题串**做不区分大小写的正则搜索（字符串或列表，全部命中才算命中） |
| `ref_shape` / `ours_shape` | 配对件的形状类（`rect/circle/ring/rod/other` 之一） |
| `ref_min/ref_max`／`ours_min/ours_max`／`delta_min/delta_max`／`abs_delta_min/abs_delta_max` | 数值区间；字段非数值（如 `shape_class` 的 `ref`）则该选择器**判不命中** |
| `top_band_equal` | 布尔：开口数差异是否**只在筒身剖口**而不改筒顶开口（把"拆面剖口"与"筒顶开口数"分开） |
| `when` | 仅对指定配对生效：`ref_path_contains`／`ours_path_contains`／`ref_kind`／`ours_kind` |

### 主题串（`subject`）

每条差异合成一个主题串，`pattern` 就作用在它上面（规则可据以写跨字段的条件）：

```
kind=added ref=0 ours=11947 delta=11947 shape=-->rod | ours#3 bbox=[622, 283, 913, 619] px=11947; 我方有、基准无
kind=background_kind ref_kind=white ours_kind=black | 底色／背景 基准 white（图 fig3b.png）／我方 black（图 _ours_fig3b.png）
```

除 T2a 的 `diffs[]` 外，工具另合成一条**配对级**行：两侧**底别不同**时出
`kind=background_kind`（白底 CAD／无背景 ↔ 黑底渲染／星空）。这是"无背景 vs 星空"
这类差异在特征表里唯一可判定的信号（星点小于 `min_area`，不会进组件表）。

## CLI

```bash
# 分诊（输入可给组件特征表 JSON，也可直接给图——给图时内部调 feat.table 先建表）
python3 -m primer.imgcmp.triage triage --ref BASE.feat.json --ours OURS.feat.json \
                                      --rules rules.yaml --out DIR [--json OUT] [--template]
python3 -m primer.imgcmp.triage triage --ref base.png --ours ours.png --rules rules.yaml \
                                      --out DIR [--ref-kind white] [--ours-kind black] \
                                      [--ref-roi X,Y,W,H]
# 交付门禁
python3 -m primer.imgcmp.triage gate --delivery-note 交付说明.md [--section 差异分诊表]
python3 -m primer.imgcmp.triage gate --template   # 打印交付说明里该章节的骨架
python3 -m primer.imgcmp.triage --selftest        # 自检（不依赖任务侧数据）
```

工具内零任务数值：锚件、清单条目号、判据号、默认档位全部走 `--rules`／`--ref-*` 参数。
仓库示例规则：`src/primer/imgcmp/triage_rules.example.yaml`（2034 的《允许差清单》两条
＋三项首轮错误构型级声明 `CFG-01/02/03`）。

> **调用约定（本工程不安装发行包）**：命令一律写 `python3 -m primer.imgcmp.triage <子命令>`。
> 本机 `import primer` 靠环境变量 `PYTHONPATH` 指向 `…/primer/src` 生效——`site-packages`
> 里既没有 `primer` 的 `dist-info`／`egg-info`，也没有任何 `primer-*` 可执行脚本（自查：
> `python3 -c "import primer; print(primer.__file__)"` 应打印 `…/primer/src/primer/__init__.py`）。
> `pyproject.toml` 的 `[project.scripts]` 里声明的那些短名（安装后别名，如
> `primer-imgtriage`）**只有执行过发行包安装之后才会存在**；**本工程不使用**这些别名。
> 下文所有示例因此都写 `python3 -m` 口径。从任意工作目录调用都可以（`PYTHONPATH` 指向
> 源码树），不必先 `cd` 到仓库。

## 产物（`--out DIR`）

| 文件 | 内容 |
|---|---|
| `差异分诊表.md` | 固定字段表：`差异｜特征证据｜分诊档位｜依据｜处置`，附三档计数、升级项索引、处置边界 |
| `triage.json` | 机器可读结果（英文键）：`rows[]`（含 `subject`／`tier`／`basis`／`disposition`）、`counts`、`escalations[]`、`rules.sha256_16` |
| `escalations/ESC-NN-<kind>.md` | 逐条升级项的《异议单》：差异描述＋特征证据＋建议方案（字段取自 `escalation.template.fields`） |

《异议单》与本表的字段口径一致：`差异描述` 取规则 `note`（无规则则取证据）、
`特征证据` 为 T2a 原始证据串（含 bbox／像素／比值）、`建议方案` 取规则 `advice`
（规则没给就写明"请执行方在提交前补写"——不替调用方编建议）。

## 门禁口径（`gate`）

1. 文件不存在 / 读不了 → 英文诊断 + 退出码 2；
2. 章节名（默认 `差异分诊表`）**不存在** → 退出码 2；
3. 章节**存在但固定字段不齐**（缺 `差异｜特征证据｜分诊档位｜依据｜处置` 之一，或正文里
   没有 Markdown 表格行）→ 退出码 2；
4. 只看**该章节体内**的表格：章节外的同名表格不能顶替（章节体＝该标题到下一个同级或更
   高级标题之间）。

通过时打印中文报告（章节行号、固定字段、数据行数）。

## 2034 回放结果（A-H1-1 / A-H1-2 / A-H1-3）

产物落 `out/still/imgcmp/h1/`，规则为 `h1/rules_2034.yaml`（仓库示例的副本，
sha256-16 见 `h1/sha256_16.txt`）。

### A-H1-1 回放（首轮错误模型）

| 用例 | 分诊行数 | 允许差 | 自由度内 | **升级** | 显式构型级命中 |
|---|---|---|---|---|---|
| 2034 真实对 `_baseline/fig3b.png` vs `match/_ours_fig3b.png` | 16 | 6 | 0 | **10** | `CFG-01`(R04 外置桅杆)、`CFG-02`(R09 开架方舱) |
| 受控缺陷对 `refgen/pair/module_split` vs `module_long`（黑底） | 18 | 0 | 0 | **18** | `CFG-01`(R07 高度分数 0.301→1.000，外置长杆取代短件) |

- **允许差（挂清单条目号）**：`R01` → `2034-AL-01`（基准无背景／我方星空）；
  `R10/R11/R13/R14/R16` → `2034-AL-02`（基准拆面展示／我方外立面闭合，仅限同一个已配对件
  上由剖口引起的几何类差异）。两条清单条目均由规则文件承载。
- **升级（显式构型级）**：真实对 `R04`＝我方独有长杆件（`added`、形状类 `rod`、最大者
  px 11947）→ `CFG-01` 外置桅杆；`R09`＝我方独有多边形舱体件（`added`、形状类 `other`、
  px 18432，位于画幅下部＝下段位置）→ `CFG-02` 开架方舱。受控对 `R07`＝高度分数
  0.301→1.000 → `CFG-01`（"长杆取代短件"的同类差异，正是 A-T2a-1 的判据）。
- **`CFG-03`（六边形主镜）在两组数据里都没有命中**：见下节"已知边界"。
- 异议单：真实对 10 张（`h1/real_pair_fig3b/escalations/`），受控对 18 张
  （`h1/controlled_pair_module/escalations/`）。

### A-H1-2 模板章节（骨架）

```markdown
## 差异分诊表

> 强制章节（A-H1-2）：字段固定为 差异｜特征证据｜分诊档位｜依据｜处置；交付物未附本表即视为未交付。
> 三档：允许差（挂清单条目号）／执行自由度内差异（挂自由度范围）／升级（出《异议单》，禁止自行维持）。
> 异议单字段：差异描述＋特征证据＋建议方案（落 `escalations/`）。

| 差异 | 特征证据 | 分诊档位 | 依据 | 处置 |
|---|---|---|---|---|
| （差异描述） | （特征表证据） | 允许差／执行自由度内差异／升级 | （清单条目号／判据号／异议单号） | （处置） |
```

`python3 -m primer.imgcmp.triage triage --template` 与 `gate --template` 打印同一骨架；
真实分诊结果里的表格就是同一字段（见 `h1/real_pair_fig3b/差异分诊表.md`）。

### A-H1-3 门禁证据

见 `h1/gate/A-H1-3_门禁证据.log`：对原样交付说明（无该章节）执行 `gate` → **退出码 2**，
stderr 英文诊断；把章节补入后同一命令 → 退出码 0。

## 已知边界（不藏）

1. **`CFG-03`（六边形主镜）未被两组数据支撑。** T2a 的形状类只有
   `rect/circle/ring/rod/other`，六边形归 `other`、圆形归 `circle`，判据本身可分；
   但 `_ours_fig3b.png` 里主镜区与遮阳屏多层盘连成一片（主镜未成为独立件、且小于
   `min_area`），`fig3a` 整图对里该区也没有独立的形状类差异。**要判它，需要**：
   （a）对同一视角、同一取景的一对图，在集光器主镜 ROI（或带锚件的 ROI 表）上跑 T2a，
   使主镜成为独立组件；或（b）`refgen` 里带命名真值（`truth.components[].label`）的
   主镜对；两者任一即可让 `CFG-03` 的 `shape_class` 规则命中。本报告如实记 2/3，
   不拿别处的差异凑数。
2. **未配准的对比对会大量出单。** 真实对的基准是论文裁切（535×490）、我方是渲染
   （1092×1000），取景/分辨不一致，故 `added/removed/count` 等 8 条非构型差异按默认档
   升级出单。这不是工具误判，而是"清单外不得自我豁免"的必然结果。**建议**：给总体
   补一条允许差条目（取景/配准差异），或规定分诊只在**同视角、同取景**配对上进行
   （先做同视角重渲，把基准与渲染配准到同一取景）。
3. **配对级 `background_kind` 只判底别。** 白底 ↔ 黑底是"无背景 ↔ 星空"的必要条件而
   非充分条件；若基准也是黑底而没有星空，规则需另给信号（如 `--pattern` 匹配证据，
   或先用 T1 边缘通道判"是否有星点"）。
4. **规则文件是调用方资产，工具不生成也不校核其内容。** 工具只能保证"没命中规则就升级"，
   保证不了规则本身写得对。

## 建议固化到《工作规范》的两条

1. **每轮渲染按轮次归档、禁止同名覆盖**（本任务踩过的坑）：`out/still/` 下若以同名文件
   覆盖上一轮渲染，事后无法复现"哪一轮对哪一张基准"的对照，分诊表的证据串（bbox／像素量）
   也跟着失去可追溯性。建议渲染产物按 `轮次/视角` 目录归档，分诊表与 JSON 里用
   绝对路径 + sha256-16 记账（本工具的 `rules.sha256_16` 与 T2a 表的 `image` 字段即此用意）。
2. **分诊只在同视角、同取景配对上进行**：取景差异会伪装成组件数/面积差异，把真差异淹没
   在默认升级里。先把基准与渲染配准到同一相机（同视角重渲）→ 再分诊。

## 实施注记

- 依赖：仅仓库既有依赖（PyYAML／numpy／Pillow）；不引新依赖。
- 复用：建表走 `feat.build_table`，对照走 `feat.compare_tables`（**import，不重写**）；
  报告／异常／CLI 骨架走 `common.report/selftest_report/run_main/make_parser`。
- 确定性：产物不含时间戳，同输入同输出（便于 diff 与复核）。
- 测试：`tests/test_imgcmp_triage.py`（37 例，含对 2034 真实产物的回放用 skipif 保护）。

---

## 第三对受控缺陷与 A-H1-1 补齐（六边形镜 vs 圆镜，2026-10-02 第二轮）

A-H1-1 的三项首轮错特征里，"六边形主镜"在 2034 真实对（`_baseline/fig3b.png` vs
`match/_ours_fig3b.png`）中**没有独立组件**（主镜区与遮阳屏多层盘连成一片），故第二轮
按已批准的机制（用 `refgen` 受控缺陷对承担首轮错特征）补了第三对：**六边形镜 vs 圆镜**。

### 造对

| 项 | 值 |
|---|---|
| 新部件 | `tests/fixtures/imgcmp/parts/hex_plate.glb` ＋生成脚本 `make_hex_plate.py`（Blender 无头，`bpy.data.meshes.from_pydata` 建六棱柱再导出 GLB） |
| 拼装声明 | `tests/fixtures/imgcmp/assemblies/mirror_hex.yaml`、`mirror_round.yaml`（**逐行同构，只有 `mirror` 一行不同**） |
| 结构 | 主镜 ×1 ＋ 遮阳屏盘 ×2 ＋ 桁架杆 ×2（`box/bar/disc/hex_plate` 四个 fixture 部件） |
| 机位 | `--angles 0,0,0 --ortho --res 1200,900`；两 spec 的 `ortho_scale`／`distance`／`target_center`／整器尺寸逐值一致 |
| 产物 | `out/still/imgcmp/refgen/pair/{mirror_hex,mirror_round}/`；分诊产物 `out/still/imgcmp/h1/mirror_pair/` |

### 真值与实测

`truth.parts.mirror` ＝ `other`（六边形板）／`circle`（圆板），其余 4 件
`circle/circle/rod/rod` 两侧一致；T2a 实测同结论：六边形 elongation **1.351**（>1.25）
→ `other`，圆板 elongation 1.000 → `circle`，出 `shape_class circle→other` 差异行，
被规则里的 `CFG-03`（"六边形主镜"）判**升级**（黑底/白底各 3 行、3 张异议单）。

### 为什么六边形板是"对边比 1.35 的六边形"而不是正六边形（能力边界）

T2a 的 `circle` 判据只用两个量（`feat.py:542`）：`circle_fill = 4A/(πwh) ∈ [0.88, 1.12]`
且 PCA 涨宽比 ≤ 1.25。**正六边形**：`circle_fill = 1.103`（在带内）、六重对称使其
PCA 涨宽比 = **1.00**；任意共同正交机位下正六边形与圆盘经受**同一个投影压缩因子**，
涨宽比恒相等 ⇒ 两者在 T2a 判据下**数学上不可分**，`shape_class` 只能给同一个 `circle`。
要让 T2a 出 `shape_class` 差异，六边形轮廓自身的涨宽比必须越过 1.25（本件 1.35，
`extent` 仍 0.866 < 0.90 故落 `other`）。

若总体要求 T2a 直接判"六边形 vs 圆"（不依赖拉长），需给 T2a 补判据，三选一：

1. 轮廓凸多边形拟合：最小外接凸多边形边数 ≥ 5 且拟合残差 ≤ 2% → `other`／新增 `polygon`
   类，圆盘因拟合残差大仍留 `circle`；
2. 把 `circle` 的 `circle_fill` 上界由 1.12 收紧到 1.05（正六边形 1.103 即落在外），
   代价是带遮挡缺口的圆件可能被踢出 `circle`；
3. 新增 `hexagon` 类：面积 ÷ 最小外接正六边形面积 ≥ 0.98 判六边形。

三者都要动 `feat.py` 的判据表与 `truth` 的语义类表（工具口径变更），不在本轮回放范围内。

### 受控对复用的两个坑（已写进 `h1/mirror_pair/A-H1-1_第三对.md`）

1. **金属镜面材质在黑底渲染里会消失**：`metallic=1.0/roughness=0.12` 的板在黑底 EEVEE
   单主光下把黑环境反射回来 → 板面近背景色 → T2a 前景掩膜整块丢掉它（黑底表只剩 4 件）。
   受控对要求"只有镜形不同"，**材质也必须同一口径**（现按 `box/bar/disc.glb` 一样不给材质）。
2. **布局要关于原点对称**：refgen 相机对准装配归零后的模型原点；镜件若不在整器包络中心，
   取景会偏心并把外侧件裁掉（首版遮阳屏盘被右边界裁掉 45 px）。现布局（镜件居中、两屏盘
   分列左右、两横杆上下）使包络中心＝原点，取景在两份 spec 间逐值一致。
   另：`rot_deg` 是 XYZ 欧拉，`bar.glb` 长边沿 X，要立起来须绕 **Y** 转 90°（绕 Z 只会
   把长边转到深度方向，件在正视里缩成小块）。
