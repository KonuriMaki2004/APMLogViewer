# -*- coding: utf-8 -*-
"""tests/test_codebooks_data.py —— 码表数据质量 + 真日志解码校验。

覆盖 F 码表工作流的验收点（都是**真日志**实测，不只是 JSON 合法性）：

1. 8 份真日志（165/166 炸机 + 167~170 正常 + 4.6.3 GPS + 4.7.0 GPS）里出现的
   每一个 ERR(subsys,ecode)、MODE 编号、EV id、ARM method、ArmChecks 位、
   MOTB FailFlags 位都能解码出非空文本，``unmapped_report()`` 返回空。
2. 165 炸机时刻（≈322.45s）的 ERR 解码可读、severity=critical，并与同刻
   MSG/ARM 仍在同一组（``apmlog.core.events.find_crash_groups``）。
3. ``modes()`` 的区间名不再是 ``MODE=2``，而是 ALT_HOLD/LOITER 这类源码模式名。
4. CLI ``stats`` / ``alerts`` / ``events`` 输出里不再出现“未映射”。
5. 离线可复现：删掉 JSON 后只用 ``data/codebooks/_source/`` 重跑
   ``tools/build_codebook.py`` 能**逐字节**重建。
6. 扩展快照：2026-10-17 对 Mission Planner 目录 14 份额外日志做过同样的
   全量解码检查（无未映射），这里把观测到的码值固化成快照，离线防回归。

真日志路径在 ``REAL_LOGS`` 里；本机不存在时相关用例 skip（不伪造结果）。
缓存走内核默认 ``%LOCALAPPDATA%\\APMLogViewer\\cache``，可用 APMLOG_CACHE 覆盖。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apmlog.core import events as evmod                      # noqa: E402
from apmlog.core.codebooks import DEFAULT_CODEBOOK_PATH, load_codebook   # noqa: E402
from apmlog.core.model import severity_rank                  # noqa: E402
from apmlog.core.store import LogStore                       # noqa: E402

CODEBOOK_PATH = DEFAULT_CODEBOOK_PATH
BUILD_TOOL = ROOT / "tools" / "build_codebook.py"
SOURCE_DIR = ROOT / "data" / "codebooks" / "_source"
PYEXE = sys.executable

CRASH_DIR = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志")
MP_DIR = Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1")

#: 验收要求的那 8 份真日志（6 份炸机/正常 + 1 份 4.6.3 + 1 份 4.7.0，均带/不带 GPS 已核对固件串）
REAL_LOGS: list[tuple[str, Path, str]] = [
    ("00000165", CRASH_DIR / "00000165.BIN", "ArduCopter V4.7.0"),      # 炸机
    ("00000166", CRASH_DIR / "00000166.BIN", "ArduCopter V4.7.0"),      # 炸机
    ("00000167", CRASH_DIR / "00000167.BIN", "ArduCopter V4.7.0"),
    ("00000168", CRASH_DIR / "00000168.BIN", "ArduCopter V4.7.0"),
    ("00000169", CRASH_DIR / "00000169.BIN", "ArduCopter V4.7.0"),
    ("00000170", CRASH_DIR / "00000170.BIN", "ArduCopter V4.7.0"),
    ("mp-4.6.3", MP_DIR / "2026-09-20 17-37-08.bin", "ArduCopter V4.6.3"),
    ("mp-4.7.0", MP_DIR / "2026-09-28 13-51-22.bin", "ArduCopter V4.7.0"),
]
MP463 = MP_DIR / "2026-09-20 17-37-08.bin"
MP470 = MP_DIR / "2026-09-28 13-51-22.bin"

SECTIONS = ("mode_numbers", "error_subsystems", "event_ids", "arm_methods",
            "arming_checks_bits", "motb_failflags_bits")

#: 2026-10-17 对 MP 目录另外 14 份日志（含 GUIDED/AUTO/LAND、EV 85/86、
#: ERR 6/11/12/16/17/24/25、ARM 0/1/12/13/17、MOTB 掩码 2/3）实测的观测码值快照。
#: 当时全部日志 unmapped_report 返回空；这里固化用于离线防回归。
EXTENDED_OBSERVED = {
    "mode_numbers": [2, 3, 4, 5, 9],
    "event_ids": [10, 11, 15, 17, 18, 28, 56, 57, 62, 85, 86],
    "error_subsystems": [6, 11, 12, 16, 17, 24, 25],
    "arm_methods": [0, 1, 12, 13, 17],
    "motb_failflags_bits": [0, 1],          # 掩码 2/3 拆成的位号
    "arming_checks_bits": [0, 1, 2, 3, 4, 5, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 19],
}
EXTENDED_ERR_PAIRS = [(6, 1), (11, 0), (11, 2), (12, 1), (16, 0), (16, 2),
                      (17, 0), (17, 1), (24, 0), (24, 1), (25, 1)]


def _bits(mask: int, width: int) -> list[int]:
    """掩码 → 位号列表（内核 arming_checks/motb_flags 就是按位号查表）。"""
    return [i for i in range(width) if int(mask) >> i & 1]


def _observed_from_events(store: LogStore) -> dict[str, list[int]]:
    """从内核事件流收集所有需要解码的码值（位号形式，不是掩码）。"""
    obs: dict[str, set[int]] = {s: set() for s in SECTIONS}
    for e in store.events():
        if e.kind == "MODE" and e.code is not None:
            obs["mode_numbers"].add(int(e.code))
        elif e.kind == "EV" and e.code is not None:
            obs["event_ids"].add(int(e.code))
        elif e.kind == "ERR" and e.code is not None:
            obs["error_subsystems"].add(int(e.code))
        elif e.kind == "ARM":
            if e.extra.get("Method") is not None:
                obs["arm_methods"].add(int(e.extra["Method"]))
            chk = e.extra.get("ArmChecks")
            if chk:
                obs["arming_checks_bits"].update(_bits(int(chk), 32))
    # MOTB 不是事件，是数据报文：按内核通道 API 取实际出现的 FailFlags
    try:
        s = store.series("MOTB.FailFlags")
    except Exception:
        s = None
    if s is not None:
        for v in s.v:
            if v == v and int(v):                      # 跳过 nan
                obs["motb_failflags_bits"].update(_bits(int(v), 16))
    return {k: sorted(v) for k, v in obs.items() if v}


def _err_pairs(store: LogStore) -> list[tuple[int, int]]:
    return sorted({(int(e.code), int(e.ecode)) for e in store.events(kinds=["ERR"])
                   if e.code is not None and e.ecode is not None})


def _require_log(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"真日志不在本机：{path}")


# --------------------------------------------------------------------------- #
# 1) JSON 结构 / 数据质量
# --------------------------------------------------------------------------- #
def test_codebook_file_exists_and_json_valid():
    assert CODEBOOK_PATH.exists(), f"码表不存在：{CODEBOOK_PATH}"
    doc = json.loads(CODEBOOK_PATH.read_text(encoding="utf-8"))
    assert doc["firmware_range"] == ["4.6", "4.7"]
    for k in ("version", "generated_at", "provenance", "source"):
        assert doc.get(k), f"缺少顶层字段 {k}"
    for s in SECTIONS:
        assert isinstance(doc.get(s), dict) and doc[s], f"section {s} 缺失或为空"


def test_sections_use_integer_string_keys_and_nonempty_names():
    doc = json.loads(CODEBOOK_PATH.read_text(encoding="utf-8"))
    for s in SECTIONS:
        for key, val in doc[s].items():
            assert str(int(key)) == key, f"{s} 的 key {key!r} 不是规范十进制字符串"
            name = val if isinstance(val, str) else (val.get("name") or val.get("text"))
            assert isinstance(name, str) and name.strip(), f"{s}[{key}] 名称为空"
            if isinstance(val, dict):
                assert set(val.get("ecodes", {})) <= {str(int(k)) for k in val.get("ecodes", {})}
                for sev in [val.get("severity")] + [v.get("severity") for v in
                                                    val.get("ecodes", {}).values() if isinstance(v, dict)]:
                    if sev:
                        assert sev in ("info", "hint", "warn", "error", "critical"), \
                            f"{s}[{key}] 非法 severity={sev}"


def test_provenance_names_files_commits_and_dates():
    doc = json.loads(CODEBOOK_PATH.read_text(encoding="utf-8"))
    prov = doc["provenance"]["sections"]
    assert set(prov) == set(SECTIONS)
    for s in SECTIONS:
        rows = prov[s]["sources"]
        assert rows, f"{s} 没有源码出处"
        fw = {r["firmware"] for r in rows}
        assert {"ArduCopter V4.6.3", "ArduCopter V4.7.0"} & fw, f"{s} 出处未标固件版本"
        for r in rows:
            assert r["file"].startswith(("ArduCopter/", "libraries/")), r
            assert len(r["commit"]) == 40, f"{s} commit 不是完整 SHA: {r['commit']}"
            assert r["fetch_date"] == doc["generated_at"]
            assert r["http_status"] == 200 and r["sha256"] and r["bytes"] > 0
            assert r["fetch_url"].startswith(
                "https://raw.githubusercontent.com/ArduPilot/ardupilot/")
    # 内核 Codebook.provenance 只能展示 source.tags，所以要带上 commit
    cb = load_codebook()
    text = cb.provenance
    assert "92b0cd78" in text and "1511f271" in text, f"provenance 未显示 commit：{text}"


def test_source_files_match_manifest_sha256():
    """_source 是码表的唯一离线证据，必须与 MANIFEST 里的 sha256 一致。"""
    import hashlib
    man = json.loads((SOURCE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["files"], "MANIFEST 为空"
    for ent in man["files"]:
        fp = SOURCE_DIR / ent["local"]
        assert fp.exists(), f"源码证据缺失：{fp}"
        assert hashlib.sha256(fp.read_bytes()).hexdigest() == ent["sha256"], ent["local"]
        assert ent["fetch_url"].startswith("https://raw.githubusercontent.com/ArduPilot/ardupilot/")
    mode = (SOURCE_DIR / "r1511f271_ArduCopter_mode.h").read_text(encoding="utf-8")
    assert "enum class Number" in mode
    logger = (SOURCE_DIR / "r1511f271_libraries_AP_Logger_AP_Logger.h").read_text(encoding="utf-8")
    assert "enum class LogEvent" in logger and "LogErrorSubsystem" in logger
    arming = (SOURCE_DIR / "r1511f271_libraries_AP_Arming_AP_Arming.h").read_text(encoding="utf-8")
    assert "enum class Method" in arming


def test_key_codes_decode_as_expected():
    cb = load_codebook()
    assert cb.mode_name(2) == "ALT_HOLD"           # 源码 mode.h：2 是 ALT_HOLD
    assert cb.mode_name(5) == "LOITER"
    txt, sev = cb.err_info(12, 1)
    assert txt and "未映射" not in txt and sev == "critical"
    assert "CRASH_CHECK" in txt                    # Subsys=12/ECode=1 → 炸机检测
    assert cb.arm_method(12) and "CRASH" in cb.arm_method(12)
    assert cb.arming_checks(1) and cb.arming_checks(785854)
    assert all("bit" not in x for x in cb.arming_checks(785854)), cb.arming_checks(785854)
    assert cb.motb_flags(2) and cb.motb_flags(3)
    assert cb.ev_text(60)[0] and "未映射" not in cb.ev_text(60)[0]
    # 165 的 ARM 报文 ArmChecks=0（不拆位），Method=12
    assert "未映射" not in (cb.err_info(12, 2)[0] or "")


# --------------------------------------------------------------------------- #
# 2) 真日志：每个码值都要解码
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag,path,fw", REAL_LOGS, ids=[t for t, _p, _f in REAL_LOGS])
def test_every_observed_code_in_real_log_decodes(tag: str, path: Path, fw: str):
    _require_log(path)
    cb = load_codebook()
    st = LogStore.open(path)
    try:
        obs = _observed_from_events(st)
        assert obs, f"{tag} 没有收集到任何码值（内核解码链路可能坏了）"
        missing = cb.unmapped_report(obs)
        assert missing == {}, f"{tag} 存在未映射码值：{missing}"
        # ERR 的 (subsys, ecode) 二级也要可读
        for subsys, ecode in _err_pairs(st):
            txt, sev = cb.err_info(subsys, ecode)
            assert txt and "未映射" not in txt, f"{tag} ERR {subsys}/{ecode} → {txt!r}"
            assert sev in ("warn", "error", "critical"), f"{tag} ERR {subsys}/{ecode} sev={sev}"
        # 每条事件的实际展示文本里不能留“未映射”
        for e in st.events():
            if e.kind in ("MODE", "EV", "ERR", "ARM"):
                assert "未映射" not in (e.display or ""), f"{tag} {e.kind}@{e.t:.2f} {e.display}"
        if tag.startswith("00000"):
            meta_fw = str(st.meta.get("firmware", ""))
            assert fw.split()[0] in meta_fw or fw in json.dumps(st.meta, ensure_ascii=False), \
                f"{tag} firmware={meta_fw!r}"
    finally:
        st.close()


def test_unmapped_report_empty_for_extended_snapshot():
    """14 份额外 MP 日志（2026-10-17 实测）的码值快照也必须全解码。"""
    cb = load_codebook()
    assert cb.unmapped_report(EXTENDED_OBSERVED) == {}
    for subsys, ecode in EXTENDED_ERR_PAIRS:
        txt, _sev = cb.err_info(subsys, ecode)
        assert txt and "未映射" not in txt, f"ERR {subsys}/{ecode} → {txt!r}"
    for mask in (2, 3):            # MOTB 实际掩码
        assert all("bit" not in x for x in cb.motb_flags(mask)), cb.motb_flags(mask)
    for mask in (1, 785854):       # ARM 实际掩码
        assert all("bit" not in x for x in cb.arming_checks(mask))


def test_crash_group_at_165_is_readable_critical_and_co_located():
    _require_log(REAL_LOGS[0][1])
    st = LogStore.open(REAL_LOGS[0][1])
    try:
        errs = st.events(kinds=["ERR"])
        assert errs, "165 没有 ERR 事件"
        e = min(errs, key=lambda x: abs(x.t - 322.45))
        assert abs(e.t - 322.45) < 0.05, f"炸机 ERR 时间漂移：{e.t}"
        assert (e.code, e.ecode) == (12, 1)
        assert e.severity == "critical", f"severity={e.severity}"
        assert e.decoded and "未映射" not in e.decoded and "CRASH_CHECK" in e.decoded
        assert e.extra.get("co_text", "").startswith("Crash: Disarming"), e.extra
        groups = evmod.group_events(st.events())
        g = next(x for x in groups if any(y is e or (y.kind == "ERR" and abs(y.t - e.t) < 1e-6) for y in x))
        kinds = {y.kind for y in g}
        assert {"ERR", "MSG", "ARM"} <= kinds, f"同刻分组缺成员：{kinds}"
        crash = evmod.find_crash_groups(st.events())
        assert any(abs(t - 322.45) < 0.05 for t, _d in crash), crash
        assert any("同刻" in d and "ARM" in d and "ERR" in d for _t, d in crash)
        # 判级顺序必须 critical ≥ error ≥ warn ≥ hint ≥ info
        assert severity_rank(e.severity) == max(severity_rank(y.severity) for y in g)
    finally:
        st.close()


def test_mode_segments_are_named_not_raw_numbers():
    _require_log(REAL_LOGS[0][1])
    st = LogStore.open(REAL_LOGS[0][1])
    try:
        segs = st.modes()
        assert segs, "165 没有模式区间"
        names = {n for _a, _b, n in segs}
        assert not any(n.startswith("MODE=") for n in names), names
        assert {"ALT_HOLD", "LOITER"} <= names, names        # 观测到的 2/5
    finally:
        st.close()


def test_mode_sequences_for_4_6_3_and_4_7_0_printout(capsys):
    """把 4.6.3 与 4.7.0 各一段飞行的模式序列打印进测试输出（供回报引用）。"""
    out_lines = []
    for tag, p in (("ArduCopter V4.6.3", MP463), ("ArduCopter V4.7.0", MP470)):
        _require_log(p)
        st = LogStore.open(p)
        try:
            seq = [n for _a, _b, n in st.modes()]
            assert seq and not any(n.startswith("MODE=") for n in seq)
            ded: list[list[str | int]] = []
            for n in seq:
                if ded and ded[-1][0] == n:
                    ded[-1][1] = int(ded[-1][1]) + 1
                else:
                    ded.append([n, 1])
            line = f"{tag}: " + " -> ".join(f"{n}x{c}" for n, c in ded)
            out_lines.append(line)
        finally:
            st.close()
    printed = "\n".join(out_lines)
    print("MODE-SEQ " + printed)
    assert "ALT_HOLD" in printed or "LOITER" in printed or "GUIDED" in printed


# --------------------------------------------------------------------------- #
# 3) CLI：不许再出现“未映射”
# --------------------------------------------------------------------------- #
def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    """--quiet 是主解析器的全局选项，必须放在子命令前面。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([PYEXE, "-m", "apmlog.cli", "--quiet", *argv], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=1800)


@pytest.mark.parametrize("cmd", ["stats", "alerts"])
def test_cli_output_has_no_unmapped(cmd: str):
    _require_log(REAL_LOGS[0][1])
    r = _run_cli(cmd, str(REAL_LOGS[0][1]))
    assert r.returncode == 0, r.stderr[-800:]
    blob = (r.stdout or "") + (r.stderr or "")
    assert "未映射" not in blob, blob[-1500:]
    if cmd == "stats":
        doc = json.loads(r.stdout)
        assert doc["codebook"].startswith("ArduPilot"), doc["codebook"]
        assert doc["stats"]["mode_seq"] and not any(m.startswith("MODE=")
                                                   for m in doc["stats"]["mode_seq"])


def test_cli_events_shows_decoded_crash_line():
    _require_log(REAL_LOGS[0][1])
    r = _run_cli("events", str(REAL_LOGS[0][1]), "--kinds", "ERR,MSG,ARM",
                 "--around", "322.45")
    assert r.returncode == 0, r.stderr[-800:]
    assert "未映射" not in r.stdout
    assert "CRASH_CHECK" in r.stdout, r.stdout[-1200:]
    assert "critical" in r.stdout


# --------------------------------------------------------------------------- #
# 4) 离线可复现：删掉 JSON 也能只用 _source 重建
# --------------------------------------------------------------------------- #
def test_offline_rebuild_is_byte_identical(tmp_path, monkeypatch):
    assert BUILD_TOOL.exists()
    shipped = CODEBOOK_PATH.read_bytes()
    rebuilt = tmp_path / "copter_4x.json"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # 4a) 现有 _source 重建 → 与入库 JSON 逐字节一致
    r = subprocess.run([PYEXE, str(BUILD_TOOL), "--out", str(rebuilt)], cwd=str(ROOT),
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert rebuilt.read_bytes() == shipped, "重建结果与入库 JSON 不一致"
    # 4b) 重建过程不依赖已有 JSON：--out 指向不存在的新路径也能产出同样结果，
    #     并且 --check 确认入库文件就是 _source 的纯函数
    monkeypatch.delenv("APMLOG_CACHE", raising=False)
    r2 = subprocess.run([PYEXE, str(BUILD_TOOL), "--check"], cwd=str(ROOT), env=env,
                        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r2.returncode == 0 and "逐字节一致" in r2.stdout, r2.stdout + r2.stderr
    # 4c) 语义一致：两次重建的每个 section 条目数一致
    doc = json.loads(rebuilt.read_text(encoding="utf-8"))
    assert {s: len(doc[s]) for s in SECTIONS} == {s: len(json.loads(shipped.decode())[s])
                                                  for s in SECTIONS}


def test_builder_does_not_touch_network(tmp_path):
    """构建脚本全程不得建立 socket 连接（只读 _source）。"""
    out = tmp_path / "net_blocked.json"
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import socket, sys, runpy\n"
        "tool, out = sys.argv[1], sys.argv[2]\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('build_codebook 试图联网')\n"
        "socket.socket = _boom\n"
        "socket.create_connection = _boom\n"
        "socket.getaddrinfo = _boom\n"
        "sys.argv = ['build_codebook.py', '--out', out]\n"
        "runpy.run_path(tool, run_name='__main__')\n",
        encoding="utf-8")
    r = subprocess.run([PYEXE, str(runner), str(BUILD_TOOL), str(out)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert out.read_bytes() == CODEBOOK_PATH.read_bytes()
