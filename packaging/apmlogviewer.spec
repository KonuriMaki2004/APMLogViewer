# -*- mode: python ; coding: utf-8 -*-
"""APMLogViewer —— PyInstaller **onedir** 打包规格（免安装版，Windows x64）。

为什么 onedir 不是 onefile：本程序是 PySide6 + numpy + pymavlink 的 GUI，onefile 每次
启动都要把 ~200MB 解到 %TEMP% 再执行，冷启动慢一个数量级，且极易被杀软当成可疑行为。

产物：``dist/APMLogViewer/APMLogViewer.exe`` + ``dist/APMLogViewer/_internal/``（整目录拷贝即可换机）。

用 ``packaging/build.py`` 调用本 spec（会先清缓存、后校验产物），别手敲 pyinstaller 命令。
"""
import os
from pathlib import Path

# ---------------------------------------------------------------- 路径基准 ----
# SPECPATH = 本 spec 所在目录（packaging/），ROOT = 工程根。相对 datas 在不同
# CWD 下不可靠，一律用绝对路径。
PKG = Path(SPECPATH).resolve()
ROOT = PKG.parent
APP_NAME = "APMLogViewer"
ICON = PKG / "icon.ico"

CONFIG_YAML = ROOT / "config" / "alerts.yaml"
CODEBOOK_JSON = ROOT / "data" / "codebooks" / "copter_4x.json"
#: 事件/告警说明知识库（双击事件时弹出）；缺它只会退回内置兜底文案，不阻止打包
KNOWLEDGE_JSON = ROOT / "data" / "knowledge" / "event_help.json"

for _p in (CONFIG_YAML, CODEBOOK_JSON):
    if not _p.is_file():
        raise SystemExit(f"[spec] 缺少必需数据文件: {_p}")

# ---------------------------------------------------------------- 数据文件 ----
# 目标目录 = 内核 Path(__file__).resolve().parents[2] 在 frozen 下算出的 bundle 根
# （onedir 即 _internal/），保持与源码工程根一致的相对结构 config/ 与 data/codebooks/。
datas = [
    (str(CONFIG_YAML), "config"),
    (str(CODEBOOK_JSON), os.path.join("data", "codebooks")),
]
if KNOWLEDGE_JSON.is_file():
    datas.append((str(KNOWLEDGE_JSON), os.path.join("data", "knowledge")))
else:                                                   # 不允许默默缺数据：点名告警
    print(f"[spec] 警告：缺少知识库 {KNOWLEDGE_JSON}，打包后弹窗将只用内置兜底文案")

# -------------------------------------------------------------- 隐藏导入 ----
hiddenimports = [
    # 入口自己转调的两个主模块（函数内 import，显式兜底）
    "apmlog.app",
    "apmlog.cli",
    "apmlog.core.alerts",
    "apmlog.core.codebooks",
    "apmlog.core.events",
    "apmlog.core.model",
    "apmlog.core.reader",
    "apmlog.core.store",
    "apmlog.link.cli_main",
    "apmlog.link.download",
    "apmlog.ui.logconfig",
    "apmlog.scan",
    "apmlog.ui.scandialog",
    "apmlog.ui.tracklegend",
    "apmlog.ui.loading",
    "apmlog.paramexport",
    "apmlog.link.params",
    "apmlog.link.endpoint",
    "apmlog.link.transport",
    # 两个“函数内才 import”的模块，静态分析会漏（构建日志里直接报
    # “invalid module named apmlog.link.fakes”，且 PYZ-00.toc 里确实没有）：
    # • fakes —— GUI 的“演示飞控”（LinkPanel._do_fake）与 cli link demo 都要用；
    # • audit —— 只读审计护栏，工具脚本与链路诊断会用（纯 py，几十 KB）。
    "apmlog.link.fakes",
    "apmlog.link.audit",
    "apmlog.ui._decim",
    "apmlog.ui._font",
    "apmlog.ui._tilemath",
    "apmlog.ui._tiles",
    "apmlog.ui.linkpanel",
    "apmlog.ui.gauges",
    "apmlog.ui.presets",
    "apmlog.ui.seriesplot",
    "apmlog.ui.settings",
    "apmlog.ui.theme",
    "apmlog.ui.trackmap",
    # pymavlink：DFReader 走 FMT 自描述解析；v20/ardupilotmega 方言是生成的纯 py 大模块，
    # 只在函数里 import，最容易被漏，必须显式列出（漏了的征兆：加载 .BIN 直接 ImportError）。
    "pymavlink",
    "pymavlink.DFReader",
    "pymavlink.dfindexer",
    "pymavlink.mavexpression",
    "pymavlink.mavutil",
    "pymavlink.generator.mavcrc",
    "pymavlink.generator.mavparse",
    "pymavlink.dialects",
    "pymavlink.dialects.v10",
    "pymavlink.dialects.v10.all",
    "pymavlink.dialects.v20",
    "pymavlink.dialects.v20.ardupilotmega",
    # pyserial：link/cli 的串口分支是函数内 import；list_ports 用于端口枚举下拉
    "serial",
    "serial.serialutil",
    "serial.serialwin32",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.tools.list_ports_common",
    "serial.tools.list_ports_windows",
    # YAML 的 C 加速与纯 py 回退都要在
    "yaml",
    "yaml.cyaml",
    "yaml.safe_loader",
    "yaml._yaml",
    # Qt 运行期动态加载的部分
    "shiboken6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",   # 地图瓦片用 QNetworkAccessManager，绝不能排除
    # pyqtgraph/Qt/OpenGLHelpers.py 用 ``importlib.import_module(f"{QT_LIB}.QtOpenGL[Widgets]")``
    # 硬导入（无 try/except，PlotCurveItem 顶层就会拉），PyInstaller 静态分析看不到 f-string
    # 目标 → 必须显式列出。实测：把它们放进 excludes 后 exe 在 import pyqtgraph 时直接
    # ModuleNotFoundError: No module named 'PySide6.QtOpenGL'。
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.support.deprecated",
    # pyqtgraph 的 UI 模板（0.14 只有 *_generic 一套）+ 多进程 bootstrap（hook 也会做，双保险）
    "pyqtgraph",
    "pyqtgraph.Qt",
    "pyqtgraph.graphicsItems.PlotItem.plotConfigTemplate_generic",
    "pyqtgraph.graphicsItems.ViewBox.axisCtrlTemplate_generic",
    "pyqtgraph.GraphicsScene.exportDialogTemplate_generic",
    "pyqtgraph.multiprocess.bootstrap",
    "pyqtgraph.exporters",
    # 插件机制本体（外部插件从目录动态加载，静态分析看不到它）。
    # 插件自己 import 的第三方库不能靠这里，靠下面的“插件可选依赖”。
    "apmlog.plugins",
    "apmlog.plugins.api",
    "apmlog.plugins.manager",
    "apmlog.ui.plugindialog",
]

# ---------------------------------------------------- 插件可选科学计算依赖 ----
# 插件是外部文件，PyInstaller 静态分析看不到它们 import 什么。免安装版里如果缺库，
# 插件一导入就 ImportError —— 所以这里**预留**常用库：venv 里装了哪些就带哪些，
# 没装的安静跳过（不报错），并在同一处把它们从 excludes 里放出来。
# 用户要加别的库：``pip install`` 后重新 ``build.py build`` 即可，不用改代码。
def _has(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


OPTIONAL_PLUGIN_MODULES = [
    # 数值/信号处理
    "scipy", "scipy.signal", "scipy.stats", "scipy.interpolate", "scipy.optimize",
    "scipy.special", "scipy.ndimage", "scipy.spatial", "scipy.fft",
    "pandas", "pandas.io.formats.style",
    "sklearn", "sklearn.linear_model", "sklearn.preprocessing", "sklearn.cluster",
    "statsmodels", "pywt", "h5py", "numexpr", "numba", "numba.core",
    # 画图（插件自带图/报告时最常用；Agg 后端无显示器也能出图）
    "matplotlib", "matplotlib.backends.backend_agg", "PIL", "PIL.Image",
    # 表格/杂项
    "tabulate", "openpyxl",
]
# 故意不在预留名单里：
# * ``duckdb`` —— venv 里装了但宿主一行代码都没用，单独带进来就是 +30 MB；要用的
#   插件自己 ``pip install duckdb`` 后把它加进下面的列表再重打包。
# * ``tkinter`` / ``IPython`` / ``pytest`` —— GUI 程序用不到，继续排除。
hiddenimports += [m for m in OPTIONAL_PLUGIN_MODULES if _has(m)]
_OPT_ROOTS = {m.split(".")[0] for m in OPTIONAL_PLUGIN_MODULES}

# 故意**不收**的模块：``apmlog.batch``（目录批量体检）。
# 它用 ``ProcessPoolExecutor``，freeze 后需要入口调 ``multiprocessing.freeze_support()``
# 并靠自身 exe 重新拉 worker，属于行为改动而不是打包配置；本任务范围只要求 GUI + apmlog.cli，
# 所以 batch 继续走源码模式（``python -m apmlog.batch``）。

# ---------------------------------------------------------------- 排除项 ----
# 只为压体积。凡是不确定的都当测试处理：先排除，再实测；跑不动就回滚（见 docs/BUILD.md）。
excludes = [
    # 没装/用不到的第三方
    "matplotlib", "scipy", "pandas", "numba", "IPython", "pytest", "setuptools",
    "PIL", "tkinter", "unittest", "pydoc_data", "duckdb",
    # 其它 Qt 绑定，避免和 PySide6 混进来
    "PyQt5", "PyQt6", "PySide2",
    # PySide6 里确定不用的模块（地图走 QGraphicsView，不用 WebEngine/QML/Quick）
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialBus", "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtDesigner",
    "PySide6.QtXml", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtConcurrent",
    "PySide6.QtDBus", "PySide6.QtNetworkAuth", "PySide6.QtHttpServer",
    # QtSvg / QtPrintSupport / QtTest：pyqtgraph 只在 try/except ImportError 里要它们，
    # 曲线导出用的是 QPixmap.grab()+CSV，不走 SVGExporter/PrintExporter。
    # 已用“屏蔽这三个模块”的源码模拟实测：pyqtgraph 导入、PlotItem 右键菜单、
    # pyqtgraph.exporters 导入、grab().save(png) 全部通过 → 排除保留。
    # 注意 QtOpenGL/QtOpenGLWidgets **不能**排除（见上面 hiddenimports 的说明）。
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtPrintSupport",
]

# 插件要用的库已经装了就不许排除（否则 hiddenimports 与 excludes 自相矛盾，
# 实测结果是打包时静默丢掉整个包）。没法装的继续排除。
excludes = [e for e in excludes
            if not (e.split(".")[0] in _OPT_ROOTS and _has(e.split(".")[0]))]

# -------------------------------------------------------------- runtime hook --
runtime_hooks = [str(PKG / "runtime_hooks" / "apmlog_paths.py")]


def _prune(items, label):
    """把明显不需要的 Qt 插件目录/DLL 剔掉（并打印，方便复查剔错了什么）。"""
    drop_kw = (
        "plugins/designer",
        "plugins/qmltooling",
        "plugins/sqldrivers",
        "plugins/canbus",
        "plugins/scxmldatamodel",
        "plugins/texttospeech",
        "plugins/multimedia",
        "plugins/renderers",
        "plugins/webview",
        "plugins/vectorimageformats",
        "imageformats/qsvg", "imageformats/qpdf", "iconengines/qsvgicon",
        "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Charts", "Qt63D", "Qt6WebEngine",
        "Qt6SerialPort", "Qt6Sql", "Qt6Test", "Qt6Location", "Qt6Bluetooth",
        "Qt6Svg", "Qt6PrintSupport", "dxcompiler", "Qt6Vulkan", "qtquick",
    )
    kept, dropped = [], []
    for it in items:
        dest = str(it[0]).replace("\\", "/")
        if any(k.lower() in dest.lower() for k in drop_kw):
            dropped.append(dest)
        else:
            kept.append(it)
    print(f"[spec] prune({label}): keep={len(kept)} drop={len(dropped)}")
    for d in sorted(set(dropped)):
        print(f"[spec]   drop {d}")
    return kept


# ------------------------------------------------------------------ GUI ----
a = Analysis(
    [str(ROOT / "run_gui.py")],
    pathex=[str(ROOT)],
    binaries=None,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=runtime_hooks,
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
a.binaries = _prune(a.binaries, "gui.binaries")
a.datas = _prune(a.datas, "gui.datas")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=f"{APP_NAME}.exe",
    debug=False,
    strip=False,
    upx=False,
    console=False,             # 窗口程序；CLI/selftest 靠入口脚本附加父控制台
    disable_windowed_traceback=False,
    uac_admin=False,           # 不需要管理员：写的是 %LOCALAPPDATA%
    uac_uiaccess=False,
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
