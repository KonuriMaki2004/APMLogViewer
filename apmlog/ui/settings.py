# -*- coding: utf-8 -*-
"""apmlog.ui.settings —— 工具级设置：悬浮提示、曲线外观、下载块数、缓存。

存在 ``%LOCALAPPDATA%\\APMLogViewer\\settings.json``（与视图记忆 views.json 同目录），
**绝不写进用户的日志目录**。改一项就落盘并发 ``changed`` 信号，界面立刻生效，不用重启。

悬浮提示的留存时间是这里的核心：Qt 的 ``QToolTip`` 默认在鼠标静止一小会儿后自己
消失，事件徽标那种多行提示根本来不及读完。Qt 6.12 已经把被动提示的过期 hint
（``SH_ToolTip_ExpireTimeout``）删掉了，留存时间**只能**由 :func:`show_tip` 传给
``QToolTip.showText`` 的 ``msec`` 形参控制；:class:`_TipStyle` 只负责把“出现前延迟”
（``SH_ToolTip_WakeUpDelay``）也接到设置上，让按钮、表格那种被动 ``setToolTip`` 同步。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from apmlog.core import reader as _rd
from apmlog.ui import modecolors as _mc        # 时间线模式色带配色（纯函数，无 Qt 依赖）
from apmlog.ui import modecolors as _modecolors
from apmlog.ui import theme                    # 界面主题（theme 只在函数里反向引用 settings）

log = logging.getLogger(__name__)

#: ``QToolTip.showText`` 的 msec 没有“无限”取值，1 小时等价“直到移开鼠标”
TIP_FOREVER_MS = 3_600_000

#: 设置项默认值（也是“恢复默认”的目标）
DEFAULTS: dict[str, Any] = {
    "tip_seconds": 0,          # 0 = 不自动消失（移开鼠标才关）
    "tip_delay_ms": 400,       # 提示出现前的延迟
    "tip_font_pt": 0,          # 0 = 跟随应用字体
    "lane_min_h": 132,         # 每条泳道最小像元高
    "ui_theme": "system",      # 界面主题 system|light|dark|custom
    "c_window": "#f2f3f5",     # 自定义：控件/窗口底色
    "c_base": "#ffffff",       # 自定义：输入区与时间线底色
    "c_text": "#1f2328",       # 自定义：正文文字
    "c_accent": "#2f6fed",     # 自定义：选中与游标强调色
    "theme": "light",          # 曲线主题 light|dark
    "badge_tol_px": 9.0,       # 事件徽标悬停命中容差
    "refine_debounce_ms": 350,  # 滚轮放大后回内核重取数的去抖
    "dl_blocks": 120,          # 飞控下载每请求块数（真机扫参最优）
    "play_rate": 4.0,          # 时间线播放倍速（日志是真实时间，4× 十分钟飞行 2.5 分播完）
    "play_follow": False,      # 播放时地图视野是否跟着飞机走
    "gauge_show": True,        # 地图左下角的罗盘/姿态球仪表
    # 告警阈值：界面上直接改，覆盖 config/alerts.yaml 里的同名项（改完立即重算）
    "vibe_warn": 30.0,         # 振动警告阈值 m/s²（1s 窗内任一轴峰值）
    "vibe_crit": 60.0,         # 振动严重阈值 m/s²
    # 电池阈值：默认按单芯电压判定（3.3V 警告 / 3.1V 严重），整包阈值 0 = 不启用
    # cell_count = 电芯数（几 S），0 = 自动识别；界面上手填的优先于自动识别
    "cell_count": 0,           # 电芯数（0=自动，例如 14 表示 14S）
    "cell_warn": 3.3,          # 单芯电压低于此值 → 警告（V/芯）
    "cell_crit": 3.1,          # 单芯电压低于此值 → 严重（V/芯）
    "pack_warn_v": 0.0,        # 整包欠压警告（V，0 = 不启用）
    "pack_crit_v": 0.0,        # 整包欠压严重（V，0 = 不启用）
    # 左侧日志列表默认只列能打开的 .BIN。一个真实日志目录里 .rlog/.tlog 往往比
    # .BIN 多十几倍（实测 1461 个里只有 106 个可打开），全列出来就是刷屏。
    "list_only_bin": True,
    # 时间线飞行模式色带的**覆盖层**：只记用户改过的模式 {"LOITER":"#rrggbb"}。
    # 存字符串而不是 dict：设置项一律标量，避免 DEFAULTS 里的可变对象被就地修改。
    "mode_colors": "{}",
    # 插件额外扫描目录（JSON 数组字符串）。默认目录永远是
    # %LOCALAPPDATA%\APMLogViewer\plugins，这里只记用户自己追加的。
    "plugin_dirs": "[]",
}

#: 取值范围，越界一律夹住而不是报错
RANGES: dict[str, tuple[float, float]] = {
    "tip_seconds": (0, 3600),
    "tip_delay_ms": (0, 3000),
    "tip_font_pt": (0, 28),
    "lane_min_h": (70, 400),
    "theme": (0, 0),
    "badge_tol_px": (2.0, 40.0),
    "refine_debounce_ms": (0, 3000),
    "vibe_warn": (1.0, 200.0),
    "vibe_crit": (1.0, 200.0),
    "cell_count": (0.0, 24.0),
    "cell_warn": (0.0, 60.0),
    "cell_crit": (0.0, 60.0),
    "pack_warn_v": (0.0, 200.0),
    "pack_crit_v": (0.0, 200.0),
    "dl_blocks": (1, 160),
    "play_rate": (0.25, 64.0),
}

#: 已经做掉的功能留下的旧键。未知键本来会被 `_raw_extras` 原样存回去（为了前向
#: 兼容），所以退役键必须在这里列一次，否则它永远烂在用户的 settings.json 里。
#: gauge_3d：3D 模型预览经讨论取消，连开关一起删了。
RETIRED_KEYS: tuple[str, ...] = ("gauge_3d",)

THEMES = ("light", "dark")
THEME_CN = {"light": "浅色", "dark": "深色"}


def settings_path() -> Path:
    """``%LOCALAPPDATA%\\APMLogViewer\\settings.json``（APMLOG_CACHE 优先时跟着走）。"""
    return _rd.cache_root().parent / "settings.json"


def _clean(key: str, value: Any) -> Any:
    """按默认值的类型收敛，并把数字夹进 RANGES。"""
    dflt = DEFAULTS.get(key)
    if key == "theme":
        s = str(value or "")
        return s if s in THEMES else str(dflt)
    if key == "ui_theme":
        s = str(value or "")
        # 插件主题写成 plugin:<pid>:<id>：语法对就收，**存不存在由应用时杏认**
        # （插件可能还没加载，或者已经卸了；那种情况由主窗回退默认主题）。
        if s in theme.UI_THEMES or theme.is_plugin_theme(s):
            return s
        return str(dflt)
    if key in theme.CUSTOM_KEYS:
        return theme.normalize_hex(value, str(dflt))
    try:
        if isinstance(dflt, bool):
            return bool(value)
        if isinstance(dflt, int):
            lo, hi = RANGES[key]
            return int(min(max(int(round(float(value))), int(lo)), int(hi)))
        if isinstance(dflt, float):
            lo, hi = RANGES[key]
            return float(min(max(float(value), lo), hi))
    except (TypeError, ValueError):
        return dflt
    return value


class Settings(QtCore.QObject):
    """一个进程内的设置仓库；``changed(key, value)`` 让界面即时跟随。"""

    changed = QtCore.Signal(str, object)

    _instance: Optional["Settings"] = None

    def __init__(self, path: Optional[Path] = None, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._path = Path(path) if path is not None else settings_path()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    # -- 单例
    @classmethod
    def instance(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, s: Optional["Settings"]) -> None:
        cls._instance = s

    # -- 读写
    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        data: dict[str, Any] = dict(DEFAULTS)
        dropped = False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                dropped = any(k in raw for k in RETIRED_KEYS)
                for k, v in raw.items():
                    if k in RETIRED_KEYS:
                        continue                                            # 已退役，不再回写
                    data[k] = _clean(k, v) if k in DEFAULTS else v   # 未知键原样留着，前向兼容
                self._raw_extras = {k: v for k, v in raw.items()
                                    if k not in DEFAULTS and k not in RETIRED_KEYS}
            else:
                self._raw_extras = {}
        except FileNotFoundError:
            self._raw_extras = {}
        except Exception as exc:                                        # noqa: BLE001
            log.warning("设置文件读不了（%s），本次用默认值：%s", self._path, exc)
            self._raw_extras = {}
        self._data = data
        if dropped:
            self.save()          # 发过现旧键就顺手洗一次文件，不靠下次写盘碰运气

    def save(self) -> bool:
        """临时文件 + os.replace，避免 GUI 与批量进程同时写时留下半截 JSON。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            out = dict(self._data)
            out.update(getattr(self, "_raw_extras", {}))
            tmp = self._path.with_name(self._path.name + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            with tmp.open("rb+") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
            return True
        except Exception as exc:                                        # noqa: BLE001
            log.warning("设置保存失败 %s: %s", self._path, exc)
            return False

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def get(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: Any, save: bool = True) -> Any:
        v = _clean(key, value)
        if self._data.get(key) == v:
            return v                                    # 同值不发信号，避免控件回环
        self._data[key] = v
        if save:
            self.save()
        self.changed.emit(key, v)
        return v

    # ---- 模式色带配色（覆盖层：没改过的模式仍用难度阶梯默认值）
    def mode_color_overrides(self) -> dict[str, str]:
        return _mc.parse_overrides(self.get("mode_colors"))

    def mode_color(self, name: str) -> str:
        """某模式当前颜色（用户覆盖优先，否则难度阶梯默认）。"""
        return _mc.resolve(name, self.mode_color_overrides())

    def set_mode_color(self, name: str, hex_color: str) -> str:
        """改一个模式的颜色；其它模式与分段划分不受影响。返回最终颜色。"""
        nm = str(name or "").strip().upper()
        norm = _mc.normalize_hex(hex_color)
        if not nm or norm is None:
            return self.mode_color(nm)
        ov = self.mode_color_overrides()
        ov[nm] = norm
        self.set("mode_colors", _mc.dump_overrides(ov))
        return norm

    def clear_mode_color(self, name: str) -> None:
        """把单个模式恢复成默认色。"""
        ov = self.mode_color_overrides()
        if ov.pop(str(name or "").strip().upper(), None) is not None:
            self.set("mode_colors", _mc.dump_overrides(ov))

    def reset_mode_colors(self) -> None:
        """恢复默认：清空覆盖层，全部回到难度阶梯配色。"""
        self.set("mode_colors", "{}")

    def mode_color_palette(self, modes: Iterable[str] = ()) -> dict[str, str]:
        return _mc.palette(self.mode_color_overrides(), modes)

    def reset(self) -> None:
        for k in DEFAULTS:
            self.set(k, DEFAULTS[k])

    # -- 派生值
    def tip_msec(self) -> int:
        """程序化 QToolTip 的留存毫秒；0 秒 → 1 小时（等价“移开才关”）。"""
        sec = float(self.get("tip_seconds") or 0)
        return TIP_FOREVER_MS if sec <= 0 else int(max(200, round(sec * 1000)))

    def tip_expire_ms(self) -> int:
        """兼容旧名：程序化提示的留存毫秒（Qt 6 已无被动提示过期 hint）。"""
        return self.tip_msec()

    def tip_wake_ms(self) -> int:
        return int(max(0, min(3000, int(self.get("tip_delay_ms") or 0))))


def settings() -> Settings:
    return Settings.instance()


# --------------------------------------------------------------------------- #
# 悬浮提示统一入口
# --------------------------------------------------------------------------- #

def show_tip(widget: Optional[QtWidgets.QWidget], text: str,
             pos: Optional[QtCore.QPoint] = None) -> None:
    """发一条悬浮提示，留存时间取设置。

    Qt 6 已经取消了“被动提示过期时间”的 style hint，鼠标静止后提示自己消失的时长
    只能由这里的 ``msec`` 形参控制：传大值就是“不自动消失，移开才关”。
    ``QToolTip`` 在 ``QtWidgets`` 不在 ``QtGui``（PySide6 6.12 实测），写错会让每次
    鼠标移动都抛 ``AttributeError``。
    """
    if not text:
        return
    p = pos if pos is not None else QtGui.QCursor.pos()
    try:
        QtWidgets.QToolTip.showText(p, text, widget, QtCore.QRect(), settings().tip_msec())
    except TypeError:                                   # pragma: no cover - 老绑定无 msec 形参
        QtWidgets.QToolTip.showText(p, text, widget)


def hide_tip() -> None:
    if QtWidgets.QToolTip.isVisible():
        QtWidgets.QToolTip.hideText()


def apply_tip_font(app: Optional[QtWidgets.QApplication], pt: Optional[float] = None) -> bool:
    """只改 TipLabel 这一类控件的字号，不动全局字体；pt<=0 恢复跟随应用字体。"""
    if app is None:
        return False
    size = int(round(float(pt if pt is not None else settings().get("tip_font_pt")) or 0))
    f = QtGui.QFont(app.font("TipLabel"))
    if size <= 0:
        f = QtGui.QFont(app.font())
    else:
        f.setPointSize(size)
    app.setFont(f, "TipLabel")
    return True


#: Qt 6.12 实测：StyleHint 里只剩 WakeUpDelay / FallAsleepDelay，
#: **SH_ToolTip_ExpireTimeout 已被删掉**，所以“留存多久”只能靠 showText 的 msec。
_WAKEUP_HINT = int(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay)


class _TipStyle(QtWidgets.QProxyStyle):
    """把设置里的“提示出现前延迟”应用到被动 tooltip（按钮、表格行那种）。"""

    def styleHint(self, hint: Any, option: Any = None, widget: Any = None,
                  extra: Any = None) -> Any:                                   # noqa: N802
        try:
            if int(hint) == _WAKEUP_HINT:
                return settings().tip_wake_ms()
        except Exception:                                                      # noqa: BLE001
            # 这里招异常会让整个窗口控件构造失败（SystemError: returned NULL），必须兑住
            log.debug("tooltip styleHint 异常，退回默认", exc_info=True)
        return super().styleHint(hint, option, widget, extra)


def install_tip_style(app: QtWidgets.QApplication, base_style: str = "Fusion") -> bool:
    """装上样式代理。必须在建任何窗口之前调用一次。

    注意 ``QApplication.setStyle()`` 会接管并删掉旧 style，所以代理不能包住当前
    style 实例（会变悬空指针），用无参构造让代理自己建一个基风格；同时留一份
    Python 引用防止包装器被回收。
    """
    try:
        # 基风格用名字而不是实例：setStyle 会删旧 style，包住当前实例会变悬空指针。
        # 选 Fusion 而不是 Windows 默认的 windowsvista：后者画工具条/标签页走系统
        # UxTheme，不听我们设的 palette，浅色主题下就会出现“浅底白字”。
        keys = [k.lower() for k in QtWidgets.QStyleFactory.keys()]
        style = _TipStyle(base_style) if base_style.lower() in keys else _TipStyle()
        app.setStyle(style)
        app._apmlog_tip_style = style
        return isinstance(app.style(), _TipStyle)
    except Exception as exc:                                                    # noqa: BLE001
        log.warning("tooltip 样式代理安装失败：%s", exc)
        return False


# --------------------------------------------------------------------------- #
# 缓存维护
# --------------------------------------------------------------------------- #

def cache_dir() -> Path:
    return _rd.cache_root()


def clear_parse_cache(keep_last: int = 0) -> tuple[int, int]:
    """删掉解析缓存目录里的 npz/json（日志重解析即可重建），返回 (删除文件数, 释放字节)。

    只动 ``cache/`` 里面的东西：``settings.json`` / ``views.json`` / ``link.json``
    在同级的 ``APMLogViewer/``，不在本函数作用范围内。
    """
    root = cache_dir()
    n = b = 0
    if not root.exists():
        return (0, 0)
    entries = sorted((p for p in root.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for d in entries[keep_last:]:
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    b += int(f.stat().st_size)
                    n += 1
            except OSError:
                continue
        shutil.rmtree(d, ignore_errors=True)
    return (n, b)


def open_in_explorer(path: Path) -> bool:
    """在系统文件管理器里打开目录（Windows 资源管理器 / xdg-open / open）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))                      # type: ignore[attr-defined]
        else:
            QtCore.QProcess.startDetached("xdg-open" if sys.platform != "darwin" else "open",
                                          [str(path)])
        return True
    except Exception as exc:                              # noqa: BLE001
        log.warning("打不开目录 %s: %s", path, exc)
        return False


# --------------------------------------------------------------------------- #
# 设置对话框
# --------------------------------------------------------------------------- #

_MIN_W = 1200          # 设置页观感最小宽（1920 屏上左右两列各约 590）
_MIN_H = 820           # 设置页观感最小高


class SettingsDialog(QtWidgets.QDialog):
    """常用设置都在这；改一项立即落盘并生效，不需要重启。"""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 s: Optional[Settings] = None,
                 cells_provider: Optional[Callable[[], dict]] = None,
                 themes_provider: Optional[Callable[[], list]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.s = s or Settings.instance()
        # 由主窗口提供“当前日志自动识别出的电芯数”，对话框自己拿不到 store
        self._cells_provider = cells_provider
        # 插件主题清单也由主窗给（对话框不碰插件管理器）：空/异常就是“没有插件主题”
        self._themes_provider = themes_provider
        self.setObjectName("SettingsDialog")
        self._widgets: dict[str, QtWidgets.QWidget] = {}
        self._color_btns: dict[str, QtWidgets.QPushButton] = {}
        self._build()
        # 在对话框开着的时候从曲线页“主题”按钮改主题，这边也要跟着动
        self.s.changed.connect(self._on_setting_changed)
        self._size_to_content()

    def _refresh_cells_label(self) -> None:
        """把"当前到底按几 S 判定"说清楚：手动优先，自动从哪来的，还是识别失败。"""
        w = self._widgets.get("cell_count")
        manual = int(w.value()) if isinstance(w, QtWidgets.QSpinBox) else 0
        info: dict = {}
        if self._cells_provider is not None:
            try:
                info = dict(self._cells_provider() or {})
            except Exception:
                log.debug("取电芯数信息失败", exc_info=True)
                info = {}
        auto = info.get("cells")
        try:
            auto_n = int(auto) if auto else 0
        except (TypeError, ValueError):
            auto_n = 0
        if manual > 0:
            txt = f"{manual}S（手动）"
            if auto_n and auto_n != manual:
                txt += f" — 自动识别是 {auto_n}S，已按手填的算"
            elif not auto_n:
                txt += " — 自动识别失败，按手填的算"
        elif auto_n:
            detail = str(info.get("detail") or "").strip()
            txt = f"{auto_n}S（自动）" + (f" — {detail}" if detail else "")
        else:
            detail = str(info.get("detail") or "没有可用的电压/参数证据").strip()
            txt = ("自动识别失败：" + detail
                   + " → 请把上面「电芯数」填成实际串数（如 14S），单芯阈值才会按它换算")
        ok = bool(manual > 0 or auto_n)
        self.lbl_cells.setText(txt)
        color = theme.readable_on(self.lbl_cells, "#1f7a3d" if ok else "#a15c00")
        self.lbl_cells.setStyleSheet(f"color:{color};")

    def _on_setting_changed(self, key: str, value: Any) -> None:
        if key == "mode_colors":
            if hasattr(self, "_mode_btns"):
                self._refresh_mode_colors()
            return
        btn = self._color_btns.get(key)
        if btn is not None:
            self._paint_color_button(key)
            return
        w = self._widgets.get(key)
        if w is None:
            return
        if isinstance(w, QtWidgets.QSpinBox) and w.value() != int(value):
            w.setValue(int(value))
        elif isinstance(w, QtWidgets.QDoubleSpinBox) and abs(w.value() - float(value)) > 1e-9:
            w.setValue(float(value))
        elif isinstance(w, QtWidgets.QComboBox) and w.currentData() != str(value):
            w.setCurrentIndex(max(0, w.findData(str(value))))

    # -- 界面主题
    def _build_ui_theme(self, root: QtWidgets.QVBoxLayout) -> None:
        form = self._group(root, "界面主题（整个程序的配色）")
        row = QtWidgets.QHBoxLayout()
        self.cb_ui_theme = QtWidgets.QComboBox()
        for k in theme.UI_THEMES:
            self.cb_ui_theme.addItem(theme.UI_THEME_CN.get(k, k), k)
        # 插件主题接在内置项后面：选中即存 plugin:<pid>:<id>，应用失败由主窗回退默认
        extra = 0
        for t in self._plugin_themes():
            if not extra:
                self.cb_ui_theme.insertSeparator(self.cb_ui_theme.count())
            self.cb_ui_theme.addItem(f"插件：{t.get('name') or t.get('id')}",
                                     str(t.get("value") or ""))
            extra += 1
        self.cb_ui_theme.setCurrentIndex(
            max(0, self.cb_ui_theme.findData(str(self.s.get("ui_theme")))))
        self.cb_ui_theme.currentIndexChanged.connect(lambda _i: self._ui_theme_changed())
        self._widgets["ui_theme"] = self.cb_ui_theme
        row.addWidget(self.cb_ui_theme)
        self.lbl_scheme = QtWidgets.QLabel("")
        row.addWidget(self.lbl_scheme)
        row.addStretch(1)
        form.addRow("界面主题", self._wrap(row))

        crow = QtWidgets.QHBoxLayout()
        for key in theme.CUSTOM_KEYS:
            b = QtWidgets.QPushButton()
            b.setMinimumWidth(104)
            b.clicked.connect(lambda _c=False, k=key: self._pick_color(k))
            self._color_btns[key] = b
            crow.addWidget(b)
        crow.addStretch(1)
        self._wrap_custom = self._wrap(crow)
        form.addRow("自定义颜色", self._wrap_custom)
        for key in theme.CUSTOM_KEYS:
            self._paint_color_button(key)
        self._sync_custom_enabled()

        hint = QtWidgets.QLabel("自定义时曲线配色按“输入区底色”明暗自动选亮线/暗线；"
                                "“跟随系统”会在系统切深浅时跟着切。")
        hint.setWordWrap(True)
        # 不写 palette(mid)：深色下几乎看不清，辅助文字宁可正常色也不要“淡到看不见”
        form.addRow("", hint)

        row2 = QtWidgets.QHBoxLayout()
        self.cb_theme = QtWidgets.QComboBox()
        for k in THEMES:
            self.cb_theme.addItem(THEME_CN.get(k, k), k)
        self.cb_theme.setCurrentIndex(max(0, self.cb_theme.findData(str(self.s.get("theme")))))
        self.cb_theme.currentIndexChanged.connect(
            lambda _i: self.s.set("theme", self.cb_theme.currentData()))
        self._widgets["theme"] = self.cb_theme
        row2.addWidget(self.cb_theme)
        row2.addWidget(QtWidgets.QLabel("（也可在曲线页右上角“主题”按钮临时切换）"))
        row2.addStretch(1)
        form.addRow("曲线配色", self._wrap(row2))

    def _paint_color_button(self, key: str) -> None:
        b = self._color_btns.get(key)
        if b is None:
            return
        hexv = str(self.s.get(key))
        fg = "#ffffff" if theme.luminance(hexv) < 0.5 else "#111318"
        b.setText(f"{theme.CUSTOM_LABELS.get(key, key)}\n{hexv}")
        b.setStyleSheet(f"background:{hexv}; color:{fg}; border:1px solid palette(mid);"
                        f"padding:4px 6px;")
        b.setToolTip(theme.CUSTOM_HINTS.get(key, ""))

    def _sync_custom_enabled(self) -> None:
        is_custom = str(self.s.get("ui_theme")) == "custom"
        for b in self._color_btns.values():
            b.setEnabled(is_custom)
        try:
            self.lbl_scheme.setText(
                f"（系统当前：{'深色' if theme.system_scheme() == 'dark' else '浅色'}）")
        except Exception:                              # noqa: BLE001 提示文字不能拖垮对话框
            self.lbl_scheme.setText("")

    def _ui_theme_changed(self) -> None:
        self.s.set("ui_theme", str(self.cb_ui_theme.currentData()))
        self._sync_custom_enabled()

    def _plugin_themes(self) -> list:
        """主窗给的插件主题清单；没接/provider 抛异常一律当“没有”。"""
        fn = self._themes_provider
        if not callable(fn):
            return []
        try:
            data = fn() or []
        except Exception:                              # noqa: BLE001
            log.debug("取插件主题清单失败", exc_info=True)
            return []
        return [t for t in data if isinstance(t, dict) and t.get("value")]

    def _pick_color(self, key: str) -> None:
        cur = QtGui.QColor(str(self.s.get(key)))
        col = QtWidgets.QColorDialog.getColor(cur, self, theme.CUSTOM_LABELS.get(key, "颜色"))
        if col.isValid():
            self.s.set(key, col.name())

    # -- 布局
    def showEvent(self, ev) -> None:                        # noqa: N802 (Qt 命名)
        super().showEvent(ev)
        if not getattr(self, "_sized_once", False):
            self._sized_once = True
            # show 之后布局才是最终值；同步来一次，再用 0ms 定时器补一次
            QtCore.QTimer.singleShot(0, self._size_to_content)

    def _size_to_content(self) -> None:
        """按内容把窗口一次开够（用户要求：不出现滑动条，内容整页放得下）。

        先 ``adjustSize`` 让布局给出真实 sizeHint，再夹到屏幕可用区内；
        最小尺寸取内容的 82%，保证小屏上窗口还能再缩，不至于超出屏幕动不了。
        """
        # 构造期布局还没 polish/activate，sizeHint 会偏小（实测 1012×806 vs 真值 1206×862），
        # 窗口就会开成“刚好差一点”，把最右/最底的内容切掉。先强制走一遍再读。
        self.ensurePolished()
        lay = self.layout()
        if lay is not None:
            lay.activate()
        self.adjustSize()
        hint = self.sizeHint()
        scr = QtWidgets.QApplication.primaryScreen()
        try:
            cur = self.screen()
            if cur is not None:
                scr = cur
        except Exception:                        # noqa: BLE001
            pass
        avail = scr.availableGeometry() if scr is not None else QtCore.QRect(0, 0, 1600, 1000)
        # 下限是"整页放大"的观感下限（真机字体下 doc 内容 997×768，正好卡在这个下限附近）；
        # 上限是屏幕可用区，两者取中间，屏幕更小时才让步。
        w = max(_MIN_W, min(hint.width() + 10, int(avail.width() * 0.96)))
        h = max(_MIN_H, min(hint.height() + 10, int(avail.height() * 0.94)))
        # 最小尺寸 = 内容尺寸：窗口不许被缩到看不全（没有滚动条兜底）。
        # 只有屏幕本身就比内容小时才让步到屏幕大小，否则窗口都拖不动。
        self.setMinimumSize(min(hint.width(), avail.width()),
                           min(hint.height(), avail.height()))
        self.resize(w, h)
        self._content_size = (int(hint.width()), int(hint.height()))

    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        # 不放滚动区（用户 2026-10-24：设置页不许出现滑动条，所有内容要在页内完整显示）：
        # 内容直接进根布局，窗口尺寸按内容 sizeHint 定（见 _size_to_content）。
        body = QtWidgets.QWidget(self)
        cols = QtWidgets.QHBoxLayout(body)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(18)
        left = QtWidgets.QVBoxLayout()
        right = QtWidgets.QVBoxLayout()
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        self._col_left, self._col_right = left, right
        root.addWidget(body, 1)
        self._body = body
        self._build_ui_theme(left)


        # ---- 模式颜色（时间线飞行模式色带）----
        _nl = chr(10)          # 本组在前，_nl 在这里先备好（后面振动组还会再定义一次）
        form_mode = self._group(right, "模式颜色（时间线飞行模式色带）")
        grid_host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(grid_host)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        _MODE_COLS = 3                     # 18 个模式 → 6 行；单页放得下（不再滚动）
        self._mode_btns: dict[str, QtWidgets.QPushButton] = {}
        self._mode_labels: dict[str, QtWidgets.QLabel] = {}
        for i, name in enumerate(_modecolors.MODE_LADDER):
            # 直接按 4 列排（色块/名字 × 2），最后一列吃掉剩余宽度：
            # 用嵌套 layout 或定宽小部件都会把第二列顶到窗口边缘、名字被裁掉。
            btn = QtWidgets.QPushButton(grid_host)
            btn.setFixedSize(34, 16)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"点击自定义 {name} 的颜色")
            btn.clicked.connect(lambda _c=False, n=name: self._pick_mode_color(n))
            lbl = QtWidgets.QLabel(name, grid_host)
            grid.addWidget(btn, i // _MODE_COLS, (i % _MODE_COLS) * 2)
            grid.addWidget(lbl, i // _MODE_COLS, (i % _MODE_COLS) * 2 + 1)
            self._mode_btns[name] = btn
            self._mode_labels[name] = lbl
        for _c in range(1, _MODE_COLS):        # 各列之间留出间隙
            grid.setColumnMinimumWidth(_c * 2, 16)
        grid.setColumnStretch(_MODE_COLS * 2, 1)   # 多余宽度给最右，别把列撑开
        form_mode.addRow(grid_host)

        row_mode = QtWidgets.QHBoxLayout()
        # 注意别叫"恢复默认"或任何含该短语的名字：对话框底部有全局"恢复默认"按钮，
        # 按文字找按钮的代码（含测试）会先命中这里、点错对象。
        self.btn_mode_reset = QtWidgets.QPushButton("全部还原为默认")
        self.btn_mode_reset.setToolTip("把所有模式颜色还原为按难度阶梯定义的默认方案")
        self.btn_mode_reset.clicked.connect(self._reset_mode_colors)
        self.lbl_mode_custom = QtWidgets.QLabel()
        self.lbl_mode_custom.setStyleSheet(f"color:{theme.readable_on(self.lbl_mode_custom, '#8a8f98')};")
        row_mode.addWidget(self.btn_mode_reset)
        row_mode.addWidget(self.lbl_mode_custom, 1)
        form_mode.addRow("", row_mode)
        self._refresh_mode_colors()

        form_tip = self._group(right, "悬浮提示（含曲线上的感叹号、时间线上的告警）")
        self._add_int(form_tip, "tip_seconds", 0, 3600, " 秒", "提示留存",
                      "鼠标停在感叹号/告警带上时，提示框停留多久。\n"
                      "0 = 不自动消失，移开鼠标才关闭。")
        self._widgets["tip_seconds"].setSpecialValueText("不自动消失（移开才关）")
        self._add_int(form_tip, "tip_delay_ms", 0, 3000, " ms", "提示延迟",
                      "鼠标停多久之后弹出提示。")
        self._add_int(form_tip, "tip_font_pt", 0, 28, " pt", "提示字号",
                      "只改提示框字号，不动界面其它字体；0 = 跟随系统。")
        self._widgets["tip_font_pt"].setSpecialValueText("跟随默认")

        form_chart = self._group(right, "曲线与时间线")
        self._add_int(form_chart, "lane_min_h", 70, 400, " px", "每道最小高",
                      "泳道多时靠滚动条展开，而不是把曲线压扁。低分辨率屏建议 150 以上。")
        self._add_float(form_chart, "badge_tol_px", 2.0, 40.0, " px", "徽标命中容差",
                        "鼠标离感叹号多远还算悬停在它上面。太小会“抓不住”提示。")
        self._add_int(form_chart, "refine_debounce_ms", 0, 3000, " ms", "放大重取数延迟",
                      "滚轮缩放/平移停手多久之后，按当前视图回内核重新取细数据。")

        form_alert = self._group(left, "告警阈值（振动）")
        _nl = chr(10)          # 用 chr(10) 拼行，避开反斜杠转义
        self._add_float(form_alert, "vibe_warn", 1, 200, " m/s²", "警告阈值",
                        "1 秒窗内任一轴振动峰值 ≥ 该值就报“振动超标（警告）”。" + _nl
                        + "参考值：ArduPilot 社区经验 X/Y 轴 < 30、Z 轴 < 60 m/s²，机型差异大。" + _nl
                        + "DataFlash 的 VIBE 单位已是 m/s²，不用换算；改完立即重算，不用重开日志。")
        self._add_float(form_alert, "vibe_crit", 1, 200, " m/s²", "严重阈值",
                        "高于该值报“严重”。必须大于警告阈值——" + _nl
                        + "把警告调得比严重还高时，严重档会自动抬到警告值（否则两档互相吃掉）。")
        _w = self._widgets["vibe_warn"]
        _c = self._widgets["vibe_crit"]
        _w.valueChanged.connect(lambda v: _c.setValue(max(float(v), _c.value())))
        note = QtWidgets.QLabel("阈值改动会立刻重算当前日志的告警，并更新告警页、时间线色带与地图。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.readable_on(note, '#8a8f98')};")
        form_alert.addRow("", note)

        form_batt = self._group(left, "告警阈值（电池）")
        _nb = chr(10)          # 同上，用 chr(10) 拼行，避开反斜杠转义
        self._add_int(form_batt, "cell_count", 0, 24, "S", "电芯数",
                      "几 S 电池，例如 14 就是 14S。0 = 自动识别（按 BATT_ARM_VOLT，"
                      + "或观测到的最高整包电压 ÷ 4.25V/芯 向上取整）。" + _nb
                      + "手填的数字优先于自动识别；改了立刻按新串数重算所有单芯阈值。")
        self._widgets["cell_count"].setSpecialValueText("自动")
        self.lbl_cells = QtWidgets.QLabel()
        self.lbl_cells.setWordWrap(True)
        self.lbl_cells.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        form_batt.addRow("当前采用", self.lbl_cells)
        self._widgets["cell_count"].valueChanged.connect(lambda _v: self._refresh_cells_label())
        self._add_float(form_batt, "cell_warn", 0, 60, " V/芯", "单芯警告",
                        "单芯电压低于该值报警，默认 3.3 V/芯。" + _nb
                        + "比较用的是“整包电压 ÷ 电芯数”，所以改电芯数会直接改变判定结果。" + _nb
                        + "日志本身记的就是单芯电压时直接比，不用除。")
        self._add_float(form_batt, "cell_crit", 0, 60, " V/芯", "单芯严重",
                        "单芯电压低于该值报“严重”，默认 3.1 V/芯。必须不高于单芯警告"
                        "（越低越严重），" + _nb + "填得比警告高会自动压回警告值。")
        self._add_float(form_batt, "pack_warn_v", 0, 200, " V", "整包警告",
                        "整包电压低于该值报警，0 = 不启用。" + _nb
                        + "按你的电池组填：4S≈14.0、6S≈21.0、12S≈42.0（按 3.5 V/芯算）。" + _nb
                        + "这条与“按参数/起转电压比例”的欠压判定并存，规则名不同。")
        self._add_float(form_batt, "pack_crit_v", 0, 200, " V", "整包严重",
                        "低于该值报“严重”，0 = 不启用。必须不高于整包警告。")
        for _k in ("cell_warn", "cell_crit"):
            self._widgets[_k].setSpecialValueText("自动")
        for _k in ("pack_warn_v", "pack_crit_v"):
            self._widgets[_k].setSpecialValueText("不启用")
        # 欠压“越低越严重”：严重档不许高于警告档，否则警告区间被吃空
        _cw, _cc = self._widgets["cell_warn"], self._widgets["cell_crit"]
        _cw.valueChanged.connect(lambda v: _cc.setValue(min(float(v) or 99.0, _cc.value())))
        _pw, _pc = self._widgets["pack_warn_v"], self._widgets["pack_crit_v"]
        _pw.valueChanged.connect(lambda v: _pc.setValue(min(float(v) or 999.0, _pc.value())))
        note_b = QtWidgets.QLabel("电池阈值同样即时重算；告警页、时间线色带与地图会一起更新。")
        note_b.setWordWrap(True)
        note_b.setStyleSheet(f"color:{theme.readable_on(note_b, '#8a8f98')};")
        form_batt.addRow("", note_b)
        self._refresh_cells_label()

        form_map = self._group(right, "地图与仪表")
        self._add_bool(form_map, "gauge_show", "显示左下角仪表（罗盘 / 姿态球）",
                       "读 ANG/ATT 的姿态报文。没有姿态报文的日志会退回用位移算的地面航迹方位，\n"
                       "并在读数里标出“地迹”。关掉就把左下角完全让给地图。")

        form_dl = self._group(right, "飞控日志下载")
        self._add_int(form_dl, "dl_blocks", 1, 160, " ×90B", "每请求块数",
                      "真机（Copter 4.6.3 / USB CDC）扫参：120 块 ≈ 58 KB/s 且零重传零 CRC；\n"
                      "20 块只有 14 KB/s；≥200 块开始出现重复帧与 CRC 错。")

        form_cache = self._group(right, "缓存")
        lab = QtWidgets.QLabel(str(cache_dir()))
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        form_cache.addRow("解析缓存目录", lab)
        row2 = QtWidgets.QHBoxLayout()
        btn_open = QtWidgets.QPushButton("打开目录…")
        btn_open.clicked.connect(lambda: open_in_explorer(cache_dir()))
        btn_clear = QtWidgets.QPushButton("清除解析缓存…")
        btn_clear.setToolTip("删掉已解析日志的 npz/json 缓存；下次打开日志会重新解析（变慢一次）。")
        btn_clear.clicked.connect(self._clear_cache)
        self.lbl_cache = QtWidgets.QLabel("")
        self.lbl_cache.setStyleSheet(f"color:{theme.readable_on(self.lbl_cache, '#8a8f98')};")
        row2.addWidget(btn_open)
        row2.addWidget(btn_clear)
        row2.addWidget(self.lbl_cache, 1)
        form_cache.addRow("", self._wrap(row2))

        foot = QtWidgets.QHBoxLayout()
        btn_reset = QtWidgets.QPushButton("恢复默认")
        btn_reset.clicked.connect(self._reset)
        btn_close = QtWidgets.QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        foot.addWidget(btn_reset)
        foot.addStretch(1)
        foot.addWidget(btn_close)
        left.addStretch(1)
        right.addStretch(1)
        root.addLayout(foot)
        self._refresh_cache_size()

    def _add_bool(self, form: QtWidgets.QFormLayout, key: str, label: str, tip: str) -> None:
        cb = QtWidgets.QCheckBox(label)
        cb.setToolTip(tip)
        cb.setChecked(bool(self.s.get(key)))
        cb.toggled.connect(lambda v: self.s.set(key, v))
        self._widgets[key] = cb
        form.addRow("", cb)

    def _group(self, root: QtWidgets.QVBoxLayout, title: str) -> QtWidgets.QFormLayout:
        box = QtWidgets.QGroupBox(title)
        f = QtWidgets.QFormLayout(box)
        f.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        f.setHorizontalSpacing(10)
        f.setVerticalSpacing(8)
        root.addWidget(box)
        return f

    def _wrap(self, lay: QtWidgets.QHBoxLayout) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setLayout(lay)
        return w

    def _add_int(self, form: QtWidgets.QFormLayout, key: str, lo: int, hi: int, suffix: str,
                 label: str, tip: str) -> None:
        sp = QtWidgets.QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        sp.setValue(int(self.s.get(key)))
        sp.setToolTip(tip)
        sp.valueChanged.connect(lambda v: self.s.set(key, v))
        self._widgets[key] = sp
        form.addRow(label, sp)

    def _add_float(self, form: QtWidgets.QFormLayout, key: str, lo: float, hi: float,
                   suffix: str, label: str, tip: str) -> None:
        sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(1)
        sp.setSingleStep(0.5)
        sp.setSuffix(suffix)
        sp.setValue(float(self.s.get(key)))
        sp.setToolTip(tip)
        sp.valueChanged.connect(lambda v: self.s.set(key, v))
        self._widgets[key] = sp
        form.addRow(label, sp)

    # -- 动作
    def _reset(self) -> None:
        self.s.reset()
        self._reload_widgets()

    # ---- 模式颜色 ---------------------------------------------------- #

    def _pick_mode_color(self, name: str) -> None:
        """点色块 → 取色对话框。改完只动这一个模式，时间线立刻重画。"""
        cur = QtGui.QColor(self.s.mode_color(name))
        col = QtWidgets.QColorDialog.getColor(cur, self, f"{name} 的颜色")
        if not col.isValid():
            return
        self.s.set_mode_color(name, col.name())
        self._refresh_mode_colors()

    def _reset_mode_colors(self) -> None:
        self.s.reset_mode_colors()
        self._refresh_mode_colors()

    def _paint_mode_btn(self, name: str) -> None:
        btn = self._mode_btns.get(name)
        if btn is None:
            return
        hexv = self.s.mode_color(name)
        ink = _modecolors.ink_on(hexv)
        custom = name in self.s.mode_color_overrides()
        btn.setStyleSheet(
            f"QPushButton{{background:{hexv};border:1px solid "
            f"{'#2f6fed' if custom else 'rgba(0,0,0,90)'};border-radius:3px;"
            f"color:{ink};font-size:7pt;}}")
        btn.setText("改" if custom else "")
        btn.setToolTip(f"{name}：{hexv}"
                       + ("（已自定义，点一下改）" if custom else "（默认，点一下自定义）"))

    def _refresh_mode_colors(self) -> None:
        for name in self._mode_btns:
            self._paint_mode_btn(name)
            lbl = self._mode_labels.get(name)
            if lbl is not None:
                lbl.setStyleSheet("font-weight:600;" if name in self.s.mode_color_overrides()
                                  else "")
        n = len(self.s.mode_color_overrides())
        self.lbl_mode_custom.setText(f"已自定义 {n} 个模式" if n else "全部为默认配色")
        self.btn_mode_reset.setEnabled(bool(n))

    def _reload_widgets(self) -> None:
        for key in theme.CUSTOM_KEYS:
            self._paint_color_button(key)
        self._sync_custom_enabled()
        for key, w in self._widgets.items():
            v = self.s.get(key)
            if isinstance(w, QtWidgets.QSpinBox):
                w.setValue(int(v))
            elif isinstance(w, QtWidgets.QDoubleSpinBox):
                w.setValue(float(v))
            elif isinstance(w, QtWidgets.QComboBox):
                w.setCurrentIndex(max(0, w.findData(str(v))))
            elif isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(v))
        if hasattr(self, "_mode_btns"):
            self._refresh_mode_colors()
        self._refresh_cache_size()

    def _clear_cache(self) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("清除解析缓存")
        box.setText(f"要删掉 {cache_dir()} 下的解析缓存吗？")
        box.setInformativeText("日志本身不会被改动；下次打开同一份日志会重新解析（慢一次）。")
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes
                               | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if box.exec() != int(QtWidgets.QMessageBox.StandardButton.Yes):
            return
        n, b = clear_parse_cache()
        try:
            from apmlog.core.model import human_size
            size = human_size(float(b))
        except Exception:                                     # noqa: BLE001
            size = f"{b} B"
        self.lbl_cache.setText(f"已清除 {n} 个文件，释放 {size}")

    def _refresh_cache_size(self) -> None:
        root = cache_dir()
        try:
            files = [f for f in root.rglob("*") if f.is_file()]
            b = sum(int(f.stat().st_size) for f in files)
        except OSError:
            b = 0
        try:
            from apmlog.core.model import human_size
            size = human_size(float(b))
        except Exception:                                     # noqa: BLE001
            size = f"{b} B"
        self.lbl_cache.setText(f"当前占用 {size}")
