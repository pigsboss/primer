# imgcmp 工具链

参考图／渲染图对照工具链。同源链路：**T1 切图 → T2a 组件特征表 → H1 差异分诊与交付门禁**；
本文写 T1、参考生成器（refgen）、T2a 三节，H1 差异分诊与交付门禁另见
[imgcmp_triage.md](imgcmp_triage.md)。

语言纪律按 [CODING_STANDARDS.md](CODING_STANDARDS.md) v1.1：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。

## 调用约定

本工程**不安装发行包**，一律以**模块方式**调用：

```bash
python3 -m primer.imgcmp.<工具> …
```

- 本机 `import primer` 靠环境里的 `PYTHONPATH`（指向本仓 `src/`）；`site-packages` 里
  **没有** `primer` 的 `dist-info`，也**没有**任何 `primer-*` 可执行脚本。若你的环境未设，
  在命令前加 `PYTHONPATH=src` 即可（在仓库根下运行）。
- `pyproject.toml` 的 `[project.scripts]` 里声明的 `primer-imgtile`／`primer-imgrefgen`／
  `primer-imgfeat`／`primer-imgtriage` 只是**安装之后才会存在**的短名，
  **本工程不使用**。文档里若出现这些短名（章节标题、JSON 的 `tool` 字段值等），
  都等价于下表的模块调用；**示例命令一律按 `python3 -m` 书写**。

| 短名（仅安装后存在） | 本工程调用 |
|---|---|
| `primer-imgtile` | `python3 -m primer.imgcmp.tiler` |
| `primer-imgrefgen` | `python3 -m primer.imgcmp.refgen` |
| `primer-imgfeat` | `python3 -m primer.imgcmp.feat` |
| `primer-imgtriage` | `python3 -m primer.imgcmp.triage` |

---

## T1 `primer-imgtile` —— 多尺度多部位切图器

### 用途

把一张大尺寸参考图（论文页／PPT 渲染页／CAD 截图，可带 ROI）切成一组"多尺度＋多
部位"的上下文图像块，供多模态 LLM **按序阅读**：先看总览建立全局构型，再按网格
由粗到细逐块细读；要核对轮廓是否闭合、杆件是否连续时，对照同坐标的边缘强度通道。

它解决的是"整图一次性传进去、细节被当成 token 而不是数据"的问题——每一块都是
**原生分辨率**，与源图对应区域逐像素相同，所以"某单块内是否原生可读"这件事是可
判定的（比对像素即可）。

### CLI

```
python3 -m primer.imgcmp.tiler <image> [--roi X,Y,W,H] [--grids N [N ...]] [--overlap F]
               [--max-full PX] [--out DIR] [--no-edge] [--selftest]
```

| 选项 | 缺省 | 说明 |
|---|---|---|
| `image` | 必填（`--selftest` 除外） | 参考图路径；PNG/JPG 均可 |
| `--roi X,Y,W,H` | 整图 | ROI，**原图像素**、左闭右开；越界自动裁到图内，裁空则报错退出 2 |
| `--grids N ...` | `2 3` | 网格数，可给多个（如 `--grids 2 3 4`）；重复值自动去重 |
| `--overlap F` | `0.12` | 每块向外扩的比例（相对块自身的宽／高），取值 `[0,1)` |
| `--max-full PX` | `1024` | 总览图最长边上限；ROI 本身不超过它时**不缩放** |
| `--out DIR` | `<图名>_tiles`（与源图同目录） | 产物目录，不存在则建 |
| `--no-edge` | 关 | 不产边缘强度通道 |
| `--selftest` | 关 | 自造图跑全流程自检，中文报告，返回 0/1 |

退出码：成功 `0`；用法／输入错误（图不存在、ROI 为空、参数越界）`2`；未预期异常 `3`。

### 产物清单

```
<out>/
  full_1x.png                        ROI 总览，最长边 ≤ --max-full
  grid{N}x{N}/tile_r{r}c{c}.png      n×n 原生分辨率块（含重叠、裁到 ROI 边界）
  edge/full_1x.png                   总览同尺寸的边缘强度通道
  edge/grid{N}x{N}/tile_r{r}c{c}.png 各原生块的同坐标边缘通道
  contact_sheet.png                  总览＋最细网格线与块编号
  manifest.json                      每块源坐标、尺度、摘要与建议阅读顺序
```

### `manifest.json` 字段

顶层：

| 键 | 含义 |
|---|---|
| `tool` | `"primer-imgtile"` |
| `image` | 源图**绝对路径** |
| `image_size` | `[W, H]` 源图像素尺寸 |
| `roi` | `[x, y, w, h]`（已裁到图内的实际 ROI） |
| `roi_box` | `[x0, y0, x1, y1]`，同一 ROI 的 xyxy 记法 |
| `overlap` | 实际生效的重叠比例 |
| `grids` | 实际生效的网格数列表 |
| `max_full` / `scale` / `overview_size` | 总览的最长边上限、相对源图的缩放比、落盘尺寸 |
| `kind` | `white`／`black`，由 ROI 边框带中位亮度判定 |
| `edge_channel` | 边缘口径：`background`／`formula`／`weight`／`gradient`／`normalize` |
| `items[]` | 每件产物一条，见下 |
| `contact_sheet` | 联系表的相对路径 |
| `reading_note` | 建议阅读顺序（中文） |

`items[]` 每条：

| 键 | 含义 |
|---|---|
| `order` | 建议阅读顺序（0 起，粗网格在前、细网格在后） |
| `kind` | `overview`／`grid`／`edge`／`contact_sheet` |
| `file` | 相对 `--out` 的 POSIX 路径 |
| `src_box` | `[x0, y0, x1, y1]`，**原图像素**、左闭右开 |
| `native` | 该文件是否与源图对应区域**逐像素相同** |
| `scale` | 相对源图的缩放比（仅 `overview`／`edge` 的总览条目） |
| `grid` / `row` / `col` | 网格块的 `[N, N]` 与行列号 |
| `sha256_16` | 该文件 sha256 的前 16 位 |
| `note` | 中文用途说明 |

### 约定

- **ROI**：一律原图像素 `(x, y, w, h)`、左闭右开；CLI 用 `"x,y,w,h"` 字符串。
  越界部分裁掉，裁完为空即报错——不静默当成空图。
- **框**：内部统一 `(x0, y0, x1, y1)`，与 PIL 的 crop 一致。
- **块边界**：每块 = ROI 均分的第 (r, c) 格向外扩 `overlap`，再**裁到 ROI**（不外扩
  到 ROI 之外、不补边）。下界取 floor、上界取 ceil，保证 n² 块的并集恰好覆盖 ROI，
  且相邻块的重叠不小于 `overlap`。
- **底别**：由 `common.detect_kind` 按 ROI 边框带中位亮度判定（≥128 为白底）。
- **原生纪律**：`native=true` 的块由源图 ROI 直接切片，不经任何缩放或重采样。
  总览若未超 `--max-full` 也照此办理（此时它同样 `native=true`）；一旦缩放即
  `native=false`。边缘通道是**算出来的**，坐标虽同源，像素不冒充原生（恒
  `native=false`）。
- **边缘口径**（对称双口径，`manifest.edge_channel` 有机器可读版）：
  白底 `e = 暗部 × 梯度幅值`（`暗部 = 1 − luma/255`）；黑底 `e = 亮部 × 梯度幅值`
  （`亮部 = luma/255`）。两者互为关于 127.5 的镜像，白底看墨线、黑底看亮部。
  梯度取 Rec.601 亮度上的中心差分幅值；归一化除数取**整个 ROI** 的最大场值
  （不是逐块归一化），所以各块灰度可跨块比较、可共用一套阈值。

### 已知边界

- 总览按 LANCZOS 缩放，只用于看全局；**任何量测都必须回到原生块或边缘块**。
- 边缘通道用中心差分的梯度幅值，线宽 1 px 的细杆在低对比处可能只有弱响应；它给的是
  轮廓证据，不是二值提取结果。
- 联系表上的网格线与编号画在**最细**那个网格上（`grids` 里最大的 N）；要引用更粗
  网格的块，用 manifest 的 `src_box`。
- 输出为 PNG，不写 EXIF／dpi；ROI 之外的页面内容一概不落盘。

---

## 参考生成器 `primer-imgrefgen` —— 合成基准与真值源

### 用途

把 3D 素材变成"**带真值的合成基准**"：一个部件给一个唯一纯色的 ID 图（逐像素部件标签）、
剪影、白底 CAD 观感图、黑底渲染图，外加一份 `truth.json`。它给 T2a（组件特征表）与
H1（差异分诊）提供**可评分的数据集**——有真值，工具的输出才能被客观
判对错，而不是互相比对自证。

它是**离 Blender 的编排器**：本体在普通 python 下跑，负责参数解析、构型声明、真值提取
与报告；渲染任务写成一个 job JSON 交给一个临时 Blender 脚本，用
`blender --background --python <tmp>` 执行，出图之后**真值仍在 refgen 侧**用 numpy/PIL
从自己的 `id.png` 里算。Blender 只出图，不算真值。

### CLI

```
python3 -m primer.imgcmp.refgen --model <glb|stl> [--assemble spec.yaml] --out DIR
                 [--views white,black,id,silhouette]
                 [--angles "az,el,roll;az,el,roll;..."]
                 [--az-el-grid "AZ_STEP,EL_STEP"]
                 [--res 1200,900] [--ortho] [--focal MM] [--scale-to M]
                 [--truth spec.yaml] [--contact-tol PX] [--timeout S] [--selftest]
```

| 选项 | 缺省 | 说明 |
|---|---|---|
| `--model FILE` | — | 单件模式：一个 `.glb`/`.gltf`/`.stl`；与 `--assemble` 互斥 |
| `--assemble SPEC.YAML` | — | 拼装模式：按 YAML 把多个部件文件拼成已知构型（见下） |
| `--out DIR` | 必填 | 产物目录 |
| `--views LIST` | 全部四类 | `white,black,id,silhouette` 的子集；**必须含 `id`**（真值由它算得） |
| `--angles` | `0,0,0` | `;` 分隔的 `az,el[,roll]` 机位表；`roll` 可省 |
| `--az-el-grid` | 关 | 位姿网格，见下；给了它就**取代**默认机位（显式 `--angles` 会一并保留） |
| `--res W,H` | `1200,900` | 渲染分辨率 |
| `--ortho` | 关 | 正交相机（对比高度分数这类量时用它，投影无透视畸变） |
| `--focal MM` | `50` | 透视焦距；`sensor_fit` 固定 `HORIZONTAL`（36 mm） |
| `--scale-to M` | 关 | 把模型最长边归一到 M 米 |
| `--truth SPEC.YAML` | 关 | **手工声明的语义**（形类／锚件）；别名 `--truth-truth`，等价 |
| `--contact-tol PX` | `2` | 包围盒间隙不超过它才算"接触/相邻" |
| `--timeout S` | `3600` | Blender 墙钟上限 |
| `--selftest` | 关 | 自造合成 ID 图跑全流程自检，中文报告，返回 0/1；**不需要 Blender** |

Blender 可执行文件按 `$PRIMER_BLENDER` → PATH 上的 `blender` → macOS 应用路径依次找。
退出码：成功 `0`；用法／输入错误（模型不存在、spec 坏、位姿或视图不合法）`2`；
未预期异常 `3`；`--selftest` 全过 `0`、有不过 `1`。

### 产物清单

```
<out>/
  views/az{A}_el{E}_roll{R}/
    id.png            每件一个唯一纯色（关抗锯齿、关阴影、无泛光）→ 逐像素部件标签
    id_legend.json    rgb -> 部件名/序号（英文键）
    silhouette.png    白对象/黑底、关抗锯齿的掩膜
    white_cad.png     白底 CAD 观感（背景纯白、环境/平光着色、无阴影）
    black_render.png  黑底渲染（背景纯黑、单一主光＋fill，**不加星空**）
    truth.json        该机位的真值
  truth_summary.json  本次运行的机位清单 ＋ 各机位 truth 的路径与 sha256
  render_meta.json    Blender 侧回执：部件↔网格映射、归一化尺寸、逐视角相机与耗时
  blender.log         渲染日志（stdout＋stderr）
```

### `truth.json` 字段

顶层：`view`、`camera`（`az_deg`/`el_deg`/`roll_deg`/`ortho`/`focal_mm`/`ortho_scale`/
`distance`/`res`/`sensor_fit`、以及 `az_convention`）、`model`（路径、`sha256_16`、
归一化后尺寸 `normalized_dim_m`、网格/顶点/面数；拼装模式还带 `parts[]` 逐件路径与 sha）、
`total_payload_height_px`、`object_pixel_count`、`component_count`（可见）／
`component_count_all`、`anchor`、`truth_schema`（口径自述）、`id_match`（色匹配统计）。

`components[]` 每条：`index`、`name`、`id_rgb`、`visible`、`pixel_count`、
`area_fraction`（占整器像素）、`bbox_xyxy`、`centroid`、`shape_class`、`height_fraction`
（该件竖直跨度／整器高度）、`anchor_ratio`（相对声明锚件的最大跨度比）、`islands`
（该件的 8 邻接连通域个数）、`contacts[]`（`part`／`gap_px`／`bbox_overlap`，
间隙为正、包围盒相交为负）、`source`。

### 真值口径（与 T2a **不同源**，这是刻意的）

- **几何量**（`bbox_xyxy`、`pixel_count`、`area_fraction`、`height_fraction`、
  `total_payload_height_px`、`contacts`、`islands`）＝ **由 `id.png` 逐像素算得**：
  按 `id_legend.json` 把像素映到部件（精确命中优先，余下按曼哈顿距离近邻、超容差即
  归背景），再做**连通域 ＋ 包围盒**。refgen **不做形状描述子、不做分割阈值、不做边缘
  检测**——那是 T2a 的活。两边从不同的量出发，T2a 的结论才不是自证。
- **语义量**（`shape_class` ∈ `rect`/`circle`/`ring`/`rod`/`other`、以及"哪件是锚件"）
  ＝ **手工声明的 YAML**，refgen 从不自动分类。
- `components[].source`：在 YAML 里被点名＝`semantic`；只是被 ID 图发现、YAML 未点名
  （形类按 `default_shape_class` 填）＝`derived`。

### 语义 YAML

```yaml
match: "*/antennas/dsn70m.glb"   # 备忘用：这份声明是给哪个模型的
default_shape_class: other       # 未点名部件的形类
anchor: {name: dish, size_m: 70.0}
parts:                           # 部件名（或 glob）-> shape_class；先命中者胜
  dish: circle
  truss_dish: circle
  collector: rod
```

部件名的匹配是**完全相等或显式 glob**（不做隐式前后缀），所以窄模式要写在宽模式之前。
随仓库的实例见 `tests/fixtures/imgcmp/model_truth/*.yaml`。

### 拼装模式 `--assemble spec.yaml`

```yaml
name: module_split
models_root: /path/to/assets/models      # 相对 spec 文件解析；part 的相对路径挂在它下面
parts:
  - name: body
    part: components/power_structure/satellite_kit/body_2.glb
    rot_deg: [0, 0, 90]
    offset_m: [0, 0, 0]
    scale: [1, 1, 1]        # 标量或三值，必须为正
truth:                      # 本 spec 内的语义声明（--truth 给了就以它为准）
  default_shape_class: other
  parts: {body: rect, radio: circle}
expect:                     # 声明期望差异，refgen 打印"期望 vs 实测"对照表
  pose: az0_el0_roll0       # 对照机位（缺省即此值）；该机位不在本次运行则跳过不表
  component_count: 4
  height_fraction: {body: 0.263, radio: 0.608}
```

摆放口径：每件先按自身包围盒中心对中，再 `scale` → `rot_deg`（XYZ 欧拉）→ `offset_m`；
全部摆好后整器按包围盒中心归零，`scale_to` 再统一缩放。**拼装模式下每项 `parts[]` ＝
一个部件**（该件文件里的多个 mesh 合成同一标签），所以组件数由 spec 说了算——这正是造
**受控缺陷对**（组件数、高度分数的差异由我指定）所需要的。front 机位
（`--angles 0,0,0 --ortho`）下屏幕右＝+X、屏幕上＝+Z，高度分数因此可解析预测。

### ID 图的颜色分配

部件序号 `i` → `palette_color(i)`：三通道各 31 级、步距 8、起于 8（最多 29791 件），
纯黑留给背景。渲染走 Workbench `color_type='OBJECT'`，`obj.color` 先做 sRGB→线性转换；
配合 `view_transform='Standard'`、`dither_intensity=0`、`render_aa='OFF'`，像素**逐字节
等于目标色**。即便如此 refgen 仍按近邻容差匹配，所以 1 LSB 的抖动也不会串件。

### 已知边界

- ID 图关抗锯齿是**必需**的：开了 AA，轮廓像素就是两种部件色的混合，既不等于任何调色板
  色、也说不清属于谁。代价是 `pixel_count` 在轮廓上有 ±1 px 量级的不确定。
- 部件颜色相同且投影相邻时，`islands` 会把它们分开，但**部件语义**只能靠 spec／YAML
  区分——refgen 不从像素猜部件边界。
- 语义 YAML 是**人写的**，写错就错到底；工具只保证"几何量不掺语义、语义量不掺几何"。
- 黑底渲染用 EEVEE、`view_transform='Standard'`、单主光＋fill，光位随机位固定，只求
  可复现，不追求与项目既有交付图的观感完全一致；`white_cad.png` 走 Workbench
  `MATERIAL` 着色，保留 glTF 的自带色，不是单色黏土。
- 单次运行的子进程峰值内存由 `RUSAGE_CHILDREN` 读出，是**运行级**读数，不是逐帧的。

---

## T2a `primer-imgfeat` —— 组件特征表与差异对照表

> **本节是 T2a 的权威口径，历次增补（第 2 轮：ROI／图注／开口归属／背景相对阈值；第 3 轮：
> 边缘分割；第 4 轮：`--merge-large-blobs`；第 5 轮：`polygon` 判据）已全部并入，
> 自相矛盾的旧表述已删。**

### 用途

把**一张图**变成**部件级的可对账数据**：输入白底 CAD 截图或黑底渲染图，输出组件特征表
（外接框、像素数、面积占比、相对锚件尺寸比、高度占载荷总高分数、形状类、相互接触/间隙
关系、孔洞/开口）；再把参考表与我方表**逐件对照**，输出差异对照表——它是 H1 差异分诊的
输入（`diffs[]` 的字段是给机器吃的，`rank` 分 major/minor）。

它解决的是"两张图看着不一样、但说不清差在哪、差多少"的问题：把"看着不一样"落到
**件数、分数、形状类、开口数**这些可复算的量上。全部处理是**确定性图像处理**
（阈值/连通域/轮廓描述子/锚定比率），只依赖 numpy 与 Pillow；**不含任何需要训练或
外部服务的模型**。

### CLI

```
python3 -m primer.imgcmp.feat table <image> [--anchors YAML] [--kind auto|white|black]
                  [--roi X,Y,W,H] [--threshold PX] [--min-area PX] [--contact-tol PX]
                  [--merge-large-blobs] [--out DIR] [--json OUT.json] [--selftest]
python3 -m primer.imgcmp.feat compare <ref.json> <ours.json> [--tol KEY=VALUE]...
                  [--out DIR] [--json OUT.json] [--selftest]
```

| 选项 | 缺省 | 说明 |
|---|---|---|
| `image` | 必填 | 输入图；PNG/JPG |
| `--anchors YAML` | 无 | 锚件声明文件；**工具内零任务数值**，锚件与实物尺寸一律走这里 |
| `--kind` | `auto` | 底别；`auto` 按边框带中位亮度判定（≥128 为白底） |
| `--roi X,Y,W,H` | 整图 | 只在 ROI 内建表（源图像素、左闭右开，同 T1 口径）；组件 bbox 仍换回源图坐标；背景统计仍取**整幅图**的边框带 |
| `--threshold PX` | 自动 | 显式单阈值覆盖，**走单阈值、不做边缘切分**（逃生口语义） |
| `--min-area PX` | `auto` | 最小区域像素数；`auto` = `max(32, 0.0002·W·H)` |
| `--contact-tol PX` | `2` | bbox 间隙不超过它算"相接" |
| `--merge-large-blobs` | 关 | 额外按"小块贴大块 + 浅谷脊"归并（见下），救纹理整器的过切 |
| `--out DIR` / `--json FILE` | — | 产物目录／显式 JSON 路径 |
| `--tol KEY=VALUE` | 见下 | compare 容差；键：`height_fraction` `area_fraction` `anchor_ratio` `contact_px` `match_score` |
| `--selftest` | 关 | 自造合成图跑全流程自检，中文报告，返回 0/1 |

退出码：成功 `0`；用法/输入错误（图不存在、`--kind`/`--min-area`/`--tol` 非法、锚件文件坏）
`2`；未预期异常 `3`。

### 分割与判据（阈值全部列在这里，机器可读版见 JSON 的 `method` 键）

**两段式分件**（`mask_strategy = inclusive+edge_split`，缺省）：

1. **包含性支撑掩膜**：`score = |luma − 背景中位|`，`support = score ≥ max(8, 6·MAD(边框带))`
   （`BG_MARGIN_FLOOR=8`、`BG_MARGIN_K=6`）。它只回答"和背景不一样"，所以对象自身偏暗/偏亮
   的部分都在内——**暗件不会被丢**。
2. **种子核**：`seed = support ∩ (luma ∓ Otsu)`，丢掉面积 < `--min-area` 的核。核回答的是
   "哪里是典型零件实体"，背景辉光/扫描噪点不在内；这一步是**过切的主要防线**（CAD 线稿会在
   实体内部造出一堆几像素的"核"）。
3. **降序分水岭**：对含 ≥2 个核的支撑连通域，按 `score` 从高到低泛滥，每个像素判给"沿最高
   路径最近的核"。分水岭线落在两核之间的**谷脊**上，也就是零件间的暗缝/明暗分界。含 0 或
   1 个核的连通域整块成一件（**0 核 = 暗件的兜底**）。
4. **浅谷合并**：相邻流域的对比度 `min(峰值) − 谷脊` 若小于 `MERGE_CONTRAST = 16`（亮度单位）
   就并回去——同一实体表面的纹理不该被当成两件。
5. **`--merge-large-blobs`（缺省关）**：在 4 之外再加一条**相对**判据——谷脊
   `< MERGE_LARGE_REL(0.6) × 较低峰值` **且** 两流域面积比 `≤ MERGE_LARGE_AREA_RATIO(0.5)`
   （"小块贴大块"）时也并。它只能**并**、不能切，用于救**纹理整器**的过切（`ast_bennu`
   黑底：4 件 → 1 件）。

`--threshold` 走单阈值（`mask_strategy = explicit`）；支撑掩膜退化（吃空或
> `MAX_FG_FRAC = 0.5` 的分析面积）时回退 `otsu`。三种策略与全部参数都写在
`table.mask_strategy` / `method` 里。

- **组件** = 上述分件结果的 **8 邻接连通域**（行程并查集，不引 scipy），面积 ≥ `--min-area`。
- **载荷总高** `total_payload_height_px` = 保留组件像素并集在 y 方向的跨度（顶到底）。
- **高度分数** = 该件 bbox 竖直跨度 ÷ 载荷总高；**面积分数** = 该件像素 ÷ 保留组件像素总数。
- **形状类**（按轮廓描述子短路判定）：

  | 类 | 判据 |
  |---|---|
  | `ring` | 件自身含孔洞，且最大孔洞面积 ≥ 0.15 × 该件面积 |
  | `polygon` | **正多边形板**：凸包抽稀后残留 5–10 条直边、残差 ≤ 0.035、凸包充实度 ≥ 0.97（见下） |
  | `circle` | `4A/(π·w·h) ∈ [0.88, 1.12]` 且朝向无关长宽比 ≤ 1.25 |
  | `rect` | 矩形度 `A/(w·h) ≥ 0.90`（**含规则矩形长条**——按矩形度优先判 rect） |
  | `rod` | 朝向无关长宽比（PCA 主轴跨度比）`≥ 3.5` |
  | `other` | 其余 |

  "长宽比"用**朝向无关**定义 `sqrt(λ1/λ2)`：实心矩形退化为 `w/h`、圆盘为 1、斜置杆件不受
  朝向影响。`3.5` 这个切口是对 refgen 受控对枚举后取的（真值声明的 `rod` elongation 3.96
  能判出，同对翼板 3.3–3.4 仍判 `rect`）；实心圆 `4A/(πwh) = 1`、实心矩形 `= 4/π ≈ 1.27`。

  **`polygon` 与 `circle` 的边界（第 5 轮补，权威口径）**：正六边形的
  `circle_fill = 4A/(πwh) ≈ 1.103` 落在 `circle` 的 `[0.88, 1.12]` 带内，六重对称又使它
  的 PCA 朝向无关长宽比 `≈ 1.00`，与圆盘经受**同一个**投影压缩因子——**只用"圆度＋长宽比"
  两个量，正六边形与圆在数学上不可分**。补的量是**轮廓的直线段结构**（`feat.py`
  `polygon_descriptor`，自实现、不引 scipy）：

  1. 取该件像素集的**凸包**（Andrew 单调链），得凸多边形轮廓；
  2. 对闭凸包做**贪心顶点抽稀**：反复去掉"到前后邻点连线垂距最小且 ≤ `POLY_SAGITTA_TOL`"的
     顶点（同一轮内不许去掉相邻两点，含环首尾相邻——否则栅格化在尖角处留下的两三个几乎
     共线的像素会被同时拿掉，把整个尖角用一条弦切平，正六边形因此被降成圆）；抽稀容差
     `= 0.035 × 等效半径`（`sqrt(凸包面积/π)`）；
  3. 判据：残留顶点数 `n ∈ [5, 10]` **且** 残差（`|抽稀多边形面积 − 凸包面积| / 凸包面积`）
     `≤ 0.035` **且** 凸包充实度（件像素数 ÷ 凸包面积）`≥ 0.97`。

  为什么这三条能分开：正六边形抽稀出 **6** 个角点、残差 ≈ 0.01；圆盘在同一容差下要把圆周
  拆成 **16–19** 段才达标（弦高 sagitta ≈ `rθ²/8 ≤ 0.035·r ⇒ θ ≈ 30°`），**撞在"边数上限
  10"之外**，故圆仍判 `circle`。充实度 `≥ 0.97` 把星形/桁架这类**凹件**的凸包挡在外面
  （凹件的凸包顶点数可能恰好是 5–10，但件面积远小于凸包面积）。`n = 6` 时另给
  `shape_descriptors.polygon_name = "hexagon"`（5/6/7/8/9 → pentagon/hexagon/heptagon/
  octagon/nonagon；**命名按抽稀后顶点数，像素级 ±1 抖动可能使它偶发落在相邻名**）。
  全部阈值见 JSON 的 `method.shape_rules` 与 `feat.py` 顶部的 `POLY_*` 常量。

  实测边界（第 5 轮）：正六边形板 `polygon/hexagon`（n=6）；同构圆板三片盘、`disc.glb`
  与遮阳屏盘均 `circle`（n=16–18），**无误判**。全语料 44 例 219 件里，唯一同时满足
  "圆规则"与"多边形规则"的两件就是那两个正六边形主镜渲染——真圆一件都没被判成
  `polygon`（误判率 0/15）。`dsn70m` 三个机位里碟面与桁架并成一件（充实度 0.57–0.75），
  没有独立的碟面件可分；该模型唯一被判 `polygon` 的是一件 575 px 的八边形碎片
  （elongation 4.05，本就不满足圆规则）。已知代价：**投影长方体**的可见外廓在本判据下会给出 6/8 边形外廓而判
  `polygon`（refgen 语料里 `grid_mini` 的盒体即如此）——这是"多边形板"类扩到 `other`
  语义之外的副作用，见 `feat.py` 的 `SHAPE_ORDER`。
- **孔洞 / 开口**：孔洞 = 背景的 4 邻接连通域中**不接触图像边框**者，按 4 邻接归属到包围它的
  组件。**开口** = 孔洞面积 ≥ `max(16 px, 0.005 × 该件面积)` 者；bbox 中心落在该件顶部 35%
  带内的另记 `top_band_count`（筒口所在区）。孔洞另给 `extent` / `circle_fill` / `elongation`，
  可直接用来判**开口的截面形状类**。
- **开口归属到件**：每个开口优先归属到**细长件**（朝向无关长宽比 ≥ `SLENDER_ELONGATION = 3.5`，
  与 `rod` 同一切口）的 bbox 内，否则归包围它的件。顶层 `openings[]` 每条给
  `host_component` / `owner_component` / `bbox_xyxy` / `area_px` / `extent` / `circle_fill` /
  `elongation` / `in_top_band` / `host_is_slender`——"每筒开口数"按 `host_component` 分组数出。
- **`annotation` 类（图注/文字）**：全部满足才判 `annotation`——面积 ≤ `ANNOT_MAX_FRAC = 0.02`
  × 前景像素；bbox 矩形度 ≥ `ANNOT_MIN_EXTENT = 0.30`；bbox 整个落在图幅边缘带内（上下
  `ANNOT_BAND_Y = 0.20`、左右 `ANNOT_BAND_X = 0.15`）；且与任何"≥10 倍自身面积"的区域
  **像素距离 > `--contact-tol`**（用膨胀后的实际像素判断，不用 bbox 间隙——图注 bbox 常整个
  落在主体 bbox 内部）。`component_count` 只数 `component`，`annotation_count` 单列。
- **接触**：两件 bbox 间隙 ≤ `--contact-tol` 即记为相接；`gap_px` 取 `common.box_gap`
  （相交为负），另给布尔 `bbox_overlap`。
- **`color_groups`**：图像为"平色块"时按主色归并的**色族数**，非平色图给 `null`。判据：
  占比 ≥ 2% 的**精确 RGB 值**合计覆盖 ≥ 85% 前景——CAD 平色截图满足，连续调渲染图不满足。

### 锚件配置 `--anchors`

```yaml
# 示例，真值由总体提供
anchors:
  - name: shield                  # 屏盘 Ø10.5
    ref_m: 10.5
    selector: {rule: largest}
  - name: primary_mirror          # 主镜 Ø3.5
    ref_m: 3.5
    selector: {rule: bbox_longest}
  - name: tube                    # 筒 9.5 x 1.6
    ref_m: 9.5
    selector: {rule: position, side: top, within_frac: 0.4}
```

`selector.rule` 三选一：`largest`（像素面积最大）、`bbox_longest`（外接框跨度最大，可给
`axis: x|y`）、`position`（落在指定边缘带内，`side: top|bottom|left|right`，`within_frac`
为带宽占载荷外接框的比例，缺省 0.35）。每件的 `anchor_ratio` = 该件最大跨度 ÷ 锚件最大跨度
（与 refgen 真值同口径）；有了 `ref_m` 还顺带给出 `size_m`。**工具本体不含任何任务数值**。
随仓库的示例见 `tests/fixtures/imgcmp/anchors/observatory_2034.yaml`。

### `table` 输出 JSON

顶层：`tool` `mode` `image` `image_size` `roi` `roi_box` `analysed_size` `kind`
`kind_requested` `threshold` `threshold_source`（= `mask_strategy`）`otsu_threshold`
`support_threshold` `seed_count` `background_luma` `min_area` `min_area_option`
`merge_large_blobs` `contact_tol_px` `foreground_pixels` `component_pixels` `region_count`
`component_count` `annotation_count` `annotation_bboxes` `islands`（前景 8 邻接连通域总数，
未过滤前）`total_payload_height_px` `payload_box_xyxy` `flat_color` `flat_color_cover`
`flat_color_plateaus` `color_groups` `anchors[]` `openings[]` `components[]` `method`。

`components[]` 每条：`index` `kind`（`component`/`annotation`）`bbox_xyxy` `bbox_whxy`
`centroid` `pixel_count` `area_fraction` `height_fraction` `span_px` `shape_class`
`shape_descriptors`（`extent`/`elongation`/`circle_fill`/`hole_area_fraction`）`median_rgb`
`color_family` `islands` `slender` `anchor_ratio` `size_m`
`holes{count,total_area_px,items[]}` `openings{count,top_band_count,min_area_px,top_band_until_y,items[]}`
`contacts[]`。

### `compare` 输出 JSON

顶层：`ref` `ours` `tolerances` `counts` `pairs[]` `diffs[]` `summary` `method`。
**计数与配对只看 `component`**，图注只报数量（`counts.ref_annotation` / `ours_annotation`）。

`diffs[]` 每条固定含 **`kind` `ref` `ours` `delta` `rank` `evidence`**：

| `kind` | 触发条件 | 档位 |
|---|---|---|
| `count` | 两表组件数不同 | major |
| `height_fraction` / `area_fraction` / `anchor_ratio` | 配对件对应量之差 > 容差 | `|Δ| > 2×容差` 为 major，否则 minor |
| `shape_class` | 配对件形状类不同 | major |
| `hole_count` | 配对件开口数（全部/顶带）不同 | major |
| `contact` | 配对件相接件数不同 | major |
| `merged` / `split` | 基准 1 件 ↔ 我方 ≥2 件 / 反之 | major |
| `added` / `removed` | 一方有、另一方无 | major |

配对口径：`score = 0.6·bbox_IoU + 0.4·exp(−质心距/平均跨度)`，贪心取
`score ≥ match_score`（缺省 0.15）的互不相交配对；`merged`/`split` 独立于配对先判。
默认容差：`height_fraction 0.05`、`area_fraction 0.02`、`anchor_ratio 0.10`、
`contact_px 2.0`、`match_score 0.15`。

### 中文报告

`table` 打印底别/阈值与掩膜策略/最小面积/前景与组件与图注数/载荷总高/平色块判定，再逐件列出
`序 类别 形类 bbox 像素 面积占 高度分 锚比 开口(全部/顶带) 相接`，有开口时再列"开口归属"；
`compare` 打印两表概要、组件数差、图注数、配对对数，再按档位列差异清单。stdout 只出中文，
stderr/异常/选项名/JSON 键一律英文。

### 评分基准：真值的两套口径（务必分清）

refgen 真值里有两套件数，**不是一回事**：

| 基准 | 含义 | 用途 |
|---|---|---|
| `truth.component_count` | **spec 声明的语义件**（遮挡感知） | 仅语义评分 |
| `sum(truth.components[].islands)` | **8 邻接孤岛总数** | 与"可见材料连通区"同口径 |

全量 refgen 机位（白底＋黑底各一张；**语料随其它轮次的 refgen 产物增长，下表为 2026-10-02
最近一次实测的 42 例**）命中率：

| 配置 | 与 `islands` 完全一致 | 排除孤岛 >100 的模型 | 与 `components` 完全一致 |
|---|---|---|---|
| 缺省（边缘分割） | 13/42 = 31% | 13/34 = 38% | 12/42 = 29% |
| 加 `--merge-large-blobs` | **14/42 = 33%** | **14/34 = 41%** | 12/42 = 29% |

（早期在 38 例语料上的同口径实测是 9/38 = 24% → 10/38 = 26%；语料变大后分子分母同步变，
趋势一致。）`--merge-large-blobs` 的逐例变化（开关对照见 `t2a/merge_large_blobs_ab.json`）：
`ast_bennu` 黑底 **4 → 1**（真值孤岛 1，救回）；`dsn70m` 黑底 3 → 5（真值 649/693，本就
不可达）；`grid_mini`/`sat_body2` 黑底各有升降，**净 +1**，`components` 口径不变。

### 已知边界（逐条实测，不藏）

- **组件 ≠ 部件**：本工具给的是**可见材料连通区**，refgen 真值是 **spec 声明的部件**。例如
  refgen 的 `wing_left` 是"两片蓝板＝一件"，像素上只能看到两片板。拿真值评分请用"配对件的
  高度分数/面积分数误差"，不要期待外接框逐坐标相等。
- **阈值法的固有缺口**：对象的某些部分与背景同侧（黑底渲染里的近黑筒体）时会整体丢失；
  白底 CAD 里比阈值更亮的高光面同理。`--threshold` 是**全局**逃生口，救不回同一张图里
  一半亮一半暗的情形。
- **黑底星空渲染会过连通**：包含性支撑掩膜把微弱的星云/银河辉光也算进前景，把本来分开的
  结构连成一片。实测 `_ours_fig3a`：Otsu 阈值 81 下 4 件、总高 354 px（**筒体丢失**）；
  边缘分割下 4 件、总高 **559 px**（筒体纳入）。需要"把暗件单独拿出来"时用 `--roi`
  收窄范围 + `--threshold` 显式定阈。
- **纹理整器会被过切**：`ast_bennu` 黑底真值 1 件，边缘分割给 4 件（表面明暗起伏的对比度
  超过 `MERGE_CONTRAST`）。**`--merge-large-blobs` 能救回 1 件**；但它只在"小块贴大块 + 浅谷"
  时动手，对 `dsn70m` 黑底会把 10 件并成 5 件（真值孤岛 649，本来就不可达），对
  `module_split`/`grid_mini`/`sat_body2` 无影响。
- **真值孤岛数不可达的模型**：`dsn70m`（孤岛 649–732）、`jwst`（585）来自桁架/镜片碎块；
  像素上这些结构连成 1–2 块。现口径下"与 islands 一致"不可能达成。
- **ROI 内的"背景"不是背景**：ROI 整块落在对象内部时，靠整幅图的边框带取背景才对（已实现，
  见 `--roi` 行）；但若支撑掩膜在该 ROI 内占比 > 50%，会回退 Otsu（实测 `fig3a` 筒顶 ROI）。
- **图注并块**：`_baseline/fig3b.png` 的 "(b)" 在低阈值下 "(" 与 "b" 连成一块（641 px）→
  图注区域数 2 而非 3；高阈值（247）下恢复为 3 块。`fig3a.png` 的 "(a)" 稳定分 3 块。
- **开口只到"件"这一层**：斜置筒的 ROI 若包住整条筒就必然带进相连的屏盘；只包筒顶则筒口
  在 ROI 边界被切开、不闭合，开口数记 0。基准 `fig3a` 的筒口在图上被内线打断，轮廓描述子
  `extent 0.19–0.31`、`circle_fill 0.23–0.39`、elongation 5–12——既不到 `rect` 也不到
  `circle`，只能判 `other`。"矩形口 vs 圆口"在 2D 轮廓上分不开（详见 t2a 验收记录的异议单）。
- **`polygon` 会误收"投影长方体"**：判据只看凸包直边数，不看"是不是正多边形"，所以一个
  长方体在斜视下的可见外廓（6/8 边形）也会判 `polygon`——refgen 语料里 `grid_mini` 的
  盒体就是如此。它只把 `other` 细分，**不影响**件数/分数口径（组件计数命中率与补判据前
  逐位一致：islands 13/42、components 12/42、soft 13/34）。要更严可加"边长均匀度"，
  目前不做（会把已判对的合页/支座类件一起打回去）。
- **开口判据里的 0.005 / 35% / 3.5 / 0.02 / 0.30 / 0.6 / 0.5 都是经验阈值**，换任务族需复核。

