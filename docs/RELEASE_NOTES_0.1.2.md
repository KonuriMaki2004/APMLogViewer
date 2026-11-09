## APMLogViewer 0.1.2 —— Windows x64（安装版 + 免安装绿色包）

ArduCopter 飞行日志（DataFlash `.BIN`）分析工具，面向 **Copter 4.6.x / 4.7.x**。
本版在 0.1.1 基础上加了两块功能，原有航迹显示、勾选、播放头、缩放等交互不变。

### 本版新增

**曲线页：预设 6 → 12 组**（只加预设条目，取数/绘制/胶囊/叠加全部复用原有链路）

| 预设 | 数据来源 | 道数 |
| --- | --- | --- |
| 罗盘（磁力计） | `MAG.MagX/MagY/MagZ` + `MAG[1].MagX`（罗盘2） | 4 |
| 陀螺与加计 | `IMU.GyrX/Y/Z`、`IMU.AccX/Y/Z` | 6 |
| PID 分量 | `PIDR.Act` + `PIDR.Tar`（虚线、默认收起）+ `P/D/FF` | 4 |
| 供电与温度 | `POWR.Vcc`、`MCU.MVolt`、`MCU.MTemp`、`BAT.Temp` | 4 |
| EKF 位置与速度 | `XKF1.PN/PE/PD`、`XKF1.VN/VE/VD` | 6 |
| 遥控输入 | `RCIN.C1..C4`（固定 1000–2000 µs） | 4 |

**地图页：右上角「轨迹差异」面板**（在轨迹勾选栏正下方，勾选变化即时刷新）

- 逐对（单航线）与总体差异百分比，例：`POS ↔ GPS#0 0.0033%`、`总体（3 对）0.0037%`；
- 口径写在面板上，不只放 tooltip：
  - 距离 = 同一时刻**水平** ENU 距离（共同原点重投影，不用地图像素/GCJ-02 坐标）；
  - 对齐 = 以采样**较稀**的一条为时间基准、另一条线性插值，空档 >2 s 或位置跳变处丢弃样本；
  - 单航线% = 均值偏差 ÷ 重叠段两条轨迹里程的平均值；总体% = Σ均值偏差 ÷ Σ平均里程（按里程加权）；
  - **不含高程**（POS/GPS 的 RelHomeAlt/AMSL 与 XKF1/VISP 的 EKF 原点基准不同）；
  - 重叠段里程 <100 m、无时间重叠、坐标系不同（有/无经纬度）都显示“—”并计入说明，不进总体。

### 验证结果

- 全量回归：**1053 passed / 1 skipped / 23 subtests**（含多轨迹勾选、播放头跟随、缩放、左栏、切页）。
- 打包版自检（103.6 MB 真机日志 `2026-10-29 14-18-00.bin`）：退出码 **0**，加载 **1.59 s**；
  71 类报文 / 2 619 459 条 / 1331 参数 / 643 事件 / 32 告警；八项冒烟全通过（含缓存清理、MAVFTP、U 盘护栏）。
- 地图页实测（同一条日志，三条轨迹，重叠 3988 s）：
  `POS↔GPS#0` 均值 0.79 m / P95 1.19 m / 最大 1.43 m / 基准里程 24.09 km → **0.0033%**；
  `POS↔XKF1` 0.83 m / 1.39 m → **0.0034%**；`GPS#0↔XKF1` 1.04 m / 2.39 m → **0.0043%**；
  总体 **0.0037%**。
- 安装版：静默安装到临时目录 → 363 个文件、exe sha256 与绿色包一致 → 自检退出码 0（1.87 s）→ 卸载后目录清空。
- 包内核对：`apmlog.core.trackdiff`、`apmlog.ui.trackdiffpanel` 在 PYZ 中；预设 id 12 个齐全；版本常量 `0.1.2`。

### 产物

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `APMLogViewer-0.1.2-win64-setup.exe` | 45,110,170 B | `71cc34fa94bc8fad19381fd7c35f27107ee30c80a5c0a9597adec969e4829869` |
| `APMLogViewer-0.1.2-win64.zip` | 65,131,447 B | `d7d4080f3813abbef85177c2f3d92dfc30c40b14d12027c82a971888d1ed74e3` |

同名 `.sha256` 用于校验。绿色包解压即用；安装包为**当前用户**安装（无需管理员），
沿用同一 `AppId`，可直接覆盖升级。

构建提交：`FlightTune/APMLogViewer@apmlog-viewer = d8ac7c9`、`APMLogViewer-repo@main = 398ea99`；
本次 tag 另含更新日志与发行说明。

### Ubuntu 24.04（.deb）

资产 `APMLogViewer-0.1.2-amd64.deb`（63,196,744 B，sha256 `2f37f512032cecb61676e92892fde65879d7b68fca7e4084711423bcd0e374b8`）
内置 **Python 3.12 + Qt 6.12**，不需要系统 Python 包。

```bash
sudo apt install ./APMLogViewer-0.1.2-amd64.deb   # 自动补齐依赖
# 或：sudo dpkg -i ./APMLogViewer-0.1.2-amd64.deb && sudo apt -f install
apmlogviewer [日志.BIN]                           # 启动（应用菜单同名）
sudo apt remove apmlogviewer                      # 卸载；purge 连包内文件一起清
```

运行时依赖：`libc6 (>= 2.35)`、`libgl1`、`libegl1`、`libxkbcommon0`、`libxkbcommon-x11-0`、
`libxcb1` 及 `libxcb-cursor0 / icccm4 / image0 / keysyms1 / randr0 / render-util0 / shape0 /
xinerama0 / xkb1`、`libxi6`、`libdbus-1-3`、`libglib2.0-0t64`、`libfontconfig1`、`libfreetype6`；
建议装 `fonts-noto-cjk`（否则中文显示为方框）。

已知限制：包未签名；`libxcb-cursor0` 等 xcb 库由 apt 自动补齐（直接 `dpkg -i` 后需 `apt -f install`）；
串口下载需把用户加入 `dialout` 组；Wayland 下若窗口异常用 `QT_QPA_PLATFORM=xcb apmlogviewer`。
构建脚本与完整说明见资产 `apmlogviewer-linux.spec`、`build_deb.sh`、`UBUNTU.md`。

### 已知限制（与 0.1.1 相同口径）

- 轨迹差异只在**同一份日志内**比较；跨日志没有共同时间基准，不做对齐；
- 偏差是两条轨迹之间的**一致性**估计，不是相对真值的误差；
- `MAV_CMD_DO_SET_MODE`（246 / param1=5，进 U 盘模式）的**真机实际发送**仍未做（会重启进 MSD、断链），
  界面上只保留 armed 拦截与确认框；0.1.1 起如实标注。
- 安装包未做代码签名，Windows SmartScreen 可能提示"未知发布者"。
