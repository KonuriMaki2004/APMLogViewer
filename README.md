# APMLogViewer

ArduCopter 飞行日志（DataFlash `.BIN`）分析工具。面向 **Copter 4.6.x / 4.7.x**版本固件，
具体功能为运行轨迹绘制、曲线绘制、告警信息、参数查询、下载日志。

当前版本 **0.1.3**（2026-11-10）。

## 功能

**轨迹地图**
- 地图显示（OSM / ESRI / 高德，高德为 GCJ-02 纠偏），本地缓存
- 多轨迹叠加：EKF 融合位置 `POS`、原始 GPS、EKF 状态、视觉位置 `VISP`，
- 无 GPS 日志自动退回本地 ENU 显示
- 轨迹播放：随播放头逐点绘制，「全部轨迹显示」/「地图跟随」
- 轨迹差异：勾选多条轨迹时，右上角按「单航线（逐对）」与「总体」给偏差百分比，
  面板写明口径与基准（同期水平 ENU、以采样稀的一条为时间基准、不含高程）
- 左下角罗盘 / 姿态球 / 高度计 + 电压电流读数

**曲线**
- 单道多曲线叠加（每次叠加一条独立胶囊图例），空通道自动灰显
- Y 轴 1%–99% 稳健取值 + 15% 余量，双击 Y 轴恢复全量程
- 工具栏「叠加绘制」：把树里选中的通道变成新道，或叠到已有道上
- 12 组曲目预设（振动 / 姿态 / 电池 / 位置 / 罗盘 / IMU / PID / 供电 / EKF / 遥控 …），
  视图记忆持久化

**判断与告警**
- 振动：分级阈值（默认警告 30 m/s²），曲线徽标 + 时间线色带 + 地图
- 电池：**按单芯电压判定**，电芯数自动识别（`BATT_ARM_VOLT`、电压峰值、日志元数据）
  也可手动设置；默认 3.3 V 警告 / 3.1 V 严重
- 剩余电量百分比（RemPct）**不参与判定**——电压推出来的估计值，用它判据等于二次推导

**时间线**
- 飞行模式色带：同一模式在所有时间段同色，默认色阶按操作难度从青绿到红、
  三级明度循环；色块可在设置里逐个改、即时生效、可一键还原
- 相邻两色过近时自动加粗分段描边

**参数**
- 参数表浏览 / 搜索 / 导出，支持全量导出与「只导改动」（与出厂默认值比对）

**飞控连接（只读）**
- USB 串口 / TCP / UDP 连接，扫描日志列表、批量下载
- 只发 `HEARTBEAT` / `PING` / `LOG_REQUEST_*` / `PARAM_REQUEST_READ`；
  **写操作路径已删除**（不是隐藏），不会 `PARAM_SET` 或 `COMMAND_LONG`

## 安装（Ubuntu 24.04）

Ubuntu 24.04：`sudo apt install ./APMLogViewer-0.1.2-amd64.deb`（[下载 .deb](https://github.com/chuanqinhou/FlightTune/releases/download/apmlog-viewer-v0.1.2/APMLogViewer-0.1.2-amd64.deb)，内置 Python/Qt，无需系统 Python 包）

## 环境

Python 3.14（实测 3.14.7），依赖见 `requirements.txt`：

```
PySide6-Essentials 6.12.0
pyqtgraph          0.14.0
pymavlink          2.4.50
pyserial           3.5
numpy              2.5.4
duckdb             1.5.6
PyYAML             6.0.3
```

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## 运行

```bash
.venv/Scripts/python.exe run_gui.py                 # 空窗口
.venv/Scripts/python.exe run_gui.py <日志.BIN>       # 直接打开一个日志
.venv/Scripts/python.exe -m apmlog.cli <日志.BIN>    # 命令行看摘要
```

## 打包

```bash
.venv/Scripts/python.exe packaging/build.py build    # PyInstaller onedir → dist/APMLogViewer/
.venv/Scripts/python.exe packaging/build.py run --log <日志.BIN>   # 冻结包自检并出 PNG
```

## 测试

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests -q
```

全部组件都要求能在 `offscreen` 平台下构造并 `grab()` 成 PNG（CI 无显示器）。

## 目录

```
apmlog/
  core/       DataFlash 解析（reader）、缓存（store）、数据模型、告警引擎、事件解码
  link/       飞控连接（只读）、日志下载、审计
  ui/         地图 / 曲线 / 时间线 / 仪表 / 设置 / 各种对话框
  batch.py    批处理与评分（独立 CLI，不打包进 exe）
config/       默认阈值（alerts.yaml）
data/         报文/模式码本（含生成用的上游源码片段与 MANIFEST）+ knowledge/ 事件说明知识库
docs/         开发契约 CONTRACT.md（累计设计决策与踩过的坑）、CHANGELOG.md（按改动分节的变更日志）
packaging/    PyInstaller 打包脚本与 spec
tests/        单元测试（约 950 个用例）
tools/        一次性核查脚本
```

## 说明

- 只支持 DataFlash `.BIN` 格式日志；`.tlog` / `.rlog` 暂不支持（等后续更新）
- 设置持久化在 `%LOCALAPPDATA%\APMLogViewer\settings.json`，
  视图记忆在 `views.json`，解析缓存同目录
- 本工程按「读日志不改日志、连飞控只读」设计，离线优先
