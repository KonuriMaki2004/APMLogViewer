# APMLogViewer 工程契约 v1（所有开发者与子智能体必读，不得自行更改公共 API）

## 0. 环境与硬性约束

- 工程根：`C:\Users\MAKI\.sclaw\projects\8261593f-5553-491d-a28b-b1f8853d9229\workspace\apmlog`
- **必须用工程虚拟环境**：`.\.venv\Scripts\python.exe`（Python 3.14.7）
  已安装：PySide6-Essentials 6.12.0、pyqtgraph 0.14.0、numpy 2.5.4、pymavlink 2.4.50、PyYAML、duckdb 1.5.6
- 禁止新增第三方依赖（需要先在回报里提出）。禁止 `python`（系统解释器）、禁止裸 `python.exe`（会挂 stdin）。
- 禁止写文件到用户日志目录；缓存统一写 `%LOCALAPPDATA%\APMLogViewer\cache`（可用 `APMLOG_CACHE` 覆盖）。
- 所有 Qt 组件必须能在 `QT_QPA_PLATFORM=offscreen` 下构造并 `grab()` 成 PNG。
- 代码风格：`from __future__ import annotations`、类型标注、中文注释、`logging`（不要用 print 调试）。
- **文件所有权**：只准改你被分配的文件。要改别人的接口，先在回报里说明。

## 1. 真实数据事实（已用真机日志核对，不要凭经验推翻）

- 固件：`ArduCopter V4.7.0 (1511f271)` / `ChibiOS: 4f34e217` / `Pixhawk6X` / `Frame: QUAD/H` / SBUS / `RCOut: PWM:1-16`；目标版本区间 **Copter 4.6.x 与 4.7.x**。
- 样例日志：
  - 炸机：`C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000165.BIN`、`00000166.BIN`
  - 正常：同目录 `00000167~170.BIN`
  - 带 GPS：`C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\`（82 个 `.bin`，共 994MB，最大 31MB；另有 589 个 tlog/rlog —— **v1 不处理 tlog/rlog**）
- **165/166 完全没有 GPS/GPS2/POS/ORGN/GPA**，位置只有 `VISP`(PX,PY,PZ,PErr,AErr) 与 `XKF1`(PN,PE,PD)。→ 这两类日志只能画本地轨迹，真实地图仅在存在经纬度时可用。
- 电机数据源是 `MOTB`(LiftMax,BatVolt,ThLimit,ThrAvMx,ThrOut,FailFlags) + `RCOU`(C1..C14) + `RCO2`(C15..C18)，**没有 `ESC`**。`FTN1/SIDS/BCL/FFT/ESC` 在这些日志里全部缺失，代码不得依赖它们存在。
- 震动 `VIBE`(TimeUS,IMU,VibeX,VibeY,VibeZ,Clip)；`IMU`(GyrX..AccZ, GHz/AHz) 为高频。
- 电量 `BAT`(TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH)。`Volt` 是整包还是单体**尚未确认**，由内核用同日志 `BATT_*` 参数反推后写进 `meta['bat_semantics']`。
- **多实例是同一类型**：`IMU/VIBE/BARO/GPS/MAG/XKF*` 用 `I`/`Inst`/`C` 区分子实例（如 BARO 有 I=0 和 I=1），**不存在 `IMU2/MAG2` 独立类型**。
- 时间字段只认 `TimeUS`（µs，自 boot 起算，首帧可能已是 200s+）。`VISP.RTimeUS` 量级异常（1.6e15），**禁止用作时间轴**。
- 炸机证据形态（必须支持）：`ERR{Subsys:12,ECode:1}` 与**同刻** `MSG{'Crash: Disarming: AngErr=171>30, Accel=0.0<3.0'}` + `ARM{ArmState:0, Method:12}` 成组出现；165 末帧 `Roll≈178.75`。
- 4.7 增值报文：`FILE`（内嵌 @SYS 文件 dump，如 `@SYS/uarts.txt`）、`PM`(CPU)、`STAK`(栈)、`POWR`(供电轨)、`HEAT`、`CANS/CAND`、`MAV/MAVC/UART`(链路丢包)、`DSF`、`LDET`、`XKQ/XKV1/XKV2/XKY0/XKY1/XKFS`、`UNIT`/`FMTU`、`PARM` 自带 `Default` 字段。
- 解析性能基线：pymavlink DFReader 约 1.7–3.9 MB/s（15.3MB ≈ 3.9s）。

## 2. 数据模型（`apmlog/core/model.py`，内核已实现，UI 只依赖它 + numpy）

```python
ChannelKey(type:str, field:str, inst:int|None)   # frozen, 可当 dict key
    .key   -> "VIBE.VibeX" / "VIBE#1.VibeX"
    .label -> "VIBE.VibeX" / "VIBE[1].VibeX"
    ChannelKey.parse(s)                            # 反解
Series(key, t:np.f64[], v:np.f64[], unit:str, label:str)     # t 单位=秒(boot)，单调不减
Envelope(key, t, vmin, vmax, vmean, count)                    # 分桶包络，t 为桶中心
ChannelInfo(key, dtype, count, t0, t1, vmin, vmax, unit, coltype)
EventRec(t, kind, code:int|None, ecode:int|None, text, decoded, severity, extra:dict)
    kind ∈ ERR EV MSG ARM MODE CMD FILE THM ALERT
    severity ∈ info hint warn error critical
TrackResult(kind:'geodetic'|'local', t, lat, lon, alt, e, n, u, speed, src:int[], src_names:list[str], origin, home, distance, max_speed, climb_min, climb_max)
    local 坐标统一为 ENU（米，e/n/u），geodetic 为 WGS-84；缺侧填 nan
AlertHit(rule, title, severity, t0, t1, value, threshold, unit, channel:ChannelKey|None,
         message, evidence:list[ChannelKey], source)          # source ∈ param default-ref user
LogStats(...)   # 见 model.py，含 firmware/frame/board/duration/mode_seq/takeoff_t/max_alt/...
```

## 3. 内核公开 API（`apmlog/core/store.py`，由主开发实现，其他人**只读**）

```python
LogStore.open(path, cache_dir=None, progress:Callable[[float,str],None]|None=None, force=False) -> LogStore
LogStore.path / .log_id / .meta:dict / .stats:LogStats
LogStore.types() -> list[str]
LogStore.channel_infos(type_prefix=None) -> list[ChannelInfo]
LogStore.series(key:ChannelKey|str, t0=None, t1=None, max_points=None) -> Series
LogStore.series_many(keys, t0=None, t1=None, max_points=None) -> dict[ChannelKey, Series]
LogStore.envelope(key, t0=None, t1=None, buckets=2000) -> Envelope
LogStore.events(kinds=None, severity_min=None, t0=None, t1=None) -> list[EventRec]
LogStore.params() -> dict[str, tuple[float, float|None]]      # name -> (value, default)
LogStore.param(name, default=None) -> float|None
LogStore.track() -> TrackResult
LogStore.alerts(config_path=None) -> list[AlertHit]
LogStore.modes() -> list[tuple[float,float,str]]              # (t0,t1,模式名)
LogStore.close()
```

## 4. 缓存格式（内核）

`%LOCALAPPDATA%\APMLogViewer\cache\<sha1(路径+size+mtime)[:16]>\`
`index.json`（元数据/类型/列统计/分段）、`<TYPE>.npz`（**不压缩**，含 `t`、`inst`、各数值列，支持 `mmap_mode='r'`）、`events.json`、`params.json`、`track.npz`。
缓存失效：路径+size+mtime 任一变化。

## 5. 各工作流负责文件与验收（互不重叠）

| 代号 | 负责文件 | 验收 |
|---|---|---|
| 内核 | `apmlog/core/{model,reader,store,events,alerts,codebooks}.py`、`apmlog/cli.py`、`config/alerts.yaml` | 8 份真日志 stats/alerts 正确；165/166 命中 crash |
| H link | `apmlog/link/{endpoint,download,fakes}.py`、`apmlog/ui/linkpanel.py`、`tests/test_link_*.py` | 离线合成 LOG_ENTRY/LOG_DATA + 本地 TCP/UDP 回环自测通过 |
| B 地图 | `apmlog/ui/trackmap.py` | 真实 GPS 航迹 offscreen 出 PNG；断网不报错；GCJ-02 换算有单测 |
| C 时序 | `apmlog/ui/seriesplot.py` | 2M 点抽稀交互；游标/框选信号正确；出 PNG |
| D 面板 | `apmlog/ui/{timeline,alertpanel,eventpanel}.py` | 模式色带+事件刻度+告警表；点击发 seek 信号；出 PNG |
| F 码表 | `data/codebooks/copter_4x.json`、`docs/CODEBOOK_SOURCES.md`、`tools/validate_codebooks.py` | 每条映射有源码出处；未映射码显式列出 |

## 6. 交付要求（每个子智能体）

1. 只创建/修改你负责的文件；不要动 `app.py`（主窗由主开发集成时写）。
2. 提供 `if __name__ == "__main__":` 自测入口，能在 `QT_QPA_PLATFORM=offscreen` 下跑通并把 PNG 存到 `selftest/<你的模块名>.png`。
3. 自测命令与真实输出（含耗时、点数、错误）写进回报；**失败也要如实回报**。
4. 组件不得反向依赖主窗，不得读取全局状态；一切输入走参数。
5. 组件必须能处理"数据缺失"：通道不存在/全 nan/0 点 → 显示占位提示而不是异常。

## 7. 对飞控严格只读（曾短暂放宽，已收回）

本工程对飞控**严格只读**，允许发送的只有：`HEARTBEAT`、`PING`、`LOG_REQUEST_LIST`、
`LOG_REQUEST_DATA`、`LOG_REQUEST_END`、`PARAM_REQUEST_READ`（日志写入检测的只读探测）。

2026-10-21 曾按用户需求实现过"检测到未开启日志写入 → 弹窗推滑块 → 下发 `PARAM_SET`
开启"，随后用户与同事商定**恢复只读原则**，该写路径已整体删除（不是禁用、不是隐藏）：

| 已删除 | 位置 |
|---|---|
| `write_param()` / `apply_log_write_enable()` / `WriteResult` | `apmlog/link/params.py` |
| `AuditTransport(allow_param_set=...)` / `TxAudit.controlled_writes` / `is_controlled()` | `apmlog/link/audit.py` |
| `param_write_done` 信号、`_do_enable_log()`、`enable_log` 命令、`_on_param_write()` | `apmlog/ui/linkpanel.py` |
| `SlideSwitch` 滑块控件、`enableRequested` 信号、`report()` 回填 | `apmlog/ui/_switch.py`（文件已删）、`apmlog/ui/logconfig.py` |

现在的行为：连上飞控后自动只读检测 `LOG_BACKEND_TYPE` / `LOG_BITMASK`；判定未开启时弹
`LogWriteDialog`，只展示**判定依据**与**用户自己动手的开启步骤**（Mission Planner /
QGC / MAVProxy 三条路径 + SD 卡与存储空间检查），窗口内没有任何可触发写入的控件。

不得回退的约束：

- `PARAM_SET` 在 `FORBIDDEN_PATTERNS` 里**无条件**计为违规；`is_read_only()` 一旦看到它就
  返回 False。`tests/test_link_params.py` 有防回退守卫：断言 `params` 模块不存在写函数、
  源码里不出现 `param_set_send`，并人为注入一条 `PARAM_SET` 验证审计确实报警。
- `LogWriteConfig.suggest` 是**给人看的建议值**（元组 `(参数名, 当前值, 建议值)`），
  不是待执行清单；`enabled` / `unknown` 时恒为空。
- 建议值 `LOG_BACKEND_TYPE=1`、`LOG_BITMASK=180222` 取自用户真机日志的 PARAM 块实测，
  不得改成凭记忆写的魔数。
- 检测判不准（参数读不到）时报 `unknown` 且**不弹窗、不建议写入**，避免误导用户改参数。

新增任何写操作（改参数、清日志、校准、重启、切模式）都不在 v1 范围内，需重新评估。

## 8. 目录扫描（不得回到 UI 线程）

历史 bug：`_LogListDock.scan()` 曾在 UI 线程里 `Path.rglob("*")` + 逐个 `stat()`，用户把
整个磁盘当目录选进来即"未响应"。现在的硬约束：

- 扫描内核在 `apmlog/scan.py`：**零 Qt 依赖、纯函数、可单测**，两阶段
  （`enumerate` 只按名字挑候选 → `stat` 取大小/时间）。
- 必须可取消：`should_cancel()` 在两阶段每一步都检查；取消时**返回已扫到的结果**并置
  `cancelled=True`，绝不抛异常、绝不丢已完成部分。
- 必须有上限：`max_files=5000 / max_dirs=20000 / max_entries=2_000_000`
  （`scandialog.DEFAULT_LIMITS`）。撞上限 → `truncated=True` + 中文原因，结果照常返回。
- 绝不跟随符号链接/junction（`follow_symlinks=False` + `realpath` 去重防环）；跳过
  `$` 开头目录、`System Volume Information`、`WinSxS`、`node_modules`、`.git` 等无价值目录。
- 单目录权限错误、单文件 stat 失败 → 计数后继续，**任何异常都不得冒出 `scan_logs`**。
- UI 侧 `ScanController` 一次只允许一个扫描；进度框的取消按钮位于进度条下方；
  `enumerate` 阶段总数未知，进度条走**忙碌态 + 目录/候选计数**，只有 `stat` 阶段显示
  **真实百分比**（不得为未知总数伪造百分比）。
- 结果必须给出"日志总数 + 按类型数量 + 可打开数量"（`summarize()`）。v1 只支持 `.BIN`，
  其它类型置灰（去掉 `ItemIsEnabled` 标志，`QListWidgetItem` 没有 `setEnabled`）且双击无效。

## 9. 多轨迹（一份日志同时画出所有位置源）

以前 `build_track()` 按 `POS→GPS→XKF1→VISP` **只挑一条**，用户拿 Mission Planner 打开
同一份日志却看到多条线。实测 112 份真机日志：**没有任何一份有第二个 GPS 实例**，
多轨迹的来源是 **POS（EKF 融合位置，约 2× 采样率）与 GPS（原始 GNSS）逐点分叉**——
`2026-10-16 16-59-28.bin` 最大 **76.6 m**、中位 40.6 m，6 份 >30 m。

约定（`SCHEMA_VERSION` 2→3 即因本节）：

- `reader.build_tracks()` 为**每个有效点 ≥5 的位置源**各存一份 `tracks/<key>.npz`
  + `tracks/index.json`；`track.npz` **仍然是主轨迹**，老读路径零改动。
- 主轨迹仍按 `POS→GPS#0→XKF1→VISP` 取第一个可用源。**光标、仪表盘、高度带、fit、
  状态栏读数一律只跟主轨迹**，语义与单轨迹时代完全一致，不得改成"跟最后勾选的那条"。
- `key`/`label` 固定：`POS`=EKF 融合位置 (POS)、`GPS#0`=原始 GNSS (GPS1)、
  `GPS#1`=原始 GNSS (GPS2)、`XKF1`=EKF 状态位置 (XKF1)、`VISP`=视觉位置 (VISP)。
  其它实例 `GPS#N` → `原始 GNSS (GPS{N+1})`。
- **XKF1/VISP 是本地 NED，必须靠 ORGN 才投成经纬度**；没有 ORGN 就不出这条轨迹，
  绝不允许把米当度画（`_xy_for` 对 `kind != "geodetic"` 直接返回空，有测试钉住）。
- GPS 无锁定剔除（NSats≥3 / HDop<99 / 经纬度非 0）与跳变守卫**每条轨迹独立生效**，
  不得因为 POS 干净就放松 GPS——否则两条线会一起被脏数据带偏。
- UI：`TrackMapWidget.set_tracks()`；副轨迹纯色、`zValue=9` 压在主轨迹下面，
  主轨迹保留高度渐变。右侧半透明 `TrackLegendPanel` 勾选，**只有一条轨迹时自动隐藏**。
  勾选组合写进 `views.json` 的 `track_vis`（仅图例可见时记录，避免用空选择覆盖用户记忆）。
- `fit_to_track()` 收拢的是**可见轨迹的并集**，只框主轨迹会把刚勾出来的第二条切出画面。
### 9.1 无 GPS / 无 ORGN 的日志也必须出轨迹（`SCHEMA_VERSION` 3→4）

纯视觉机（如事故复盘用的 `00000165.BIN`）**完全没有 GPS、没有 POS、`ORGN` 只在 FMT 里
声明而实际 0 行**。这类日志没有任何经纬度基准，投到真实地图上是不可能的，但**不代表
没有轨迹**：`XKF1`/`VISP` 的本地 NED(PN,PE,PD) 本来就是同一 EKF 原点下的米坐标，
几条轨迹可以直接叠在本地 ENU 视图里对比形状。

因此：

- `reader` 对这类日志给 `kind="local"` 的**多条**轨迹，`meta.track.kind="local"`，
  不得再判成 `kind="none"`（旧行为会让状态栏显示"位置源 无(none)"、地图空白）。
- 地图本地模式映射与主轨迹**完全同一套**：`x = 东米`、`y = -北米`。
- 副轨迹按模式过滤：地理模式只画 geodetic，本地模式只画 local。坐标系不同硬叠
  就是"把米当度"，宁可**不画**，并在勾选栏里把那条置灰 + 说明原因（禁止出现
  "能勾但什么都不发生"的控件，与 §11 的 UI 铁律一致）。
- 本地模式下场景矩形要撑到**可见轨迹的并集**（`QGraphicsView` 默认 clipToSceneRect，
  不撑开副轨迹会被剪掉）。

**教训（记进铁律）**：缓存 bump 的判据是"语义变了"，不是"文件格式变了"。本次事故的
成因是开发中间快照（19:19）已经把 schema 写成 3、内容却是"无轨迹"，最终版同样是 3，
而 `is_fresh()` 只能比 schema+size+mtime+路径，源文件时间戳不会因我们的代码变化而动
→ 脏缓存永久有效。**同一 schema 号不允许承载两种轨迹语义。**


## 10. 状态栏只有一处显示（左侧）

底部状态文本一律走 `QStatusBar.showMessage()`（左侧）。**不得**再挂右侧
`addPermanentWidget(QLabel)`——同一句话左右各一份是用户明确否掉的，
`status_label` 已删除，出现即为回归。临时消息（`temporary=True`，4s）超时后由
`_on_status_message()` 把常驻文本恢复回来，避免临时提示把状态顶掉后留白。

## 11. 地图缩放与读取遮罩

### 11.1 滚轮方向（本地模式曾双向都放大）

`_MapView.wheelEvent` 把上滚映射成 `zoom_by(+1)`、下滚 `zoom_by(-1)`，方向语义**只在这里
定义一次**。`zoom_by` 里两种模式必须各用各的基准：

- 地理模式：基准是**缩放级** `zoom()`（`REF_Z + log2(k)`），按级 ±1 再夹到源 min/max。
- 本地模式（无卫星图）：基准必须是视图变换的**实际 ppm** `_k()`，**不许用 `zoom()`**——
  那是缩放级（≈27），拿 ppm 除以它算比例会让上下滚都 >1，表现就是
  “没有卫星图时怎么滚都是放大”。算完要**回写 `_local_ppm`**，否则下一滚仍拿旧值，步长不是 2×。
- `LOCAL_PPM_MAX` 从 400 px/m 放宽到 1e6：本地坐标就是米，没有天然上限；纯视觉日志
  位移常常只有一米量级，fit 完就贴着 400，上滚只能再放 6% 看起来又像没反应。

### 11.2 读取遮罩 `apmlog/ui/loading.py`

打开日志时右侧内容区（地图/曲线那一片）显示 **主题底色 + 「正在读取日志」+ 跟随解析
百分比的进度条**，解析结束（成功或失败）恢复。

- 底色走 `QPalette.Window` + `autoFillBackground` + 自绘 `fillRect`：**必须不透明**，
  半透会透出上一份日志没清掉的曲线残影；**不许硬编码颜色**（换主题会露馅）。
- 只盖标签页内容区，**标签条露在外面**（几何由 `eventFilter` 跟 host resize 同步），
  用户加载途中仍能看到自己在哪个页签。
- **延迟 `SHOW_DELAY_MS=180` 才显示**：命中缓存时解析只要 0.01s，一调用就显示会白闪。
  快加载全程不可见，这是 `test_fast_load_never_flickers` 钉住的行为。
- 进度真源是 `reader.build()` 的 `progress(fraction, message)`（0→0.97 按字节偏移，
  完成时 1.0）。`_on_load_progress` 同时喂遮罩和状态栏：遮罩可能还没到显示延迟，
  状态栏是唯一一直可见的进度出口。
- `end()` 必须在 `_on_loaded` **填完界面之后**、以及 `_on_failed` 开头各调一次；
  早收会看见“空图表闪一下再填数据”，漏收就是遮罩转到天荒地老。

## 12. 参数导出（`apmlog/paramexport.py`，纯逻辑无 Qt）

参数页「导出参数…」可导 **完整参数** 或 **只导与出厂默认不同的**（另给一档
「只导当前筛选结果」），格式 `.param`（`NAME,VALUE`，Mission Planner / QGC 可读）或
`.csv`（带出厂默认与差异列）。

- **数据来源必须写进文件头**：记录值与出厂默认都取自日志自身的 `PARM` 块
  （`Value` / `Default` 两列，`Default` 要较新固件才写），**不是从飞控实时读的**，
  而且是那次飞行时的快照。导出用途是对比与存档，文件头明确提醒不要盲目批量写回。
- **判定"是否改动"只有一个真源**：`ParamRow.changed`（容差 `TOL=1e-9`）。
  `_ParamTable._is_changed` 必须委托它——两边各写一套容差就会出现
  "表格里显示相同、却被导进差异文件"的自相矛盾（有测试钉住）。
- **判不了的不算"没改动"**：缺 `Default`、或记录值非有限的行，归 `no_default` /
  `no_value` 单列统计并在界面与文件头里说出来，**不进差异导出**（保守，不吓人）。
- **`.param` 数值不许用 `%g`**：那会把 1376550 写成 `1.37655e+06`，MP/QGC 解析器不认
  科学计数法。规则是整数写整数、其余 6 位小数（`fmt_value`）。
- **非有限值直接不写那一行**：写 `NaN` 进去会让对方整份文件解析失败，宁可少一条。
- 空结果**不弹保存框**：否则用户选了路径、拿到一个只有注释头的空文件还以为导出成功。
- 用户手打文件名不带后缀时**补上对应扩展名**，不许静默按另一种格式写。

## 13. 左侧日志列表筛选

真实日志根（Mission Planner）实测 **1461 个文件里只有 106 个 `.BIN` 能打开**，
其余 `.rlog/.tlog/.log` 全部列出来只会显示"暂不支持"，把要看的那条淹掉。因此列表
有两个叠加的筛选条件：

- **「只看 .BIN」默认勾上**（设置键 `list_only_bin`，默认 `True`，切换即持久化）。
  勾上时不可打开的直接 `setHidden`，**条目仍然建出来**——这样取消勾选是瞬时的，
  不需要重扫磁盘。取消后不可打开的照旧去掉 `ItemIsEnabled` 置灰、无 `UserRole` 路径、
  双击无反应（§8 的规矩不变）。
- **文件名子串筛选**（不区分大小写，只筛已扫到的名字，不重新扫盘）。

必须遵守：

- **汇总行要同时说清"扫到什么"和"现在给你看多少"**：`共 1461 个日志（…），可打开 106 个`
  加上一行 `当前显示 106 个，已筛选掉 1355 个（不可打开的）`。只报一个数会让人以为
  扫描漏了。
- 筛选后**光标必须落在可见行上**。回归事故：以前固定 `setCurrentRow(0)`，默认筛选开启
  时第 0 行恰恰是被隐藏的 `.rlog`，看起来像"什么都没选中"。
- 筛选状态变化要顺带修掉基线文本（`_sum_base`），否则扫描失败后再勾一下筛选，
  会把上一次扫描的旧汇总顶回来。
- 一千多条逐项 `setHidden` 要 `setUpdatesEnabled(False)` 包住，否则逐次重绘会闪。

## 14. 播放拖影与电池显示

### 14.1 屏幕恒定尺寸图元：禁用 `ItemIgnoresTransformations`

用户报「开倍速播放时地图上有拖影」。根因不是播放引擎，是**脏区算小了**：
游标/徽标/圆点原来用 `ItemIgnoresTransformations` 保持像素尺寸，但 Qt 算脏区时
把「场景坐标的 `boundingRect`」乘上视图变换，而 `paint()` 是按固定像素画的。
缩到 z=17（0.5 px/m）时脏区只有 16 px、实际画 32 px → 每次移动留 16 px 残影；
倍速下一帧跳几十像素，残影不再互相覆盖，就串成一串箭头。

约定：

- 这类图元统一继承 `_ScreenItem`，用 **`setScale(1/k)`** 保持屏幕尺寸，**不许**再用
  `ItemIgnoresTransformations`（`test_no_item_relies_on_ignore_transformations` 反向守卫）。
- **每一条改变视图变换的路径都必须重算**：`_set_zoom`、`_set_local_ppm`、`zoom_by`
  都调 `_sync_screen_items()`；只补其中一两条，图元就保留旧比例，脏区差一个 k 倍，
  拖影照旧（本次就是先漏了 `_set_zoom`/`zoom_by` 才没修好）。
- 不变式（有测试钉住）：`boundingRect` 经 `deviceTransform` 映射后的设备尺寸必须
  在所有缩放下恒等于设计像素尺寸（游标 32×32）。这条比值一旦不为 1，就是拖影。
- 不要用「播放时切 `FullViewportUpdate`」来代替上面的几何修正：实测整视口重绘
  在 z≈19 的瓦片图上代价高一个量级，20fps 会明显卡顿，而几何修法是零成本的。

### 14.2 电池：只显示原始量，不在图上标"电量"

用户 2026-10-24 决定：**地图上不再出现"剩余电量偏低"**（红带 + 徽标都去掉），
理由是 `RemPct` 本身是按**电压**推出来的估计值，把它画成地理区域等于把估计当事实。

- `MAP_HIDDEN_RULES = ("batt_remain_crit", "batt_remain_warn")`，经 `map_regions()`
  过滤后才交给 `map.set_regions`。**告警页与时间线保留**这两条，不算删功能。
- 仪表左下角新增一行**原始测量量**：`电压 55.18 V　电流 27.0 A`，与姿态读数同面板、
  同一套 `readable()` 墨色；任一项缺失就只显示另一项，都没有就整行隐藏（不留空占位）。
- **报文名是 `BAT`**（`Volt/Curr/RemPct…`），`BATT_` 只是**参数**前缀。
  写成 `BATT.Volt` 会直接 `KeyError`，而 `_feed_battery` 的容错会让它**静默变成空读数**
  ——实测踩过一次。取数顺序 `BAT.*` → `BATT.*`（兼容），两条都没有才放弃。
- 采样点不插值、超过 3 s 无新数据就不显示：电压电流是实测点，插值出来反而像真的。

## 15. 大日志下的交互性能

实测样本：`2026-10-24 14-15-40.bin`，69 MB / 170 万条报文 / 4 条轨迹 / 11.3 万段折线。
用户报「移动地图非常卡，整个软件也跟着卡」。查下来是两个独立原因，都不是播放引擎。

### 15.1 折线必须抽稀 + 切块

- **抽稀**：`THIN_PX = 1.6`，按屏幕像素（`step = THIN_PX / k`）贪心保序抽点。
  实测与不抽稀相比只差 **0.46% 像素**，肉眼无差；但段数 113,000 → 62,678。
- **切块**：`CHUNK_SEGMENTS = 400`，每条 `QPainterPath` 不超过 400 段。
  **这是关键**：一刀不切的单条大 path，Qt 无法按包围盒裁掉视口外的段，每帧全量描边。
  实测副轨迹（9.4 万段一条）单独占掉一帧的 500 ms；藏掉它整屏从 564 ms 降到 61 ms。
- 抽稀依赖当前缩放，所以**放大超过 1.6 倍后要重建**（防抖 400 ms，等缩放停下来再做，
  否则滚轮连滚会一顿一顿）；**缩小方向永不重建**（点只会更密）。
- `set_track` 必须**先 fit 再画**：先画后 fit 会让 `_built_k` 立刻过期，载入 400 ms 后
  自己又重建一次（多一次卡顿），`test_load_leaves_no_pending_rebuild` 钉住这条。
- `_rescale_pens` / `_sync_scale` 只在**值真的变了**才 `setPen`/`setScale`：
  这两个调用都会让图元变脏重绘，每次滚动几百次无效调用白浪费。

实测：整屏重绘 **615 → 141 ms**，拖地图单帧 **20 → 2.0 ms**（中位）。

### 15.2 瓦片请求不许在 UI 线程连发

`QNetworkAccessManager.get()` 是**同步**调用（本机实测 16–19 ms/次，首次还要代理/DNS 预热）。
老 `_pump()` 在一个 `while` 里连发到并发上限（6 个）→ 一次滚动就在 UI 线程吞掉 100 ms 以上，
**实测事件循环最大停顿 608 ms**，这才是"整个软件都卡"的直接原因。

约定：

- `_pump()` 只**排一个 0 ms 定时器**，`_pump_step()` 每轮事件循环**只发一个请求**，
  发完再 `_pump()` 排下一轮。实测确认 Qt 的 `processEvents()` 一轮只跑一个（有测试钉住）。
- 并发上限（`max_concurrent`，默认 6）照旧由 `_inflight` 控制，吞吐不受影响。
- `_pump_pending` 防重复排队；`cancel_all()` 清它；`shutdown()` 后迟到的定时器
  必须安全返回（不能炸、不能再发请求）。
- 改这块后事件循环最大停顿 608 → ~200 ms，且拖地图本身稳定在 2 ms/帧
  （剩余尖峰来自新瓦片的 `get()` 与磁盘解码，随缓存命中会消失）。

## 16. 告警阈值可配置（设置页）

用户 2026-10-24：「设置页面加一些功能，比如振动值超标（设定超过多少为警告，默认为30）」。

### 16.1 阈值分层与真源

- **默认值**：振动警告 `rms_warn = 30 m/s²`、严重 `rms_crit = 60 m/s²`（代码 `alerts.DEFAULTS`
  与 `config/alerts.yaml` **必须一致**，`test_yaml_agrees_with_code_defaults` 钉住）。
  依据是 ArduPilot 社区经验（X/Y < 30、Z < 60，机型差异大），不是我们拍的。
- **三层优先级**：`alerts.DEFAULTS` → `config/alerts.yaml` → **界面设置覆盖**
  （`Settings` 的 `vibe_warn` / `vibe_crit`，经 `evaluate(..., overrides=)` 深合并）。
  yaml 仍是命令行/无界面场景的真源；界面只覆盖用户能改的那两项。
- **严重必须 ≥ 警告**：两处兜底——引擎里 `crit = max(crit, warn)`，对话框里调高警告时
  自动把严重抬到同一值。调反了会让 `mask_w` 恒空（全被 `mask_c` 吃掉），
  变成"没到警告的振动被报成严重"。
- 阈值改动**立即重算**：`_reload_alerts()` 只刷新与告警有关的四处
  （告警页 / 时间线色带 / 地图区域 / 曲线区域 + 状态栏计数），
  **禁止调 `_apply()`**——那会把所有泳道重新取数（实测 ~1 s）。实测重算 ~45 ms。
- 数字框连续改动**防抖 250 ms**，不然拖一次要算十几次。

### 16.2 设置对话框必须能长大

所有分组已移进 `QScrollArea`（底部"恢复默认/关闭"固定在滚动区外），
**横向滚动条永久关闭**（长标签撑出横向条很难看），纵向按需。
以后加设置项直接往 `inner` 里加分组即可，不要再往 `self` 的根布局塞。

### 16.3 测试不许依赖产品默认阈值

`test_core_kernel.VibrationAlertTest` 的合成数据是按 15/30 设计的（刺激 18 落 warn、
40 达 critical）。默认一改成 30/60 它就红。这类"验机制"的测试必须**显式钉死自己需要的
阈值**，不要读 `load_config()` 的默认值——否则产品调个默认值就会连带误伤测试。

### 16.4 已知取舍（要主动告知用户）

用他们自己的日志实测 VIBE 峰值：`10-24 10-59-50` X5.3/Y6.0/Z10.4、
`10-21 11-06-02` Z15.3、`10-16 16-59-28` Z28.5。**默认 30 意味着这三份日志都不会报振动**；
旧默认 15 会在后两份上报警。默认值按用户要求取 30，用户觉得太松可在设置里下调。

## 17. 设置页两列 + 电池阈值

### 17.1 布局

设置对话框内容改为**两列**（`QScrollArea` 内一个 `QHBoxLayout` 两个 `QVBoxLayout`），
默认尺寸 **980×720**，竖向可滚、**横向滚动条永久关闭**。
左列放主题与告警阈值，右列放提示/曲线/地图/下载/缓存。
`_group(列布局, 标题)` 直接往列里加；**不要**再往 `self` 的根布局塞分组（会撑破窗口）。

### 17.2 电池阈值（单体 + 整包）

| 设置项 | 默认 | 语义 |
| --- | --- | --- |
| `cell_warn` / `cell_crit` | **0 = 自动** | 单体电压（V/芯）。自动时按 `BATT_VOLT_MIN ÷ 串数` 推导（`source=param`），推不出来才用 yaml 参考值 3.50/3.20 |
| `pack_warn_v` / `pack_crit_v` | **0 = 不启用** | 整包电压（V）。填了才按整包电压比，规则名 `batt_pack_low_{warn,crit}`，与参数/比例的欠压判定**并存** |

- **覆盖层里 0 的键必须省略**（`_alert_overrides` 只放 > 0 的），否则 yaml 的
  3.50/3.20 会被 0 顶成"阈值 0 → 永不报警"。
- **"用户是否显式设过"必须由 `evaluate` 传下来**（`batt_alerts(..., user_keys=)`）。
  yaml 本来就有非零的 cell 参考值，光看"值 > 0"分不清"用户填了 3.5"和"yaml 默认 3.5"，
  一旦误判就会把按 `BATT_VOLT_MIN` 推导的证据链（`source=param`）静默顶掉
  —— 这是实际踩过的回归，`test_core_kernel.BatteryAlertTest` 抓到的。
- **整包量级 + 推不出串数**时，用户设的单体阈值无法换算：必须出一条 `hint`
  （`batt_cell_unscalable`）告诉用户改用整包阈值，**不许静默忽略**。
  实测他们的日志就是这种：`mode=pack`、54.5–57.0 V、`cells_est=None`
  （`BATT_ARM_VOLT=0`、无 `BATT_VOLT_MIN`）→ 单体阈值用不了，所以整包阈值是必需的。
- **欠压"越低越严重"**：`warn ≥ crit`。引擎里两档都夹（`min/max`），
  对话框里调高警告会把严重压回不高于它。调反会让警告区间被严重档吃空。
- 引擎里 `cfg.get("cell_crit", 3.2) or 3.2`：**显式 0 与不写同义**（都是自动）。

## 18. 电芯数（几 S）与单芯电压判定

用户 2026-10-24：「新增电池电芯数，用几 S 表示…电芯数要能自动识别，也支持手动调节，
手动设置优先于自动识别，并清楚显示当前采用的电芯数；自动识别失败时允许直接手动填写。
默认按单电芯电压判断，低于 3.3V 为警告，低于 3.1V 为严重；修改电芯数后，警告和严重判定
要按新的串数换算并即时生效，不要写死串数。」

### 18.1 数据模型

| 项 | 默认 | 含义 |
| --- | --- | --- |
| `cell_count` | **0** | 电芯数（"14S" 记 14）。**0 = 自动识别**；>0 为手动，**手动优先** |
| `cell_warn` | **3.3** | 单芯电压低于此值 → 警告（V/芯） |
| `cell_crit` | **3.1** | 单芯电压低于此值 → 严重（V/芯）；引擎里 `crit = min(crit, warn)` |

`cell_warn/cell_crit` **不再是"0 = 自动"**（那套语义在 §17 只对整包阈值 0=不启用 保留）。
0 现在等价于"用 3.3/3.1"，因为 `cfg.get(k, 3.3) or 3.3` 把 0 当日默认。

### 18.2 自动识别的三级证据（`alerts.resolve_cells`）

按可靠度从高到低，命中即返回，**并把依据写进 `note` 供界面与告警正文引用**：

1. `bat_semantics.cells_est`（单芯量级日志里 ARM÷观测值反推的）→ `自动识别 nS`
2. `BATT_ARM_VOLT ÷ 4.2`（满电整包电压；ArduPilot 建议按满电填）→ 同上
3. `观测最高整包电压 ÷ 4.25 V/芯` 向上取整（4.25 = 锂电单芯上限）→ **`自动估算`**

第 3 级是估算，**必须在文案里带"估算"二字**，并且一定允许手动覆盖。
证据不足（无 ARM 参数、无有效电压）→ `(None, "none", ...)`：
**绝不编一个默认串数**（编出来的 S 会把单芯电压算错，比不报更危险）。

### 18.3 判定路径

- 日志是**单芯量级**（`mode == "cell"`）：`Volt` 直接就是单芯值，**不除串数**；
- 日志是**整包量级**且串数已知：`单芯 = 整包 ÷ cells`；
- 两条都不成立（整包量级 + 串数未知）：退回 §17 的参数/比例路径
  （`source=param`），并额外发 **`batt_cells_unknown`**（hint）提示去手填电芯数。
  **顺序变了**：`BATT_VOLT_MIN` 推导从"默认"降级为"兜底"，这是用户指定的行为变更。

告警正文必须同时给出：单芯最低值、阈值、整包值（整包量级时）、**按几 S 换算**、
依据（`手动设置 6S` / `自动估算 14S（最高整包电压 57.0V ÷ 4.25V/芯 向上取整）`）。
值离阈值很近（<5e-3）时多显示一位小数，避免"3.900 < 3.90"这种自相矛盾的观感。

### 18.4 即时生效

- `_alert_overrides()` 把 `cell_count`（>0 时）**转成 float 放进覆盖层**——手填串数
  不下发等于没填。
- `_apply_setting` 的防抖集合必须含 `cell_count`；只刷新四处告警表面（§16），
  不重跑 `_apply()`。
- 实测（`2026-10-24 10-59-50.bin`，整包 54.5–57.0 V）：自动 14S + 3.95/3.89 → 19 条；
  改成手填 6S 后 **0 条**（单芯变 9 V），状态栏即时显示"单芯 3.95/3.89 V（6S 手动）"。

### 18.5 界面

「设置 → 告警阈值（电池）」第一行是 `电芯数`（0 显示为"自动"，后缀 S，范围 0–24），
紧跟一行只读「当前采用」标签，三态：

- `14S（自动） — 自动估算 14S（…）`（绿色）
- `6S（手动） — 自动识别是 14S，已按手填的算`（绿色）
- `自动识别失败：… → 请把上面「电芯数」填成实际串数（如 14S）…`（琥珀色，引导手填）

标签数据由主窗口 `MainWindow.cells_info()` 提供（对话框自己拿不到 store），
通过 `SettingsDialog(cells_provider=...)` 注入；`cell_count` 变化时实时刷新。

### 18.6 不许写死串数

`test_core_kernel.test_thresholds_follow_cell_count`（6S 不报 / 12S 报）与
`test_battery_thresholds.TestCellCount.test_changing_s_changes_the_alerts_immediately`
（13S 报 / 6S 不报）是这条的反向守卫：任何把 S 或阈值写死的实现都会让它们转红。

## 19. RemPct 退出判定（判定只认估算单芯电压）

用户 2026-10-24：「把分析标定改为按软件设定估算出来的电压自动进行，不再使用日志里的
rempct 做分析标定；rempct 相关的判定路径一并停用或移除，确保告警判定、串数显示和告警
正文都只依据估算电压和当前采用的电芯数。」

### 19.1 为什么删

`BAT.RemPct` 是飞控**按电压反推**的估计值。拿它再做判定等于在同一条证据上二次推导，
而且它跟"整包 / 单芯"量级的判定是一个环：量级判错 → RemPct 解读错 → 阈值错。
现在单芯电压是直接算出来的（整包 ÷ 当前电芯数），**没有理由再绕一层估计值**。

### 19.2 代码层（删除，不是隐藏）

- `alerts.batt_alerts()` 的整段 `# --- 剩余电量 ---` 已删；`DEFAULTS["battery"]` 与
  `config/alerts.yaml` 的 `remain_warn` / `remain_crit` 同步移除。
  旧 `settings.json` / 旧 yaml 里残留这两个键**没有消费者，一律忽略**（不是报错）。
- `app.MAP_HIDDEN_RULES` 与 `app.map_regions()` 一并删掉：它们存在的唯一理由就是
  "把 RemPct 告警挡在地图外"，规则没了，过滤层也没有存在意义（`set_regions` 直接收 `rg`）。
- `ui/presets.py` 电池预设**保留** `BAT.RemPct` 曲线（日志里有的量，用户可能要看），
  但**去掉 `threshold=20.0`**：阈值线等于暗示"低于它就报警"，而引擎已经不这么判了。
- `batch.py` 的列 `bat_rempct_min` → **`bat_cell_min_v`（最低估算单芯电压）**
  = `BAT.Volt.min ÷ 自动识别电芯数`；风险分改为 `<3.1 V +150 / <3.3 V +50`
  （与产品默认一致）；识别不出串数**留空**，不猜。删掉三个 RemPct 报告引用。
  > 附带修复：该文件两处非法 f-string（第 861、932 行）此前让 `apmlog.batch`
  > **无法导入**（长期 Blocked），现已可编译，`_cell_min_v` / `risk_score` 实测可用。

### 19.3 判定链（唯一）

```
单芯电压 = BAT.Volt（整包量级）÷ 电芯数（手动优先，否则自动识别）
         = BAT.Volt（本身就是单芯量级，不除）
单芯 < cell_crit(3.1) → 严重；< cell_warn(3.3) → 警告
```

电芯数识别只用**电压类证据**（`cells_est` / `BATT_ARM_VOLT` / 观测峰值电压，见 §18.2），
告警正文只含单芯值、阈值、整包值、串数与来源，**不含百分比**。

实测（`2026-10-24 10-59-50.bin`、`2026-10-21 11-06-02.bin`）：改前各有一条
`batt_remain_crit`（RemPct 掉到 0%），改后**零条**，剩下只有 `vibe_axis`。

### 19.4 反向守卫（不许把 RemPct 判定加回来）

- `test_app_load.TestNoRemPctJudgement.test_low_rempct_produces_no_alert`
  —— RemPct 掉到 3% 也不许出任何 `*remain*` 规则；
- `test_app_load.TestNoRemPctJudgement.test_remain_keys_are_gone_from_defaults_and_yaml`
- `test_app_load.TestNoRemPctJudgement.test_old_config_keys_are_ignored`
- `test_battery_thresholds.TestCellCount.test_message_has_no_rempct_or_percent`
  —— 正文不许出现 `RemPct` / `剩余电量` / `%`，但必须写明几 S。

## 20. 曲线页四项体验升级

### 20.1 单泳道多曲线同轴叠加（Overlay）

- `LaneSpec.overlays: list[LaneSpec]`：同一泳道里的对比曲线。空列表 = 单曲线泳道，
  **老行为完全不变**（反向守卫：`test_lane_without_overlays_keeps_old_behaviour`）。
- 预设侧用 `LanePair(type, primary, overlays...)` 描述，`LaneRequest` 继续可用。
  `attitude` 预设从 10 道收成 **5 道**：Roll/DesRoll、Pitch/DesPitch、Yaw/DesYaw、R/RDes、P/PDes。
- 运行时每条叠加曲线是一个 `_Ghost`：**挂在同一个 `PlotItem`（同一 ViewBox）**，
  所以共用 X/Y 轴与游标；只画虚线（raw）或均值线（env），不画 min/max 填充——
  填充会盖住主曲线，叠加曲线的用途是"比形状"。
- **每条曲线一个胶囊**（`_chip_map[(道号, 曲线号)]`），点谁切谁：
  `_toggle_curve(idx, curve, on)`；`_toggle_lane` 退化为 `curve=0` 的包装。
  `laneVisibilityChanged` 携带**曲线**标签（如 `ATT.DesRoll`），不是道标签。
- 叠加曲线的单位：日志 FMTU 常不给 `Des*` 列单位（实测 `ATT.DesRoll` 单位为空），
  同一根 Y 轴上单位必须一致，所以为空时**继承主曲线单位**。
- 姿态预设不再钉死 `±180`：钉住之后"个别 -180° 尖峰压平整道"的问题根本用不上稳健量程
  （用户举的例子正是 ATT）。要固定看 ±180，用右键「固定量程…」。

### 20.2 空通道自动初始隐藏

- `presets._blank_channel(info)`：按**整份日志**的 `count/vmin/vmax` 判空
  （不是当前视图窗口——窗口刚好是静止段会误判）；`EMPTY_ATOL=1e-9`。
  元数据缺失时才退回按数据判（`_blank_values`）。
- 空 → `spec.empty=True` + `spec.hidden=True`：泳道不占行，胶囊**初始未勾选并置灰**
  （灰边框 `#e9eaec`/灰字），tooltip 写明"该通道全时段恒为 0（空通道）"。
- **置灰 ≠ 禁用**：胶囊仍可点，点了照样显示（用户偶尔要看）。
- `TRACK.*` 合成通道不参与判空（速度为 0 是真实状态，不是缺通道）。
- 实测四旋翼日志：`RCOU.C1~C4` 正常、`C5~C8` 全 0 → 默认只展开 4 道。

### 20.3 Y 轴抗离群尖峰

- `seriesplot.robustness_range(v)`：`1%~99%` 分位 + **15% 外扩**（`Y_ROBUST_MARGIN`）。
  点少于 `Y_ROBUST_MIN_POINTS=20` 时退化 min/max（分位数样本不足没有意义）。
- 量程由**该道所有可见曲线**（主 + 叠加）合并计算，固定量程（`y_min/y_max`）优先。
- **双击 Y 轴**恢复全量：命中用 `_lane_row_at_pos`（按整行，含轴区）+ `_is_on_y_axis`
  （点在 ViewBox 左侧那一竖条）；双击绘图区仍然是原来的"重置视图"。
  再双击一次切回 1%~99%；右键菜单也有同款入口（`_toggle_y_full`）。
  钉住状态存在 `_y_full`，`reset_view()` 与重建泳道时清空。

### 20.4 精简底部读数条

- 格式统一为 **`字段名 = 数值 单位`**（`_fmt_readout`），去掉 `(桶N点)`。
  桶点数只在**悬停提示**里给（那是有诊断价值的场合，且不会挤爆一行）。
- 同一泳道的叠加曲线读数**紧挨着**主曲线（组内两空格，组间 `|`），便于对比。
- 道数 >6 时名字截短到 10 字 + `…`，完整名在 tooltip 里。

## 21. 曲线页「叠加绘制」

用户 2026-10-24：「在曲线位置加个"叠加绘制"功能」——按钮放在曲线页工具条
「每道最小高」右侧。

### 21.1 两种用法

- **新建一条泳道**：所选通道合成一条新泳道，**第 1 条为主曲线**，其余为同轴叠加（虚线）。
  至少选 2 条（1 条不成叠加，OK 按钮置灰，`MIN_NEW_CHANNELS=2`）。
- **叠加到已有泳道**：所选通道作为那条道的叠加曲线追加（例如把 `BARO.Alt` 叠到轨迹高度上）。
  这种模式 1 条即可。

对话框 `ui/overlaydialog.py::OverlayDialog`：目标下拉（新建 / 每条现有泳道的标签）、
通道多选列表（带搜索过滤，左树已选中的会自动勾上）、底部实时提示"已选 N 条…"。
**左树改成 ExtendedSelection**，`selected_keys()` 按**树里显示顺序**返回（选中顺序不可靠）。

### 21.2 与预设叠加的关系

两者共用同一套运行时对象 `_Ghost`（同一个 `ViewBox` = 同一根 Y 轴，各自一条胶囊、
显隐独立），区别只在"谁声明的"：

| | 预设声明 | 运行时叠加 |
| --- | --- | --- |
| 来源 | `LanePair.overlays` | `SeriesPlot.add_overlay(idx, spec)` |
| 默认显隐 | `overlays_hidden=True`（先看实测值） | **显示**（用户主动加的） |
| 颜色 | 预设按序分配 | `OVERLAY_COLORS` 按序分配 |

`spec.color=None` 表示"交给上层分配"：`_Ghost.__init__` 里兜底成 `OVERLAY_COLORS[0]`，
`set_lanes` 里由主题调色板分配——**任何一条路径都不许让 color 停在 None**（踩过：
分组头 color=None 直接进 `_Ghost` → `TypeError: 'NoneType' object is not iterable`）。

### 21.3 为什么要有 `_group_lane_specs`

`_apply()` 里"分组 → LaneSpec"那段原先没法在 offscreen 下单测（要真日志），
抽成 `MainWindow._group_lane_specs(win, skipped)` 后可直接用假 store 验证：
一个分组=**一条**泳道、叠加色互不相同、单位继承、主曲线取数失败要报进 skipped 而不是崩。

### 21.4 重建与持久化

- `set_lanes()` 会重建所有泳道 → 运行时叠加会消失，所以 `_apply()` 在
  `set_lanes` 之后立刻调 `_apply_lane_overlays(win)` 把"叠加到已有泳道"的曲线补回来；
  分组走 LaneSpec 声明，天然跟着重建。
- `views.json` 新增 `channel_groups`（`[["ATT.Roll","ATT.DesRoll"], …]`）与
  `lane_overlays`（`[["BARO.Alt", ["ATT.DesPitch"]]]`）；换日志时按现有通道过滤，
  **过滤后不足 2 条的分组整组丢弃**（一条不构成叠加）。「清除视图记忆」一并清空。
- ⚠ 验证脚本必须把 `_views_path` 指到临时目录：它是**全局**的
  `%LOCALAPPDATA%\APMLogVieweriews.json`，写坏会污染用户真实视图记忆（本次已发生并清理）。

## 22. 地图轨迹播放（跟随时间线）

用户 2026-10-24：「地图页加轨迹播放：跟随时间线逐点绘制，支持多条轨迹同时绘制对比，
各条不同颜色并标出名称；默认打开日志显示全部轨迹；拖时间线到某处就显示到那一刻。
时间线上方、'地图跟随'前加'全部轨迹显示'按钮，点击恢复完整轨迹，再点切回跟随。」

### 22.1 接口

| 层 | 接口 | 语义 |
| --- | --- | --- |
| `TrackMapWidget` | `set_track_follow(on)` / `track_follow()` | True=只画到播放指针；False=完整轨迹（默认） |
| | `set_playhead(t)` | 记录"画到哪里"，跟随模式下**合并**后重画 |
| | `_redraw_follow()` | 重画主+副轨迹、同步屏幕空间图元、排一次层刷新 |
| `PlayBar` | `btn_all_tracks`（可勾选按钮） | **按下=完整轨迹**（默认），弹起=跟随 |
| | `trackModeToggled(bool)` | bool 就是"全部显示" |
| `MainWindow` | `_track_mode_changed(full)` | 调地图 + 切到跟随模式时用当前游标定位 |
| | `_on_loaded` | 每次加载日志都回到"全部轨迹显示" |

### 22.2 裁剪在哪一层

- 主轨迹：`_draw_track()` 里 `n = searchsorted(self._t, playhead, 'right')`，
  之后所有绘制都用 `sx[:n] / sy[:n] / alt[:n]`（**不是**改数组本体）。
- 副轨迹：`_xy_for(tr, until=...)` 在**同一组掩码**上加时间条件，
  保证 x/y/t 三者仍对齐；跟随模式下**不用 `e.xy` 缓存**（缓存可能是裁剪过的）。
- 模式切换时把 `e.xy` 全部作废，否则切回完整轨迹会画出一条"短轨迹"。

### 22.3 性能（实测）

`FOLLOW_REDRAW_MS=110` 把 20fps 的播放时间更新合并到 ~9fps。72 MB / 20000 点、4 条轨迹全开：
重画 **22 ms（30%）→ 69 ms（100%）**，靠既有抽稀（`THIN_PX`）+ 分块（`CHUNK_SEGMENTS`）。

### 22.4 两个真实 bug（本次顺带修掉）

1. **图例说谎**：`TrackLegendPanel.set_tracks()` 在**填充过程中** `setChecked` 就触发
   `toggled` → `_changed` → `visibilityChanged`（半成品列表）→ 主窗把它当用户选择存进
   `views.json` 再回灌，于是**主轨迹 POS 未勾选却照样画**。修法：先 `setChecked` 再
   `connect`；并让 `_draw_track()` 每次重画后按 `_primary_on` 收口（取消勾选后任何重画
   都不许画回来）。`_primary_hidden()` 只认 `_primary_on` 单一真源。
2. **播放刚开始画不出轨迹**：高度渐变色按 24 档分组，而跟随裁剪后可能只剩 21 个点 →
   每档 1 点，`_runs()` 丢掉单点段 → 整条轨迹消失。修法：档数随点数收缩
   （`24 if n >= 240 else max(2, n // 4)`），并在"一个图元都没画出来"时用单色兜底。

### 22.5 测试口径

`_layer_track2` 现在同时装折线与**轨迹名称标签**，所以既有测试的"一条轨迹一个 path"
改成只数折线图元（`_paths(layer)` 过滤 `QGraphicsPathItem`），断言语义不变。
新增 `tests/test_track_follow.py` 14 条：默认全量、裁剪点正好落在游标、切回全量、
副轨迹一起裁、`set_time` 只在跟随模式动指针、重画合并、名称标签（多条才有、单条不加）、
颜色互不相同、取消勾选主轨迹后重画仍不显示、按钮位置与信号、主窗处理函数。

## 23. 时间线飞行模式配色（按模式，可自定义）

用户 2026-10-24：「为每种模式分配专属颜色：同一模式在所有时间段同色、不同模式可区分；
默认按操作难度由低到高在色相与明度上递进；色块/分段边界/当前选中状态都清晰；
设置里按模式列名字+颜色，点色块可自定义、立即生效并持久化，另有恢复默认；自定义只
影响该模式，相邻太像时靠分段边界或描边保持可读。」

### 23.1 颜色是「模式的属性」

旧实现按**段序号**轮换 6 个浅色（`_MODE_COLORS[i % 6]`），同一模式在不同段颜色不同，
时间线上根本认不出是什么模式。现在一律 `mode_color_of(模式名)`。

### 23.2 默认方案 = 难度阶梯

`ui/modecolors.py::MODE_LADDER`（18 个模式）按**操作难度**从低到高排：
LOITER → POSHOLD → ALT_HOLD → STABILIZE → GUIDED → AUTO → RTL → SMART_RTL →
AUTO_RTL → CIRCLE → DRIFT → SPORT → ACRO → AUTOTUNE → LAND → THROW → FLIP → TURTLE
（低 = 全自主、几乎不用人管；高 = 纯手动/高风险动作。顺序写死在代码里，不随日志变化）。

配色由三件事共同决定，缺一不可：
- **色相** 190° → 4°（青绿 → 红，安全 → 危险）；
- **饱和度** 55% → 80%；
- **明度 3 级循环** 70% / 56% / 42%。

为什么要明度循环：18 个模式只做色相渐变时相邻只差约 10°，一屏上分不开；
叠一层明度循环后相邻最小感知距离 **86**、两两最小 **65**，都能分辨。

阶梯外的模式（新固件新增/未登记）按名字 **md5 稳定散列** 取 `EXTRA_COLORS`
（蓝紫一侧，一眼看出不是标准模式），跨会话不变——不许每帧随机。

### 23.3 可读性保障（不靠"颜色看着不一样"）

- **分段边界永远画**：每段左边界一条线，颜色取该段墨色的半透明，任何主题下都可见。
- **相邻太像就加粗**：`modecolors.too_close(prev, cur)`（近似感知距离 < `TOO_CLOSE=80`）
  → 边界线宽 1.0 → 2.0。默认阶梯的相邻距离 86，所以默认不触发；用户把两个模式调成
  近似色时才触发。`mode_boundary_widths()` 把它算成可测的列表。
- **当前段有强调描边**：播放指针所在段用 `QPalette.Highlight` 描 2px 边 + 标签加粗。
- **段内文字墨色按底色亮度算**（`ink_on`：亮度 > 150 用近黑，否则白），窄段不写字，
  但悬停会显示「模式 X　a–b 秒（时长）」。

### 23.4 设置与持久化

- 只存**覆盖层**：`Settings["mode_colors"]` 是 JSON **字符串**
  （`{"LOITER":"#123456"}`）。设置项一律标量，避免 `DEFAULTS` 里的可变 dict 被就地改写；
  `reset()` 也会把它清成 `"{}"`。
- `Settings.mode_color/set_mode_color/clear_mode_color/reset_mode_colors/mode_color_palette`。
  非法颜色**忽略**（保持原值，不清空）；模式名统一大写。
- 设置页「模式颜色（时间线飞行模式色带）」：18 个色块 × 2 列排布，点色块弹
  `QColorDialog`；改完 `changed("mode_colors")` → `MainWindow._push_mode_colors()`
  立即重画时间线（含日志里出现但阶梯没登记的模式）；重启后从 settings.json 读回。
- ⚠ 该组的按钮叫 **「全部还原为默认」**，不能叫「恢复默认」：对话框底部有全局
  「恢复默认」，按文字找按钮的代码（含既有测试）会先命中它而点错（已踩）。

### 23.5 兼容性说明

`normalize_hex` 接受 3 位简写（`"bad"` = `#bbaadd`），所以"非法值"测试要用
`"nope"` 这类真正解析不了的字符串——写成 `"bad"` 会通过并被当成合法颜色。

## 24. 设置页整页布局（无滚动条）

用户 2026-10-24：「把设置页面整体做大一些，模式颜色的色块改成多列排列，删掉模式颜色的
说明文字；让设置页面不要出现滑动条，所有内容都能在页面内完整显示。」

### 24.1 取消滚动区

`SettingsDialog._build` 不再套 `QScrollArea`：内容 `QWidget(body)` 直接进根布局，
`_col_left` / `_col_right` 两列照旧（左：界面主题 / 振动 / 电池；右：模式颜色 / 悬浮提示 /
曲线与时间线 / 地图与仪表 / 飞控日志下载 / 缓存）。

### 24.2 尺寸规则（`_size_to_content`）

⚠ **构造期 `sizeHint()` 不可信**：布局还没 polish/activate 时它偏小
（实测 1012×806，真值 1206×862 或真机 1011×810），窗口会"刚好差一点"，
把最右/最底的内容切掉——没有滚动条时这就是硬故障。所以：

1. 先 `ensurePolished()` + `layout().activate()` + `adjustSize()` 再读 `sizeHint()`；
2. `showEvent` 里再校正一次（`QTimer.singleShot(0, ...)`），因为字体/样式在 show 时才最终生效。
   （真机复现：只按构造期尺寸开窗得到 1022×823，右侧列与底部被切。）



1. `adjustSize()` 让布局算真实 `sizeHint`；
2. 窗口尺寸 = `sizeHint + 10`，夹到屏幕可用区的 96%/94%；
3. **最小尺寸 = 内容尺寸**（屏幕比内容小才让步到屏幕）——这是"没有滚动条也看不丢内容"
   的硬保证：窗口拖不小到把内容裁掉。
真机实测（1920×1080，Qt 真实字体）：内容 `sizeHint` = **1011×810**，窗口 **1200 × 820**
（原先 980 × 720）。观感下限用 `_MIN_W=1200 / _MIN_H=820` 写死，屏幕上不够才让步；
两列各约 590 px，表单行距 8 px、列间距 18 px。

⚠ 没有滚动条兜底 ⇒ 今后往设置页加内容必须重新看 `sizeHint`；屏幕装不下时 Qt 会裁，
不会再出现滚动条（这是用户选定的取舍）。

### 24.3 模式颜色组

- 删除原先那 3 行说明文字（难度递进/即时生效那些话移到契约与 tooltip 里）。
- 色块 **3 列 × 6 行**（18 个模式），每格「色块 + 模式名」，列间 16px 间隙，
  最右一列吃掉多余宽度——用嵌套 layout 或定宽小部件都会把后两列顶到窗口边缘。
- 该组位于**右列最前**（左列只有 3 组，右列 6 组，两列高度接近，避免大片空白）。

### 24.4 编辑事故（写下来防复发）

用"切片取出再插回"的方式移动代码块时，**先删后插**的顺序把
`悬浮提示/曲线与时间线/振动/电池` 整段复制了一份（`_build` 里同名分组建了两遍，
`sizeHint` 从 757 涨到 1253）。已按"找到两处 `form_tip = ...` 删掉前一份"修好，
并在改完后**打印所有 `_group(left|right, ...)` 的顺序**核对归属。
同类改动的自检清单：分组数量 == 9、`sizeHint` 没异常变大、左右列归属正确。

### 24.5 测试口径变更

- `test_alert_thresholds.test_rows_live_in_a_scroll_area` → **`test_no_scroll_area_and_everything_fits`**
  （反向守卫：不许出现 `QScrollArea`、不许有可见滚动条、最小尺寸容得下内容、
  按内容尺寸放开后没有部件越过 body 底边）。
- `test_battery_thresholds.test_two_column_layout` 改读 `dlg._body.layout()`（原先读滚动区
  内的 widget），宽度断言提到 ≥1000。
- `test_mode_colors.test_grid_is_multi_column_and_has_no_explainer`：每行 ≥3 个色块、
  ≤6 行、且不再有含「难度」的说明标签。

## 25. 插件机制（外部目录 / 只读接口 / 七类挂载点 / 异常隔离）

用户 2026-10-28：顶部「设置」与「关于」之间加「插件」入口，通过**稳定接口**向插件开放宿主
能力；免安装版要能扫外部目录、坏插件只停自己、打包要预留常用科学计算库。

### 25.1 入口与发现

- 工具栏顺序固定为「… 设置 / 插件 / 关于」，`tests/test_plugins.py` 按位置守卫。
- 默认目录 `%LOCALAPPDATA%\APMLogViewer\plugins`（不存在就建空目录）；追加目录来自设置
  `plugin_dirs`（JSON 数组字符串）与环境变量 `APMLOG_PLUGIN_DIRS`（`os.pathsep` 分隔），
  去重保序。插件页的「添加目录…」= 写 `plugin_dirs` + 重载。
- 一个 `.py` 文件或一个包目录（有 `__init__.py`，或只有 `plugin.py`）= 一个插件；
  以 `_` / `.` 开头的跳过。入口固定 `register(ctx)`，可选 `PLUGIN_NAME` / `__version__`。
- 模块名 `_apmlog_plugin_<id>`（注册进 `sys.modules`，dataclass/类的 `__qualname__` 才正常）。

### 25.2 只读数据接口 `ctx.store`

`StoreView` 内部只存一组**闭包**，实例里没有 `LogStore` 引用，`__setattr__`/`__delattr__`
直接抛 `AttributeError`。方法：`available / name / path / meta / stats / types / has_type /
channels / keys / channel_range / series / series_dict / envelope / events / params / param /
modes / tracks / track`。

- 时序/包络返回 `SeriesData` / `EnvelopeData`（frozen dataclass + 拷贝后 `writeable=False`
  的 numpy），插件改不动宿主数据，也改不动别的插件看到的副本。
- `meta()/stats()/params()` 都是深拷贝/新字典。
- 没打开日志时给 `empty_store_view()`：`available=False`，取数**抛 RuntimeError**
  （"还没有打开日志"），不返回编造的空曲线。

### 25.3 控制口与信号

`ctx.control`（白名单方法，无通用后门）：`open_log / seek / zoom / play / set_preset /
add_channel / select_tab / status / refresh / export_png`。每个都返回 bool，宿主侧异常被
吞掉并转成 `pluginError` 信号（不炸插件，也不炸主程序）。

`ctx.signals`（Qt Signal，参数全是纯数据）：`log_loaded(dict) / log_closed / time_changed(float) /
play_state(bool) / mode_changed(str) / alerts_updated(list) / settings_changed(str, object) /
selection_changed(dict) / plugins_changed(list)`。

发射点：`_on_loaded`（加载完、遮罩收掉之后）、`_on_time`、`_start_play`/`_stop_play`、
`_reload_alerts`、`_apply_setting`、`_on_region`、管理器 `changed`。`mode_changed` 只在
模式真的变了才发（每帧都发会把插件刷爆）。

顺序（实测，2026-10-28）：换日志 `log_closed → alerts_updated → log_loaded`；首次加载只有
`alerts_updated → log_loaded`；点「复位视图」也会发 `selection_changed(whole_log=True)`，
载荷的 `t0/t1` 是全程（取不到时为 nan，不编造）。

### 25.4 七类挂载点

| API | 必填 | 校验（不合格就拒，不猜） |
| --- | --- | --- |
| `register_alert_rule(fn)` | `fn(store) -> [dict]` | `title` 非空；`severity` 不在集合内就按 `warn`；`t0/t1/value/threshold` 转 float |
| `register_synthetic_channel(key, fn, *, label, unit)` | `key` 必须 `TYPE.field` | 键非法/重复/非可调用一律拒；返回必须 `(t, v)` 且等长 |
| `register_preset(preset)` | `id/name/lanes` | id 非空且不与已注册重复（内置 id 也不许占） |
| `register_plot_tool(name, cb)` | `cb(ctx, PlotSelection)` | 名称非空；返回字符串则宿主弹窗显示 |
| `register_map_layer(layer)` | `layer.render(store)` | 只认 line/point/circle；坐标 `nan`/越界整条丢弃；颜色非法退回图层默认色；单层 ≤400 图元、单线 ≤5000 点、≤50 层 |
| `register_tab(title, widget)` | `QWidget` 实例 | 非 QWidget 直接拒；停用插件即摘掉该页 |
| `register_exporter(name, fn)` | `fn(store, path)` | 返回字符串 → 宿主写文件；没生成文件算失败 |

合成通道会以「插件通道」组出现在通道树（双击可加进曲线页），也能被插件预设引用：
`_apply()` 与 `_spec_for_key()` 走 `PluginManager.channel_source(store)`（真通道 + 合成通道
的通道源），而不是裸 `LogStore`。

### 25.5 异常隔离（硬要求）

- `_load_candidate` 捕获 `BaseException`（`KeyboardInterrupt` 除外）→ 该插件 `failed`，
  记 `error` + 完整 traceback，`_drop(pid)` 摘掉它已经注册的东西。
- 运行期所有插件代码都在 `guard(pid, what, fn, *a)` 里跑；异常 → `FAILED` 哨兵 + 计数；
  同一插件累计 `MAX_PLUGIN_ERRORS=5` 次自动 `disable()`（"连续 N 次异常，已自动停用"）。
- `disable()` 摘贡献 → 界面立刻回到没有它的样子（预设从下拉消失、标签页移除、地图图层清空、
  告警规则不再参与）；`enable()` 按原路径重载。
- 主程序侧：`_load_plugins_safely()` 连 `load_all()` 都包了 try，插件系统自身故障按
  "无插件"继续启动。**无插件、有坏插件、插件全炸 —— 三种情况下主程序都必须能正常启动。**

### 25.6 免安装打包（PyInstaller）

- `hiddenimports` 显式加 `apmlog.plugins`、`apmlog.plugins.api`、`apmlog.plugins.manager`、
  `apmlog.ui.plugindialog`（外部插件是运行时加载，静态分析看不到它）。
- `OPTIONAL_PLUGIN_MODULES` 预留插件常用库（scipy 各子模块 / pandas / sklearn / h5py /
  matplotlib+Agg / PIL / numba / statsmodels / pywt / tabulate / openpyxl …）：**装了才带，
  没装安静跳过**；同时把这些模块从 `excludes` 里放出来（否则 hiddenimports 与 excludes
  自相矛盾，打包时静默丢包）。用户要加别的库：`pip install` 后重跑 `build.py build`。
- 插件目录不在包内，换机时把 `plugins/` 一起拷走即可。

### 25.7 测试口径

`tests/test_plugins.py`（51 个用例）：外部目录发现（含 `_`/`.` 跳过、包目录、缺目录自建）、
导入/注册/回调三级异常隔离与自动停用、`StoreView` 只读（拷贝、`writeable=False`、无
`_store`、空视图报错）、控制口路由与失败上报、信号载荷、七类挂载点各一条正例 + 脏数据拒绝、
主窗集成（工具栏位置、预设/标签页/通道树/告警合并/地图图层、停用后全清）、
打包预留（spec 含 hiddenimports 与 excludes 放行规则、spec 能 compile）。

### 25.8 已知边界（写清楚，别假装没有）

- 插件在**宿主进程内、主线程**执行：没有超时强杀，长任务请插件自己起线程；
  能拖慢界面的插件，宿主只能靠用户停用它。
- 信任模型是"用户自己放进来的代码"：不做沙箱级隔离（同进程无法真正限制）。
  只保证**数据接口只读**与**异常不外溢**两条。
- `apmlog.batch` 仍不打包（`ProcessPoolExecutor` 需要入口 `freeze_support`），与本机制无关。

## 26. 主题插件（注册 / 选择 / 应用 / 失效回退）

用户 2026-10-28：在插件机制里增加**主题插件类型与入口**，允许加载、选择并应用自定义主题，
向主题插件开放「注册主题、应用主题」等能力；主题不受支持或插件在加载/应用/运行中异常时，
**自动回退内置默认主题、记录错误、只停用该主题插件**，不影响主程序启动运行与其它插件。

### 26.1 数据契约：四色 + 可选曲线档

主题只有四个颜色角色（与内置 `light`/`dark` 同一套 QPalette 生成器）：

| 角色 | 别名 | 含义 |
| --- | --- | --- |
| `window` | `c_window` | 窗口、工具条、表格底 |
| `base` | `c_base` | 列表、时间线、输入框底 |
| `text` | `c_text` | 正文文字 |
| `accent` | `c_accent` | 选中行、游标、按钮高亮 |

```python
from apmlog.plugins import ThemeSpec
ctx.register_theme(ThemeSpec(id="midnight", name="午夜蓝",
                             colors={"window": "#131a26", "base": "#0d1320",
                                     "text": "#e4ecff", "accent": "#4d9dff"}))
```

- 颜色只收 `#rgb` / `#rrggbb`；少一个角色、或写成 `red`/`#12345` → **拒绝注册**（不猜、不补色）。
- `curve` 留空 = 按 `base` 亮度自动选亮线/暗线；也可强制 `"light"`/`"dark"`。
- id 限 `[0-9A-Za-z_.-]{1,40}`，**同一插件内**不许重复（不同插件可同名，值里带插件 id 区分）。
- 存进设置的值是 `plugin:<pid>:<theme_id>`：停用/卸载插件后还能一眼看出属于谁。

### 26.2 两个入口

1. **设置 → 界面主题**：内置四项后面接分隔线 + 「插件：<名称>」若干项，选中即生效并落盘。
2. **插件页 → 插件主题**：列出全部插件主题（`●` 标当前生效的），「应用主题」按钮切换。
   表里「提供的扩展」列会显示 `主题`。

插件自身也能切：`ctx.apply_theme("我的主题 id")` / `ctx.apply_theme("dark")` /
`ctx.apply_theme("plugin:other:neon")`；`ctx.themes()` 拿全部已注册主题。

### 26.3 失效回退（硬要求）

四类失效都要走到**同一个回退路径** `MainWindow._theme_fallback()`：

| 失效点 | 处理 |
| --- | --- |
| 注册时颜色/字段不合法 | 拒掉该主题 + 记错误；插件若**只提供主题且全部被拒** → 整插件判 `failed`（"提供的 N 个主题全部不受支持"） |
| 设置里存着 `plugin:<pid>:<id>`，但插件没加载/已停用/已删除 | 回退默认主题 + 状态栏提示；没有可停用的插件就只回退 |
| 应用时主题数据被改坏（`spec_from_theme` 抛错） | 回退默认主题 + `disable(pid)` + 记日志 |
| 宿主应用过程抛异常（`plugin_apply_theme` 抛） | 同上；异常在管理器里被收住，主窗不崩 |

回退动作固定为：`ui_theme = "system"`（`theme.DEFAULT_THEME`）→ 用内置配色重刷 palette →
`statusBar` 一句话 → `plugins.disable(pid, "主题不可用：…")` → 发 `theme_changed`。
**不重启、不清设置文件、不动其它插件。**

### 26.4 启动时序（为什么这么做）

`main()` 里第一次 `ui_theme.apply_settings()` 发生在插件加载之前，此时插件主题**还解析不到**：
按未知模式处理 → 用内置默认色开窗。插件加载完成后 `_sync_plugin_ui()` 末尾的
`_sync_active_theme()` 再补一次：能解析就应用插件主题，解析不到就走回退。
这样「设置里存着插件主题 + 该插件坏了」也不会卡住启动，只是回到默认外观 + 一条提示。

`_apply_ui_theme` 用 `_applying_theme` 标志防重入（回退时 `Settings.set` 会再触发一次换肤）。

### 26.5 测试口径

`tests/test_theme_plugins.py`（26 个用例）：注册校验（缺色/非十六进制/坏 id/坏 curve/重复 id、
跨插件同名允许、只提供坏主题整插件判死、好坏参半保留好的）、`ThemeSpec` 对插件不可变、
管理器→宿主应用链路与失败归因、`utility: theme_value/parse/spec_from_theme`、
设置项接受 `plugin:` 值并拒绝垃圾值、主窗集成（设置下拉含插件项并分组、选中即换 palette 且
写回 `theme` 曲线档、`theme_changed` 信号、停用插件立刻回退、应用期数据损坏回退 + 停用、
宿主抛异常不崩、启动时陈旧值回退、插件页列出并可应用主题）。

### 26.6 已知边界

- 插件主题只能改**四色**（外加曲线亮/暗）；不能替换控件绘制、不能注册 QSS。
  这是刻意的：全局 QSS 会和 `install_tip_style` 的 QProxyStyle 抢活（见 `theme.py` 模块注释）。
- 主题切换是全局的、立即生效；插件不要假设自己能"只给自己那个标签页换色"。


---

## 27. 事件/告警说明弹窗（2026-10-28）

用户要求：日志里**警告/错误类型**的消息，双击后要弹出**对应那条类型**的说明窗口，至少含
问题说明 / 可能原因 / 处理建议，建议要具体可操作；没匹配到也必须明确提示，不能空白；
且**不能影响**现有日志查看与双击行为。

### 27.1 与双击原有行为的关系（"加"而不是"换"）

| 位置 | 单击 | 双击（新增） | 右键菜单 |
| --- | --- | --- | --- |
| 「事件」页 | 光标跳到该时刻（原有） | 弹出该类型说明 | 原有"导出 CSV"+"查看该类型的问题说明…" |
| 「告警」页 | 选中即跳转（原有） | 弹出该规则说明 | 原有"导出 CSV"+"查看该规则的问题说明…" |

- 双击**不移动光标、不改窗口、不切页签、不选中行**：`_EventTable._on_dbl`/`_AlertTable._on_dbl`
  只 `emit helpRequested`；`_open_event_help` / `_open_alert_help` 只读 `store`/`ev`/`hit`。
- 弹窗是模态，关掉后原选中与视图保持不变（测试 `test_help_does_not_touch_view_state` 断言）。
- CSV 导出走 `_CsvMenuMixin._menu_extra(menu)` 钩子，**不在** `_EventTable` 里重写 `contextMenuEvent`，
  避免把原有导出菜单吃掉。

### 27.2 知识库是数据，不是代码

`data/knowledge/event_help.json`：`by_code`（子系统+错误码）、`by_subsys`（31 个子系统全覆盖）、
`by_msg`（关键词）、`by_alert_rule`（规则 id 前缀）、`by_kind`（ERR/MSG/ARM/EV/FMT/…）、
`resolved`（已恢复/ecode 0）、`fallback`（兜底）。每条含 `title/summary/causes[>=2]/actions[>=2]`。

- 匹配优先级：`by_code` → `resolved` → `by_subsys`（附「码表含义」note）→ `by_msg` →
  `by_kind`（附 note「未找到子系统 N 的专门条目…」）→ `fallback`。
- **绝不与码表混淆**：码表原文一律以「码表含义」引用，curated 建议单独成段。
- 数据缺失/损坏 → `_BUILTIN_*_FALLBACK` 常量兜底；连弹窗都构造失败 → `QMessageBox`（三层兜底，永不空白）。

### 27.3 已知边界

- 只有 `warn/error/critical` 级别才有「说明」意义，但**任何**事件都允许双击（info 会给通用说明）。
- `MSG` 未命中关键词时走 `by_kind` 通用条（`matched=True` 但 note 标注"未匹配到专门条目"）；
  只有"类型"本身未见过的（如插件自定义 kind）才是 `matched=False` + 琥珀色横幅。
- 告警规则 id 前缀必须与 `alerts.py` 实际产出对齐（`vibe_vibex` 而非 `vibe_x`）；
  `tests/test_event_help.py::test_real_alert_rule_ids_all_match` 守住这条，防数据/代码漂移。

---

## 28. 插件页不能让左侧栏变窄且锁死（2026-10-29 修）

**症状**：装第三方插件后左侧栏被挤到几十像素，并把分隔条往右拖也拉不回来。

**根因**：`QSplitter` 按两侧的 `minimumSizeHint` 分配宽度；插件控件直接 `addTab` 进
`self.tabs`，它的 `minimumWidth`（实测某 AI 插件页 `setMinimumWidth(560)` + 一排宽按钮
→ 整页最小宽 1246）把 `QTabWidget` 最小宽顶到 1252，于是 1728 窗口里左侧最多只能到
1728−1252−5 ≈ 471，拖也拖不动（分隔条不允许把右侧压到最小宽以下）。

**修法**：`app._PluginPageHost`（`QScrollArea`，无边框、`widgetResizable`、
`minimumSizeHint` 只留滚动条宽度）。插件控件本身体积、API、`register_tab` 用法都不变，
只是页的最小宽降级成"内容宽"：页窄了出横向滚动条，而不是抢左侧栏。

**实测对比**（同一份真实插件，1728×960）：

| | 标签区最小宽 | 左侧可拖动范围 |
| --- | --- | --- |
| 修前 | 1252 | 369 → 471（到底） |
| 修后 | 846（= 内置「曲线」页的固有最小宽） | 369 → 703（可自由来回） |

**边界**：内置「曲线」页工具条本身有 846 的最小宽，所以左侧最多能拉到
≈ 窗口宽 − 851；这是本次之外的老约束，未改。插件页窄于其内容最小宽时出现横向滚动条。
宿主不抢焦点，滚轮事件照旧给插件控件。

### 28.1 第二个同源病因：地图底部状态行

同一台机器上还有一处一样的东西：`TrackMapWidget.status`（数据源/缩放/光标经纬度/
游标时刻/瓦片缓存与失败数）是一行 `QLabel`，**文字多长、它的最小宽就有多长**。
瓦片下载失败提示一出现，地图页最小宽从 566 涨到 1204（打包版实测标签区最小宽 1840），
于是左侧栏直接被压成一条缝（用户截图上约 125 px）——这也是"拉不回来"的一半原因。

修法：`status.setSizePolicy(Ignored, Preferred)`，并把这一行的内容摘要放进 tooltip。
布局不再按它算最小宽；窗口窄时文字被截断，但不再抢左侧栏的地盘。

### 28.2 修完的实测（1728×960，真实第三方插件 + 真实炸机日志）

| 场景 | 标签区最小宽 | 左侧栏 |
| --- | --- | --- |
| 修前（插件页 1246 / 状态行 1204，取大） | 1252～1840 | 125～471，拖不动 |
| 修后 | 1098（= 内置「曲线」页工具条）+ 插件页 22 | 342 起，可拉到 ~625 |

「曲线」页工具条（预设框 + 4 个按钮 + 每道最小高 + 叠加绘制）是**固定成本**，
不随数据变；它现在就是左侧栏能拉多宽的上限。要再放宽，得把工具条做成可横向滚动
或改写标签文，属于另一件事，未做。

---

## 29. 任何页面都不得把分隔条钉住（2026-10-29 续）

§28 修的是"插件页 + 地图状态行"，但同样的病理还有第二处：**曲线页顶部的胶囊行**
（一条曲线一颗胶囊）直接进页面布局，曲线多了这一行能到 1800+，标签区最小宽被顶到
1836，左侧栏再次被压到 136 且拉不动。

规则（以后加页面/加行都按这个来）：

1. **一行会随数据变长的控件，不许直接进页面布局**。要么包一层横向滚动宿主
   （`_PluginPageHost` / `SeriesPlot._header_host`），要么让它的横向尺寸诉求失效
   （`QSizePolicy.Ignored`，如地图状态行）。
2. 滚动宿主的高度要跟着"有没有横向滚动条"重算，否则内容被切一半
   （见 `SeriesPlot._sync_header_height`）。
3. 判断标准写进自检：`--selftest` 会 `setSizes([620, …])` 再读回实际宽度，
   打印"可拉大 / 被钉住!"。**只看启动瞬间的宽度不算数**——页面绘制后最小宽才会长出来。

当前各页最小宽（1600×950，真实日志，含第三方插件）：轨迹地图 70、曲线 906、
告警 70、事件 70、参数 400、插件页 22。其中**曲线页的 906 是设计成本**
（预设框 + 4 个按钮 + 每道最小高 + 叠加绘制），它决定了左侧栏能拉多宽的上限。
