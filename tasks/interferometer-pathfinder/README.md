# 觅音计划 2028 干涉探测任务——三器建模（pathfinder）

本目录是工作区 `_primer/scene/interferometer/pathfinder/` 的**版本化副本**。按分工：可复用与可交付的
代码入库 primer，数据资产与一次性脚本留在工作区。

> **工作区那份是权威副本**——kimi work 的验收闭环（建模 → 自动验收 → 目检）在那里跑。本目录用于留存
> 版本历史；两处改动需手动同步（本目录不含素材，见下）。

## 内容

| 文件 | 作用 |
|---|---|
| `collector.py` | 阶段一 集光器单体：梯形平面的金色 MLI 平台舱、黑色镜筒（根部嵌入舱顶、+Y 侧向出光窗口）、3 台斜置机构、2 只胶囊推进剂储箱、太阳翼（展开/收拢两种状态） |
| `combiner.py` | 阶段二 合束器单体：无镜筒；深灰圆柱载荷舱 ＋ 接收折转镜 ×2 ＋ 敏感器 ×1；同族构件直接调用 `collector`，不复制常量 |
| `assembly.py` | 阶段三 三器组合体（在轨组合体测试状态）：共机架两级堆叠，三器太阳翼全部展开 |
| `verify.py`<br>`verify_combiner.py`<br>`verify_assembly.py` | 三阶段自动验收：A1–A13 / C1–C13 / D1–D12；只读检查，日志写 `out/<阶段>/verify_log.txt` |
| `formation.py` | 阶段四 分布式编队（三器＋星光束＋器间束；显示基线机制、`EMPTY_LAYOUT` 整体缩放） |
| `wing.py` | 太阳翼构件（两型共用）；阶段五经《验收反馈_09》**限域解冻**：斜面安装＋SADA 方位修正＋折叠运动学导出 |
| `separation.py` | 阶段五 组合体→分布式编队的分离与展开动画（关键帧＋正式档渲染＋`--still4k` 静帧通道） |
| `verify_formation.py`<br>`verify_separation.py` | 阶段四 E1–E24／阶段五 F1–F12 自动验收（网格真值、射线奇偶没入、贴合类双侧限） |
| `bench_compare.py` | mat_bench：与 `jwst.glb` 同框做材质标定（素材在工作区，不在本仓库） |
| `*.md` | kimi work 的任务说明、建模规格、验收清单、验收反馈（01–09），以及各阶段交付说明 |

## 跑法

需要 Blender（实测 5.1.1；脚本只用了 3.6+／4.x 都具备的 API）：

```bash
blender --background --python collector.py     # 建模 + 四视角自检渲染到 out/
blender --background --python verify.py        # 自动验收，输出 PASS/FAIL 清单
# combiner / assembly / formation / separation 同理
```

依赖链：`assembly.py` 需 `collector.py`＋`combiner.py`；`formation.py` 需三者；`separation.py` 需 `formation.py`；
`wing.py` 被 collector／combiner 共用。

## 运行时依赖的外部素材（不在本仓库）

任务的参考图与动画抽帧由 kimi work 提供，留在工作区
`_primer/scene/interferometer/pathfinder/refs/`。本目录的脚本**不读**它们——自动验收全部是程序化判定
（几何、比例、材质、朝向、包络），只有人工目检那一步才需要那些图。

## 状态（2026-10-02）

- **本包未定稿（interim）：入库 ≠ 定稿。** 阶段一～五均已交付并经验收（阶段五由提出方 2026-10-02 验收：
  F1–F12 **12/12**，四套回归 13/13、13/13、12/12、24/24）。提交 `ac93e51` 为阶段五任务包入库提交，
  **历史保留、不重写**。
- **定稿条件**（依提出方 2026-10-02 决定）：① kimi work 布置的**两层工具**——T1 多尺度多部位切图器、
  T2a 组件特征表／T2b 姿态反求，以及 H1 差异分诊与交付门禁——**完成并通过总体 A-T\*/A-H\* 验收**；
  ② 用该工具对本包跑完**特征级 v3 复核**并处理结论；③ 复核后再做一次入库提交，届时方视为**定稿**。
- **（2026-10-06 补记）T2b 注销**：提交方已裁定**不保留**姿态反求工具——`primer.imgcmp.pose`
  （`primer-imgpose`）与剪影适配器（`adapters/model_silhouette.py`、`adapters/observatory_silhouette.py`）
  及 `tests/test_imgcmp_pose.py` 已删除；定稿条件①中 **T2b 一项就此注销**（T1／T2a／H1 不受影响）。
- **已登记未办项**：《验收反馈_09》§三.4 要求的"阶段三/四定图联动重出"**未执行**（`wing.py` 已变更）。
- 冻结与哈希记账见工作区 `out/separation/script_hashes.json`（唯一例外：`wing.py` 限域解冻，
  `a140096fb563fe03` ← `fa6e95a3e1ed76ea`）。
