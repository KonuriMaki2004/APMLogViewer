"""apmlog.link —— MAVLink 连接与飞控日志下载（H 工作流）。

模块划分：
    transport.py  字节传输（串口/TCP 客户端/TCP 服务端/UDP/UDP 出站/内存管道）
    endpoint.py   目标串解析、严格 CRC 分帧、MavLinkSession（心跳/订阅/定向发送）
    download.py   LOG_REQUEST_LIST 聚合与 LOG_REQUEST_DATA/LOG_DATA 分块下载、断点续传
    fakes.py      FakeFC 假飞控（无硬件自测，按实测线语义应答）
    cli_main.py   ``python -m apmlog.cli link ...`` 的入口（函数 cli_main(args) -> int）

线语义与字段数值证据全部记录在 docs/LOG_DOWNLOAD_NOTES.md（源自本机真实 tlog）。
这里显式再导出 ``cli_main``，使主 CLI 的 ``from apmlog.link import cli_main`` 拿到的是**函数**。
"""
from __future__ import annotations

from .cli_main import cli_main
from .download import DownloadResult, RemoteLog, default_download_dir, download_log, list_logs
from .endpoint import Endpoint, MavLinkSession, parse_target
from .transport import LinkError

__all__ = [
    "cli_main", "LinkError", "Endpoint", "MavLinkSession", "parse_target",
    "RemoteLog", "DownloadResult", "list_logs", "download_log", "default_download_dir",
]
