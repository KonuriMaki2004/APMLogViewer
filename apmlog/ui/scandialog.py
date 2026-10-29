# -*- coding: utf-8 -*-
"""apmlog.ui.scandialog —— 目录扫描的后台线程 + 进度对话框。

存在的理由是一个真实崩溃：原来 ``_LogListDock.scan()`` 直接在 UI 线程里
``Path.rglob("*")`` 再逐个 ``stat()``。用户把整个磁盘当目录选进来，就是几百万条目，
界面当场"未响应"，而且 rglob 撞无权限目录还会抛异常。

这里把扫描挪进 QThread，配一个可取消的进度框：

* 阶段一 ``enumerate`` 只走目录树挑候选，总数未知 → 进度条走忙碌态，同时显示
  "已遍历 N 个目录 / 已发现 M 个日志"；
* 阶段二 ``stat`` 对候选取大小和时间，总数已知 → 显示**真实百分比**；
* 取消按钮**任何阶段都在**：按下后内核在下一个检查点收手，已经扫到的日志照常列出来，
  不丢已完成的部分；
* 扫完不自动关窗，先把"日志数量 + 各类型数量"摊在人眼前，点"完成"才关。

内核在 ``apmlog.scan``（零 Qt 依赖），本模块只负责线程、信号和界面。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ._font import ui_font
from .theme import readable

__all__ = ["ScanRunner", "ScanProgressDialog", "ScanController"]


class ScanRunner(QtCore.QObject):
    """在别的线程里跑 apmlog.scan.scan_logs。

    放在独立 QObject 里是为了让 worker 能被 moveToThread：扫描内核是纯函数，
    唯一的交互就是两个回调，跨线程只需把回调转成信号即可。
    """

    progress = QtCore.Signal(str, int, object, int)      # phase, done, total|None, found
    finished = QtCore.Signal(object)                     # ScanResult
    failed = QtCore.Signal(str)                          # 中文错误说明

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False
        self._job: Optional[tuple] = None

    # -- 线程内 ---------------------------------------------------------------
    def request_cancel(self) -> None:
        self._cancel = True

    def start_job(self, root: str, limits: dict) -> None:
        """跨线程槽：信号带 (str, dict)，不能直接用 **kwargs 的 start。"""
        self.start(root, **(limits or {}))

    def start(self, root: str, **limits) -> None:
        """在 worker 线程里执行（由 ScanController 通过信号跨线程触发）。"""
        self._cancel = False
        self._job = (root, dict(limits))
        self._run()

    def _run(self) -> None:
        from ..scan import scan_logs                      # 延迟导入：便于单测打桩
        root, limits = self._job or (None, {})
        if not root:
            self.failed.emit("没有要扫描的目录")
            return
        try:
            res = scan_logs(root,
                            progress=lambda *a: self.progress.emit(*a),
                            should_cancel=lambda: self._cancel,
                            **limits)
        except Exception as exc:                          # noqa: BLE001
            # 内核承诺不抛；真抛了说明有 bug，这里兜住并说人话，绝不让线程静默死掉
            import traceback
            traceback.print_exc(file=sys.stderr)
            self.failed.emit(f"扫描出错：{type(exc).__name__}: {exc}")
            return
        self.finished.emit(res)
class ScanProgressDialog(QtWidgets.QDialog):
    """进度框。取消按钮在进度条**下方**，扫描中始终可点。"""

    #: 用户按了取消/关窗。由外部接到 runner.request_cancel。
    cancelRequested = QtCore.Signal()

    def __init__(self, root: str, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._root = str(root)
        self._result = None
        self._done_ok = False
        self.setWindowTitle("扫描日志目录")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setWindowFlags(self.windowFlags()
                            & ~QtCore.Qt.WindowType.WindowContextHelpButtonHint)

        head = QtWidgets.QLabel("正在扫描日志")
        head.setFont(ui_font(10.5, bold=True))
        self.lbl_dir = QtWidgets.QLabel(self._root)
        self.lbl_dir.setFont(ui_font(8.5))
        self.lbl_dir.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        # 长路径要省略号显示，不能把窗口撑爆
        self.lbl_dir.setToolTip(self._root)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)                       # 先忙碌态，等 stat 阶段再定范围
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        self.bar.setMinimumHeight(18)

        self.phase = QtWidgets.QLabel("准备中…")
        self.phase.setFont(ui_font(9.0))
        self.phase.setWordWrap(True)

        self.summary = QtWidgets.QLabel("")
        self.summary.setFont(ui_font(9.0))
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setVisible(False)

        self.btn_cancel = QtWidgets.QPushButton("取消")
        self.btn_cancel.setToolTip("停止扫描，**已经扫到的日志仍会列出来**")
        self.btn_done = QtWidgets.QPushButton("完成")
        self.btn_done.setVisible(False)
        self.btn_done.setDefault(True)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(9)
        lay.addWidget(head)
        lay.addWidget(self.lbl_dir)
        lay.addWidget(self.bar)                       # 进度条
        lay.addWidget(self.phase)                     # 百分比/计数说明
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        row.addWidget(self.btn_cancel)                # 取消在进度条下方
        row.addWidget(self.btn_done)
        lay.addLayout(row)
        lay.addWidget(self.summary)

        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_done.clicked.connect(self.accept)
        self.rejected.connect(self._on_reject)

    # ------------------------------------------------------------------ 回调
    def on_progress(self, phase: str, done: int, total, found: int) -> None:
        if self._done_ok:
            return
        if phase == "stat":
            t = int(total or 0)
            if t <= 0:
                self.bar.setRange(0, 0)
                self.phase.setText("正在读取文件信息…")
                return
            self.bar.setRange(0, t)
            self.bar.setValue(min(int(done), t))
            pct = int(round(done * 100.0 / t)) if t else 100
            self.phase.setText(f"正在读取文件信息　{pct}%　（{done}/{t}）")
        else:
            self.bar.setRange(0, 0)                   # 总数未知 → 忙碌条
            self.phase.setText(f"正在建立文件清单… 已遍历 {done} 个目录，"
                               f"发现 {found} 个日志")

    def on_finished(self, res) -> None:
        self._done_ok = True
        try:
            n = int(getattr(res, "total", 0))
        except Exception:
            n = 0
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.btn_cancel.setVisible(False)
        self.btn_done.setVisible(True)
        self.summary.setText(self._summary_text(res))
        self.summary.setVisible(True)
        self.phase.setText("扫描结束" if not getattr(res, "cancelled", False) else "已取消")
        self._tint_summary(res)

    def on_failed(self, text: str) -> None:
        self._done_ok = True
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.btn_cancel.setVisible(False)
        self.btn_done.setVisible(True)
        self.summary.setText(text)
        self.summary.setVisible(True)
        self.phase.setText("扫描失败")
        self.summary.setStyleSheet(f"color:{readable('#c92a2a', self._bg(), 4.5)};")

    # ------------------------------------------------------------------ 内部
    def _bg(self) -> str:
        return self.palette().color(QtGui.QPalette.ColorRole.Window).name()

    def _tint_summary(self, res) -> None:
        """取消/截断要说得醒目但不吓人。"""
        if getattr(res, "cancelled", False) or getattr(res, "truncated", False):
            self.summary.setStyleSheet(
                f"color:{readable('#8a6d1b', self._bg(), 4.5)};")

    def _summary_text(self, res) -> str:
        from ..scan import summarize
        try:
            base = summarize(res)
        except Exception:                             # noqa: BLE001
            base = f"共 {getattr(res, 'total', 0)} 个日志"
        bits = []
        d = int(getattr(res, "dirs_scanned", 0) or 0)
        e = int(getattr(res, "entries_scanned", 0) or 0)
        if d or e:
            bits.append(f"遍历 {d} 个目录 / {e} 个条目")
        extra = getattr(res, "errors", 0)
        if extra:
            bits.append(f"跳过 {extra} 个无权限项")
        return base + (("\n" + "　".join(bits)) if bits else "")

    def _on_cancel(self) -> None:
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("正在取消…")
        self.phase.setText("正在取消… 已经扫到的日志会保留")
        self.cancelRequested.emit()

    def _on_reject(self) -> None:
        # Esc 也算取消意图，别让人以为关窗就停了
        if not self._done_ok:
            self.btn_cancel.setEnabled(False)
            self.cancelRequested.emit()

    def keyPressEvent(self, ev) -> None:              # noqa: N802
        if ev.key() == QtCore.Qt.Key.Key_Escape and not self._done_ok:
            self._on_cancel()
            return
        super().keyPressEvent(ev)


#: 默认上限。选错整盘时这几个数字就是抱住不回应的底线：宁可报“结果不完整”，
#: 也不能把 UI 卡住。上限命中后已发现的日志仍然会列出来。
DEFAULT_LIMITS: dict = {"max_files": 5000, "max_dirs": 20000, "max_entries": 2_000_000}


class ScanController(QtCore.QObject):
    """把“一个目录”变成“一次可取消的后台扫描”。

    一次只跑一个：重复点扫描按钮不会开第二个线程（那是上一个 bug 的另一半，
    几个 rglob 同时跑会把磁盘队列打满）。
    """

    done = QtCore.Signal(object)              # ScanResult；失败时为 None
    notice = QtCore.Signal(str)               # 状态栏/日志行
    busyChanged = QtCore.Signal(bool)
    #: 必须在类上声明：Qt 信号不是实例属性，self.kick = QtCore.Signal(...) 是错的。
    kick = QtCore.Signal(str, dict)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 limits: Optional[dict] = None) -> None:
        super().__init__(parent)
        self._host = parent
        self._limits = dict(DEFAULT_LIMITS)
        if limits:
            self._limits.update(limits)
        self._thread: Optional[QtCore.QThread] = None
        self._runner: Optional[ScanRunner] = None
        self._dlg: Optional[ScanProgressDialog] = None
        self._modal = True
        # （信号已在类上声明）

    # ------------------------------------------------------------------ 对外
    def running(self) -> bool:
        return self._thread is not None

    def start(self, root: str, *, modal: bool = True) -> bool:
        """返回 False 表示根本没开跑（口录无效或已在扫）。"""
        root = str(root or "").strip().strip('"')
        if not root:
            self.notice.emit("请先选择日志目录")
            return False
        p = Path(root)
        try:
            if not p.is_dir():
                self.notice.emit(f"目录不存在或不是文件夹：{root}")
                return False
        except OSError as exc:
            self.notice.emit(f"目录不可用：{type(exc).__name__}: {exc}")
            return False
        if self.running():
            self.notice.emit("已经在扫描了，请先等当前这一个完成或取消")
            if self._dlg is not None:
                self._dlg.raise_()
            return False
        self._modal = bool(modal)
        self._spawn(root)
        return True

    def cancel(self) -> None:
        if self._runner is not None:
            self._runner.request_cancel()

    def shutdown(self) -> None:
        """关窗/退出前收尾：让线程把已扫到的结果交回来，最多等 3 秒。"""
        if not self.running():
            return
        self.cancel()
        if self._dlg is not None:
            try:
                self._dlg.reject()
            except RuntimeError:              # pragma: no cover
                self._dlg = None
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)

    # ------------------------------------------------------------------ 内部
    def _spawn(self, root: str) -> None:
        th = QtCore.QThread(self)
        th.setObjectName("log-scan")
        runner = ScanRunner()
        runner.moveToThread(th)
        self.kick.connect(runner.start_job)           # queued：主线程发、worker 线程执行
        runner.progress.connect(self._on_progress)    # worker → 主线程（queued）
        runner.finished.connect(self._on_finished)
        runner.failed.connect(self._on_failed)
        th.start()
        self._thread, self._runner = th, runner
        self.busyChanged.emit(True)
        if self._modal:
            self._dlg = ScanProgressDialog(root, self._host)
            self._dlg.cancelRequested.connect(self.cancel)
        self.kick.emit(root, dict(self._limits))
        if self._dlg is not None:
            try:
                self._dlg.exec()
            finally:
                self._close_dialog()

    def _close_dialog(self) -> None:
        dlg, self._dlg = self._dlg, None
        if dlg is not None:
            dlg.deleteLater()

    def _teardown(self) -> None:
        th, self._thread = self._thread, None
        self._runner = None
        if th is not None:
            th.quit()
            th.wait(2000)
            th.deleteLater()
        self.busyChanged.emit(False)

    def _on_progress(self, phase: str, done: int, total, found: int) -> None:
        dlg = self._dlg
        if dlg is not None:
            dlg.on_progress(phase, done, total, found)
        else:
            if phase == "stat" and total:
                self.notice.emit(f"正在扫描… {int(done) * 100 // int(total)}%")
            else:
                self.notice.emit(f"正在扫描… 已遍历 {done} 个目录，发现 {found} 个日志")

    def _on_finished(self, res) -> None:
        dlg = self._dlg
        if dlg is not None:
            dlg.on_finished(res)
        from ..scan import summarize
        try:
            text = summarize(res)
        except Exception:                            # noqa: BLE001
            text = f"共 {getattr(res, 'total', 0)} 个日志"
        self.notice.emit(text)
        self.done.emit(res)
        self._teardown()

    def _on_failed(self, text: str) -> None:
        dlg = self._dlg
        if dlg is not None:
            dlg.on_failed(text)
        self.notice.emit(text)
        self.done.emit(None)
        self._teardown()
