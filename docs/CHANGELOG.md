# APMLogViewer 变更日志

本文件按改动分节记录：**问题背景 → 改法 → 受影响的界面行为 → 验证结果**。
每条都写清"为什么改"和"怎么验的"，验证一律用真实日志、真实第三方插件和可复现的命令。

- 工程：APMLogViewer（ArduCopter DataFlash `.BIN` 日志分析桌面端）
- 版本：0.1.0 ｜ Copter 4.6.x / 4.7.x
- 记录范围：`2026-10-28` ～ `2026-10-29` 两次改动

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
