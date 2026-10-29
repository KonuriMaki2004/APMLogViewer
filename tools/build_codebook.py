#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_codebook.py —— 从 ``data/codebooks/_source/`` 离线生成 ``copter_4x.json``。

硬性约束
--------
1. **不联网**：只读 ``_source/`` 里的 ArduPilot 官方源码原文与 ``_source/MANIFEST.json``。
   重新抓取是人工步骤（URL 规则见文末），脚本本身不访问网络。
2. **确定性**：同一份 ``_source`` 重复运行产出**逐字节一致**的 JSON —— 因此
   ``generated_at`` 取自 MANIFEST 的抓取日期，而不是运行时钟。
3. **不猜**：name 与数值一律来自源码正则解析；中文只是枚举名的直译注记；
   源码给不出的条目进 ``unknown``。
4. **4.7 为准**：4.6/4.7 差异合并进同一 section，4.6 差异写进条目 ``note``。
5. **结构对齐内核**：``apmlog/core/codebooks.py`` 从顶层读
   ``mode_numbers`` / ``error_subsystems`` / ``event_ids`` / ``arm_methods`` /
   ``arming_checks_bits`` / ``motb_failflags_bits``，取值支持字符串或
   ``{"name"|"text", "severity", "ecodes": {...}}``；本文档所有条目用 dict 形式。

用法::

    .\\.venv\\Scripts\\python.exe tools\\build_codebook.py            # 生成
    .\\.venv\\Scripts\\python.exe tools\\build_codebook.py --check     # 校验可重现
    .\\.venv\\Scripts\\python.exe tools\\build_codebook.py --report    # 打印统计

源码抓取（人工，一次即可，产物入库 + 更新 MANIFEST.json 的 sha256）::

    https://raw.githubusercontent.com/ArduPilot/ardupilot/92b0cd78/<path>   # ArduCopter V4.6.3
    https://raw.githubusercontent.com/ArduPilot/ardupilot/1511f271/<path>   # ArduCopter V4.7.0

两个 commit 直接来自真日志固件自报版本，比分支/tag 名更精确；本环境抓取时
``Copter-4.6``/``Copter-4.7``/``ArduCopter-4.6.3``/``ArduCopter-4.7.0`` 这些 ref 全部 404，
只有 commit SHA 与 ``master`` 可解析（详见 _source/MANIFEST.json 的 ref_selection.notes）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:                                    # Windows 控制台默认 GBK，中文/° 会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                        # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "data" / "codebooks" / "_source"
OUT_PATH = ROOT / "data" / "codebooks" / "copter_4x.json"

S46, S47 = "4.6", "4.7"
F_MODE = "ArduCopter/mode.h"
F_LOGGER = "libraries/AP_Logger/AP_Logger.h"
F_LOGSTRUCT = "libraries/AP_Logger/LogStructure.h"
F_ERR_H = "libraries/AP_InternalError/AP_InternalError.h"
F_ERR_C = "libraries/AP_InternalError/AP_InternalError.cpp"
F_ARMING = "libraries/AP_Arming/AP_Arming.h"
F_MOT_CPP = "libraries/AP_Motors/AP_MotorsMulticopter.cpp"
F_MOT_H = "libraries/AP_Motors/AP_Motors_Class.h"
F_CRASH = "ArduCopter/crash_check.cpp"
F_EVENTS = "ArduCopter/events.cpp"

# --------------------------------------------------------------------------- #
# 源码证据读取 + 校验
# --------------------------------------------------------------------------- #


def load_manifest() -> dict[str, Any]:
    fp = SRC_DIR / "MANIFEST.json"
    if not fp.exists():
        raise SystemExit(f"缺少源码清单：{fp}")
    man = json.loads(fp.read_text(encoding="utf-8"))
    if not isinstance(man, dict) or not isinstance(man.get("files"), list) or not man["files"]:
        raise SystemExit(f"MANIFEST 结构不对：{fp}")
    return man


def verified_sources(man: dict[str, Any]) -> dict[str, dict[str, str]]:
    """series -> {upstream_path: 原文}；逐文件校验 sha256。"""
    out: dict[str, dict[str, str]] = {}
    for ent in man["files"]:
        fp = SRC_DIR / ent["local"]
        if not fp.exists():
            raise SystemExit(f"源码缺失：{fp}")
        raw = fp.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ent["sha256"]:
            raise SystemExit(f"源码校验失败（sha256 不符）：{ent['local']}")
        out.setdefault(ent["series"], {})[ent["upstream_path"]] = raw.decode("utf-8", "replace")
    for s in (S46, S47):
        if s not in out:
            raise SystemExit(f"MANIFEST 里没有 {s} 系列源码")
    return out


def need(src: dict[str, str], path: str) -> str:
    if path not in src:
        raise SystemExit(f"_source 里没有 {path}（请补抓并更新 MANIFEST）")
    return src[path]


# --------------------------------------------------------------------------- #
# C++ enum 解析
# --------------------------------------------------------------------------- #
_ENUM_HEAD = re.compile(r"^\s*enum\s+(?:class\s+)?(?P<name>[A-Za-z_]\w*)\s*"
                        r"(?::\s*[A-Za-z_]\w*\s*)?\{\s*$")
_MEMBER = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<val>[^,/;]+?)\s*,?\s*"
                     r"(?://\s*(?P<c>.*))?$")
_COMMENT = re.compile(r"^\s*//\s*(?P<c>.*?)\s*$")
_ENUM_END = re.compile(r"^\s*\};\s*$")


def _eval_int(expr: str) -> Optional[int]:
    """十进制 / 0x.. / ``(1U << n)`` / ``1 << n``；解析不了返回 None（不猜）。"""
    e = expr.strip().rstrip("UuLl")
    e = e.replace("(1U", "(1").replace("(1u", "(1")
    e = re.sub(r"<<\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)", r"<<\1", e)
    e = e.strip().strip("()").strip()
    if m := re.fullmatch(r"1\s*<<\s*(\d+)", e):
        return 1 << int(m.group(1))
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", e):
        return int(e, 16)
    if re.fullmatch(r"\d+", e):
        return int(e)
    return None


def parse_enum(text: str, enum_name: str) -> list[dict[str, Any]]:
    """解析 ``enum [class] <enum_name>[: type] { ... };`` → [{name,value,comment,group}]。

    ``group`` 取 enum 体内最近一条独立注释行（``LogErrorCode`` 靠它区分各子系统的
    专用错误码），值来自源码，不是推测。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = _ENUM_HEAD.match(ln)
        if m and m.group("name") == enum_name:
            start = i + 1
            break
    if start is None:
        raise SystemExit(f"源码里找不到 enum {enum_name}")
    members: list[dict[str, Any]] = []
    group = ""
    for j in range(start, len(lines)):
        ln = lines[j]
        if _ENUM_END.match(ln):
            return members
        if not ln.strip():
            continue
        cm = _COMMENT.match(ln)
        if cm and "=" not in ln:
            g = cm.group("c")
            if g and not g.startswith("@"):
                group = g
            continue
        mm = _MEMBER.match(ln)
        if not mm:
            continue
        members.append({"name": mm.group("name"), "value": _eval_int(mm.group("val")),
                        "comment": (mm.group("c") or "").strip(), "group": group})
    raise SystemExit(f"enum {enum_name} 没有正常结束的 '}};'")


def enum_map(text: str, enum_name: str) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for m in parse_enum(text, enum_name):
        if m["value"] is None:
            print(f"[warn] enum {enum_name} 成员 {m['name']} 的值无法解析，已跳过（不猜）")
            continue
        out[int(m["value"])] = m
    if not out:
        raise SystemExit(f"enum {enum_name} 解析结果为空")
    return out


def name_map(text: str, enum_name: str) -> dict[str, int]:
    return {e["name"]: e["value"] for e in parse_enum(text, enum_name) if e["value"] is not None}


# --------------------------------------------------------------------------- #
# 直译注记（英文枚举名 → 中文；不改语义）
# --------------------------------------------------------------------------- #
MODE_ZH: dict[str, str] = {}     # 模式名保持源码大写原文，便于与飞控/GCS 对齐

EV_ZH = {
    "ARMED": "解锁", "DISARMED": "上锁", "AUTO_ARMED": "自动解锁",
    "LAND_COMPLETE_MAYBE": "疑似着陆", "LAND_COMPLETE": "着陆完成", "LOST_GPS": "丢失 GPS",
    "FLIP_START": "翻滚开始", "FLIP_END": "翻滚结束", "SET_HOME": "设置 HOME",
    "SET_SIMPLE_ON": "简单模式开", "SET_SIMPLE_OFF": "简单模式关", "NOT_LANDED": "未着陆",
    "SET_SUPERSIMPLE_ON": "超级简单模式开", "AUTOTUNE_INITIALISED": "自动调参初始化",
    "AUTOTUNE_OFF": "自动调参关", "AUTOTUNE_RESTART": "自动调参重启",
    "AUTOTUNE_SUCCESS": "自动调参成功", "AUTOTUNE_FAILED": "自动调参失败",
    "AUTOTUNE_REACHED_LIMIT": "自动调参达上限", "AUTOTUNE_PILOT_TESTING": "自动调参试飞",
    "AUTOTUNE_SAVEDGAINS": "自动调参存增益", "SAVE_TRIM": "保存配平",
    "SAVEWP_ADD_WP": "保存航点", "FENCE_ENABLE": "围栏启用", "FENCE_DISABLE": "围栏关闭",
    "ACRO_TRAINER_OFF": "特技教练关", "ACRO_TRAINER_LEVELING": "特技教练自动水平",
    "ACRO_TRAINER_LIMITED": "特技教练限制", "GRIPPER_GRAB": "夹爪抓取",
    "GRIPPER_RELEASE": "夹爪释放", "PARACHUTE_DISABLED": "降落伞禁用",
    "PARACHUTE_ENABLED": "降落伞启用", "PARACHUTE_RELEASED": "抛伞",
    "LANDING_GEAR_DEPLOYED": "起落架放下", "LANDING_GEAR_RETRACTED": "起落架收起",
    "MOTORS_EMERGENCY_STOPPED": "电机急停", "MOTORS_EMERGENCY_STOP_CLEARED": "电机急停解除",
    "MOTORS_INTERLOCK_DISABLED": "动力互锁关闭", "MOTORS_INTERLOCK_ENABLED": "动力互锁开启",
    "ROTOR_RUNUP_COMPLETE": "旋翼加速完成", "ROTOR_SPEED_BELOW_CRITICAL": "转速低于临界",
    "EKF_ALT_RESET": "EKF 高度重置", "LAND_CANCELLED_BY_PILOT": "飞行员取消降落",
    "EKF_YAW_RESET": "EKF 航向重置", "AVOIDANCE_ADSB_ENABLE": "ADS-B 避碰启用",
    "AVOIDANCE_ADSB_DISABLE": "ADS-B 避碰关闭", "AVOIDANCE_PROXIMITY_ENABLE": "近距避障启用",
    "AVOIDANCE_PROXIMITY_DISABLE": "近距避障关闭", "GPS_PRIMARY_CHANGED": "主 GPS 切换",
    "ZIGZAG_STORE_A": "之字形 A 点存储", "ZIGZAG_STORE_B": "之字形 B 点存储",
    "LAND_REPO_ACTIVE": "着陆点上报激活", "STANDBY_ENABLE": "STANDBY 启用",
    "STANDBY_DISABLE": "STANDBY 关闭", "FENCE_ALT_MAX_ENABLE": "最高围栏启用",
    "FENCE_ALT_MAX_DISABLE": "最高围栏关闭", "FENCE_CIRCLE_ENABLE": "圆形围栏启用",
    "FENCE_CIRCLE_DISABLE": "圆形围栏关闭", "FENCE_ALT_MIN_ENABLE": "最低围栏启用",
    "FENCE_ALT_MIN_DISABLE": "最低围栏关闭", "FENCE_POLYGON_ENABLE": "多边形围栏启用",
    "FENCE_POLYGON_DISABLE": "多边形围栏关闭", "EK3_SOURCES_SET_TO_PRIMARY": "EK3 源设为主",
    "EK3_SOURCES_SET_TO_SECONDARY": "EK3 源设为次", "EK3_SOURCES_SET_TO_TERTIARY": "EK3 源设为第三",
    "AIRSPEED_PRIMARY_CHANGED": "主空速源切换", "SURFACED": "已浮出水面",
    "NOT_SURFACED": "未浮出水面", "BOTTOMED": "已触底", "NOT_BOTTOMED": "未触底",
}
#: 事件判级：ArduPilot 不给 EV 判级，这是 APMLogViewer 的展示判级（provenance 标 editorial）
EV_SEVERITY = {
    "LOST_GPS": "error", "MOTORS_EMERGENCY_STOPPED": "critical",
    "MOTORS_INTERLOCK_DISABLED": "error", "EKF_ALT_RESET": "warn", "EKF_YAW_RESET": "warn",
    "DISARMED": "hint", "PARACHUTE_RELEASED": "critical", "AUTOTUNE_FAILED": "warn",
    "ROTOR_SPEED_BELOW_CRITICAL": "error", "FENCE_ENABLE": "hint",
    "STANDBY_ENABLE": "hint", "STANDBY_DISABLE": "hint",
}
SUBSYS_ZH = {
    "MAIN": "主控/导航", "RADIO": "遥控链路", "COMPASS": "罗盘", "OPTFLOW": "光流",
    "FAILSAFE_RADIO": "遥控失效保护", "FAILSAFE_BATT": "电池失效保护",
    "FAILSAFE_GPS": "GPS 失效保护", "FAILSAFE_GCS": "地面站失效保护",
    "FAILSAFE_FENCE": "围栏失效保护", "FLIGHT_MODE": "飞行模式", "GPS": "GPS",
    "CRASH_CHECK": "炸机检测", "FLIP": "翻滚", "AUTOTUNE": "自动调参",
    "PARACHUTES": "降落伞", "EKFCHECK": "EKF 检查", "FAILSAFE_EKFINAV": "EKF 导航失效保护",
    "BARO": "气压计", "CPU": "CPU", "FAILSAFE_ADSB": "ADS-B 失效保护",
    "TERRAIN": "地形数据", "NAVIGATION": "导航", "FAILSAFE_TERRAIN": "地形失效保护",
    "EKF_PRIMARY": "EKF 主源", "THRUST_LOSS_CHECK": "推力损失检测",
    "FAILSAFE_SENSORS": "传感器失效保护", "FAILSAFE_LEAK": "漏水失效保护",
    "PILOT_INPUT": "飞行员输入", "FAILSAFE_VIBE": "震动失效保护",
    "INTERNAL_ERROR": "内部错误", "FAILSAFE_DEADRECKON": "推算导航失效保护",
}
ECODE_ZH = {
    "ERROR_RESOLVED": "已恢复", "FAILED_TO_INITIALISE": "初始化失败", "UNHEALTHY": "状态不健康",
    "RADIO_LATE_FRAME": "遥控帧迟到", "FAILSAFE_RESOLVED": "失效保护解除",
    "FAILSAFE_OCCURRED": "失效保护触发", "MAIN_INS_DELAY": "惯导数据延迟",
    "CRASH_CHECK_CRASH": "炸机判据触发", "CRASH_CHECK_LOSS_OF_CONTROL": "失控（抛伞判据）",
    "FLIP_ABANDONED": "翻滚被放弃", "MISSING_TERRAIN_DATA": "缺少地形数据",
    "FAILED_TO_SET_DESTINATION": "目的地设置失败", "RESTARTED_RTL": "RTL 重新启动",
    "FAILED_CIRCLE_INIT": "环绕初始化失败", "DEST_OUTSIDE_FENCE": "目的地在围栏外",
    "RTL_MISSING_RNGFND": "RTL 缺少测距仪", "INTERNAL_ERRORS_DETECTED": "检测到内部错误",
    "PARACHUTE_TOO_LOW": "高度过低未抛伞", "PARACHUTE_LANDED": "已着陆",
    "EKFCHECK_BAD_VARIANCE": "EKF 方差过大", "EKFCHECK_VARIANCE_CLEARED": "EKF 方差恢复",
    "BARO_GLITCH": "气压计跳变", "BAD_DEPTH": "深度异常", "GPS_GLITCH": "GPS 跳变",
}
METHOD_ZH = {
    "RUDDER": "舵量组合解锁", "MAVLINK": "MAVLink 指令解锁", "AUXSWITCH": "辅助通道开关解锁",
    "MOTORTEST": "电机测试", "SCRIPTING": "脚本(Lua)", "TERMINATION": "失控终止(仅上锁)",
    "CPUFAILSAFE": "CPU 失效保护(仅上锁)", "BATTERYFAILSAFE": "电池失效保护(仅上锁)",
    "SOLOPAUSEWHENLANDED": "Solo 着陆暂停(仅上锁)", "AFS": "AFS(仅上锁)",
    "ADSBCOLLISIONACTION": "ADS-B 避碰动作(仅上锁)", "PARACHUTE_RELEASE": "抛伞(仅上锁)",
    "CRASH": "炸机(仅上锁)", "LANDED": "着陆(仅上锁)", "MISSIONEXIT": "任务结束(仅上锁)",
    "FENCEBREACH": "越出围栏(仅上锁)", "RADIOFAILSAFE": "遥控失效保护(仅上锁)",
    "DISARMDELAY": "延时上锁(仅上锁)", "GCSFAILSAFE": "地面站失效保护(仅上锁)",
    "TERRRAINFAILSAFE": "地形失效保护(源码双 r 拼写)", "FAILSAFE_ACTION_TERMINATE": "失效保护终止动作",
    "TERRAINFAILSAFE": "地形失效保护", "MOTORDETECTDONE": "电机检测完成",
    "BADFLOWOFCONTROL": "异常控制流", "EKFFAILSAFE": "EKF 失效保护",
    "GCS_FAILSAFE_SURFACEFAILED": "GCS 失效保护迫降失败", "GCS_FAILSAFE_HOLDFAILED": "GCS 失效保护悬停失败",
    "TAKEOFFTIMEOUT": "起飞超时", "AUTOLANDED": "自动着陆", "PILOT_INPUT_FAILSAFE": "飞行员输入失效保护",
    "TOYMODELANDTHROTTLE": "玩具模式油门降落", "TOYMODELANDFORCE": "玩具模式强制降落",
    "LANDING": "降落", "DEADRECKON_FAILSAFE": "推算导航失效保护", "BLACKBOX": "黑匣子",
    "DDS": "DDS", "AUTO_ARM_ONCE": "一次性自动解锁", "TURTLE_MODE": "乌龟模式",
    "TOYMODE": "玩具模式", "UNKNOWN": "未知",
}
CHECK_ZH = {
    "BARO": "气压计", "COMPASS": "罗盘", "GPS": "GPS", "INS": "惯导", "PARAMETERS": "参数",
    "RC": "遥控", "VOLTAGE": "电压", "BATTERY": "电池", "AIRSPEED": "空速", "LOGGING": "日志",
    "SWITCH": "解锁开关", "GPS_CONFIG": "GPS 配置", "SYSTEM": "系统", "MISSION": "任务",
    "RANGEFINDER": "测距仪", "CAMERA": "相机", "AUX_AUTH": "辅助授权", "VISION": "视觉",
    "FFT": "FFT", "OSD": "OSD", "CHECK_LAST": "检查项上限（哨兵，非真实检查）",
}
CRASH_GLOSS = "姿态误差>30°、倾角>15°、加速度<3m/s²、速度<10m/s 持续 2s"
#: 这两条 ERR 在 crash_check.cpp 里紧跟 MAV_SEVERITY_EMERGENCY 的 GCS 文本 → 判级有源码依据
SEVERITY_FROM_SOURCE = {"CRASH_CHECK": "critical", "THRUST_LOSS_CHECK": "critical"}
#: 其余子系统判级是 APMLogViewer 的展示判级（provenance 里标 editorial）
SEVERITY_EDITORIAL = {
    "INTERNAL_ERROR": "critical", "PARACHUTES": "critical",
    "FAILSAFE_RADIO": "error", "FAILSAFE_BATT": "error", "FAILSAFE_GPS": "error",
    "FAILSAFE_GCS": "error", "FAILSAFE_FENCE": "error", "FAILSAFE_EKFINAV": "error",
    "FAILSAFE_ADSB": "error", "FAILSAFE_TERRAIN": "error", "FAILSAFE_SENSORS": "error",
    "FAILSAFE_LEAK": "error", "FAILSAFE_VIBE": "error", "FAILSAFE_DEADRECKON": "error",
    "GPS": "error", "COMPASS": "error", "BARO": "error", "EKFCHECK": "error", "MAIN": "error",
    "NAVIGATION": "error", "EKF_PRIMARY": "warn", "CPU": "warn", "FLIGHT_MODE": "warn",
    "FLIP": "warn", "TERRAIN": "warn", "PILOT_INPUT": "warn", "OPTFLOW": "warn",
    "AUTOTUNE": "warn",
}
#: LogErrorCode 的 enum 内分组注释（原文见 _source）→ 适用的子系统枚举名
GROUP_KEYWORDS = [
    ("general error codes", ("__ALL__",)),
    ("radio", ("RADIO",)),
    ("failsafe_thr, batt, gps", ("FAILSAFE_BATT", "FAILSAFE_GPS", "GPS")),
    ("main", ("MAIN",)),
    ("crash checker", ("CRASH_CHECK",)),
    ("flip", ("FLIP",)),
    ("terrain", ("TERRAIN", "FAILSAFE_TERRAIN")),
    ("navigation", ("NAVIGATION",)),
    ("internal_error", ("INTERNAL_ERROR",)),
    ("parachute", ("PARACHUTES",)),
    ("ekf check", ("EKFCHECK",)),
    ("baro", ("BARO",)),
    ("gps specific", ("GPS",)),
]
GENERAL_CODES = ("ERROR_RESOLVED", "FAILED_TO_INITIALISE", "UNHEALTHY")
FAILSAFE_GENERIC = ("FAILSAFE_OCCURRED", "FAILSAFE_RESOLVED")


def _zh(enum_name: str, table: dict[str, str]) -> str:
    g = table.get(enum_name)
    if not g or g.strip().lower() == enum_name.strip().lower():
        return enum_name          # 直译与原文同名（如 GPS）时不重复输出
    return f"{enum_name} {g}"


def _notes(items: list[str]) -> Optional[str]:
    items = [x for x in items if x]
    return "；".join(items) or None


def _ver_notes(v46: Optional[int], v47: Optional[int], only46: bool, only47: bool) -> Optional[str]:
    if only47:
        return f"4.7 新增（4.6 无此编号）"
    if only46:
        return "仅 4.6 定义，4.7 已移除"
    if v46 is not None and v47 is not None and v46 != v47:
        return f"4.6 为 {v46}，4.7 为 {v47}，本表按 4.7"
    return None


def _merge(src46map: dict[int, Any], src47map: dict[int, Any]) -> list[tuple[int, Any, Any, Any]]:
    """→ [(编号, 用于取名的条目(4.7 优先), 4.6 条目, 4.7 条目)]"""
    rows = []
    for num in sorted(set(src46map) | set(src47map)):
        e46, e47 = src46map.get(num), src47map.get(num)
        rows.append((num, e47 or e46, e46, e47))
    return rows


# --------------------------------------------------------------------------- #
# section 构建
# --------------------------------------------------------------------------- #
def _same_note(section: str, m46: list[dict[str, Any]], m47: list[dict[str, Any]], label: str) -> None:
    """脚本自己比两版 enum 成员，把结论写进 provenance（不靠人工 diff 声明）。

    参数是 parse_enum 的完整成员列表（而不是 value→entry 的 dict），
    因为 LogErrorCode 故意大量重复数值，用 dict 会丢成员。
    """
    p46 = {(e["value"], e["name"]) for e in m46 if e["value"] is not None}
    p47 = {(e["value"], e["name"]) for e in m47 if e["value"] is not None}
    diffs = [f"{n}={v} 仅 4.6" for v, n in sorted(p46 - p47)] + \
            [f"{n}={v} 仅 4.7" for v, n in sorted(p47 - p46)]
    DYNAMIC_NOTES.setdefault(section, []).append(
        f"{label}：4.6 与 4.7 逐成员比对结果 —— " + ("；".join(diffs) if diffs else "完全一致"))


def build_mode_numbers(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    txt46, txt47 = need(s46, F_MODE), need(s47, F_MODE)
    m46, m47 = enum_map(txt46, "Number"), enum_map(txt47, "Number")
    _same_note("mode_numbers", parse_enum(txt46, "Number"), parse_enum(txt47, "Number"),
               "Mode::Number")
    table: dict[str, Any] = {}
    for num, ref, e46, e47 in _merge(m46, m47):
        item: dict[str, Any] = {"name": ref["name"]}          # 保持源码大写原文
        if ref["comment"]:
            item["source_comment"] = ref["comment"]
        if ref["name"] in MODE_ZH:
            item["zh"] = MODE_ZH[ref["name"]]
        n = _ver_notes(e46["value"] if e46 else None, e47["value"] if e47 else None,
                       e47 is None, e46 is None)
        if n:
            item["note"] = n
        table[str(num)] = item
    return table


def build_event_ids(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    txt47, txt46 = need(s47, F_LOGGER), need(s46, F_LOGGER)
    e47, e46 = enum_map(txt47, "LogEvent"), enum_map(txt46, "LogEvent")
    _same_note("event_ids", parse_enum(txt46, "LogEvent"), parse_enum(txt47, "LogEvent"),
               "LogEvent")
    table: dict[str, Any] = {}
    for num, ref, a46, a47 in _merge(e46, e47):
        item: dict[str, Any] = {"name": _zh(ref["name"], EV_ZH)}
        if ref["comment"]:
            item["source_comment"] = ref["comment"]
        if ref["name"] in EV_SEVERITY:
            item["severity"] = EV_SEVERITY[ref["name"]]
            item["severity_basis"] = "editorial"
        n = _ver_notes(a46["value"] if a46 else None, a47["value"] if a47 else None,
                       a47 is None, a46 is None)
        if n:
            item["note"] = n
        table[str(num)] = item
    return table


def _write_error_pairs(src: dict[str, str]) -> dict[tuple[int, int], dict[str, Any]]:
    """ArduCopter 源码里真实的 LOGGER_WRITE_ERROR(子系统, 错误码) 调用。

    返回 (subsys值, ecode值) -> {"ecode_name": 源码里的错误码枚举名, "paths": [调用所在文件]}。
    ecode_name 直接取调用点写出的枚举名，避免同一数值在多个子系统下被猜错。
    """
    pat = re.compile(r"LOGGER_WRITE_ERROR\(\s*LogErrorSubsystem::(\w+)\s*,\s*"
                     r"LogErrorCode::(\w+)\s*\)")
    pairs: dict[tuple[int, int], dict[str, Any]] = {}
    names = name_map(need(src, F_LOGGER), "LogErrorSubsystem")
    codes = name_map(need(src, F_LOGGER), "LogErrorCode")
    for path in (F_CRASH, F_EVENTS):
        if path not in src:
            continue
        for subsys, ecode in pat.findall(src[path]):
            sv, cv = names.get(subsys), codes.get(ecode)
            if sv is None or cv is None:
                print(f"[warn] {path} 里的 {subsys}/{ecode} 在枚举中找不到，跳过")
                continue
            rec = pairs.setdefault((sv, cv), {"ecode_name": ecode, "paths": set()})
            rec["paths"].add(path)
    return pairs


def _merge_used(u46: dict[tuple[int, int], dict[str, Any]],
                u47: dict[tuple[int, int], dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    """合并两版证据；同名冲突按 4.7 为准，paths 取并集。"""
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key in sorted(set(u46) | set(u47)):
        a, b = u46.get(key), u47.get(key)
        out[key] = {"ecode_name": (b or a)["ecode_name"],
                    "paths": set(a["paths"] if a else ()) | set(b["paths"] if b else ())}
    return out


def build_error_subsystems(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    txt47, txt46 = need(s47, F_LOGGER), need(s46, F_LOGGER)
    sys47, sys46 = enum_map(txt47, "LogErrorSubsystem"), enum_map(txt46, "LogErrorSubsystem")
    # LogErrorCode 的数值大量重复（源码注释自承认），必须用完整成员列表；
    # 用 value→entry 的 dict 会把同值码（如 ERROR_RESOLVED/FAILSAFE_RESOLVED）丢掉。
    code47 = parse_enum(txt47, "LogErrorCode")
    code46 = parse_enum(txt46, "LogErrorCode")
    _same_note("error_subsystems", parse_enum(txt46, "LogErrorSubsystem"),
               parse_enum(txt47, "LogErrorSubsystem"), "LogErrorSubsystem")
    name2sub = {e["name"]: v for v, e in sys47.items()}

    # (a) 用 enum 内的分组注释把 LogErrorCode 路由到子系统
    routed: dict[str, dict[int, str]] = {n: {} for n in name2sub}
    for e in sorted(code47, key=lambda x: (x["value"], x["name"])):
        grp = (e["group"] or "").lower()
        targets: list[str] = []
        for kw, subs in GROUP_KEYWORDS:
            if kw in grp:
                targets.extend(subs)
        if "__ALL__" in targets:                      # general error codes 适用所有子系统
            targets = [n for n in name2sub]
        if e["name"] in FAILSAFE_GENERIC:
            targets.extend([n for n in name2sub if n.startswith("FAILSAFE_")] + ["GPS"])
        for t in sorted(set(targets)):
            cur = routed[t].get(e["value"])
            if cur is None or (cur in GENERAL_CODES and e["name"] not in GENERAL_CODES):
                routed[t][e["value"]] = e["name"]

    # (b) 真实调用是最强证据，覆盖 (a)
    used = _merge_used(_write_error_pairs(s46), _write_error_pairs(s47))

    # 两版 LogErrorCode 差异（脚本自己核，不只靠人工 diff）
    p46 = {(e["name"], e["value"]) for e in code46}
    p47 = {(e["name"], e["value"]) for e in code47}
    for nm, v in sorted(p46 - p47):
        DYNAMIC_NOTES.setdefault("error_subsystems", []).append(f"4.6 独有 LogErrorCode：{nm}={v}")
    for nm, v in sorted(p47 - p46):
        DYNAMIC_NOTES.setdefault("error_subsystems", []).append(f"4.7 新增 LogErrorCode：{nm}={v}")

    table: dict[str, Any] = {}
    for num, ref, a46, a47 in _merge(sys46, sys47):
        subsys = ref["name"]
        sev = SEVERITY_FROM_SOURCE.get(subsys) or SEVERITY_EDITORIAL.get(subsys, "error")
        notes: list[str] = []
        if "not used" in ref["comment"].lower():
            notes.append("源码注释标注 not used（4.6/4.7 均保留编号）")
        n = _ver_notes(a46["value"] if a46 else None, a47["value"] if a47 else None,
                       a47 is None, a46 is None)
        if n:
            notes.append(n)
        codes: dict[int, str] = dict(routed.get(subsys, {}))
        for (sv, cv), rec in used.items():
            if sv == num:
                codes[cv] = rec["ecode_name"]
        if num == name2sub.get("CRASH_CHECK"):
            codes[1] = "CRASH_CHECK_CRASH"
            codes[2] = "CRASH_CHECK_LOSS_OF_CONTROL"
        ecodes: dict[str, Any] = {}
        for cv in sorted(codes):
            en = codes[cv]
            gloss = ECODE_ZH.get(en, "")
            cell: dict[str, Any] = {}
            if cv == 1 and num == name2sub.get("CRASH_CHECK"):
                cell["name"] = f"CRASH_CHECK_CRASH 炸机判据触发（{CRASH_GLOSS}）"
            else:
                cell["name"] = f"{en} {gloss}" if gloss else en
            rec = used.get((num, cv))
            src_paths = sorted(rec["paths"]) if rec else []
            if src_paths:
                cell["source_usage"] = src_paths
            ecodes[str(cv)] = cell
        item: dict[str, Any] = {"name": _zh(subsys, SUBSYS_ZH), "severity": sev,
                                "severity_basis": ("source: paired MAV_SEVERITY_EMERGENCY in "
                                                   "ArduCopter/crash_check.cpp"
                                                   if subsys in SEVERITY_FROM_SOURCE else "editorial")}
        if ecodes:
            item["ecodes"] = ecodes
        if notes:
            item["note"] = _notes(notes)
        table[str(num)] = item
    return table


def build_arm_methods(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    txt46, txt47 = need(s46, F_ARMING), need(s47, F_ARMING)
    m46, m47 = enum_map(txt46, "Method"), enum_map(txt47, "Method")
    _same_note("arm_methods", parse_enum(txt46, "Method"), parse_enum(txt47, "Method"),
               "AP_Arming::Method")
    table: dict[str, Any] = {}
    for num, ref, a46, a47 in _merge(m46, m47):
        item: dict[str, Any] = {"name": _zh(ref["name"], METHOD_ZH)}
        if ref["comment"]:
            item["source_comment"] = ref["comment"]
        n = _ver_notes(a46["value"] if a46 else None, a47["value"] if a47 else None,
                       a47 is None, a46 is None)
        if n:
            item["note"] = n
        table[str(num)] = item
    return table


def _bit_index(value: int, name: str) -> int:
    """(1U << n) 型位标枚举 → 位号 n。内核按位号查表，不是按掩码值。"""
    if value <= 0 or value & (value - 1):
        raise SystemExit(f"{name} 的值 {value} 不是单一位，无法得位号（不猜）")
    return value.bit_length() - 1


def build_arming_checks(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    """4.6: ``enum ArmingChecks``（含 ARMING_CHECK_ALL=1<<0）；4.7: ``enum class Check``。"""
    c46 = enum_map(need(s46, F_ARMING), "ArmingChecks")
    c47 = enum_map(need(s47, F_ARMING), "Check")
    # key 用位号（1<<n 的 n），不是掩码值
    n46 = {e["name"].removeprefix("ARMING_CHECK_"): (_bit_index(v, e["name"]), e)
           for v, e in c46.items()}
    n47 = {e["name"]: (_bit_index(v, e["name"]), e) for v, e in c47.items()}
    _same_note("arming_checks_bits",
               [{"name": k, "value": b} for k, (b, _e) in n46.items()],
               [{"name": k, "value": b} for k, (b, _e) in n47.items()],
               "ArmingChecks/Check 位集")
    table: dict[str, Any] = {}
    for nm in sorted(set(n46) | set(n47),
                     key=lambda x: (n47.get(x, n46.get(x, (0, None)))[0], x)):
        a47, a46 = n47.get(nm), n46.get(nm)
        bit, ref = a47 or a46
        assert ref is not None and bit is not None
        notes: list[str] = []
        if bit == 0:
            label = "ALL 全部检查（位0：4.6 为 ARMING_CHECK_ALL；" \
                    "4.7 注释 “0 used to be ALL” 但 ARM 报文仍会读到该位）"
        elif nm == "CHECK_LAST":
            label = "CHECK_LAST 检查项上限（哨兵位，非真实检查项）"
        else:
            label = _zh(nm, CHECK_ZH)
        if a47 is None:
            notes.append("仅 4.6 有该位（4.7 的 enum class Check 未定义）")
        elif a46 is None:
            notes.append("4.7 新增该位（4.6 无）")
        elif a46[0] != a47[0]:
            notes.append(f"4.6 位号 {a46[0]}，4.7 位号 {a47[0]}，按 4.7")
        item: dict[str, Any] = {"name": label}
        if ref["comment"] and bit != 0:
            item["source_comment"] = ref["comment"]
        if notes:
            item["note"] = _notes(notes)
        table[str(bit)] = item
    return table


#: 成员注释的直译（英文原文保留在 source_comment 里）
MOTB_ZH = {"_thrust_boost": "推力提升已启用（应对电机失效）",
           "_thrust_balanced": "输出推力已平衡"}


def _motb_expr(text: str) -> str:
    m = re.search(r"mot_fail_flags\s*:\s*\(uint8_t\)(?P<expr>\([^,]*\))", text)
    if not m:
        raise SystemExit("AP_MotorsMulticopter.cpp 里找不到 mot_fail_flags 的位组装表达式")
    return m.group("expr")


def _parse_flags(expr: str) -> list[tuple[str, Optional[int]]]:
    out: list[tuple[str, Optional[int]]] = []
    for part in expr.strip().strip("()").split("|"):
        part = part.strip().strip("()").strip()
        m = re.fullmatch(r"(?P<flag>_?\w+)(?:\s*<<\s*(?P<shift>\d+)U?)?", part)
        if not m:
            raise SystemExit(f"无法解析 MOTB 位表达式片段 {part!r}（不猜，直接失败）")
        out.append((m.group("flag"),
                    int(m.group("shift")) if m.group("shift") else None))
    return out


def build_motb_flags(s46: dict[str, str], s47: dict[str, str]) -> dict[str, Any]:
    e47, e46 = _motb_expr(need(s47, F_MOT_CPP)), _motb_expr(need(s46, F_MOT_CPP))
    f47, f46 = _parse_flags(e47), _parse_flags(e46)

    def doc(flag: str) -> str:
        for src in (s47, s46):
            m = re.search(rf"\bbool\s+{re.escape(flag)}\s*;\s*//\s*(?P<c>[^\n]*)", need(src, F_MOT_H))
            if m:
                return m.group("c").strip()
        return ""

    table: dict[str, Any] = {}
    for i, (flag, shift) in enumerate(f47):
        bit = shift if shift is not None else i
        d = doc(flag)
        zh = MOTB_ZH.get(flag, "")
        label = flag.lstrip("_")
        if zh:
            label = f"{label} {zh}"
        item: dict[str, Any] = {"name": label}
        item["source_field"] = flag
        if d:
            item["source_comment"] = d
        if f46 != f47:
            item["note"] = f"4.6 表达式为 {e46}，本表按 4.7（{e47}）"
        table[str(bit)] = item
    return table


# --------------------------------------------------------------------------- #
# 出处 / unknown
# --------------------------------------------------------------------------- #
#: 构建过程中算出的两版差异，最后写进对应 section 的 provenance.note
DYNAMIC_NOTES: dict[str, list[str]] = {}

SECTION_META: dict[str, dict[str, Any]] = {
    "mode_numbers": {
        "enum": "Mode::Number", "files": [F_MODE],
        "basis": "逐条解析枚举成员；name 为源码枚举名原文（大写），未加解释",
    },
    "event_ids": {
        "enum": "LogEvent", "files": [F_LOGGER],
        "basis": "逐条解析枚举成员；severity 是 APMLogViewer 的展示判级（条目内标 severity_basis=editorial）",
        "note": "旧命名 EVArmDisable/EVDISARM/EVStabilize 在 4.6/4.7 源码中不存在；解锁/上锁是 "
                "LogEvent::ARMED(10)/DISARMED(11)。"
                "未发现 Copter 机型专有 EV 段扩展（ArduCopter/*.cpp 只用 LogEvent::）。",
    },
    "error_subsystems": {
        "enum": ["LogErrorSubsystem", "LogErrorCode"], "files": [F_LOGGER, F_CRASH, F_EVENTS,
                                                                 F_ERR_H, F_ERR_C],
        "basis": "Subsys 名来自 LogErrorSubsystem；ECode 先用 LogErrorCode 的 enum 内分组注释路由到"
                 "子系统，再用 ArduCopter 源码里真实的 LOGGER_WRITE_ERROR(LogErrorSubsystem::X, "
                 "LogErrorCode::Y) 调用校正（条目内 source_usage 给出文件）",
        "note": "ERR.Subsys 不来自 AP_InternalError。4.6/4.7 的 AP_InternalError 已改为 "
                "error_t 位掩码 + error_bit_descriptions[]（见 _source 原文），既没有 subsystem 数组"
                "也没有 error_code_str。源码注释原文：“bizarrely this enumeration has lots of "
                "duplicate values”——同一 ECode 在不同子系统下语义不同，所以 ecodes 挂在子系统下。",
    },
    "arm_methods": {
        "enum": "AP_Arming::Method", "files": [F_ARMING],
        "basis": "逐条解析枚举成员",
        "note": "该库在 4.6/4.7 已由 AC_Arming 改名为 AP_Arming（libraries/AC_Arming 在两版均不存在，"
                "抓取时 404）。4.7 比 4.6 多 36/37/38（AUTO_ARM_ONCE/TURTLE_MODE/TOYMODE）。",
    },
    "arming_checks_bits": {
        "enum": ["AP_Arming::Check (4.7)", "AP_Arming::ArmingChecks (4.6)"], "files": [F_ARMING],
        "basis": "key 是位号（由源码 (1U << n) 移位表达式解析），value 是检查名",
        "note": "4.7 把 enum ArmingChecks 改名 enum class Check 并去掉 ARMING_CHECK_ 前缀；"
                "位 1..20 名称与位号在两版一致；位 0 在 4.6 是 ARMING_CHECK_ALL，4.7 注释说明曾为 ALL；"
                "位 21 是 4.7 的 CHECK_LAST 哨兵。",
    },
    "motb_failflags_bits": {
        "enum": None, "files": [F_MOT_CPP, F_MOT_H, F_LOGSTRUCT],
        "basis": "解析 AP_MotorsMulticopter::Log_Write() 里 "
                 "mot_fail_flags : (uint8_t)(_thrust_boost | (_thrust_balanced << 1U)) 的位组装"
                 "表达式得到位号，语义取 AP_Motors_Class.h 成员声明行的注释；条目 name 里的中文是"
                 "该行注释的直译，source_comment 保留英文原文",
        "note": "MOTB 报文格式见 LogStructure.h 的 \"MOTB\", \"QfffffB\", "
                "\"TimeUS,LiftMax,BatVolt,ThLimit,ThrAvMx,ThrOut,FailFlags\"，FailFlags 是 uint8_t，"
                "4.6/4.7 只有这 2 位；AP_Motors 里没有独立的 FailFlags 枚举。",
    },
}

UNKNOWN = [
    {"item": "EV.Arg", "reason": "EV 报文 Arg 的语义随 Id 变化，4.6/4.7 源码没有统一枚举；"
                                 "内核 EventRec 只在 extra 保留原值，故不映射"},
    {"item": "MODE.Rsn", "reason": "ModeReason 在 libraries/AP_Vehicle/ModeReason.h，未抓取且内核"
                                   "codebooks.py 无对应 section"},
    {"item": "CMD.Cmd", "reason": "属 MAVLink MAV_CMD 枚举，非 Copter 源码，内核无对应 section"},
    {"item": "ERR Subsys > 31", "reason": "4.6/4.7 的 LogErrorSubsystem 最大 31，真日志未出现更大值，不猜测"},
    {"item": "AP_InternalError 内部错误位", "reason": "4.6/4.7 已改为 error_t 位掩码 + "
                                                     "error_bit_descriptions，与 ERR.Subsys/ECode 不是同一套"
                                                     "编码；内核目前没有对应 section，需要时再加"},
    {"item": "mode 编号 30 / 127", "reason": "mode.h 只有注释保留（30=offboard/lua、127=Skybrush 分叉），"
                                             "没有枚举名，不收录"},
    {"item": "LogEvent 68/69/70", "reason": "源码注释 “68, 69, 70 were winch events”，已废弃，不收录"},
    {"item": "MOTB FailFlags 位 >=2", "reason": "源码只组装 2 位，其余位不出现；不编造"},
]

USED_FILES = {
    "mode_numbers": [F_MODE],
    "event_ids": [F_LOGGER],
    "error_subsystems": [F_LOGGER, F_CRASH, F_EVENTS, F_ERR_H, F_ERR_C],
    "arm_methods": [F_ARMING],
    "arming_checks_bits": [F_ARMING],
    "motb_failflags_bits": [F_MOT_CPP, F_MOT_H, F_LOGSTRUCT],
}


def provenance_block(man: dict[str, Any]) -> dict[str, Any]:
    if set(USED_FILES) != set(SECTION_META) or set(USED_FILES) != set(SECTIONS_ORDER):
        raise SystemExit(f"USED_FILES/SECTION_META/SECTIONS_ORDER 丌不齐：{set(USED_FILES)} vs "
                         f"{set(SECTION_META)}")
    idx = {(e["series"], e["upstream_path"]): e for e in man["files"]}
    out: dict[str, Any] = {}
    for section, files in USED_FILES.items():
        rows = []
        for f in files:
            for s in (S46, S47):
                ent = idx.get((s, f))
                if ent is None:
                    continue
                rows.append({"firmware": ent["firmware"], "ref": ent["ref_short"],
                             "commit": ent["commit_full"], "commit_subject": ent["commit_subject"],
                             "file": ent["upstream_path"], "fetch_url": ent["fetch_url"],
                             "http_status": ent["http_status"], "fetch_date": ent["fetch_date"],
                             "sha256": ent["sha256"], "bytes": ent["bytes"]})
        if not rows:
            raise SystemExit(f"{section} 的出处文件在 MANIFEST 里找不到：{files}")
        meta = dict(SECTION_META[section])
        extra = DYNAMIC_NOTES.get(section, [])
        if extra:
            meta["note"] = _notes([meta.get("note", "")] + extra)
        meta["sources"] = rows
        out[section] = meta
    return out


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build(man: dict[str, Any]) -> dict[str, Any]:
    src = verified_sources(man)
    s46, s47 = src[S46], src[S47]
    if "CRASH_CHECK_CRASH" not in need(s47, F_CRASH):
        raise SystemExit("crash_check.cpp 里没有 CRASH_CHECK_CRASH，源码与预期不符")
    sections = {
        "mode_numbers": build_mode_numbers(s46, s47),
        "event_ids": build_event_ids(s46, s47),
        "error_subsystems": build_error_subsystems(s46, s47),
        "arm_methods": build_arm_methods(s46, s47),
        "arming_checks_bits": build_arming_checks(s46, s47),
        "motb_failflags_bits": build_motb_flags(s46, s47),
    }
    refs = {e["series"]: e for e in man["files"]}
    for s in (S46, S47):
        if s not in refs:
            raise SystemExit(f"MANIFEST 缺 {s} 系列")
    doc: dict[str, Any] = {
        "version": "1.0.0",
        "firmware_range": [S46, S47],
        "generated_at": man["fetch_date"],
        "generated_at_note": "取 _source/MANIFEST.json 的源码抓取日期；构建过程不含运行时时钟，"
                             "因此离线重建可逐字节一致",
        "generator": "tools/build_codebook.py（只读 data/codebooks/_source/，不联网）",
        # 内核 Codebook.provenance 只渲染 source.tags，所以把固件版本 + commit 写进 tags
        "source": {
            "project": "ArduPilot",
            "repository": man["repository"],
            "tags": [f"{refs[S46]['firmware']}@{refs[S46]['ref_short']}",
                     f"{refs[S47]['firmware']}@{refs[S47]['ref_short']}"],
            "commits": {s: {"short": refs[s]["ref_short"], "full": refs[s]["commit_full"],
                            "subject": refs[s]["commit_subject"]} for s in (S46, S47)},
            "ref_note": "不用分支/tag 名（抓取时 Copter-4.6、Copter-4.7、ArduCopter-4.6.3、"
                        "ArduCopter-4.7.0 全部 404），改用真日志固件自报的 git commit",
            "files": sorted({e["upstream_path"] for e in man["files"]}),
        },
        "provenance": {
            "basis": "每个 section 给出源码文件 + commit + 抓取日期 + sha256；"
                     "raw 原文与逐文件校验位在 data/codebooks/_source/（含 MANIFEST.json）",
            "offline_reproducible": True,
            "sections": provenance_block(man),
        },
        "unknown": UNKNOWN,
    }
    doc.update(sections)                        # 内核按顶层 section 名读取
    return doc


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


SECTIONS_ORDER = ("mode_numbers", "event_ids", "error_subsystems", "arm_methods",
                  "arming_checks_bits", "motb_failflags_bits")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="从 _source 离线生成 copter_4x.json")
    ap.add_argument("--check", action="store_true", help="只比对现有 JSON 与重建结果是否逐字节一致")
    ap.add_argument("--report", action="store_true", help="打印各 section 条目统计")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    doc = build(load_manifest())
    data = canonical_bytes(doc)
    out = Path(args.out)
    if args.check:
        if not out.exists():
            print(f"[FAIL] {out} 不存在")
            return 1
        same = out.read_bytes() == data
        print(f"[{'OK' if same else 'FAIL'}] {out.name} 与 _source 重建结果"
              f"{'逐字节一致' if same else '不一致'}（{len(data)} 字节）")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    nl = data.count(b"\n")
    print(f"[OK] 写出 {out}  {len(data)} 字节 / {nl} 行")
    if args.report:
        for s in SECTIONS_ORDER:
            t = doc[s]
            ec = sum(len(v.get("ecodes", {})) for v in t.values() if isinstance(v, dict))
            print(f"  {s:22s} {len(t):3d} 条" + (f"（含 {ec} 个 ECode 项）" if ec else ""))
        print(f"  unknown                {len(doc['unknown'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
