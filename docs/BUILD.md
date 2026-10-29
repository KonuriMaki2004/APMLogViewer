# APMLogViewer 免安装打包（PyInstaller onedir）

> 本文只描述 `packaging/` + `run_gui.py` 这层打包壳，不改内核。
> 所有数字都是 2026-10-17 在本机（Windows 11 26200 / x64 / venv Python 3.14.7 /
> PySide6-Essentials 6.12.0 / pyqtgraph 0.14.0 / numpy 2.5.4 / pymavlink 2.4.50 /
> PyYAML 6.0.3 / pyserial 3.5 / PyInstaller 6.22.3）实测得到的。

## 1. 产物

| 项 | 值 |
| --- | --- |
| 目录 | `dist/APMLogViewer/`（**onedir**，不是 onefile） |
| 入口 | `dist/APMLogViewer/APMLogViewer.exe`，7.71 MiB（8 088 442 B） |
| 整包 | 155.1 MiB（162 672 410 B，362 个文件） |
| 构建耗时 | 111.4 s（`packaging/build.py build`，含图标生成；首次冷缓存约 100–115 s） |
| 图标 | `packaging/icon.ico`（本仓库自绘：俯视四轴 + 震动波形，7 档尺寸 16…256，45 106 B）；已用 pefile 确认 exe 里 `RT_ICON(3)` + `RT_GROUP_ICON(14)` 都在 |

> 本节的数字与下面所有验收数字对应**内核指纹 `8897b181ed8914da`**（`apmlog/**/*.py` 的
> mtime+size 归一哈希，构建于 15:33:05–15:34:55）。内核在动，改过任何 `apmlog/` 文件后
> 必须先重建再取数（见 9 节“重建纪律”）。

为什么 onedir：GUI + PySide6 + numpy 用 onefile 每次启动要解包 ~150 MB 到 `%TEMP%`，
冷启动慢一个数量级且容易被杀软盯上。

## 2. 怎么构建

```powershell
cd <工程根>
.\.venv\Scripts\python.exe packaging\build.py all      # clean + build + report
# 或分步：
.\.venv\Scripts\python.exe packaging\build.py icon     # 只重画图标
.\.venv\Scripts\python.exe packaging\build.py clean    # 清 build/ 与 dist/
.\.venv\Scripts\python.exe packaging\build.py build    # PyInstaller（日志进 packaging/logs/）
.\.venv\Scripts\python.exe packaging\build.py report   # 体积 / 数据文件 / 图标资源 / 警告数
.\.venv\Scripts\python.exe packaging\build.py run   --log <BIN> --png <out.png> --platform offscreen|windows
.\.venv\Scripts\python.exe packaging\build.py smoke --rounds 3   # 连开连关看退出码
.\.venv\Scripts\python.exe packaging\build.py gui   --log <BIN> --expect-in-title <子串>   # 真实窗口+标题+干净退出
.\.venv\Scripts\python.exe packaging\build.py gui   --log <BIN> --profile <空目录>          # 模拟新机器用户目录
```

必须用 `.\.venv\Scripts\python.exe`（裸 `python` 会卡在 stdin）。
本任务唯一新增的依赖是 `pyinstaller`（`pip install pyinstaller`，已装 6.22.3）。

## 3. 用法（同一个 exe）

```powershell
dist\APMLogViewer\APMLogViewer.exe                       # 图形界面（空主窗）
dist\APMLogViewer\APMLogViewer.exe D:\logs\00000165.BIN   # 直接带日志启动，窗口会显示且标题带上文件名
dist\APMLogViewer\APMLogViewer.exe --selftest X.BIN --preset vibe --png out.png
dist\APMLogViewer\APMLogViewer.exe cli stats X.BIN       # 转调 apmlog.cli（scan/stats/channels/series/events/alerts/track/link）
dist\APMLogViewer\APMLogViewer.exe cli link demo --src X.BIN --out D:\tmp\demo
```

* `console=False`，双击不闪黑框。带参数启动时入口脚本会 `AttachConsole(ATTACH_PARENT_PROCESS)`
  挂回调用者控制台并 `SetConsoleOutputCP(65001)`，所以 CLI/selftest 的 stdout 能正常看到；
  挂不上（资源管理器双击）就安静跳过，**不会** AllocConsole。
* `APMLOG_LOGFILE=<path>`：把 stdout/stderr 同时 tee 到文件（自动化验收取真实输出用）。
* `APMLOG_NO_CONSOLE=1` 禁止附加控制台；`APMLOG_ATTACH_CONSOLE=1` 强制附加；
  `APMLOG_PKG_DEBUG=1` 打印 runtime hook 的数据文件定位诊断（走 stderr）。
* 没有做第二个 `APMLogCLI.exe`：按“二选一”选了 exe 参数路线，省一遍 Analysis（构建时间对半）。

## 4. 数据文件与路径解析（关键）

内核按源码相对路径找数据，**frozen 下算出的位置正好是 onedir 的 `_internal/`**，所以不需要改内核：

| 内核位置 | 解析式 | frozen 实际解析到 |
| --- | --- | --- |
| `apmlog/core/alerts.py:25` | `Path(__file__).resolve().parents[2]/"config"/"alerts.yaml"` | `dist/APMLogViewer/_internal/config/alerts.yaml` |
| `apmlog/core/codebooks.py:43` | `Path(__file__).resolve().parents[2]/"data"/"codebooks"/"copter_4x.json"` | `dist/APMLogViewer/_internal/data/codebooks/copter_4x.json` |

`APMLOG_PKG_DEBUG=1` 的实测输出（runtime hook 用同一套算法校验）：

```
[apmlog-paths] frozen=True _MEIPASS=...\dist\APMLogViewer\_internal exe=...\APMLogViewer.exe
[apmlog-paths] OK       config/alerts.yaml -> ...\_\internal\config\alerts.yaml
[apmlog-paths] OK       data/codebooks/copter_4x.json -> ...\_\internal\data\codebooks\copter_4x.json
```

两个文件与源文件 sha256 一致（`copter_4x.json` = `88e48748…`，`alerts.yaml` = `34f732d4…`）。
整目录拷到别处（实测拷到 `%TEMP%\APMLogViewer_rel`）后 `_MEIPASS` 跟着走，仍然 `OK`。
runtime hook 还带一个兜底：万一将来内核改了路径层级，它会在 bundle 内按文件名搜到并复制过去
（只写安装目录内，绝不写用户日志目录），并在 `APMLOG_PKG_DEBUG=1` 时打 `REPAIRED`。

## 5. 隐藏导入 / 排除项（都实测过）

**必须显式列出的**（静态分析看不到）：

* `pymavlink.dialects.v20.ardupilotmega`（2.3 MB 生成的纯 py 方言）+ `v10.all`、
  `DFReader`、`mavutil`、`mavexpression`、`generator.mavcrc/mavparse`、`dfindexer`（.pyd）。
* `PySide6.QtOpenGL` / `PySide6.QtOpenGLWidgets` —— **踩过坑**：
  `pyqtgraph/Qt/OpenGLHelpers.py` 用 `importlib.import_module(f"{QT_LIB}.QtOpenGL")` 硬导入且无
  try/except，`PlotCurveItem` 顶层就会拉。把它们放进 `excludes` 后 exe 在 `import pyqtgraph`
  阶段直接 `ModuleNotFoundError: No module named 'PySide6.QtOpenGL'`（已回滚为显式 hiddenimport）。
  代价：`QtOpenGL.pyd` 8.28 MiB + `opengl32sw.dll` 19.68 MiB（合计约 28 MiB）。
* `apmlog.link.fakes`、`apmlog.link.audit` —— GUI 的“演示飞控”（`LinkPanel._do_fake`）与
  `cli link demo` 在函数里 import；不加时构建日志直接报
  `invalid module named apmlog.link.fakes` 且 `PYZ-00.toc` 里确实没有（已补）。
* `serial.tools.list_ports*`、`yaml._yaml`/`yaml.cyaml`、`pyqtgraph` 的 `*_generic` 模板与
  `multiprocess.bootstrap`、`shiboken6`、`PySide6.support.deprecated`。
* Qt 插件由 PySide6 钩子自动收：`platforms/qwindows.dll`、`qoffscreen.dll`（离屏验收要用）、
  `styles/qmodernwindowsstyle.dll`、`imageformats/*`（含 `qjpeg`，瓦片解码要用）、
  `networkinformation/qnetworklistmanager.dll`、`tls/*`（HTTPS 瓦片必需）。

**排除掉且验证安全的**：`QtQml/QtQuick/Qt3D/QtWebEngine/QtPdf/QtCharts/QtSql/QtSerialPort/
QtBluetooth/QtTest/QtDBus/QtHelp/QtUiTools/QtDesigner/QtXml/...` 以及
`QtSvg/QtSvgWidgets/QtPrintSupport`。
QtSvg 那条单独验过：用 meta-path 屏蔽 `PySide6.QtSvg/QtPrintSupport/QtTest` 后跑源码，
`import pyqtgraph` → `PlotItem.getContextMenus()`（右键菜单）→ `import pyqtgraph.exporters` →
`grab().save(png)` 全部通过，所以曲线导 PNG（走 `QPixmap.grab`）不依赖 QtSvg，排除保留。
`matplotlib/scipy/pandas/numba/duckdb/tkinter/unittest` 一并排除（本 venv 里 duckdb 根本没被内核引用）。

**故意不收**：`apmlog.batch`（目录批量体检）。它用 `ProcessPoolExecutor`，freeze 后需要入口调
`multiprocessing.freeze_support()` 并由 exe 自己拉 worker，属于行为改动而不是打包配置，超出本次范围；
批量体检继续走源码 `python -m apmlog.batch <目录>`。

**没做的进一步瘦身**（收益/风险不对称，留着）：`lxml` 6.8 MiB（`DFReader.metadata_tree()` 会用到，
虽在 try/except 里）、`libcrypto-3.dll` + `libcrypto-3-x64.dll` 两份（CPython `_ssl` 与 Qt TLS 各一份）、
`opengl32sw.dll` 19.68 MiB（无 GPU 驱动/RDP 机器的 OpenGL 软件回退，去掉省 13% 但换机风险不可测）。

## 6. 验收实测（三份真日志，只读）

日志：`00000165.BIN`（无 GPS、VIO 本地轨迹、炸机）、`00000170.BIN`、
`Mission Planner/…/2026-10-16 16-52-18.bin`（有 GPS，真实瓦片）。
源码模式与产物模式各用**独立且同样从空开始**的缓存目录，保证“冷”可比。

### 6.1 启动与加载耗时（并排）

| 场景 | 源码 | 产物 exe |
| --- | --- | --- |
| 纯 bootstrap（`--help`，不建窗不读日志） | 1.18 / 1.18 / 1.16 s | **0.87 / 0.87 / 0.93 s** |
| selftest 冷缓存 165 | 10.48 s（解析 2.77 s） | 9.91 s（解析 2.65 s） |
| selftest 冷缓存 170 | 10.33 s（1.90 s） | 8.61 s（1.65 s） |
| selftest 冷缓存 GPS | 10.94 s（2.22 s） | 9.67 s（2.37 s） |
| selftest 暖缓存 165 | 9.27 s（0.39 s） | **7.53 s（0.32 s）** |
| selftest 暖缓存 170 | 9.21 s（0.36 s） | 7.91 s（0.43 s） |
| selftest 暖缓存 GPS | 9.33 s（0.39 s） | 7.85 s（0.41 s） |
| `QT_QPA_PLATFORM=offscreen` 暖缓存 | — | 6.57 / 6.32 / 6.69 s |
| 带日志启动到窗口标题正确（真实 windows） | — | 7.25 / 8.27 s（含 6 s 停留） |
| `cli events` / `alerts` / `stats`（暖缓存） | — | 0.49 / 0.51 / 0.44 s |

selftest 的墙钟里固定含 4.0 s + 0.6 s 的事件泵等待（`app.py::_pump`），所以“启动+加载+渲染”
的真实开销 ≈ 墙钟 − 4.6 s。exe 全程不比源码慢，多数场景还快 10–20%（字节码已冻结、少一轮
`__pycache__` 查找）。**注意**：这里的“冷启动”指进程级冷启动；系统文件缓存没清（清 `StandbyList`
要管理员），所以磁盘冷读未单独量化。

### 6.2 结果一致性（源码 vs 产物，必须逐项相同）

| 日志 | 告警 | critical | 事件 | 未映射 | 位置判定 | 炸机线索 | rc |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 165 源码 | 6 | 3 | 39 | 0 | `kind=local source=XKF1` | `322.45s Crash: Disarming: AngErr=171>30, Accel=0.0<3.0［同刻 ARM, ERR, EV］` | 0 |
| 165 产物(offscreen) | 6 | 3 | 39 | 0 | 同上 | 同上 | 0 |
| 165 产物(windows) | 6 | 3 | 39 | 0 | 同上 | 同上 | 0 |
| 170 源码 / 产物 | 2 | 0 | 36 | 0 | `local / XKF1` | 无 | 0 |
| GPS 源码 / 产物 | 1 | 0 | 36 | 0 | `geodetic / POS` | 无 | 0 |

* 码表生效：`APMLogViewer.exe cli --quiet --cache <同目录> events 165.BIN` 的输出与源码模式
  **内容逐字节相同**（40 行，`共 39 条`，归一化行尾后 `diff` 无差异；唯一区别是源码那次走
  控制台重定向落成 CRLF、产物 tee 文件是 LF），`未映射` 出现 **0 次**；`EKF_ALT_RESET`、
  `AUTO_ARMED`、`MOTORS_INTERLOCK_DISABLED` 等 EV 名与 `ALT_HOLD/LOITER` 模式名都正常解出。
* 曲线通道：两种模式都是 5 道 `VIBE[0].VibeX/Y/Z、VIBE[0].Clip、陀螺高频IMU[0].GHz`。
* PNG：`--png` 与 `_map.png` 都生成且非空（离线主窗 80 503 B、离线轨迹页 58 443 B）。
  GPS 日志同一暖缓存下连跑 3 源码 + 3 产物，**6 张 PNG sha256 全等**（`ee0d2b13c7…`）。
* **PNG 哈希不能单独当判据（实测结论）**：165 的窗口截图在**源码模式自己重复跑**也会变
  （3 次 3 个哈希），差异区是地图目录输入框/日志下拉弹窗（x3–251 y26–251）与曲线道头
  悬停区（x876–1435 y106–124），属自截图时机对弹窗/悬停状态敏感，与打包无关。
  正确的等价性判据是：把跨模式差异像素集与“同模式重复跑”的抖动像素集做集合差 ——
  实测 **跨模式独有像素 = 0 / 10282**，即产物与源码的差异完全落在同模式自身的抖动范围内。
  （建议内核侧：`--selftest` 截图前 `popup.hide()` + 固定焦点，回归比对才能用单张哈希。）
* 离屏 PNG 里中文是方框属正常（`offscreen` 平台无字体库）；真实 windows 平台的
  `selftest/pkg/exe_win_165.png` 中文、配色、告警标注、时间线全部正常。

### 6.3 关窗与退出码（含带日志启动的真窗验收）

`packaging/build.py gui --log <BIN> --expect-in-title <子串>`（真实 windows 平台，不起 offscreen）：

| 启动方式 | 窗口出现 | 标题实测值 | rc | 日志里 Traceback |
| --- | --- | --- | --- | --- |
| `APMLogViewer.exe "…/2026-10-16 16-52-18.bin"` | 是 | `APMLogViewer 0.1.0 —— 2026-10-16 16-52-18.bin` | 0 | 无 |
| `APMLogViewer.exe "…/测试3/日志/00000165.BIN"` | 是 | `APMLogViewer 0.1.0 —— 00000165.BIN` | 0 | 无 |

（标题含空格与中文路径都正确；进程自己退出，没靠 `kill` 兼退出。）

`packaging/build.py smoke`（起进程 → 等主窗 → `PostMessage(WM_CLOSE)` → 等退出）：

* 不带日志 3 轮：`rc=0` ×3，无 `killed`，单轮 8.24 / 8.21 / 8.27 s。
* **带日志（GPS）3 轮：`rc=0` ×3**，单轮 8.29 / 8.60 / 8.28 s —— 这条路径会真的建地图控件，
  所以 `closeEvent → TrackMapWidget.close()` 是在“已加载日志 + 地图已初始化”的状态下跑通的。
* 另外 3 轮 `--selftest` GPS（每轮换新空瓦片缓存、退出时仍有在途瓦片请求）也全部 `rc=0`。

### 6.4 网络与离线降级

* 在线：`APMLOG_TILECACHE=<空目录>` 跑 GPS 日志，产物 exe 真的拉下 **40 张 ESRI 影像瓦片 / 551 360 B**
  并解码显示（轨迹页 PNG 里能看到卫星底图 + 红橙渐变航迹）→ 说明 `QtNetwork` + `tls` 插件在包内可用。
* 不可达源：本机没有管理员权限，不能改 hosts / 防火墙 / 网卡，所以用**整目录副本删掉
  `_internal/PySide6/plugins/tls`**（`qcertonlybackend.dll`/`qopensslbackend.dll`/`qschannelbackend.dll`
  三个，删后副本 154.5 MB；原产物未被动过）来制造不可达：
  `rc=0`、Traceback **0 次**、瓦片落盘 **0 张**、日志
  `WARNING apmlog.ui._tiles: 瓦片源 esri_imagery 连续失败 4 次（最近原因 UnknownNetworkError/HTTP0），
  暂停 60s 并降级为占位显示`（该 WARNING 出现 2 次），
  告警/事件/曲线结果与在线完全一致（`V4.7.0 (1511f271)`、`QUAD/H`、165.1 s、类型 62、
  报文 70756、参数 1118、`geodetic/POS`、告警 1（critical 0）、事件 36、5 道），PNG 照常产出。
  （试过用 `http_proxy/https_proxy=127.0.0.1:9` 掐网，Qt 的默认 `NoProxy` 不认环境变量代理，
  该次 39 张瓦片仍下载成功 → 结论：环境变量代理不是有效的离线手段，已改用删插件副本法。）

### 6.4b 不依赖开发机用户目录（首运验收）

用 `build.py gui --profile <空目录>` 把 `LOCALAPPDATA`/`APPDATA`/`USERPROFILE`/`HOME` 全部重定向到
一个刚创建的空目录，并且**不设** `APMLOG_CACHE`/`APMLOG_TILECACHE`：

* 结果：`rc=0`、无 Traceback、标题正确，共生成 114 个文件，全部落在
  `PROF\LocalAppData\APMLogViewer\{cache,tiles}`（其下 40 张瓦片）——即首运自动建目录，
  不读开发机的用户数据也能跑。
* 同一份干净目录下跑 `--selftest 165.BIN`：解析 2.88 s，告警 6（critical 3）、事件 39、
  位置 `local/XKF1`、炸机线索与曲线通道与开发机目录跑出的完全相同。
* `views.json` 的定位：它是**运行时用户数据**，路径由 `apmlog/app.py::_views_path()`
  = `reader.cache_root().parent / "views.json"` 算出，**不是 datas**；已确认
  `dist/APMLogViewer/_internal/` 下没有任何 `views.json`、`cache/`、`tiles/`、`*.BIN`（datas 只
  有 `config/alerts.yaml` 1 788 B 与 `data/codebooks/copter_4x.json` 52 094 B 两个文件）。
  `_restore_view()` 是“存在才读、读失败静默回退默认”，干净目录下实测不报错。
* 另注：`_save_view()` 只在用户动作（改预设/加通道/改参数过滤）时写，**不因关窗而写**，
  所以自动化跑完后干净目录里 `views.json` 仍是 0 个（这是内核现有行为，不是打包遗漏）。

### 6.5 缓存落点（不能写进 dist/ 或日志目录）

* 日志索引缓存：`%LOCALAPPDATA%\APMLogViewer\cache`（内核 `reader.cache_root()`，
  `APMLOG_CACHE`/`--cache` 可覆盖；Git-Bash 下 `LOCALAPPDATA` 为空时回退 `~\AppData\Local`）。
  实测跑完 `dist/` 的文件清单+大小 **零变化**，两份真日志目录（1939 个文件）也 **零变化**；
  日志目录只读，本轮所有 `--png` 输出都写到工程内 `selftest/pkg/` 或 TEMP。
* 瓦片缓存：`APMLOG_TILECACHE` 优先，否则 `%LOCALAPPDATA%\APMLogViewer\tiles`；
  **Git-Bash 里 `LOCALAPPDATA` 是空串** → `_tiles.default_cache_dir()` 回退到
  `~/.local/share/APMLogViewer/tiles`（同样是用户目录，不是 dist/，不是日志目录）。
  这是 `_tiles.py` 与 `reader.py` 回退策略不一致的**观察项**（不是打包引入的，源码模式一样），
  从 cmd/Explorer 正常启动时两者都落在 `%LOCALAPPDATA%`。
* 下载目录：`cli link` 的默认落点在用户目录（本次验收都显式传 `--out` 指到 TEMP）。

### 6.6 构建警告

`build/pyi/apmlogviewer/warn-apmlogviewer.txt` 共 265 行，值得看的只有这些，全部判定为无害：

* `excluded module named PySide6.QtSvg / QtTest / unittest` —— 主动排除的。
* `missing module named lxml_html_clean / cssselect / bs4 / html5lib` —— lxml.html 的可选后端。
* `missing module named wsproto / MAVProxy / line_profiler / 'pymavlink.mavuser' / imp` —— pymavlink 可选分支。
* `missing module named h5py / colorcet / 'scipy.signal' / 'matplotlib.*' / numba` —— pyqtgraph 可选依赖。
* `missing module named fcntl / termios / pwd / grp / posix / _posixshmem / 'System.IO'` —— POSIX/CLI 分支。
* `missing module named pyqtgraph.Qt.QtCore.QRectF …` —— pyqtgraph 运行时 `setattr` 复制属性，静态分析看不到。
* 补上 `apmlog.link.fakes` 后，`invalid module named apmlog.link.fakes` 这条已消失；
  现在整个文件里 **没有任何 `apmlog.*` 缺失条目**。

## 7. 换机部署清单

1. **拷贝方式**：整目录 `dist/APMLogViewer/` 原样复制即可（U 盘/共享盘/解压到任意路径都行）。
   已实测拷到 `%TEMP%\APMLogViewer_rel` 后 `_MEIPASS`、数据文件、结果全部正确。
   不要只拷 exe；`_internal/` 必须在它旁边、保持相对结构。路径里有中文/空格没问题
   （实测 `C:\Users\MAKI\Desktop\工作相关\…` 的日志能读）。
2. **系统要求**：Windows 10/11 **x64**（Qt 6.12 与 Python 3.14 都不支持 Win7/8.1 与 x86）。
   ARM64 未验证（需要在该架构上重新构建）。
3. **VC 运行库**：**不需要**单独装 VC redist —— 包内已带 `VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、
   `ucrtbase.dll` 以及 39 个 `api-ms-win-*` downlevel shim。目标机若连 UCRT 补丁都没有
   （Win10 1507 之前的裸系统），补 KB2999226 即可；Win10 1809+/Win11 自带。
4. **权限**：不需要管理员（spec 里 `uac_admin=False`）。写的是 `%LOCALAPPDATA%\APMLogViewer\`，
   普通用户目录。装到 `C:\Program Files\` 下反而会让“改 `_internal/config/alerts.yaml`”需要提权，
   建议放 `D:\tools\APMLogViewer\` 或用户目录。
5. **杀软**：onedir + 无自解压 + 无网络服务，行为面比 onefile 干净；若企业杀软按签名拦截，
   把整个目录加白（或给 exe 做代码签名，本仓库不做）。
6. **可选配置**（都是环境变量，不改包）：`APMLOG_CACHE`、`APMLOG_TILECACHE`、
   `APMLOG_LOGFILE`、`APMLOG_PKG_DEBUG`、`APMLOG_NO_CONSOLE`。
   阈值可直接编辑 `_internal/config/alerts.yaml`（onedir 下是普通文件，改完重启生效）。
7. **离线机器**：地图会自动降级为占位（见 6.4），其余功能不受影响；要预先缓存瓦片就在有网机器上
   跑一遍再把 `%LOCALAPPDATA%\APMLogViewer\tiles` 拷过去。
8. **首次运行**：Windows SmartScreen 可能提示“未知发布者”→“仍要运行”（无签名时的正常现象）。

## 8. 已知限制 / 没做成的事

* **`apmlog.batch` 未进产物**（原因见第 5 节）。
* **磁盘级冷启动未单独量化**（要清系统文件缓存，需管理员）。
* **产物模式没做 GUI 点击级回归**（右键菜单/导出 CSV 等交互只在源码模式下用屏蔽模块法验证过
  pyqtgraph 的可用性）。产物侧的交互覆盖到“窗口出现 + 日志加载 + 地图初始化 + 关窗退出”
  与 `cli link demo` 端到端：同一次 165.BIN 模拟链路下载，源码与产物都是
  `2 854 912 B / 31 722 帧 / 重试 0 / CRC 错 0`，下载件与源件 **sha1 均为
  `de913af14a4fd90957d1d114a0997f34e416d43e`**（两目录输出件 `cmp` 逐字节一致），
  产物 14.19 s vs 源码 13.86 s。
* 打包产物与 `build/`、`packaging/logs/` 都是可再生的，别提交进版本库。

## 9. 重建纪律（本轮真实踩过的坑）

1. **改完内核必须重建再取数**。本轮就遇到：产物构建于 15:08，而 `apmlog/core/reader.py`
   在 15:14 被改——当时所有“产物 vs 源码”数字其实是在拿旧字节码比新源码。
   自检命令（输出为空才是干净的）：
   ```powershell
   Get-ChildItem -Recurse apmlog -Filter *.py | Where-Object { $_.LastWriteTime -gt (Get-Item dist\APMLogViewer\APMLogViewer.exe).LastWriteTime }
   ```
2. **冻结应用的 traceback 会“说谎”**：`co_filename` 存的是构建机上的绝对源码路径，所以
   旧字节码报 `AttributeError: … 'AsNeeded'` 时，Python 的 `linecache` 会从磁盘读到**已经改过**
   的那一行，看起来像“源码里明明写的是 `ScrollBarAsNeeded` 却报错”。遇到自相矛盾的
   traceback 先对比 mtime 与构建时间，再怀疑代码。（目标机上没这个源码文件，反而只会
   显示行号不显示源码，不会误导。）
3. **打包壳未变时不必改 spec**：本轮新增的只是 `build.py` 的 `gui` 子命令与文档；
   spec 的 `hiddenimports`/`excludes` 经实测对当前内核仍成立（新增代码未引入新第三方依赖，
   `seriesplot` 的 CSV 导出只用标准库 `csv` + 已有 `QtWidgets`）。
4. 本轮因清理进程而中断的跑：`packaging/logs/smoke-*.txt` 里 0 字节的文件是**正常现象**
   （GUI 模式本身不写 stdout，tee 文件为空），不是被中断的证据；所有计入结论的跑
   都是看到了完整尾行（`PNG: … OK` / `共 39 条` / `rc=0`）的那次。

---

## 安装包（Inno Setup，Windows x64 安装版）

免安装 zip 之外，仓库另带一份**安装版**构建脚本 `packaging/installer.iss`：
装到 `%LOCALAPPDATA%\Programs\APMLogViewer`（不需要管理员）、带开始菜单/可选桌面快捷方式、
带标准卸载项；装的就是完整 onedir 包（`_internal/data/knowledge`、`_internal/data/codebooks`、
`_internal/config/alerts.yaml` 全在里面）。

### 构建

```powershell
# 1) 先出冻结包
.venv\Scripts\python.exe packaging\build.py build

# 2) 再压安装包（Inno Setup 6，ISCC 在用户目录）
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
# 产物：release\APMLogViewer-<版本>-win64-setup.exe
```

`packaging/ChineseSimplified.isl` 是安装界面的简体中文语言包（取自 jrsoftware/issrc，
Inno 6.5.0+ 格式），与 `installer.iss` 放在一起编译，不需要预装语言包。

### 静默安装 / 卸载（回归用）

```powershell
# 安装到默认位置
.\release\APMLogViewer-0.1.0-win64-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
    /LOG="%TEMP%\apmlog_install.log"

# 安装到指定目录
... /DIR="D:\tools\APMLogViewer"

# 卸载
& "$env:LOCALAPPDATA\Programs\APMLogViewer\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

安装后可用 `--selftest` 做装置冒烟（报告写在 PNG 同目录的 `*_selftest.txt`）：

```powershell
& "$env:LOCALAPPDATA\Programs\APMLogViewer\APMLogViewer.exe" --selftest "<日志.BIN>" --png "$env:TEMP\exe.png"
```

报告里应看到：`知识库: {...by_subsys: 31...}`、`知识库试匹配 ERR 12/1 → … 命=True`、
`码表: mode(2)=ALT_HOLD…`、`详情弹窗: 打开 2 个…`、`插件: …=loaded`、
`左侧栏拖动: … 可拉大`。

### 目标平台

只有 **Windows 10/11 x64**。PyInstaller 不能交叉编译，Linux/macOS 安装包必须在对应系统上
构建（Qt 6.12 + Python 3.14 的 PySide6 轮子也按平台分发）；本项目当前没有 Linux/macOS 构建机，
所以发布只含 Windows 安装包。
