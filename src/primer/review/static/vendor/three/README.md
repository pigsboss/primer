# vendor/three —— 三维视图用的 three.js（r160）

会话舱"资产树里 kind=stl 的资产"要一个能转、能缩、能平移的 3D 视图，本地静态服务直接供 ES 模块，
不引入构建链、不走 CDN（评审环境可能离线）。

| 文件 | 来源（three@0.160.0，MIT） |
|---|---|
| `three.module.js` | https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js |
| `OrbitControls.js` | https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js |
| `STLLoader.js` | https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/STLLoader.js |
| `LICENSE` | https://cdn.jsdelivr.net/npm/three@0.160.0/LICENSE |

**唯一改动**：两个 addon 的 `from 'three'` 改写为 `from './three.module.js'`（避免 importmap
的浏览器支持问题），其余逐字节保持上游。升级时照此重做，并在本文件更新版本号与来源。

许可：MIT（见 `LICENSE`）；随 primer 源码一同分发，属可再分发依赖。
