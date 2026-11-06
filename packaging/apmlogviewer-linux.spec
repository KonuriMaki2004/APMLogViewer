# -*- mode: python ; coding: utf-8 -*-
"""APMLogViewer —— PyInstaller **onedir** 规格（Ubuntu / Linux x86_64）。

与 Windows 的 `apmlogviewer.spec` 保持同一套数据文件与隐藏导入，差异只在：
* ``console=True``：Linux 下 GUI 程序带控制台没有副作用，但 ``--selftest`` 的输出
  能直接进终端/日志（GUI 双击启动也不弹额外窗口）；
* 不适用 Windows 的 prune（Qt 插件目录结构不同），改为在 Analysis 的 excludes 里
  排除用不到的 Qt 模块；
* 产物 ``dist/APMLogViewer/APMLogViewer`` 由 ``packaging/build_deb.sh`` 收进 .deb。
"""
import os
from pathlib import Path

PKG = Path(SPECPATH).resolve()
ROOT = PKG.parent
APP_NAME = "APMLogViewer"

CONFIG_YAML = ROOT / "config" / "alerts.yaml"
CODEBOOK_JSON = ROOT / "data" / "codebooks" / "copter_4x.json"
KNOWLEDGE_JSON = ROOT / "data" / "knowledge" / "event_help.json"

for _p in (CONFIG_YAML, CODEBOOK_JSON):
    if not _p.is_file():
        raise SystemExit(f"[spec] 缺少必需数据文件: {_p}")

datas = [
    (str(CONFIG_YAML), "config"),
    (str(CODEBOOK_JSON), os.path.join("data", "codebooks")),
]
if KNOWLEDGE_JSON.is_file():
    datas.append((str(KNOWLEDGE_JSON), os.path.join("data", "knowledge")))

hiddenimports = [
    "apmlog.app",
    "apmlog.cli",
    "apmlog.core.alerts",
    "apmlog.core.codebooks",
    "apmlog.core.events",
    "apmlog.core.model",
    "apmlog.core.reader",
    "apmlog.core.store",
    "apmlog.core.trackdiff",
    "apmlog.link.cli_main",
    "apmlog.link.download",
    "apmlog.ui.logconfig",
    "apmlog.scan",
    "apmlog.ui.scandialog",
    "apmlog.ui.tracklegend",
    "apmlog.ui.trackdiffpanel",
    "apmlog.ui.loading",
    "apmlog.paramexport",
    "apmlog.link.params",
    "apmlog.link.endpoint",
    "apmlog.link.transport",
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
    "apmlog.ui.eventhelp",
    "apmlog.ui.modecolors",
    "apmlog.ui.overlaydialog",
    "apmlog.ui.plugindialog",
    "apmlog.ui._switch",
    "apmlog.ui._tiles",
    "apmlog.plugins",
    "apmlog.plugins.api",
    "apmlog.plugins.manager",
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
    "serial",
    "serial.serialutil",
    "serial.serialposix",
    "serial.tools",
    "serial.tools.list_ports",
    "yaml",
    "yaml.safe_loader",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "pyqtgraph",
    "pyqtgraph.Qt",
    "pyqtgraph.graphicsItems.PlotItem.plotConfigTemplate_generic",
    "pyqtgraph.graphicsItems.ViewBox.axisCtrlTemplate_generic",
    "pyqtgraph.GraphicsScene.exportDialogTemplate_generic",
    "pyqtgraph.multiprocess.bootstrap",
    "pyqtgraph.exporters",
]

excludes = [
    "matplotlib", "scipy", "pandas", "numba", "IPython", "pytest", "setuptools",
    "PIL", "tkinter", "unittest", "pydoc_data", "duckdb",
    "PyQt5", "PyQt6", "PySide2",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtGraphs", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer",
]

a = Analysis(
    [str(ROOT / "run_gui.py")],
    pathex=[str(ROOT)],
    binaries=None,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PKG / "runtime_hooks" / "apmlog_paths.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=True,              # Linux：控制台无副作用，selftest 输出直接可读
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
