# APMLogViewer 变更日志

本文件按改动分节记录：**问题背景 → 改法 → 受影响的界面行为 → 验证结果**。
每条都写清"为什么改"和"怎么验的"，验证一律用真实日志、真实第三方插件和可复现的命令。

- 工程：APMLogViewer（ArduCopter DataFlash `.BIN` 日志分析桌面端）
- 版本：0.1.1 ｜ Copter 4.6.x / 4.7.x
- 记录范围：`2026-10-28` ～ `2026-10-30`

---

## 1. 事件/告警详情弹窗：双击看"这条类型到底怎么了、该怎么办"

### 1.1 问题背景

日志页里"事件"和"告警"两个表以前只能**看**：想弄清 `ERR 12/1 CRASH_CHECK` 或
`振动超标（VibeZ[0]）` 是什么意思、该动哪里，得自己查码表、翻 ArduPilot 文档，
或者凭经验猜。新手最容易做的是两件危险的事：**原因没查清就复飞**、**照着错误的方向
调参**。

要求里有两条硬约束：

1. 弹出来的必须是**双击那条消息所属类型**的说明，不是笼统的帮助；
2. **没匹配到条目也必须给明确提示**，不能空白、不能静默失败；
3. 不能破坏原有操作（单击跳转、右键导出 CSV）。

### 1.2 改法

**(a) 知识库是数据，不是代码。** 新增 `data/knowledge/event_help.json`，按来源分桶：

| 桶 | 条数 | 用途 |
| --- | --- | --- |
| `by_code` | 10 | 按"子系统 + 错误码"精确命中（`ERR 12/1`、`ERR 16/2`…） |
| `by_subsys` | 31 | 码表 31 个子系统**全覆盖**，只有子系统命中时给该子系统的通用解释 |
| `by_msg` | 13 | 文本关键词（crash / prearm / variance / glitch / compass / failsafe / battery / vibration / thrust loss / internal error / terrain / arming / log） |
| `by_alert_rule` | 13 | 本工程告警规则 id 前缀（`vibe_vibex/y/z`、`vibe_clip`、`vibe_axis`、`batt_undervolt`、`batt_pack_low`、`batt_sag`、`batt_temp`、`batt_end_drop`、`batt_semantics`、`batt_cells_unknown`） |
| `by_kind` | 7 | 连子系统都没有时的按事件类型兜底（ERR/MSG/ARM/EV/…） |
| `resolved` | — | "已恢复"/ecode 0 的事件：只解释"发生了什么"，不给故障处置建议 |
| `fallback` | — | 真的没收录时的通用排查步骤 |

每条都含 `title / summary / causes[≥2] / actions[≥2]`，`actions` 是**能照着做的动作**
（例：炸机条给"现场取证 → 对齐曲线看炸机前 10 s 的 VIBE / ATT / RATE / BATVolt / GPS →
翻同刻 MSG 原文 → 未查清原因别复飞"）。

**(b) 匹配优先级固定**（`apmlog/core/knowledge.py`）：

```
by_code → resolved → by_subsys → by_msg → by_kind → fallback
```

- 命中 `by_subsys` 时，**码表原文单独以「码表含义」引用**，不和自己写的建议混在一段；
- 命中 `by_kind` 而子系统有编号却没专项条目时，附注「未找到子系统 N 的专门条目」，
  让用户知道自己看到的是通用解释。
- 数据文件缺失/损坏 → 代码里的内置兜底常量；连弹窗都构造失败 → `QMessageBox`。
  **三层兜底，任何一层都不会给空白。**

**(c) 弹窗**（`apmlog/ui/eventhelp.py::EventHelpDialog`）：主体三段（问题说明 / 可能原因 /
处理建议），顶部一个范围徽标（`具体类型` / `未收录`），未收录时再多一条琥珀色横幅
「该类型暂未收录具体说明与方案，下面是通用排查步骤」；底部「复制说明」可整段拷成纯文本
（便于贴进群/写报告），「关闭」为默认按钮。

**(d) 接入方式：双击是"加"，不是"换"。** 两个表各加一个 `helpRequested` 信号：

| 位置 | 单击 | 双击（新增） | 右键菜单 |
| --- | --- | --- | --- |
| 「事件」页 | 光标跳到该时刻（原样） | 弹出该类型说明 | 原有"导出 CSV" + "查看该类型的问题说明…" |
| 「告警」页 | 选中即跳转（原样） | 弹出该规则说明 | 原有"导出 CSV" + "查看该规则的问题说明…" |

CSV 导出走 `_CsvMenuMixin._menu_extra(menu)` 钩子，**没有**重写 `contextMenuEvent`，
所以原有导出菜单不会被吃掉。

**(e) 打包**：`packaging/apmlogviewer.spec` 把 `data/knowledge/event_help.json` 放进 `datas`，
缺文件只告警、不阻止打包（因为代码里有兜底）。

### 1.3 受影响的界面行为

- 「事件」页、「告警」页双击 → 弹出说明窗；双击**不移动光标、不切页签、不改选中行**，
  关掉弹窗后视图与选中保持原样。
- 「事件」页任一行的 tooltip 与右键菜单都能到达同一份说明（不止双击一条路）。
- 未收录类型**照样有窗**：横幅说明未收录 + 5 条通用排查步骤 + 原始文本原样列出。
- 打包版与非打包版行为一致（知识库随包分发）。

### 1.4 验证结果

真实日志（`测试3\日志\00000165.BIN`，Copter 4.7.0，含炸机）：

| 操作 | 结果 |
| --- | --- |
| 双击 `ERR 12/1 CRASH_CHECK`（t=322.45 s） | 命中 `by_code` → 「炸机判据触发（CRASH_CHECK_CRASH）」，4 条建议，视图未变 |
| 双击同刻 `MSG Crash: Disarming: AngErr=171>30, Accel=0.0<3.0` | 命中 `by_msg` → 炸机关键词条 |
| 双击真实告警 `vibe_vibez`（VibeZ 峰值 60.84 m/s² ≥ 60） | 命中 `by_alert_rule` → 「振动超标（Z 轴）」 |
| 双击一个知识库完全没见过的类型 | `matched=false`，琥珀横幅 + 兜底 5 步（**不空白**） |
| 单击事件行 / 单击告警行 | 照旧跳转（回归用例断言） |

- 顺带修掉一个真实缺陷：告警规则的真实 id 是 `vibe_vibez`（`f"vibe_{axis.lower()}"`），
  知识库原先按 `vibe_z` 写前缀，导致真实告警**全部落到兜底**。已按真实 id 改前缀，
  并加 `test_real_alert_rule_ids_all_match` 把 16 个真实规则 id 逐个钉住，防数据/代码漂移。
- 单元测试：`tests/test_event_help.py` **32 项**（数据完整性、31 子系统覆盖、优先级、
  resolved、MSG 关键词、告警前缀、插件告警、兜底、坏数据、弹窗不空白、双击发信号、
  单击仍跳转、坏载荷不崩）。
- 全量回归：**946 passed / 3 skipped**（当时基线）。
- 打包版：`_internal/data/knowledge/event_help.json` 与源文件 **55,619 B、SHA256 一致**；
  `--selftest` 报告里 `知识库: {'by_code': 10, 'by_subsys': 31, ...}` 且
  `知识库试匹配 ERR 12/1 → 炸机判据触发（CRASH_CHECK_CRASH） 命=True 建议4条`——
  证明打包版真的在读数据文件并跑匹配，不是靠内置兜底。
- 截图：`selftest/event_help_matched.png`、`event_help_fallback.png`、`event_help_alert.png`。

---

## 2. 修复：装了第三方插件后，左侧栏被挤扁而且拉不回来

### 2.1 问题背景

装上一个真实第三方插件（AI 助手，`register_tab` 注册一整页聊天面板）后，
左侧栏（日志列表 + 通道树）被压到约 **125 像素**，而且把分隔条往左拉也**拉不回来**，
通道名只剩几个字。

不是插件的 Bug，是宿主没有隔离插件对布局的尺寸诉求。两处同源病因：

1. **插件页直接进标签区。** `QSplitter` 按两侧的 `minimumSizeHint` 分配宽度。
   该插件页上 `setMinimumWidth(560)` 加一排宽按钮，整页最小宽 **1246**，
   于是 `QTabWidget` 最小宽变成 1252 —— 1728 宽窗口里左侧最多只能到
   `1728 − 1252 − 5 ≈ 471`，再多就拖不动（分隔条不允许把右侧压到它最小宽以下）。
2. **地图底部状态行是"文字多长就多宽"的 `QLabel`。** 这一行放数据源 / 缩放 /
   光标经纬度 / 游标时刻 / 瓦片缓存与失败数，瓦片失败提示一出现，地图页最小宽从
   **566 涨到 1204**（打包版实测标签区最小宽 1840），左侧栏直接被压成一条缝。
   这条**与插件无关**，是"装完插件才注意到"而已。

### 2.2 改法

**(a) 插件页套一层可滚动宿主**：`apmlog/app.py::_PluginPageHost`（`QScrollArea`，
无边框、无外边距、`widgetResizable`、`minimumSizeHint` 只留滚动条宽度 ≈22 px）。
插件的 API、`register_tab` 用法、控件本身体积**全部不变**；变化的只是"页的最小宽"
降级成"内容宽"：页窄了出横向滚动条，而不是去抢左侧栏。宿主不抢焦点，滚轮事件照旧
给插件控件。宿主按"插件控件对象"缓存、**不回收**（插件停用后再启用常拿同一个控件
重新注册，此时删旧宿主会把已交给插件的控件一起 `deleteLater`，属于崩溃隐患）。

**(b) 地图状态行放弃横向尺寸诉求**：`TrackMapWidget.status` 设
`QSizePolicy(Ignored, Preferred)`，并把内容摘要放进 tooltip。布局不再按它算最小宽，
窗口窄时文字自身截断，但不再挤左侧栏。

### 2.3 受影响的界面行为

- 左侧栏恢复可自由拖动；插件页内容超出时**插件页内部**出横向滚动条（不再牺牲左侧栏）。
- 插件页的视觉与交互不变（无边框、无内边距、占满视口）；`plugin.select_tab("名字")`
  仍按标题切换。
- 停用/重载插件、切换主题、切页签都不影响宿主；插件页最小宽恒为滚动条宽度。
- 地图状态行在窄窗口下末尾会被截断（悬停 tooltip 看全文）。

### 2.4 验证结果

真实第三方插件（`%LOCALAPPDATA%\APMLogViewer\plugins\ai_assistant.py`）+ 真实炸机日志，
1728×960：

| 场景 | 标签区最小宽 | 左侧栏 |
| --- | --- | --- |
| 修前 | 1252 ～ 1840 | 125 ～ 471，**拖不动** |
| 修后 | 1098（内置「曲线」页工具条）+ 插件页 22 | 342 起，**可拉到约 625** |

打包版 `--selftest`（1600 宽）：

| | 标签区最小宽 | 左侧栏 |
| --- | --- | --- |
| 修前 | 1840 | 136 |
| 修后 | 1098 | 342，`插件: ai_assistant=loaded`、插件页含「AI识别」 |

- 新增 `tests/test_left_pane.py` **5 项**：状态行无横向尺寸诉求、长文字不涨最小宽、
  状态行文字仍然可读、插件页 + 超长状态行同时存在时标签区最小宽 < 1100、
  分隔条能把左侧拉到 650 以上、左侧仍有可用最小宽。
- `tests/test_plugins.py` 追加 **3 项**：插件页确实套了滚动宿主（控件本体不变）、
  插件要求 1400 最小宽时标签区最小宽不涨、按名字选中插件页仍可用。
- 全量回归：**953 passed / 3 skipped**；唯一 1 个失败是 `/link` 已知 flaky
  （`test_link_download.py::TestThreadContainment::test_yanking_client_socket_does_not_escape_thread`，
  单独跑 1.28 s 通过，与本次改动无关）。
- 截图：`selftest/fix_left_pane_wide.png`、`fix_left_pane_narrow.png`、
  `fix_left_pane_runtime.png`。

### 2.5 已知边界（未做）

「曲线」页工具条（预设下拉 + 4 个按钮 + 每道最小高 + 叠加绘制）本身有 **1098** 的
最小宽，是**固定成本、不随数据变**，现在它就是"左侧栏能拉多宽"的上限（1728 宽窗口
约 625）。要再放宽，得把工具条做成可横向滚动或精简按钮文案，属于另一件事。

---

## 3. 契约与打包

- `docs/CONTRACT.md` 新增 §27（事件/告警说明弹窗：与双击/CSV 的关系、匹配优先级、
  已知边界）、§28（左侧栏被挤扁的两个病因、修法、修前后数据、天花板说明）。
- `packaging/apmlogviewer.spec`：`data/knowledge/event_help.json` 进 `datas`；
  打包后 `_internal/data/knowledge/` 路径与运行期解析一致。
- 冻结包自检报告（`--selftest`）现在额外输出：知识库条数与一次真实试匹配、
  插件加载状态与插件页列表、左侧栏宽度与标签区最小宽——便于以后一眼看出
  "是不是数据没带进去""是不是布局又被顶住了"。

## 4. 复现验证的命令

```powershell
# 全量回归（offscreen）
$env:QT_QPA_PLATFORM="offscreen"
./.venv/Scripts/python.exe -m pytest tests -q --tb=line --no-header

# 两个改动各自的专项
./.venv/Scripts/python.exe -m pytest tests/test_event_help.py tests/test_left_pane.py -q

# 真实日志：双击说明（会另存三张弹窗截图）
./.venv/Scripts/python.exe tools/_check_event_help.py "C:\...\测试3\日志\00000165.BIN"

# 真实插件：左侧栏能不能拉
./.venv/Scripts/python.exe tools/_check_left_pane.py

# 打包 + 自检（报告写到 PNG 同目录的 *_selftest.txt）
./.venv/Scripts/python.exe packaging/build.py build
dist\APMLogViewer\APMLogViewer.exe --selftest "<日志路径>" --png selftest\pkg\exe.png
```

---

## 3. 曲线页胶囊行不再顶宽整页（左侧栏第二次被"钉住"）+ 自检扩成安装包冒烟

### 3.1 问题背景

上一节修完以后，干净窗口里左侧栏确实能拖了；但**画出曲线页之后**又拖不动了。
原因是曲线页顶部那行"胶囊"（一条曲线一颗，点击单独显隐）直接放在页面布局里，
曲线/幽灵曲线一多，这一行的最小宽就从 948 涨到 **1828**，于是整个标签区的最小宽
变成 1836 —— 1600 宽的窗口里左侧栏又被压回 136，且拉不动。

这个问题是**"自检"自己发现的**：把安装包冒烟项加进 `--selftest` 之后，
`左侧栏拖动: 136 → 136（被钉住!）` 直接把它暴露出来（原先只看启动瞬间的宽度，
那时页面还没绘制过，所以一直是"正常"）。

### 3.2 改法

- `ui/seriesplot.py`：胶囊行改由 `_header_host`（`QScrollArea`，无边框、
  `widgetResizable`、纵向滚动条关闭、横向按需）承载；行高由 `_sync_header_height()`
  跟着"有没有横向滚动条"重算，保证胶囊不被切掉。胶囊本身、点击逻辑、样式一律不变。
- `app.py --selftest`：新增三项冒烟输出
  - **码表**：`mode(2)` 与 `ERR 12/1` 的真实解码（证明 `data/codebooks` 被正确读到）
  - **详情弹窗**：真构建一次 `EventHelpDialog`（不弹模态），报告标题/命中/建议条数/纯文本字数
  - **左侧栏拖动**：`setSizes([620, …])` 后读回实际宽度，给出"可拉大 / 被钉住!"

### 3.3 受影响的界面行为

- 曲线页胶囊行在页面变窄时**横向滚动**，不再把整页撑宽；页面最小宽稳定在 906，
  左侧栏在 1600 宽窗口里可从 342 拉到 620+（1728 宽约 780）。
- 胶囊的显隐、颜色、置灰（空通道）行为不变；`叠加绘制`、预设框、导出按钮位置不变。
- `--selftest` 报告多三行，便于以后在**安装到真机之后**验证数据/弹窗/布局。

### 3.4 验证结果

| 检查 | 修前 | 修后 |
| --- | --- | --- |
| 画出曲线页后 `页1.minimumSizeHint()` | 1836 | **906** |
| 标签区最小宽 | 1840 | **910** |
| 左侧栏拖动（1600×950） | 136 → 136（钉住） | **342 → 620（可拉大）** |

- 全量回归：**954 passed / 3 skipped / 23 subtests**（改动前后一致，无新增失败）。
- 已知测试顺序敏感（**改动前就存在**，已在未改动的干净克隆上复现）：
  `pytest tests/test_seriesplot.py tests/test_event_help.py` 这一组合里
  `TestMainWindowWiring` 的两条"单击定位"用例会失败（先跑 event_help 或跑全量都通过）。
  与本次改动无关，未处理。

---

## 4. 100 MB 日志不再卡：解析 / 切页 / 缩放 / 拖动全量提速

### 4.1 问题背景

用户报的：“103 MB 的日志打开后整个界面发糊——滚轮缩放要卡一秒多，切页要等好几秒，
拖时间线一顿一顿，开着窗口内存一直涨”。定位到四个独立瓶颈，各自量过：

| 现象 | 修前实测 | 根因 |
| --- | --- | --- |
| 首次加载慢 | **88.3 s** | 每条消息都 `to_dict()` 建字典（2.6 M 条） |
| 切到事件/告警页卡 | **4.35 s** | 切页触发一次完整重建（全部 lane + 全部事件图元） |
| 滚轮缩放卡 | 最坏一步 **1.59 s** | 3300+ 个事件标记各自一个 `InfiniteLine`，缩放时逐项重算 |
| 时间线拖动发糊 | 单帧 **55 ms** 起步 | 每次 `valueChanged` 都同步重采样 + 立即重建图元 |

### 4.2 改法

1. **解析快路径**（`apmlog/core/reader.py`）：只对特殊/事件类型调 `to_dict()`，其余类型按
   `FMT` 预编译 `_numeric_plan()` 直接填 ndarray；`_instance_of_plan()` 判实例。
   新旧实现产出逐字节比对：64 种消息类型的数组 + `index.json` 的 stats/types/params/events 全等。
2. **事件标记改单个图元**（`apmlog/ui/seriesplot.py`）：每条 lane 一个 `_EventStrips`
   （一个 `QGraphicsObject`），`paint()` 里只画可视区间内的事件，顶部 16% 高度、1 px 笔。
3. **缩放后重采样真去抖**：`_refine_view()` 只 `start()` 一个单次 `QTimer`，
   真正重取数在 `_run_refine()`；去抖时长用设置项 `refine_debounce_ms`（默认 350）。
   采样点数改为按像素：`nb = min(2000, max(400, int(wpx)))`。
4. **切页只做便宜的那部分**：`tabs.currentChanged` → `_apply(keep_window=True, cheap=True)`，
   用指纹 `(preset, id(store), win, len(events), len(alerts), extra, groups)` 跳过重复构建。

### 4.3 受影响的界面行为

- 加载遮罩上的阶段文字不变，但同样的日志更早进到「解析完成」。
- 事件/告警页打开后不再“先白一下再出表”。
- 缩放时事件标记从“一缩放就满屏重画”变成只在可视区画；视觉上标记仍在顶部条带。
- 时间线拖动跟手（每次拖动不再排队一堆积压的重绘）。
- 没有任何功能被删；曲线、叠加绘制、事件双击说明全部照旧。

### 4.4 验证结果（同一份 103.6 MB 日志）

| 动作 | 修前 | 修后 |
| --- | --- | --- |
| 首次加载（冷） | 88.3 s | **44.7 s** |
| 再次加载（热） | — | **1.37～1.8 s** |
| 切事件/告警页 | 4.35 s | **0.01 s** |
| 切曲线页首绘 | — | **0.5 s** |
| 滚轮缩放最坏一步 | 1.59 s | **0.14 s** |
| 时间线连拖 20 次 | — | 合计 **0.94 s**，最坏单次 **59 ms** |
| 常驻内存 | — | **233 MB**（开曲线页 242 MB） |

- 全量回归：**954 passed / 3 skipped / 23 subtests**（本次 A/B 又加 29 条，见 §5）。
- 五项启冒烟（`--selftest`，真实 103.6 MB 日志）：全绿，**10.2 s**。

---

## 5. 两种搬日志的方式：U 盘模式 + MAVFTP 只读通道

> **真机未验证。** 本节链路行为全部跑在 `FakeFC`（假飞控）回环上；手上没有可用飞控。
> 真机验证一栏为空，不是“已验证”。

### 5.1 问题背景

用户问“能不能直接把飞控当 U 盘”，随后要求“两种功能都加上”：

- 大批量拷（整个 `APM/LOGS`）用 U 盘模式最快，但要重启飞控、有风险，必须拦截和二次确认；
- 单日志下载原来走 `LOG_REQUEST_DATA`，每次请求只 10.8 KB、每帧 90 B（实测真机 58 KB/s），
  100 MB 日志要半小时；MAVFTP 每帧 237 B、一次突发 16 KB，请求数少一个量级。

### 5.2 改法

**A. U 盘模式**（`MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN` 246 / `param1=5`）

- 链路面板新增「重启到 U 盘模式…」按钮；点击先弹二次确认（四项后果写清，默认「取消」）。
- **armed 拦截在最前面**：`MavLinkSession.is_armed()` 为真就不发命令，直接报错。
- 发完读 `COMMAND_ACK`：`ACCEPTED` → 提示等盘符、日志在 `APM/LOGS`；
  `UNSUPPORTED` → 明确说“固件没编进这个功能”；6 s 无 ACK → 报“可能已经重启”，
  不当作失败。

**B. MAVFTP 只读通道**（`FILE_TRANSFER_PROTOCOL` 110）

- 新模块 `apmlog/link/ftp.py`：`MavFtp.open_ro/read/burst_read/terminate/list_dir/fetch`，
  `fetch` 写 `.part` 改名校验，支持取消与重试。
- 面板新增「下载通道」下拉，持久化到 `dl_transport`：
  `自动（先试 MAVFTP，失败回退 LOG_*）/ MAVFTP / LOG_*（兼容模式）`。
- `auto` 下任何 MAVFTP 失败都自动回退，并在输出区打一行“已回退”；选了 `LOG_*` 就一条 110 都不发。

### 5.3 受影响的界面行为

- 链路面板“下载控制”一行多两个控件：`每请求块数` 后面是 `下载通道`；按钮区多了 `重启到 U 盘模式…`。
- 下载进度文本会标当前通道（`MAVFTP` / `LOG_*`）；回退时输出区有明确一行。
- U 盘模式成功后弹提示框（非阻塞式告知），失败/拦截弹警告框，文字不会为空。
- 其余（列表、续传、批量队列）不变。

### 5.4 验证结果与实测数据

新测试：`tests/test_mavftp.py`（**18**）+ `tests/test_mass_storage.py`（**11**），共 **29** 条，全过。
关键断言：armed 时 `FakeFC.commands == []`；UNSUPPORTED 文案含“不支持/固件”；
`param1==5.0`；取消确认后不投命令；`LOG_*` 通道 `ftp_ops == []`；`auto` 回退后内容仍逐字节一致。

**同一条日志（31.0 MB，真实 .BIN）两个通道各跑一遍**（`tools/_bench_channels.py`）：

| 通道 | 字节 | 耗时 | 吞吐 | 请求数 | 每次请求字节 | 内容校验 |
| --- | --- | --- | --- | --- | --- | --- |
| `LOG_*` | 31,031,296 | 418.44 s | 0.07 MB/s | 2874 | 10,797 | OK |
| MAVFTP | 31,031,296 | 36.14 s | **0.86 MB/s** | 1871 | 16,585 | OK |

回环上 MAVFTP 快 **11.6×**；参考日志全文（108,645,616 B）走 MAVFTP **126.11 s / 0.86 MB/s /
6549 次 burst 请求**，校验一致。

这些数字是本机 TCP 回环，代表“本端处理 + 协议请求次数”而不是空中带宽；
真机（57600 波特≈ 5.6 KB/s 理论上限 / USB CDC）两者都远达不到这个值。

### 5.5 已知边界

- **真机未验证**：没有飞控可接，MSD 与 MAVFTP 只对 `FakeFC` 跑过。
- MSD 模式下飞控会直接关机重启，本工具**不会**去模拟“拷完自动回飞控”，靠用户按提示断电重启。
- MAVFTP 只实现了只读几个操作码（Open/Read/Burst/Terminate/List），写/删/改名一律没有入口。

---

## 6. 真机验收：MAVFTP 三个协议 bug、路径映射、U 盘护栏（2026-10-30）

### 6.1 问题背景

接上一块 APM 固件飞控（COM94），把 FakeFC 回环跑到的东西拿到真实链路上验。
结果 MAVFTP **一次都没成功**：发出去的请求要么没应答、要么回一个空 ACK。

### 6.2 改法（四条，全部是“自洽的假飞控”掩盖了的错）

1. **载荷被写坏**：`endpoint.send_file_transfer_protocol()` 把 `payload[0..2]` 当成“目标地址前缀”
   去重写，而它其实是 `seq` 低/高字节 + `session`。改正后路径才真正传给飞控。
2. **报文格式**：按 `pymavlink/mavftp.py` 的权威实现改成 `<HBBBBBBI`（seq 为 uint16、
   头 12 字节、数据从第 12 字节起、单包上限 239），应答用 `128(ACK)/129(NAK)`。
3. **读请求语义**：要多少字节要放在**头部 `size` 字段**，不是 4 字节 payload；
   `session=0` 是合法会话（ArduPilot 从 0 开始），不能当失败。
4. **burst 真实行为**：应答 seq 逐包递增、服务端会对正在跑的流继续往后突。
   客户端改成“先跟着流跑到底、再回头补洞”，并在失败时 `Terminate` + `ResetSessions`，
   否则残留流会把下一次请求顶成超时。

另：日志路径改为 `LOG_ENTRY.id` **就是**文件名编号（id=10 → `00000010.BIN`）。

### 6.3 受影响的界面行为

- 链路面板选「MAVFTP」时真的能下日志了（以前一律超时/空文件）；下载完的文件名与飞控上一致。
- `自动` 通道在 MAVFTP 打不开时仍然安静回退到 `LOG_*`（实测已验证）。
- 下载失败会留下 `.part` 并把飞控侧会话清干净，不会让下一次下载卡死。
U 盘模式界面无变化（护栏行为见 6.4）。

### 6.4 验证结果（真机，同一份日志 1,148,993 B）

| 通道 | 耗时 | 吞吐 | 回退 | 内容 |
| --- | --- | --- | --- | --- |
| MAVFTP | 14.10 s | **79.6 KB/s** | 否 | ok |
| 自动（FTP 故意打不开） | 19.76 s | 56.8 KB/s | **是** | ok |
| `LOG_*` | 19.69 s | 57.0 KB/s | 否 | ok |

三条通道落盘 **sha256 完全一致**。MAVFTP 在这条链路上快 1.4×。

U 盘模式（**未触发重启**）：armed 拦截后 tx 帧 **2 → 2（差 0）**；二次确认必备要素齐全、
默认「取消」；ACK 0/3/4/6 s 超时提示均符合预期。真发 `246/param1=5` 会重启进 MSD 并断链，
需断电重启才回正常固件，属于破坏性动作，**待用户确认**后再做。

---

## 7. 大日志切页卡顿：只重建这一页需要的那部分（2026-10-30）

### 7.1 问题背景

用户报：打开较大日志后，地图↔曲线来回切会卡一段。实测定位到“切页会触发一次全量 `_apply()`”，
而 `_apply()` 里有三块重活无论变没变都重做：

| 重活 | 单次成本 |
| --- | --- |
| 重建 5 条泳道（PlotItem + 轴 + 胶囊） | 302 ms |
| 地图 2.6 M 点轨迹再推一遍 | 185 + 51 ms |
| 160 个告警区间图元重建 | 105 ms |

### 7.2 改法

1. `cheap=True`（切页）不再重推地图/时间线数据——它们只随日志与事件集合变。
2. `SeriesPlot._lanes_signature()`：通道集合/颜色/单位/阈值/band/叠加曲线一致时**原地换数据**，
   不拆控件；`hidden` 不进签名，用户勾选不会被切页重置。
3. 事件条带与告警区间按内容签名去重（一样就不重建图元）。
4. `set_lanes` 全程 `setUpdatesEnabled(False)`，完事只画一次（以前一次重建要画三遍）。
5. 图表视图开 `CacheBackground`，主题切换/重建时 `resetCachedContent()`。

### 7.3 受影响的界面行为

- 地图↔曲线、↔告警、↔事件、↔参数来回切不再“等一会儿”；切页不再重置胶囊勾选。
- 空通道置灰改为**就地刷新胶囊**（以前它一变就整页重建）。
- 视觉效果、数据、交互都没变；加载耗时不变。

### 7.4 验证结果（103.6 MB 真实日志，offscreen，含 `grab()` 强制重绘）

| 场景 | 修前 | 修后 |
| --- | --- | --- |
| 第一次进「曲线」（强制重建） | 1411 ms | **563 ms** |
| 第一次进「告警」/「事件」 | 845 / 841 ms | **258 / 258 ms** |
| 第一次进「参数」 | 743 ms | **318 ms** |
| 切页且窗口变了 | 932 ms | **453 ms** |
| 切页且窗口没变（纯切页） | ~0.4 ms 逻辑 | ~0.4 ms 逻辑 |

- 全量回归：**983 passed / 3 skipped / 23 subtests**。
- 五项启冒烟（真实 103.6 MB 日志）：全绿，加载 **1.10 s**（热缓存）。
- 加载/显示无回归：曲线通道、码表、详情弹窗、左侧栏拖动结果与修改前一致。

---

## 8. 缓存界面：自动清理（每 3/7 天、超 1 GB、自定义）

### 8.1 问题背景

设置里的缓存只能“看一眼占用 + 手动清”。日志多了以后缓存会一直长（一份大日志的解析缓存就是几十 MB），
用户得自己想起来去点按钮；也没有任何“多久没用过就可以删”的口径。

### 8.2 改法

* 设置 → 缓存 新增：**开启缓存自动清理** + **策略下拉**（每 7 天 / 每 3 天 / 超过 1 GB / 自定义）
  + 自定义的两个数字（间隔天数、容量 MB）+ **立即清理**按钮；
* 清理由后台线程做（`CacheJanitor`），扫目录/删文件不卡主窗；
* 触发点：程序启动一次、开着时每 30 分钟巡检一次、改策略/开开关立即一次、按钮强制一次；
* 记账：每次跑完写 `cache_clean_last_ts`，间隔策略在同一时间段内不会重复跑（重启也一样）；
* 口径：间隔策略只删“N 天没用过”的条目；容量策略超限时从最久没用过的开始删，
  **永远保留最新一份**；只动 `cache/` 下的内容。
* “用过”按**读取**算：命中缓存打开一份日志时，`LogStore.open` 会调 `reader.touch_cache()`
  刷一下该缓存条目的 mtime；否则天天打开、但从不重解析的日志会静止在上次解析时间，
  被当成冷缓存删掉。实测：一份 30 天前解析的缓存，打开一次后 `used_at` 变成今天，
  3 天策略不再删它。

### 8.3 受影响的界面行为

- 缓存分组多一行：`[✓ 自动清理缓存] [策略▾] [天数] [MB]`，下方一行状态（开启后显示触发条件 + 上次清理时间）。
- “当前占用”改成 `当前占用 X（N 份缓存 / M 个文件）`，清理完成后自动刷新。
- 非“自定义”策略下两个数字框置灰（不用也就不容易改错）。
- 设置页仍然无滑动条：只多了 1 行（842 → 855 px），宽 1196 → 1262。

### 8.4 验证结果

- 新增 `tests/test_cache_cleanup.py`：**28 条**（扫描/按天/按容量/保留最新/触发条件/持久化/
  界面/主窗启动/“读取刷新使用时间”）全过。
- 全量回归：**1011 passed / 3 skipped / 23 subtests**。
- 五项启冒烟（真实 103.6 MB 日志）：全绿。
- 截图：`selftest/settings_cache_auto.png`。

---

## 9. 0.1.1 发布

### 9.1 本版包含

| 块 | 内容 | 对应章节 |
| --- | --- | --- |
| 真机协议修正 | MAVFTP 四个协议 bug（载荷被写坏、`<HBBBBBBI` 头、ACK/NAK、读请求长度字段、`session=0`）+ 路径映射 `id` 即编号 + burst 跟流/补洞/残留会话清理 | §6 |
| U 盘模式护栏 | `246/param1=5`：armed 先拦截（一个包不发）、二次确认四要素、ACK/超时提示映射 | §6.4 |
| 切页性能 | 分域指纹 + 泳道原地换数据 + 事件/区间签名去重 + 重建期间关绘制 + 背景缓存：首次进曲线 1411 → **563 ms**，重重建 932 → **453 ms** | §7 |
| 缓存自动清理 | 每 3/7 天、超 1 GB、自定义；启动 + 每 30 分钟 + 改策略即时触发；保留最新一份；`touch_cache` 让“读一次”也算使用 | §8 |

### 9.2 验证

- 全量：**1011 passed / 3 skipped / 23 subtests**。
- 五项启冒烟扩到 **八项**（新增：缓存自动清理 / MAVFTP 回环下载 / U 盘护栏），并对打包版跑过。
- 真机（COM94）：同一份 1,148,993 B 日志，MAVFTP 79.6 KB/s、`LOG_*` 57.0 KB/s、自动回退
  56.8 KB/s，三者 sha256 完全一致。
- **未做**：真正发 `246/param1=5` 重启进 MSD（会断链、需断电重启），待用户确认。

### 9.3 产物

`release/APMLogViewer-0.1.1-win64.zip`、`release/APMLogViewer-0.1.1-win64-setup.exe`，
sha256 见同目录 `*.sha256`；`release/build_commits.txt` 记录本次构建对应提交。

---

## 10. 曲线页新增 6 组曲线（罗盘 / IMU / PID / 供电 / EKF / 遥控）

### 10.1 问题背景

曲线页原来只有 6 个标配预设（位置与高度、电机与执行器、电量、震动、姿态与控制、GNSS 与 EKF），
罗盘、原始 IMU、PID 分量、供电温度、EKF 位置、遥控通道这些常用量得去通道树一条条手加。

### 10.2 改法

只给 `PRESETS` 加 6 个 `Preset` 条目，取数/绘制/胶囊/叠加全部走现有链路：

| 预设 | 数据来源 | 道数 | 说明 |
| --- | --- | --- | --- |
| 罗盘（磁力计） | `MAG.MagX/Y/Z`(inst 0) + `MAG[1].MagX`(前缀“罗盘2”) | 4 | 双罗盘对比 |
| 陀螺与加计 | `IMU.GyrX/Y/Z` + `IMU.AccX/Y/Z` | 6 | 最小实例 |
| PID 分量 | `PIDR.Act` + `PIDR.Tar`(虚线、默认收) + `P/D/FF` | 4 | 实际 vs 目标 |
| 供电与温度 | `POWR.Vcc`、`MCU.MVolt`、`MCU.MTemp`、`BAT.Temp` | 4 | 两条 band |
| EKF 位置与速度 | `XKF1.PN/PE/PD`、`XKF1.VN/VE/VD` | 6 | EKF 输出 |
| 遥控输入 | `RCIN.C1..C4` | 4 | 固定 1000–2000 µs |

### 10.3 受影响的界面行为

- 曲线页的「预设」下拉从 6 项变 12 项，选到新项即按新组合重建泳道；选中状态照旧存进 `views.json`
  （实测：选「罗盘」→ 关窗 → 重开，仍是罗盘、仍是 4 道）。
- 新泳道与老泳道完全同构：顶部胶囊可单独显隐（PID 的“目标”默认收起）、右键菜单、双击 Y 轴、
  叠加绘制、缩放重取数都照旧。
- 老预设的通道组合、颜色、阈值、band **一个都没改**（单测快照钉住）。
- 页面最小宽仍为 **910 px**，左侧栏拖动仍可拉到 620（未回归 §29）。

### 10.4 验证结果

- 新增 `tests/test_presets_extra.py`：**14 条**（注册表/老预设快照/6 组泳道与键/缺通道跳过/
  预设框 12 项/选完落盘并重开恢复/SeriesPlot 真建泳道与胶囊）全过。
- 全量回归：**1025 passed / 3 skipped / 23 subtests**。
- 真实 103.6 MB 日志：`--preset mag` → 4 道；`--preset pid` → 4 道；默认 `vibe` 仍 5 道，
  八项启冒烟全绿（含缓存清理 / MAVFTP / U 盘护栏）。
- 截图：`selftest/preset_mag.png`、`preset_imu.png`、`preset_pid.png`、`preset_power.png`、
  `preset_ekf.png`、`preset_rc.png`。

---

## 11. 地图页新增「轨迹差异」对比（2026-10-31）

### 11.1 需求与做法

地图同时显示多条航迹时，用户想知道不同定位设备（VIO/VISP 与 GPS/POS）到底差多少。
现在在右上角**轨迹勾选栏正下方**多一个「轨迹差异」面板：逐对列百分比 + 总体百分比，
勾选变化后立即刷新；口径与基准写在面板脚注里（不只放 tooltip）。

### 11.2 指标定义

* 同一时刻水平偏差：`d(t) = |A(t) − B(t)|`（共同原点 ENU）；
* 对齐：以采样稀的一条为时间基准，另一条线性插值，空档 > 2 s / 跳变 > 200 m/s 丢弃；
* **单航线差异%** = `mean(d) ÷ L × 100`，`L` = 重叠段两条轨迹里程的平均值；
* **总体差异%** = `Σmean(d) ÷ ΣL × 100`（按里程加权）。

不含高程；不跨日志；里程 < 100 m 不给百分比。

### 11.3 真机实测（103.6 MB，Copter 4.6.3）

三条轨迹：POS 39 780 点 / 10 Hz、GPS#0 19 889 点 / 5 Hz、XKF1 39 780 点 / 10 Hz，
重叠 3988 s。

| 轨迹对 | 均值 | P95 | 最大 | 基准里程 | 单航线差异% |
| --- | --- | --- | --- | --- | --- |
| POS ↔ GPS#0 | 0.79 m | 1.19 m | 1.43 m | 24.09 km | 0.0033% |
| POS ↔ XKF1 | 0.83 m | 1.36 m | 1.39 m | 24.09 km | 0.0034% |
| GPS#0 ↔ XKF1 | 1.04 m | 2.04 m | 2.39 m | 24.09 km | 0.0043% |
| **总体（3 对）** | Σ2.66 m | — | — | Σ72.27 km | **0.0037%** |

取消 XKF1 勾选后面板立即变为 1 对、总体 0.0033%（与手算一致）。

### 11.4 验证

- 新增 `tests/test_trackdiff.py`（18 条）+ `tests/test_trackdiff_ui.py`（8 条）：
  几何/双原点纠正/采样率差/无重叠/混坐标系/空档丢弃/短里程/加权总体/格式化，
  以及面板位置（在勾选栏下方、右对齐）、勾选同步、提示态、缓存复用。
- 相关回归：`test_multitrack_ui`/`test_trackmap_local`/`test_track_follow`/`test_tracks`/
  `test_track_perf`/`test_playback`/`test_timeline_click`/`test_left_pane` 共 **195 passed**。
- 全量回归：见本次回报。
- 截图：`selftest/trackdiff_all.png`（3 对）、`selftest/trackdiff_pair.png`（1 对）。

---

## 12. 0.1.2 发布（2026-11-05）

### 12.1 版本与内容

* 版本号：**0.1.2**（`apmlog.__version__` 与 `packaging/installer.iss` 同步）；
* 曲线页：预设 **6 → 12 组**，新增罗盘、陀螺与加计、PID 分量、供电与温度、
  EKF 位置与速度、遥控输入（口径见 §35 / CHANGELOG §10）；
* 地图页：右上角轨迹勾选栏**下方**新增「轨迹差异」面板，逐对 + 总体偏差百分比，
  勾选变化即时刷新（口径见 CONTRACT §36 / CHANGELOG §11）；
* 版本号、README 功能列表同步更新。

### 12.2 验证

* 全量回归：**1053 passed / 1 skipped / 23 subtests**（含多轨迹勾选、播放头跟随、
  缩放、左栏、切页性能等既有交互）。
* 打包版自检（103.6 MB 真机日志）：退出码 0，加载 **1.59 s**；八项冒烟全通过；
  地图页截图可见差异面板：`POS↔GPS#0 0.0033%`、`POS↔XKF1 0.0034%`、
  `GPS#0↔XKF1 0.0043%`、`总体（3 对）0.0037%`。
* 安装版（静默安装到临时目录）自检：退出码 0，加载 1.87 s，八项冒烟全通过，
  363 个文件、exe sha256 与绿色包一致；卸载后目录清空。
* 包内核对：`apmlog.core.trackdiff` / `apmlog.ui.trackdiffpanel` 均在 PYZ 中，
  预设 id 12 个齐全，版本常量 `0.1.2`。

### 12.3 产物

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `APMLogViewer-0.1.2-win64-setup.exe` | 45,110,170 B | `71cc34fa94bc8fad…` |
| `APMLogViewer-0.1.2-win64.zip` | 65,131,447 B | `d7d4080f3813abbe…` |

构建提交：`FlightTune/APMLogViewer@apmlog-viewer = d8ac7c9`、
`APMLogViewer-repo@main = 398ea99`（详见 `release/build_commits.txt`）。

### 12.4 升级说明

* 旧版 0.1.1 安装（`%LOCALAPPDATA%\Programs\APMLogViewer`）已卸载；
  安装包沿用同一 `AppId`，可直接覆盖升级；
* `views.json` 兼容：新增预设只是新的字符串 id，老的视图记忆照常读；
* 轨迹差异只在**同一份日志内**比较，不含高程（基准不同），详见 CONTRACT §36。

---

## 13. Ubuntu 24.04 适配：`.deb` 打包（2026-11-06）

### 13.1 产物

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `APMLogViewer-0.1.2-amd64.deb` | 63,196,744 B | `2f37f512032cecb61676e92892fde65879d7b68fca7e4084711423bcd0e374b8` |

安装后占用约 224 MB（`Installed-Size: 228572` KB），内置 Python 3.12 + Qt 6.12 运行库。

### 13.2 适配改动

* `packaging/apmlogviewer-linux.spec`：Linux onedir 规格（与 Windows 同一套 datas /
  hiddenimports，`console=True` 让 `--selftest` 输出进终端；新增 `trackdiff` /
  `trackdiffpanel` 的显式 hiddenimports）；
* `packaging/build_deb.sh`：PyInstaller → 组装 `/opt/apmlogviewer`、`/usr/bin/apmlogviewer`、
  `.desktop`、hicolor 图标、`DEBIAN/{control,postinst,postrm}` → `dpkg-deb --root-owner-group`；
* `packaging/apmlogviewer.png`：从 `icon.ico` 的 256×256 帧原样取出（不是放大 16×16）；
* `docs/UBUNTU.md`：依赖、构建、安装/卸载、自检、与 Windows 差异、已知限制。

### 13.3 验证

* 解包核对（`dpkg-deb -x` + PYZ）：`apmlog.core.trackdiff`、`apmlog.ui.trackdiffpanel`
  均在；`apmlog` 版本常量 `0.1.2`；预设 id 12 个齐全（含 `mag/imu/pid/power/ekf/rc`）。
* 安装：`dpkg --root=<临时根> --force-depends -i` → `Status: install ok installed`；
  `/usr/bin/apmlogviewer` 符号链接、`.desktop`、`/opt/apmlogviewer` 均按预期落位。
* 启动自检（安装目录里的可执行文件，offscreen）：**EXIT=0**，日志无 traceback；
  加载 103.6 MB 真机日志 **25.45 s**（冷缓存 + 虚拟机磁盘），71 类报文 / 2 619 459 条 /
  1331 参数 / 643 事件 / 32 告警；八项冒烟全通过。
* 地图页截图：轨迹勾选栏下方「轨迹差异」面板可见，
  `POS↔GPS#0 0.0033%`、`POS↔XKF1 0.0034%`、`GPS#0↔XKF1 0.0043%`、
  `总体（3 对）0.0037%`，与 Windows 版同机同日志一致。

### 13.4 限制

* 构建机账号无免密 sudo → 未做真实系统级 `dpkg -i`（用假根安装 + 直接运行验证）；
* 包未签名；`libxcb-cursor0` 等 xcb 依赖由 apt 补齐；
* 串口下载需 `dialout` 组；MAVFTP / U 盘模式未在 Linux 真机上验证（仅 FakeFC 回环）。

---

## 14. 环境重建与一致性复核（2026-11-06）

### 14.1 `.venv` 重建

`workspace/apmlog/.venv` 被外部清理后重建：Python 3.14.7，按 `requirements.txt` 锁定版本
（PySide6-Essentials 6.12.0 / shiboken6 6.12.0、pyqtgraph 0.14.0、pymavlink 2.4.50、
pyserial 3.5、numpy 2.5.4、duckdb 1.5.6、PyYAML 6.0.3），另装测试所需的
pytest 9.2.0 + pytest-subtests 0.15.0；**未引入清单外的新依赖**。

### 14.2 全量回归

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests -q
```

**1051 passed / 3 skipped / 23 subtests passed（8 分 38 秒）**，与文档改动前的
1051 / 3 / 23 完全一致——本次改动只有 `README.md`、`docs/RELEASE_NOTES_0.1.2.md`、
`docs/CHANGELOG.md` 等文本，没有触及代码。

### 14.3 八项自检：源码 vs 打包版

同一份 103.6 MB 真机日志（`2026-10-29 14-18-00.bin`）：

| 运行方式 | 退出码 | 加载耗时 | 冒烟总判定 |
| --- | --- | --- | --- |
| 源码（重建后的 venv） | 0 | 1.17 s | 通过 |
| 打包版（安装副本 exe sha256 `b0df567e…52b0`） | 0 | 1.25 s | 通过 |

两份报告逐行比对：固件 `ArduCopter V4.6.3`、71 类报文、2 619 459 条、1331 参数、
643 事件、32 告警（critical 0）、曲线通道 5 道、知识库计数、插件页、地图页、
缓存清理、MAVFTP、U 盘护栏、总判定**全部一致**；只有两处非代码差异——
「标签区最小宽」795（打包环境的字形度量）vs 910（源码环境），以及 MAVFTP
0.42 s vs 0.43 s 的计时抖动。

源码最后一次修改是 2026-11-05 17:30:56，早于打包时间 17:33:43，此后只改了文档。
说明：工作区的 `dist/` 已被环境清理，本次用
`%LOCALAPPDATA%\Programs\APMLogViewer-0.1.2` 的安装副本（与 Release 的 zip/setup
同一次构建）代表打包版。
