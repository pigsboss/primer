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
| `*.md` | kimi work 的任务说明、建模规格、验收清单、验收反馈（01/02），以及三份交付说明 |

## 跑法

需要 Blender（实测 5.1.1；脚本只用了 3.6+／4.x 都具备的 API）：

```bash
blender --background --python collector.py     # 建模 + 四视角自检渲染到 out/
blender --background --python verify.py        # 自动验收，输出 PASS/FAIL 清单
# combiner.py / verify_combiner.py、assembly.py / verify_assembly.py 同理
```

`assembly.py` 依赖同目录的 `collector.py` 与 `combiner.py`；`combiner.py` 依赖 `collector.py`。

## 运行时依赖的外部素材（不在本仓库）

任务的参考图与动画抽帧由 kimi work 提供，留在工作区
`_primer/scene/interferometer/pathfinder/refs/`。本目录的脚本**不读**它们——自动验收全部是程序化判定
（几何、比例、材质、朝向、包络），只有人工目检那一步才需要那些图。

## 状态

- 阶段一 `verify.py` 13/13、阶段二 `verify_combiner.py` 13/13、阶段三 `verify_assembly.py` 12/12 PASS；
- 阶段四（粉色星光入射光束、红色器间光束、分离过程动画）未做；
- 已知待 kimi work 确认的三处判据读法见《阶段三_交付说明》第 6–8 条。
