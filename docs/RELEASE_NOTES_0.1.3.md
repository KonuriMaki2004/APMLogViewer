## APMLogViewer 0.1.3 —— Windows x64（安装版 + 免安装绿色包）

ArduCopter DataFlash（`.BIN`）日志分析工具，面向 **Copter 4.6.x / 4.7.x**。
本版在 0.1.2 基础上修复了一个地图页的显示缺陷；0.1.2 引入的**6 组新曲线**与
**右上角「轨迹差异」面板**在本版中保持不变、继续可用。

### 本版修复

- **「轨迹差异」面板切页后一次性显示所有轨迹组合**
  地图页不是当前 tab 时整页被 Qt 隐藏，`visible_tracks()` 曾因 `legend.isVisible()`
  为 False 而回退成“全部轨迹”；切页时的 `_apply()` 会重建轨迹并触发差异重算，
  于是按全选算了一遍，切回地图页后不再刷新，面板就列出了未勾选轨迹的组合。
  现在以**勾选栏行状态**为唯一真源，并在 `showEvent()`（从别的页切回）补算一次。
  新增 2 条 UI 回归；真机复现：隐藏时与切回后都只显示已勾选的 3 对。

### 本版包含（0.1.2 起）

- **曲线页 12 组预设**：振动 / 姿态 / 电池 / 位置 / GNSS 与 EKF / **罗盘 / IMU /
  PID 分量 / 供电与温度 / EKF 位置与速度 / 遥控输入**；
- **地图页「轨迹差异」**：勾选栏下方按「单航线（逐对）」与「总体」给出偏差百分比，
  面板注明口径与基准（同期水平 ENU、以采样稀的一条为时间基准、不含高程）。

### 验证结果

- 全量回归：**1053 passed / 3 skipped / 23 subtests passed**。
- 打包版八项自检（103.6 MB 真机日志 `2026-10-29 14-18-00.bin`）：退出码 **0**，
  加载 **1.36 s**，71 类报文 / 2 619 459 条 / 1331 参数 / 643 事件；
  地图页、曲线页、事件/告警详情、左栏拖动、缓存清理、MAVFTP、U 盘护栏全部通过。
- 包内核验：版本常量 `0.1.3`，`trackmap` 含 `showEvent`/`visible_tracks`，
  `apmlog.core.trackdiff` 与 `apmlog.ui.trackdiffpanel` 均在，12 个预设 id 齐全。

### 产物

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `APMLogViewer-0.1.3-win64-setup.exe` | 45,117,756 B | `0e740c97fd68628a34f01eaa267abc8fffbbc54e5a987954aafec06e5eb65b9c` |
| `APMLogViewer-0.1.3-win64.zip` | 65,131,970 B | `4a92e835712e80da86f1daa155509e9e44e6819a625babbb1475789d30b63b5b` |

同名 `.sha256` 用于校验。

### 安装

- **安装版**：双击 `APMLogViewer-0.1.3-win64-setup.exe`，按当前用户安装
  （`%LOCALAPPDATA%\Programs\APMLogViewer`），沿用同一 `AppId`，可覆盖升级；
  卸载走 Windows「应用和功能」。
- **绿色包**：解压 `APMLogViewer-0.1.3-win64.zip`，运行 `APMLogViewer\APMLogViewer.exe`。

### 已知边界

- Ubuntu `.deb` 仍停留在 0.1.2（见 `apmlog-viewer-v0.1.2` Release），本版未重打；
- 安装包未做代码签名，SmartScreen 可能提示“未知发布者”；
- 轨迹差异只在**同一份日志内**比较，不含高程；
- `MAV_CMD_DO_SET_MODE`（246 / param1=5，进 U 盘模式）在真机上仍未实际发送
  （会重启进 MSD、断链），界面只保留 armed 拦截与确认框。
