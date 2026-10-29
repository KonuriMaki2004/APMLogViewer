"""apmlog.ui.plugindialog —— 「插件」页：外部目录、加载状态、错误详情、导出器。

界面原则跟设置页一致：一屏看完，不塞大段说明文字；每行只给状态和它提供的扩展，
出错才看详情（traceback 放在独立对话框里，不挤在表格里）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..plugins.manager import PluginManager, STATUS_CN
from .settings import Settings
from . import theme as ui_theme

log = logging.getLogger(__name__)


class PluginDialog(QtWidgets.QDialog):
    """插件管理页。所有改动即时生效（停用/启用/重载），关窗不需要额外确认。"""

    def __init__(self, manager: PluginManager, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._mgr = manager
        self.setWindowTitle("插件")
        self.resize(880, 780)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ---------------- 目录 ----------------
        dir_box = QtWidgets.QGroupBox("插件目录")
        dv = QtWidgets.QVBoxLayout(dir_box)
        dv.setSpacing(6)
        self.dir_list = QtWidgets.QListWidget()
        self.dir_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.dir_list.setMaximumHeight(96)
        dv.addWidget(self.dir_list)
        drow = QtWidgets.QHBoxLayout()
        self.btn_open_dir = QtWidgets.QPushButton("打开目录…")
        self.btn_add_dir = QtWidgets.QPushButton("添加目录…")
        self.btn_add_dir.setToolTip("追加一个存放插件的目录（写进设置，重启后仍然有效）")
        self.btn_remove_dir = QtWidgets.QPushButton("移除")
        self.btn_remove_dir.setToolTip("只移除追加的目录；默认目录不受影响")
        for b in (self.btn_open_dir, self.btn_add_dir, self.btn_remove_dir):
            drow.addWidget(b)
        drow.addStretch(1)
        dv.addLayout(drow)
        root.addWidget(dir_box)

        # ---------------- 列表 ----------------
        list_box = QtWidgets.QGroupBox("已发现的插件")
        lv = QtWidgets.QVBoxLayout(list_box)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["插件", "版本", "状态", "提供的扩展"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 240)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 96)
        lv.addWidget(self.table, 1)
        brow = QtWidgets.QHBoxLayout()
        self.btn_reload = QtWidgets.QPushButton("重新扫描并重载")
        self.btn_disable = QtWidgets.QPushButton("停用")
        self.btn_enable = QtWidgets.QPushButton("启用")
        self.btn_error = QtWidgets.QPushButton("查看错误…")
        self.btn_error.setEnabled(False)
        for b in (self.btn_reload, self.btn_disable, self.btn_enable, self.btn_error):
            brow.addWidget(b)
        brow.addStretch(1)
        self.lbl_status = QtWidgets.QLabel("")
        brow.addWidget(self.lbl_status)
        lv.addLayout(brow)
        root.addWidget(list_box, 1)

        # ---------------- 导出器 ----------------
        exp_box = QtWidgets.QGroupBox("插件导出器")
        ev = QtWidgets.QHBoxLayout(exp_box)
        self.exp_list = QtWidgets.QListWidget()
        self.exp_list.setMaximumHeight(84)
        ev.addWidget(self.exp_list, 1)
        self.btn_export = QtWidgets.QPushButton("运行…")
        self.btn_export.setToolTip("选一个导出器，再选保存位置；导出内容由插件决定")
        ev.addWidget(self.btn_export)
        root.addWidget(exp_box)

        # ---------------- 主题 ----------------
        theme_box = QtWidgets.QGroupBox("插件主题")
        thv = QtWidgets.QHBoxLayout(theme_box)
        self.theme_list = QtWidgets.QListWidget()
        self.theme_list.setMaximumHeight(84)
        thv.addWidget(self.theme_list, 1)
        self.btn_theme = QtWidgets.QPushButton("应用主题")
        self.btn_theme.setToolTip("切换到这个主题；主题不受支持时会自动回退内置默认主题")
        thv.addWidget(self.btn_theme)
        root.addWidget(theme_box)

        foot = QtWidgets.QHBoxLayout()
        self.lbl_hint = QtWidgets.QLabel("")
        self.lbl_hint.setWordWrap(True)
        foot.addWidget(self.lbl_hint, 1)
        self.btn_close = QtWidgets.QPushButton("关闭")
        foot.addWidget(self.btn_close)
        root.addLayout(foot)

        # ---------------- 接线 ----------------
        self.btn_close.clicked.connect(self.accept)
        self.btn_reload.clicked.connect(self._reload)
        self.btn_disable.clicked.connect(self._disable)
        self.btn_enable.clicked.connect(self._enable)
        self.btn_error.clicked.connect(self._show_error)
        self.btn_open_dir.clicked.connect(self._open_dir)
        self.btn_add_dir.clicked.connect(self._add_dir)
        self.btn_remove_dir.clicked.connect(self._remove_dir)
        self.btn_export.clicked.connect(self._run_exporter)
        self.btn_theme.clicked.connect(self._apply_theme)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self._show_error())
        try:
            self._mgr.changed.connect(self.refresh)
        except Exception:                              # noqa: BLE001
            log.debug("插件管理器信号连接失败", exc_info=True)
        self._paint_hint()
        self.refresh()

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        self._refresh_dirs()
        infos = self._mgr.plugins()
        self.table.setRowCount(len(infos))
        for row, info in enumerate(infos):
            name = info.name or info.id
            it0 = QtWidgets.QTableWidgetItem(name)
            it0.setData(QtCore.Qt.ItemDataRole.UserRole, info.id)
            it0.setToolTip(f"{info.path}\n{info.error}" if info.error else info.path)
            self.table.setItem(row, 0, it0)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(info.version or "—"))
            it2 = QtWidgets.QTableWidgetItem(STATUS_CN.get(info.status, info.status))
            color = {"loaded": "#2e7d32", "failed": "#c0392b", "disabled": "#8a6d1b"}.get(
                info.status, "#666666")
            it2.setForeground(QtGui.QBrush(QtGui.QColor(color)))
            self.table.setItem(row, 2, it2)
            prov = "、".join(info.provides) or "—"
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(prov))
        self._refresh_exporters()
        self._refresh_themes()
        if not infos:
            self.lbl_status.setText("没有发现插件")
        else:
            bad = sum(1 for i in infos if i.status != "loaded")
            self.lbl_status.setText(f"共 {len(infos)} 个" + (f"，{bad} 个未启用" if bad else ""))
        self._sync_buttons()

    def _refresh_dirs(self) -> None:
        cur = self.dir_list.currentRow()
        self.dir_list.clear()
        for d in self._mgr.dirs():
            it = QtWidgets.QListWidgetItem(str(d))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, str(d))
            self.dir_list.addItem(it)
        if self.dir_list.count():
            self.dir_list.setCurrentRow(min(max(cur, 0), self.dir_list.count() - 1))

    def _refresh_exporters(self) -> None:
        self.exp_list.clear()
        for pid, name, _fn in self._mgr.exporters():
            it = QtWidgets.QListWidgetItem(f"{name}  （{pid}）")
            it.setData(QtCore.Qt.ItemDataRole.UserRole, (pid, name))
            self.exp_list.addItem(it)
        if not self.exp_list.count():
            it = QtWidgets.QListWidgetItem("（没有插件提供导出器）")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.exp_list.addItem(it)
        self.btn_export.setEnabled(bool(self._mgr.exporters()))

    def _refresh_themes(self) -> None:
        self.theme_list.clear()
        themes = self._mgr.themes_available()
        current = ""
        try:
            from ..ui.settings import Settings
            current = str(Settings.instance().get("ui_theme") or "")
        except Exception:                              # noqa: BLE001
            current = ""
        for t in themes:
            mark = "● " if str(t.get("value")) == current else "○ "
            it = QtWidgets.QListWidgetItem(f"{mark}{t.get('name')}  （{t.get('plugin')}）")
            it.setData(QtCore.Qt.ItemDataRole.UserRole, str(t.get("value")))
            self.theme_list.addItem(it)
        if not themes:
            it = QtWidgets.QListWidgetItem("（没有插件提供主题）")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.theme_list.addItem(it)
        self.btn_theme.setEnabled(bool(themes))

    def _apply_theme(self) -> None:
        it = self.theme_list.currentItem()
        if it is None:
            return
        value = str(it.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        if not value:
            return
        ok = self._mgr.apply_theme(value)
        self.lbl_status.setText("主题已切换" if ok else "主题不可用，已回退内置默认主题")
        self._refresh_themes()

    def _selected_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        it = self.table.item(row, 0)
        return str(it.data(QtCore.Qt.ItemDataRole.UserRole) or "") if it else ""

    def _selected_info(self) -> Any:
        pid = self._selected_id()
        for i in self._mgr.plugins():
            if i.id == pid:
                return i
        return None

    def _sync_buttons(self) -> None:
        info = self._selected_info()
        self.btn_disable.setEnabled(bool(info and info.status == "loaded"))
        self.btn_enable.setEnabled(bool(info and info.status != "loaded"))
        self.btn_error.setEnabled(bool(info and (info.error or info.traceback_text)))

    # ------------------------------------------------------------------ 动作
    def _reload(self) -> None:
        try:
            self._mgr.reload()
            self.lbl_status.setText(f"已重新扫描：{len(self._mgr.plugins())} 个插件")
        except Exception as exc:                       # noqa: BLE001
            log.exception("重载插件失败")
            self.lbl_status.setText(f"重载失败：{type(exc).__name__}: {exc}")

    def _disable(self) -> None:
        pid = self._selected_id()
        if pid:
            self._mgr.disable(pid)

    def _enable(self) -> None:
        pid = self._selected_id()
        if pid:
            ok = self._mgr.enable(pid)
            self.lbl_status.setText(f"{pid} 已启用" if ok else f"{pid} 仍然失败，看「查看错误」")

    def _show_error(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        text = info.traceback_text or info.error or "（没有错误信息）"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("插件错误")
        box.setText(f"{info.name or info.id}：{info.error or '未知错误'}")
        box.setDetailedText(text)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.exec()

    def _open_dir(self) -> None:
        it = self.dir_list.currentItem()
        if it is None:
            return
        path = Path(str(it.data(QtCore.Qt.ItemDataRole.UserRole) or ""))
        try:
            path.mkdir(parents=True, exist_ok=True)
            from ..ui.settings import open_in_explorer
            open_in_explorer(path)
        except Exception:                              # noqa: BLE001
            log.debug("打开插件目录失败", exc_info=True)

    def _add_dir(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择插件目录")
        if not d:
            return
        extras = self._extra_dirs()
        if d not in extras:
            extras.append(d)
            self._save_extra_dirs(extras)
        self._mgr.reload()

    def _remove_dir(self) -> None:
        it = self.dir_list.currentItem()
        if it is None:
            return
        path = str(it.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        extras = self._extra_dirs()
        if path in extras:
            extras = [p for p in extras if p != path]
            self._save_extra_dirs(extras)
            self._mgr.reload()
        else:
            self.lbl_status.setText("默认目录不能移除")

    def _extra_dirs(self) -> list[str]:
        raw = Settings.instance().get("plugin_dirs")
        try:
            data = json.loads(str(raw or "[]"))
        except (TypeError, ValueError):
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def _save_extra_dirs(self, dirs: list[str]) -> None:
        Settings.instance().set("plugin_dirs", json.dumps(dirs, ensure_ascii=False))

    def _run_exporter(self) -> None:
        it = self.exp_list.currentItem()
        if it is None:
            return
        data = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(data, (tuple, list)) or len(data) != 2:
            return
        pid, name = str(data[0]), str(data[1])
        fn = None
        for p, n, f in self._mgr.exporters():
            if p == pid and n == name:
                fn = f
                break
        if fn is None:
            return
        log_name = "apmlog"
        try:
            store = getattr(self.parent(), "store", None)
            if store is not None:
                log_name = Path(str(store.path)).stem
        except Exception:                              # noqa: BLE001
            pass
        suggested = f"{log_name}_{name}.txt"
        path, _flt = QtWidgets.QFileDialog.getSaveFileName(self, "导出", suggested,
                                                           "文本 (*.txt *.csv *.json);;所有文件 (*)")
        if not path:
            return
        ok, msg = self._mgr.run_exporter(pid, name, fn, path)
        self.lbl_status.setText(msg)
        if not ok:
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle("导出失败")
            box.setText(msg)
            box.exec()

    def _paint_hint(self) -> None:
        self.lbl_hint.setText("放进上面任一目录即被发现；单个插件出错只会停用它自己。")
        color = ui_theme.readable_on(self.lbl_hint, "#8a8f98") if hasattr(
            ui_theme, "readable_on") else "#8a8f98"
        self.lbl_hint.setStyleSheet(f"color:{color};")


__all__ = ["PluginDialog"]
