# -*- coding: utf-8 -*-
"""创建/更新 apmlog-viewer 分支的 GitHub Release 并上传 zip。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OWNER_REPO = "chuanqinhou/FlightTune"
BRANCH = "apmlog-viewer"
TAG = "apmlog-viewer-v0.1.0"
NAME = "APMLogViewer 0.1.0（apmlog-viewer）"
COMMIT = "b150787"
ZIP = Path("release/APMLogViewer-0.1.0-win64-b150787.zip").resolve()


def token() -> str:
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=inp, capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("拿不到 GitHub 凭据")


TOK = token()
API = f"https://api.github.com/repos/{OWNER_REPO}"
HDR = {"Authorization": f"Bearer {TOK}", "Accept": "application/vnd.github+json",
       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "apmlog-release"}


def call(url: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HDR)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        return json.loads(body) if body else {}


sha = hashlib.sha256(ZIP.read_bytes()).hexdigest()
size = ZIP.stat().st_size
print(f"本地 zip: {ZIP.name} {size:,} B sha256={sha[:16]}…")

body = f"""APMLogViewer 0.1.0 —— Windows x64 免安装绿色包

- **构建自提交**：`{COMMIT}`（分支 `{BRANCH}`）
- **附件**：`{ZIP.name}` ｜ {size:,} B（{size / 1048576:.1f} MB）
- **SHA256**：`{sha}`

## zip 里有什么

```
APMLogViewer/
  APMLogViewer.exe                    主程序（PyInstaller onedir，8,454,478 B）
  _internal/                          运行库与资源（无需安装 Python）
    PySide6/                          Qt 6.12（Essentials）
    pyqtgraph/ numpy/ pymavlink/ …    绘图、数值、MAVLink 协议栈
    data/
      knowledge/event_help.json       事件/告警说明知识库（双击弹窗用）
      codebooks/copter_4x.json        31 个子系统码本（Copter 4.6.3 / 4.7.0）
      …
    config/alerts.yaml                默认告警阈值
```

解压后双击 `APMLogViewer/APMLogViewer.exe` 即可运行（启动较慢属正常，首次解压杀软扫描）。
**不含** `apmlog/batch.py` 批处理 CLI —— 按设计不打包进 exe。

自检（会输出 PNG 与同目录 `*_selftest.txt`）：

```powershell
APMLogViewer.exe --selftest "<某份 .BIN>" --png selftest\\exe.png
```

## 本版包含的改动

### 1. 事件/告警详情弹窗（知识库 + 未收录兜底）

- 「事件」「告警」两个表双击即可看**该类型**的问题说明 / 可能原因 / 处理建议；
  建议是能照着做的动作。
- 知识库 `data/knowledge/event_help.json`：`by_code` 10、`by_subsys` 31（码表全覆盖）、
  `by_msg` 13、`by_alert_rule` 13、`by_kind` 7，外加"已恢复"与兜底条目。
- 匹配顺序 `by_code → resolved → by_subsys → by_msg → by_kind → fallback`；
  码表原文以「码表含义」单独引用。
- **未收录也不空白**：琥珀横幅说明"暂未收录"，仍给通用排查步骤 + 原始文本。
- 单击跳转、右键导出 CSV 等原有行为全部保留；双击不移动光标。

### 2. 修复：装第三方插件后左侧栏被挤扁且拉不回来

- 插件页改由滚动宿主（`QScrollArea`）承载，插件的 `minimumWidth` 不再顶住分隔条；
  插件 API 与 `register_tab` 用法不变。
- 地图底部状态行的长文字不再决定地图页最小宽。
- 实测（1728×960，真实插件 + 真实炸机日志）：标签区最小宽 1252/1840 → 1098，
  左侧栏 125～471 拖不动 → 342 起可拉到约 625。

详见仓库 `docs/CHANGELOG.md`（按改动分节：问题背景 / 改法 / 界面行为 / 验证结果）。

## 验证

- 全量回归 `953 passed / 3 skipped`（`/link` 的 1 个已知 flaky 单跑通过）。
- 真实日志（Copter 4.7.0 炸机）：`ERR 12/1` → 「炸机判据触发（CRASH_CHECK_CRASH）」4 条建议；
  真实告警 `vibe_vibez` → 「振动超标（Z 轴）」；未收录类型走兜底横幅。
- 本包 `--selftest` 输出：知识库 `by_subsys 31`、试匹配 `ERR 12/1` 命中、
  插件 `ai_assistant=loaded`、`左侧栏宽 342 ｜标签区最小宽 1098`。
"""

existing = call(f"{API}/releases?per_page=100")
rel = next((r for r in existing if r["tag_name"] == TAG), None)
if rel is None:
    rel = call(f"{API}/releases", "POST", {
        "tag_name": TAG,
        "target_commitish": BRANCH,
        "name": NAME,
        "body": body,
        "draft": False,
        "prerelease": False,
    })
    print(f"已创建 release: {rel['tag_name']} id={rel['id']} → {rel['html_url']}")
else:
    rel = call(f"{API}/releases/{rel['id']}", "PATCH", {"name": NAME, "body": body})
    print(f"已更新 release: {rel['tag_name']} id={rel['id']}")

# 同名附件先删（重跑时避免 422 already_exists）
for a in rel.get("assets", []):
    if a["name"] == ZIP.name:
        call(f"{API}/releases/assets/{a['id']}", "DELETE")
        print(f"删除旧附件 {a['name']}")

up_url = (f"https://uploads.github.com/repos/{OWNER_REPO}/releases/{rel['id']}"
          f"/assets?name={ZIP.name}")
req = urllib.request.Request(up_url, data=ZIP.read_bytes(), method="POST", headers={
    **HDR, "Content-Type": "application/zip"})
t0 = time.time()
with urllib.request.urlopen(req, timeout=1800) as r:
    asset = json.loads(r.read())
print(f"上传完成 {asset['name']} {asset['size']:,} B 用时 {time.time()-t0:.0f}s "
      f"state={asset['state']}")

rel2 = call(f"{API}/releases/{rel['id']}")
print(f"release: {rel2['tag_name']} target={rel2.get('target_commitish')} "
      f"draft={rel2['draft']} url={rel2['html_url']}")
for a in rel2["assets"]:
    print(f"  附件 {a['name']} {a['size']:,} B 下载数={a['download_count']} "
          f"{a['browser_download_url']}")
Path("release/release_info.json").write_text(json.dumps({
    "tag": rel2["tag_name"], "id": rel2["id"], "url": rel2["html_url"],
    "assets": [{"name": a["name"], "size": a["size"], "url": a["browser_download_url"],
                "api": a["url"]} for a in rel2["assets"]],
    "local_sha256": sha, "local_size": size,
}, ensure_ascii=False, indent=2), encoding="utf-8")
