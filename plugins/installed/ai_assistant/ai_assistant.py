# -*- coding: utf-8 -*-
"""
APMLogViewer 插件：AI 智能日志识别助手 (ai_assistant.py)
适用版本：宿主 APMLogViewer 0.1.0 / 插件接口 API_VERSION = 1

功能特性：
1. 顶部“AI识别”双入口 & 紧凑响应式布局：
   - 自动在主窗口顶部工具栏注入「🤖 AI识别」按钮（支持快捷键 Ctrl+Shift+A），呼出可置顶的非模态悬浮对话框；
   - 同时注册「AI识别」独立标签页，与悬浮对话框实时同步对话记录；
   - 采用紧凑自适应工具栏设计，在常规小窗口或非最大化状态下所有按钮、模型选择框与思考等级均完整显示不溢出。
2. 多 API 账号保存与多模型动态拉取（Antigravity 风格模型切换）：
   - 支持保存多个不同服务商或不同网关的 API 配置（APIProfile），每个 API 可关联多个可用模型；
   - 输入 API Key 后，支持自动或一键从远端接口（GET `/v1/models`、`/v1beta/models`）拉取该 API 提供的全部模型列表；
   - 在对话页面顶部直接提供「🤖 模型」下拉框，像 Antigravity 一样随时切换不同 API 下的不同模型进行提问。
3. 支持 OpenAI 兼容格式 与 Antigravity 兼容格式：
   - OpenAI 兼容格式 (`/v1/chat/completions`)：适配 DeepSeek、通义千问、OpenAI、硅基流动、Kimi、智谱、豆包、Ollama 等；
   - Antigravity 兼容格式：适配 `/v1/messages`、Gemini 原生 `/v1beta/models` 及 `v1internal` 网关协议。
4. 零卡顿流式渲染 & 完整思考+正文输出保障：
   - 流式信号采用节流机制（160ms/帧）+ 历史消息 HTML 缓存 + 轻量级流式预览，彻底消除流式打字时的界面卡顿；
   - 修复 `max_tokens` / `maxOutputTokens` 截断与 Gemini `thoughtSignature` 分类问题，确保模型思考完成后完整输出正式诊断结论；
   - 思考过程支持一键「展开/收起」，正式回答生成后自动折叠冗长思考过程，重点结论一目了然。
5. 深度日志特征提取、按需时序下钻与交互式跳转：
   - 点击回复中的时间戳（如 125.4s）定位游标，点击时间段（如 80.0s-95.0s）放大曲线，点击通道名加入曲线页。
"""

from __future__ import annotations

PLUGIN_NAME = "AI识别"
__version__ = "1.4.0"

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

try:
    from PySide6 import QtNetwork
except ImportError:
    QtNetwork = None  # type: ignore

try:
    import ssl
    import urllib.error
    import urllib.request
    HAS_URLLIB = True
except ImportError:
    ssl = None  # type: ignore
    urllib = None  # type: ignore
    HAS_URLLIB = False

try:
    from apmlog.plugins import API_VERSION
except ImportError:
    API_VERSION = 1


def _gen_short_id(prefix: str = "api") -> str:
    """无需依赖 uuid 模块生成短唯一 ID（兼容 PyInstaller 精简打包环境）。"""
    try:
        rand_hex = os.urandom(4).hex()
    except Exception:
        rand_hex = f"{int(time.time() * 1000000) & 0xFFFFFFFF:08x}"
    return f"{prefix}_{rand_hex}"


def _html_escape(text: Any) -> str:
    """无需依赖 html 标准库模块进行 HTML 转义（兼容 PyInstaller 精简打包环境）。"""
    s = str(text if text is not None else "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


# ============================================================================
# 1. 协议格式、服务商多模型预设、思考等级定义与多 API 配置管理
# ============================================================================

API_FORMATS: List[Dict[str, str]] = [
    {
        "id": "openai",
        "name": "OpenAI 兼容格式 (/v1/chat/completions)",
        "short": "OpenAI格式",
    },
    {
        "id": "antigravity",
        "name": "Antigravity 兼容格式 (自动适配 /v1/messages · Gemini · v1internal)",
        "short": "Antigravity格式",
    },
    {
        "id": "antigravity_messages",
        "name": "Antigravity Messages 格式 (/v1/messages · Claude/反代模式)",
        "short": "Antigravity(/v1/messages)",
    },
    {
        "id": "antigravity_gemini",
        "name": "Antigravity Gemini 原生格式 (/v1beta/models · thinkingConfig)",
        "short": "Antigravity(Gemini)",
    },
]

VALID_API_FORMATS = {item["id"] for item in API_FORMATS}


def get_api_format_info(fmt_id: str) -> Dict[str, str]:
    for item in API_FORMATS:
        if item["id"] == fmt_id:
            return item
    return API_FORMATS[0]


PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "antigravity",
        "name": "Antigravity (本地反代网关 / Antigravity Tools)",
        "api_format": "antigravity",
        "base_url": "http://127.0.0.1:8045/v1",
        "model": "claude-sonnet-4-6",
        "models": [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-3-7-sonnet",
            "gemini-3-pro",
            "gemini-3-pro-high",
            "gemini-3.8-flash",
            "gemini-3.8-flash-high",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ],
        "placeholder_key": "sk-... 或 your-api-key-1 (Antigravity 网关密钥)",
    },
    {
        "id": "antigravity_gemini",
        "name": "Antigravity (Gemini 原生协议网关)",
        "api_format": "antigravity_gemini",
        "base_url": "http://127.0.0.1:8045",
        "model": "gemini-3-pro",
        "models": [
            "gemini-3-pro",
            "gemini-3.8-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-4-argon",
        ],
        "placeholder_key": "sk-... 或 your-api-key-1 (Antigravity 网关密钥)",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek (官方)",
        "api_format": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "placeholder_key": "sk-...",
    },
    {
        "id": "qwen",
        "name": "通义千问 Qwen (阿里云百炼)",
        "api_format": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": [
            "qwen-plus",
            "qwen-max",
            "qwen-turbo",
            "qwq-plus",
            "qwen3-235b-a22b",
            "qwen3-32b",
            "deepseek-r1",
            "deepseek-v3",
        ],
        "placeholder_key": "sk-...",
    },
    {
        "id": "openai",
        "name": "OpenAI (官方 / 兼容代理)",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "models": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3-mini",
            "o4-mini",
            "o3",
            "o1",
        ],
        "placeholder_key": "sk-...",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 (SiliconFlow)",
        "api_format": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
            "Qwen/QwQ-32B",
            "Qwen/Qwen2.5-72B-Instruct",
            "Pro/deepseek-ai/DeepSeek-R1",
            "Pro/deepseek-ai/DeepSeek-V3",
        ],
        "placeholder_key": "sk-...",
    },
    {
        "id": "moonshot",
        "name": "Kimi (Moonshot AI)",
        "api_format": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "models": [
            "kimi-k2-0711-preview",
            "kimi-latest",
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
        ],
        "placeholder_key": "sk-...",
    },
    {
        "id": "zhipu",
        "name": "智谱清言 (GLM)",
        "api_format": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "models": [
            "glm-4-plus",
            "glm-z1-air",
            "glm-z1-flash",
            "glm-4-air",
            "glm-4-flash",
            "glm-4-long",
        ],
        "placeholder_key": "填写智谱 API Key",
    },
    {
        "id": "doubao",
        "name": "豆包 (火山方舟 Ark)",
        "api_format": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-pro-32k-250115",
        "models": [
            "doubao-1-5-pro-32k-250115",
            "doubao-seed-1-6-250615",
            "doubao-seed-1-6-thinking-250615",
            "deepseek-r1-250120",
            "deepseek-v3-250324",
        ],
        "placeholder_key": "填写火山方舟 API Key",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama (无需 API Key)",
        "api_format": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:14b",
        "models": [
            "qwen2.5:14b",
            "qwen2.5:7b",
            "deepseek-r1:14b",
            "deepseek-r1:7b",
            "llama3.1:8b",
        ],
        "placeholder_key": "ollama (本地服务可留空或填任意字符)",
    },
    {
        "id": "custom",
        "name": "自定义 (手动配置 OpenAI 或 Antigravity 接口)",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
            "claude-sonnet-4-6",
            "gemini-3-pro",
            "deepseek-chat",
        ],
        "placeholder_key": "sk-...",
    },
]


def get_provider_preset(provider_id: str) -> Dict[str, Any]:
    for p in PROVIDER_PRESETS:
        if p["id"] == provider_id:
            return p
    return PROVIDER_PRESETS[-1]


# 思考等级选项定义（保持适度思考预算，预留充足正文输出空间）
THINKING_LEVELS: List[Dict[str, Any]] = [
    {
        "id": "auto",
        "name": "自动 (模型默认)",
        "short": "自动",
        "budget": None,
        "gemini_level": None,
        "prompt_hint": "【思考与输出要求】若进行思考推导，请保持思考精炼聚焦（控制在 600 字以内），思考完成后必须立即输出完整、详尽的中文诊断结论。",
    },
    {
        "id": "off",
        "name": "关闭思考 (极速直出)",
        "short": "关闭思考",
        "budget": 0,
        "gemini_level": "LOW",
        "prompt_hint": "【思考模式：关闭】请直接给出清晰完整的识别结论与关键数据证据，无需展开思考推导过程。",
    },
    {
        "id": "low",
        "name": "低度思考 (Low)",
        "short": "低度思考",
        "budget": 1024,
        "gemini_level": "LOW",
        "prompt_hint": "【思考模式：低度思考 (Low)】请先进行简短快速的关键指标核对（不超过 300 字），随后立即输出完整的正式诊断结论。",
    },
    {
        "id": "medium",
        "name": "中度思考 (Medium)",
        "short": "中度思考",
        "budget": 4096,
        "gemini_level": "MEDIUM",
        "prompt_hint": "【思考模式：中度思考 (Medium)】请系统性地比对相关子系统通道、事件与飞行模式（思考过程控制在 800 字以内），随后务必输出完整详尽的正式诊断报告。",
    },
    {
        "id": "high",
        "name": "深度思考 (High)",
        "short": "深度思考",
        "budget": 16384,
        "gemini_level": "HIGH",
        "prompt_hint": "【思考模式：深度思考 (High)】请针对日志中的所有异常迹象、时序先后顺序与控制回路耦合关系进行严密归因推导，推导完成后必须输出完整、详尽的正式诊断结论，切勿只输出思考过程而中断正文。",
    },
]

VALID_THINKING_IDS = {item["id"] for item in THINKING_LEVELS}


def get_thinking_level_info(level_id: str) -> Dict[str, Any]:
    for item in THINKING_LEVELS:
        if item["id"] == level_id:
            return item
    return THINKING_LEVELS[0]


def _get_config_dir() -> Path:
    """获取插件专属配置存储目录（遵循开发指南 7.4 节建议）。"""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base = Path(local_appdata)
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Local"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    cfg_dir = base / "APMLogViewer" / "ai_assistant"
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return cfg_dir


@dataclass
class APIProfile:
    """单个 API 站点/账号配置，可包含多个可选模型。"""

    id: str = field(default_factory=lambda: _gen_short_id("api"))
    name: str = "DeepSeek (官方)"
    provider: str = "deepseek"
    api_format: str = "openai"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    models: List[str] = field(default_factory=lambda: ["deepseek-chat", "deepseek-reasoner"])
    default_model: str = "deepseek-chat"

    def ensure_valid_models(self) -> None:
        cleaned: List[str] = []
        for m in self.models:
            ms = str(m).strip()
            if ms and ms not in cleaned:
                cleaned.append(ms)
        if not cleaned:
            preset = get_provider_preset(self.provider)
            cleaned = list(preset.get("models") or [preset.get("model", "deepseek-chat")])
        self.models = cleaned
        if not self.default_model or self.default_model not in self.models:
            self.default_model = self.models[0]

    def is_configured(self) -> bool:
        self.ensure_valid_models()
        if not self.base_url.strip() or not self.default_model.strip():
            return False
        if self.provider == "ollama":
            return True
        return bool(self.api_key.strip())

    def to_dict(self) -> Dict[str, Any]:
        self.ensure_valid_models()
        return {
            "id": self.id,
            "name": self.name.strip() or self.provider,
            "provider": self.provider.strip(),
            "api_format": self.api_format.strip(),
            "base_url": self.base_url.strip(),
            "api_key": self.api_key.strip(),
            "models": list(self.models),
            "default_model": self.default_model.strip(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "APIProfile":
        prov = str(d.get("provider", "deepseek")).strip()
        preset = get_provider_preset(prov)
        default_fmt = "antigravity" if prov.startswith("antigravity") else preset.get("api_format", "openai")
        fmt = str(d.get("api_format", default_fmt)).strip().lower()
        if fmt not in VALID_API_FORMATS:
            fmt = default_fmt
        raw_models = d.get("models")
        if isinstance(raw_models, list) and raw_models:
            models = [str(x).strip() for x in raw_models if str(x).strip()]
        else:
            single_m = str(d.get("default_model") or d.get("model") or preset.get("model", "deepseek-chat")).strip()
            preset_models = list(preset.get("models") or [])
            models = [single_m] + [pm for pm in preset_models if pm != single_m]
        def_model = str(d.get("default_model") or d.get("model") or (models[0] if models else "deepseek-chat")).strip()
        prof = cls(
            id=str(d.get("id") or _gen_short_id("api")),
            name=str(d.get("name") or preset.get("name") or prov).strip(),
            provider=prov,
            api_format=fmt,
            base_url=str(d.get("base_url") or preset.get("base_url", "https://api.deepseek.com/v1")).strip(),
            api_key=str(d.get("api_key", "")).strip(),
            models=models,
            default_model=def_model,
        )
        prof.ensure_valid_models()
        return prof


class AIConfig:
    """
    全局 AI 配置容器：
    - 支持保存多个 `APIProfile`（`profiles` 列表），每个 API 拥有自己的多个模型列表 `models`；
    - 记录当前在对话页面选中的 `active_profile_id` 与 `active_model`；
    - 默认 `max_tokens = 16384`，防止推理模型在思考阶段因 token 上限过低（如旧版 4096）而被截断。
    """

    def __init__(
        self,
        provider: str = "deepseek",
        api_format: Optional[str] = None,
        base_url: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        model: str = "deepseek-chat",
        thinking_level: str = "medium",
        temperature: float = 0.2,
        max_tokens: int = 16384,
        stream: bool = True,
        always_on_top: bool = True,
        first_prompt_shown: bool = False,
        profiles: Optional[List[APIProfile]] = None,
        active_profile_id: str = "",
        active_model: str = "",
    ) -> None:
        self.thinking_level = thinking_level if thinking_level in VALID_THINKING_IDS else "medium"
        self.temperature = float(temperature)
        # 保证至少 16384，避免旧配置文件中的 4096 导致思考未完成即停止
        self.max_tokens = max(16384, int(max_tokens or 16384))
        self.stream = bool(stream)
        self.always_on_top = bool(always_on_top)
        self.first_prompt_shown = bool(first_prompt_shown)

        if profiles is not None and len(profiles) > 0:
            self.profiles: List[APIProfile] = list(profiles)
        else:
            preset = get_provider_preset(provider)
            resolved_fmt = api_format or ("antigravity" if provider.startswith("antigravity") else preset.get("api_format", "openai"))
            preset_models = list(preset.get("models") or [])
            init_models = [model] + [m for m in preset_models if m != model] if model else preset_models
            default_prof = APIProfile(
                id="default_api",
                name=preset.get("name", provider).split(" (")[0],
                provider=provider,
                api_format=resolved_fmt,
                base_url=base_url,
                api_key=api_key,
                models=init_models,
                default_model=model or preset.get("model", "deepseek-chat"),
            )
            default_prof.ensure_valid_models()
            self.profiles = [default_prof]

        self.active_profile_id = active_profile_id or self.profiles[0].id
        active_prof = self.get_active_profile()
        self.active_model = active_model or active_prof.default_model

    def get_active_profile(self) -> APIProfile:
        for p in self.profiles:
            if p.id == self.active_profile_id:
                p.ensure_valid_models()
                return p
        if not self.profiles:
            self.profiles.append(APIProfile(id="default_api"))
        self.active_profile_id = self.profiles[0].id
        self.profiles[0].ensure_valid_models()
        return self.profiles[0]

    def get_profile_by_id(self, profile_id: str) -> Optional[APIProfile]:
        for p in self.profiles:
            if p.id == profile_id:
                p.ensure_valid_models()
                return p
        return None

    def set_active_selection(self, profile_id: str, model_name: str) -> bool:
        prof = self.get_profile_by_id(profile_id)
        if prof is None:
            return False
        m_clean = model_name.strip()
        if m_clean and m_clean not in prof.models:
            prof.models.insert(0, m_clean)
        self.active_profile_id = prof.id
        if m_clean:
            self.active_model = m_clean
            prof.default_model = m_clean
        else:
            self.active_model = prof.default_model
        return True

    def list_all_model_choices(self) -> List[Dict[str, str]]:
        configured_choices: List[Dict[str, str]] = []
        unconfigured_choices: List[Dict[str, str]] = []
        for prof in self.profiles:
            prof.ensure_valid_models()
            fmt_short = get_api_format_info(prof.api_format)["short"]
            target_list = configured_choices if prof.is_configured() else unconfigured_choices
            for m in prof.models:
                target_list.append({
                    "profile_id": prof.id,
                    "profile_name": prof.name,
                    "api_format": prof.api_format,
                    "model": m,
                    "label": f"[{prof.name}] {m}",
                    "tooltip": f"API: {prof.name} ({fmt_short})\n地址: {prof.base_url}\n模型: {m}",
                })
        return configured_choices if configured_choices else unconfigured_choices

    @property
    def provider(self) -> str:
        return self.get_active_profile().provider

    @provider.setter
    def provider(self, val: str) -> None:
        self.get_active_profile().provider = str(val).strip()

    @property
    def api_format(self) -> str:
        return self.get_active_profile().api_format

    @api_format.setter
    def api_format(self, val: str) -> None:
        self.get_active_profile().api_format = str(val).strip()

    @property
    def base_url(self) -> str:
        return self.get_active_profile().base_url

    @base_url.setter
    def base_url(self, val: str) -> None:
        self.get_active_profile().base_url = str(val).strip()

    @property
    def api_key(self) -> str:
        return self.get_active_profile().api_key

    @api_key.setter
    def api_key(self, val: str) -> None:
        self.get_active_profile().api_key = str(val).strip()

    @property
    def model(self) -> str:
        prof = self.get_active_profile()
        if self.active_model and self.active_model in prof.models:
            return self.active_model
        return prof.default_model

    @model.setter
    def model(self, val: str) -> None:
        m = str(val).strip()
        prof = self.get_active_profile()
        if m:
            if m not in prof.models:
                prof.models.insert(0, m)
            prof.default_model = m
            self.active_model = m

    @classmethod
    def config_path(cls) -> Path:
        return _get_config_dir() / "config.json"

    @classmethod
    def load(cls, logger=None) -> "AIConfig":
        path = cls.config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return cls()

            t_level = str(raw.get("thinking_level", "medium")).strip().lower()
            if t_level not in VALID_THINKING_IDS:
                t_level = "medium"

            raw_profiles = raw.get("profiles")
            profiles: List[APIProfile] = []
            if isinstance(raw_profiles, list) and raw_profiles:
                for item in raw_profiles:
                    if isinstance(item, dict):
                        profiles.append(APIProfile.from_dict(item))

            if not profiles:
                profiles.append(APIProfile.from_dict(raw))

            active_pid = str(raw.get("active_profile_id") or profiles[0].id).strip()
            active_mod = str(raw.get("active_model") or raw.get("model") or "").strip()
            saved_max_tokens = int(raw.get("max_tokens", 16384))
            if saved_max_tokens <= 4096:
                saved_max_tokens = 16384

            return cls(
                thinking_level=t_level,
                temperature=float(raw.get("temperature", 0.2)),
                max_tokens=saved_max_tokens,
                stream=bool(raw.get("stream", True)),
                always_on_top=bool(raw.get("always_on_top", True)),
                first_prompt_shown=bool(raw.get("first_prompt_shown", False)),
                profiles=profiles,
                active_profile_id=active_pid,
                active_model=active_mod,
            )
        except Exception as exc:
            if logger:
                logger.warning("读取 AI 配置文件失败，使用默认配置: %s", exc)
            return cls()

    def save(self, logger=None) -> bool:
        path = self.config_path()
        if self.thinking_level not in VALID_THINKING_IDS:
            self.thinking_level = "medium"
        active_prof = self.get_active_profile()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "active_profile_id": active_prof.id,
                "active_model": self.model,
                "profiles": [p.to_dict() for p in self.profiles],
                "provider": active_prof.provider,
                "api_format": active_prof.api_format,
                "base_url": active_prof.base_url.strip(),
                "api_key": active_prof.api_key.strip(),
                "model": self.model,
                "thinking_level": self.thinking_level,
                "temperature": float(self.temperature),
                "max_tokens": int(self.max_tokens),
                "stream": bool(self.stream),
                "always_on_top": bool(self.always_on_top),
                "first_prompt_shown": bool(self.first_prompt_shown),
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            if logger:
                logger.warning("保存 AI 配置文件失败: %s", exc)
            return False

    def is_configured(self) -> bool:
        if self.get_active_profile().is_configured():
            return True
        for p in self.profiles:
            if p.is_configured():
                self.active_profile_id = p.id
                self.active_model = p.default_model
                return True
        return False

    def is_antigravity_format(self) -> bool:
        prof = self.get_active_profile()
        return prof.api_format.startswith("antigravity") or prof.provider.startswith("antigravity")


# ============================================================================
# 1.5 远端 API 模型列表自动探测器 (ModelDiscoveryWorker)
# ============================================================================

class _HttpStatusError(Exception):
    def __init__(self, code: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.body = body


def _http_get_text(url: str, headers: Dict[str, str], timeout_sec: int = 12) -> str:
    """兼容 urllib 与 PySide6.QtNetwork 的 GET 请求封装。"""
    if HAS_URLLIB and urllib is not None:
        try:
            ssl_ctx = ssl.create_default_context() if ssl is not None else None
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            if ssl is not None and hasattr(ssl, "_create_unverified_context"):
                try:
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl._create_unverified_context()) as resp:
                        return resp.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
    if QtNetwork is not None:
        nam = QtNetwork.QNetworkAccessManager()
        qreq = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
        for k, v in headers.items():
            qreq.setRawHeader(k.encode("utf-8"), str(v).encode("utf-8"))
        loop = QtCore.QEventLoop()
        reply = nam.get(qreq)
        QtCore.QTimer.singleShot(timeout_sec * 1000, loop.quit)
        reply.finished.connect(loop.quit)
        loop.exec()
        status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw_bytes = bytes(reply.readAll())
        reply.deleteLater()
        if status and int(status) >= 400:
            raise _HttpStatusError(int(status), f"HTTP {status}", raw_bytes.decode("utf-8", errors="replace"))
        return raw_bytes.decode("utf-8", errors="replace")
    raise RuntimeError("当前环境未找到可用的网络模块 (QtNetwork / urllib)")


def fetch_models_from_api(base_url: str, api_key: str, api_format: str = "openai", provider: str = "") -> List[str]:
    """根据用户输入的 Base URL 与 API Key，向远端服务请求可用模型列表。"""
    base = base_url.strip().rstrip("/")
    if not base:
        return []

    key = api_key.strip() or "ollama"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "x-goog-api-key": key,
        "anthropic-version": "2023-06-01",
        "User-Agent": "APMLogViewer-AIAssistant/1.4",
    }

    endpoints: List[str] = []
    clean_root = base
    for suffix in ("/chat/completions", "/v1/messages", "/messages"):
        if clean_root.endswith(suffix):
            clean_root = clean_root[: -len(suffix)].rstrip("/")

    if api_format == "antigravity_gemini" or "/v1beta" in clean_root:
        root_no_v1 = clean_root[:-3] if clean_root.endswith("/v1") else clean_root
        if root_no_v1.endswith("/v1beta"):
            endpoints.append(f"{root_no_v1}/models")
        else:
            endpoints.append(f"{root_no_v1}/v1beta/models")

    if clean_root.endswith("/v1") or clean_root.endswith("/v3") or clean_root.endswith("/v4"):
        endpoints.append(f"{clean_root}/models")
    else:
        endpoints.append(f"{clean_root}/v1/models")
        endpoints.append(f"{clean_root}/models")

    root_no_v1 = clean_root[:-3] if clean_root.endswith("/v1") else clean_root
    if f"{root_no_v1}/v1beta/models" not in endpoints:
        endpoints.append(f"{root_no_v1}/v1beta/models")
    if provider == "ollama" or ":11434" in clean_root:
        endpoints.append(f"{root_no_v1}/api/tags")

    discovered: List[str] = []
    for ep in endpoints:
        try:
            raw_body = _http_get_text(ep, headers, timeout_sec=12)
            if not raw_body:
                continue
            pkt = json.loads(raw_body)
            items = []
            if isinstance(pkt, dict):
                items = pkt.get("data") or pkt.get("models") or []
            elif isinstance(pkt, list):
                items = pkt

            for item in items:
                m_id = ""
                if isinstance(item, str):
                    m_id = item.strip()
                elif isinstance(item, dict):
                    m_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
                    if m_id.startswith("models/"):
                        m_id = m_id[len("models/"):]
                if m_id and m_id not in discovered:
                    discovered.append(m_id)
            if discovered:
                break
        except Exception:
            continue

    return discovered


class ModelDiscoveryWorker(QtCore.QThread):
    """在后台线程拉取 API 可用模型列表，避免阻塞配置弹窗。"""

    models_fetched = QtCore.Signal(list)
    fetch_failed = QtCore.Signal(str)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_format: str,
        provider: str,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key
        self.api_format = api_format
        self.provider = provider

    def run(self) -> None:
        try:
            models = fetch_models_from_api(self.base_url, self.api_key, self.api_format, self.provider)
            if models:
                self.models_fetched.emit(models)
            else:
                self.fetch_failed.emit("接口未返回动态模型列表（已为您保留该服务商的内置多模型清单，也可手动添加）。")
        except Exception as exc:
            self.fetch_failed.emit(f"拉取模型列表失败：{exc}")


# ============================================================================
# 2. 日志智能特征提取引擎 (LogKnowledgeExtractor)
# ============================================================================

CORE_SUBSYSTEM_CHANNELS: List[Tuple[str, List[str]]] = [
    ("电池与电源 (BAT/POWR)", [
        "BAT.Volt", "BAT.VoltR", "BAT.Curr", "BAT.CurrTot", "BAT.EnrgTot", "BAT.Temp",
        "POWR.Vcc", "POWR.VServo", "POWR.Flags",
    ]),
    ("三轴震动与削顶 (VIBE)", [
        "VIBE.VibeX", "VIBE.VibeY", "VIBE.VibeZ",
        "VIBE.Clip0", "VIBE.Clip1", "VIBE.Clip2",
    ]),
    ("姿态与期望跟踪 (ATT)", [
        "ATT.DesRoll", "ATT.Roll", "ATT.DesPitch", "ATT.Pitch",
        "ATT.DesYaw", "ATT.Yaw", "ATT.ErrRP", "ATT.ErrYaw",
    ]),
    ("角速度控制 (RATE)", [
        "RATE.RDes", "RATE.R", "RATE.PDes", "RATE.P", "RATE.YDes", "RATE.Y",
    ]),
    ("高度、垂速与油门 (CTUN/POS)", [
        "CTUN.Alt", "CTUN.DAlt", "CTUN.BAlt", "CTUN.SAlt",
        "CTUN.ThO", "CTUN.ThI", "CTUN.CRt", "CTUN.DCRt",
        "POS.Alt", "POS.RelHomeAlt",
    ]),
    ("GPS 与卫星定位 (GPS)", [
        "GPS.Status", "GPS.NSats", "GPS.HDop", "GPS.Spd", "GPS.Alt", "GPS.VZ",
    ]),
    ("电机 PWM 输出 (RCOU)", [
        "RCOU.C1", "RCOU.C2", "RCOU.C3", "RCOU.C4",
        "RCOU.C5", "RCOU.C6", "RCOU.C7", "RCOU.C8",
    ]),
    ("遥控器输入 (RCIN)", [
        "RCIN.C1", "RCIN.C2", "RCIN.C3", "RCIN.C4",
    ]),
    ("EKF 导航状态与创新量 (XKF4/NKF4)", [
        "XKF4.SV", "XKF4.SP", "XKF4.SH", "XKF4.SM", "XKF4.VT", "XKF4.FS", "XKF4.SS",
        "NKF4.SV", "NKF4.SP", "NKF4.SH", "NKF4.SM",
    ]),
    ("罗盘与气压计 (MAG/BARO)", [
        "MAG.MagX", "MAG.MagY", "MAG.MagZ",
        "BARO.Alt", "BARO.Press", "BARO.Temp",
    ]),
]

KEY_PARAM_PREFIXES = (
    "FRAME_", "BATT_", "MOT_", "INS_ACCEL_FILTER", "INS_GYRO_FILTER",
    "INS_HNTCH_", "ATC_ANG_", "ATC_RAT_RLL_", "ATC_RAT_PIT_", "ATC_RAT_YAW_",
    "PSC_POSZ_", "PSC_VELZ_", "PSC_ACCZ_", "WPNAV_", "LOIT_",
    "FS_THR_", "FS_EKF_", "FS_BATT_", "FS_GCS_", "ARMING_", "RTL_",
)


def _fmt_num(val: Any, digits: int = 2) -> str:
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return "NaN"
        if abs(f) >= 1000 or (0 < abs(f) < 0.01):
            return f"{f:.{digits}f}"
        return f"{f:.{digits}f}".rstrip("0").rstrip(".") if "." in f"{f:.{digits}f}" else f"{f}"
    except Exception:
        return str(val)


class LogKnowledgeExtractor:
    """
    从只读 ctx.store 中提取结构化飞控日志摘要，并支持按需抽取指定时间段或通道的精细时序。
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._cached_log_path: Optional[str] = None
        self._cached_digest: Optional[str] = None
        self.latest_alerts: List[Dict[str, Any]] = []
        self.current_time: float = 0.0
        self.current_mode: str = ""
        self.selection: Dict[str, Any] = {"t0": None, "t1": None, "whole_log": True}

    def clear_cache(self) -> None:
        self._cached_log_path = None
        self._cached_digest = None

    def on_alerts_updated(self, alerts: Any) -> None:
        if isinstance(alerts, list):
            self.latest_alerts = [dict(a) for a in alerts if isinstance(a, dict)]
            self._cached_digest = None

    def get_base_digest(self) -> str:
        store = self.ctx.store
        if not store.available:
            return "【当前状态】宿主尚未打开任何 .BIN 日志文件。请提示用户先打开一份 ArduPilot DataFlash 日志，或解答通用飞控问题。"

        try:
            cur_path = store.path() or store.name()
        except Exception:
            cur_path = None

        if self._cached_digest and cur_path and cur_path == self._cached_log_path:
            return self._cached_digest

        digest = self._build_full_digest(store)
        self._cached_log_path = cur_path
        self._cached_digest = digest
        return digest

    def _build_full_digest(self, store) -> str:
        sections: List[str] = []

        try:
            stats = store.stats() or {}
            name = store.name()
        except Exception:
            stats = {}
            name = "未知日志"

        duration = float(stats.get("duration") or 0.0)
        t_start = float(stats.get("t_start") or 0.0)
        t_end = float(stats.get("t_end") or duration)
        takeoff_t = stats.get("takeoff_t")
        land_t = stats.get("land_t")

        info_lines = [
            "=== 1. 日志基础概况 ===",
            f"- 文件名: {name}",
            f"- 固件与硬件: 固件={stats.get('firmware') or '未知'} | 飞控板={stats.get('board') or '未知'} | 机架={stats.get('frame_type') or '未知'} | Git={stats.get('git') or '-'}",
            f"- 时间范围: {t_start:.1f}s – {t_end:.1f}s (总时长 {duration:.1f}s)"
            + (f" | 起飞时刻={float(takeoff_t):.1f}s" if takeoff_t is not None else "")
            + (f" | 降落时刻={float(land_t):.1f}s" if land_t is not None else ""),
            f"- 飞行包线: 最大高度={_fmt_num(stats.get('max_alt'))}m | 总航程={_fmt_num(stats.get('distance'))}m | 最大地速={_fmt_num(stats.get('max_speed'))}m/s | 位置源={stats.get('pos_source') or '无'} ({stats.get('pos_kind') or '-'})",
            f"- 报文规模: 总消息数={stats.get('n_messages')} | 报文类型数={stats.get('n_types')} | 参数数={stats.get('n_params')} | 事件数={stats.get('n_events')}",
        ]
        if stats.get("crashes"):
            info_lines.append(f"- ⚠️ 坠机/冲击记录 (crashes): {stats.get('crashes')}")
        if stats.get("warnings"):
            info_lines.append(f"- ⚠️ 摘要警告 (warnings): {stats.get('warnings')}")

        try:
            all_types = store.types()
            info_lines.append(f"- 包含的报文类型 ({len(all_types)}种): {', '.join(all_types[:50])}")
        except Exception:
            all_types = []
        sections.append("\n".join(info_lines))

        try:
            modes = store.modes() or []
        except Exception:
            modes = []
        if modes:
            m_lines = ["=== 2. 飞行模式切换序列 (modes) ==="]
            for m in modes[:30]:
                mt0 = float(m.get("t0") or 0.0)
                mt1 = float(m.get("t1") or mt0)
                m_lines.append(f"- {mt0:.1f}s – {mt1:.1f}s ({mt1 - mt0:.1f}s): {m.get('mode', '?')}")
            if len(modes) > 30:
                m_lines.append(f"  ... 其余 {len(modes) - 30} 次切换省略")
            sections.append("\n".join(m_lines))

        if self.latest_alerts:
            a_lines = [f"=== 3. 自动告警规则命中 ({len(self.latest_alerts)} 条) ==="]
            for a in self.latest_alerts[:30]:
                sev = str(a.get("severity") or "warn").upper()
                title = a.get("title") or a.get("rule") or "告警"
                at0 = float(a.get("t0") or 0.0)
                at1 = float(a.get("t1") or at0)
                val_s = _fmt_num(a.get("value"))
                thr_s = _fmt_num(a.get("threshold"))
                unit = a.get("unit") or ""
                msg = a.get("message") or ""
                ch = a.get("channel_label") or a.get("channel") or ""
                time_str = f"{at0:.1f}s" if abs(at1 - at0) < 0.05 else f"{at0:.1f}s–{at1:.1f}s"
                a_lines.append(
                    f"- [{sev}] {time_str} | {title} (通道:{ch}, 触发值:{val_s}{unit}, 阈值:{thr_s}{unit}) — {msg}"
                )
            sections.append("\n".join(a_lines))
        else:
            sections.append("=== 3. 自动告警规则命中 ===\n- 当前未触发预设阈值告警（仍需结合下方通道极值与事件排查隐患）。")

        try:
            events = store.events() or []
        except Exception:
            events = []
        if events:
            e_lines = [f"=== 4. 关键飞控事件与消息记录 (共 {len(events)} 条) ==="]
            selected_events = events if len(events) <= 45 else (
                [e for e in events if str(e.get("severity", "")).lower() in ("warn", "error", "critical") or e.get("kind") in ("ERR", "EV")][:30]
                + events[:15]
            )
            seen_idx = set()
            dedup_events = []
            for e in selected_events:
                sig = (round(float(e.get("t") or 0.0), 2), e.get("kind"), e.get("display") or e.get("decoded") or e.get("text"))
                if sig not in seen_idx:
                    seen_idx.add(sig)
                    dedup_events.append(e)
            dedup_events.sort(key=lambda x: float(x.get("t") or 0.0))
            for e in dedup_events[:45]:
                et = float(e.get("t") or 0.0)
                kind = e.get("kind") or "EVT"
                sev = e.get("severity") or "info"
                disp = e.get("display") or e.get("decoded") or e.get("text") or ""
                e_lines.append(f"- t={et:.2f}s [{kind}/{sev}] {disp}")
            sections.append("\n".join(e_lines))

        ch_lines = ["=== 5. 核心飞控子系统通道统计与极值时刻 ==="]
        for group_name, cand_keys in CORE_SUBSYSTEM_CHANNELS:
            group_items: List[str] = []
            for key in cand_keys:
                stat_line = self._summarize_single_channel(store, key)
                if stat_line:
                    group_items.append(stat_line)
            if group_items:
                ch_lines.append(f"【{group_name}】")
                ch_lines.extend(group_items)

        derived_lines = self._compute_derived_diagnostics(store)
        if derived_lines:
            ch_lines.append("【插件自动计算的交叉诊断指标】")
            ch_lines.extend(derived_lines)

        sections.append("\n".join(ch_lines))

        try:
            params = store.params() or {}
        except Exception:
            params = {}
        if params:
            p_lines = ["=== 6. 关键飞控参数 (params) ==="]
            matched_params: List[str] = []
            for pname in sorted(params.keys()):
                if any(pname.startswith(pfx) for pfx in KEY_PARAM_PREFIXES):
                    pinfo = params[pname]
                    pval = pinfo.get("value") if isinstance(pinfo, dict) else pinfo
                    pdef = pinfo.get("default") if isinstance(pinfo, dict) else None
                    if pdef is not None and pval is not None and abs(float(pval) - float(pdef)) > 1e-6:
                        matched_params.append(f"{pname}={_fmt_num(pval, 4)} (默认:{_fmt_num(pdef, 4)})")
                    else:
                        matched_params.append(f"{pname}={_fmt_num(pval, 4)}")
            if matched_params:
                for i in range(0, min(len(matched_params), 60), 4):
                    p_lines.append("- " + " | ".join(matched_params[i : i + 4]))
                sections.append("\n".join(p_lines))

        return "\n\n".join(sections)

    def _summarize_single_channel(
        self,
        store,
        key: str,
        t0: Optional[float] = None,
        t1: Optional[float] = None,
        max_points: int = 4000,
    ) -> Optional[str]:
        try:
            s = store.series(key, t0=t0, t1=t1, max_points=max_points, drop_nan=True)
        except (RuntimeError, KeyError, Exception):
            return None
        if s is None or s.n == 0 or s.v.size == 0:
            return None
        v = np.asarray(s.v, dtype=np.float64)
        t = np.asarray(s.t, dtype=np.float64)
        finite_mask = np.isfinite(v) & np.isfinite(t)
        if not np.any(finite_mask):
            return None
        vf = v[finite_mask]
        tf = t[finite_mask]
        idx_min = int(np.argmin(vf))
        idx_max = int(np.argmax(vf))
        vmin, t_min = float(vf[idx_min]), float(tf[idx_min])
        vmax, t_max = float(vf[idx_max]), float(tf[idx_max])
        vmean = float(np.mean(vf))
        vstd = float(np.std(vf))
        vp05 = float(np.percentile(vf, 5))
        vp95 = float(np.percentile(vf, 95))
        unit = f" {s.unit}" if getattr(s, "unit", "") else ""
        return (
            f"  * {s.key} ({s.label}): 均值={_fmt_num(vmean)}{unit}, 标准差={_fmt_num(vstd)}, "
            f"最小={_fmt_num(vmin)}{unit} (@{t_min:.1f}s), 最大={_fmt_num(vmax)}{unit} (@{t_max:.1f}s), "
            f"P05~P95=[{_fmt_num(vp05)}, {_fmt_num(vp95)}], 采样数={s.n}"
        )

    def _compute_derived_diagnostics(self, store) -> List[str]:
        out: List[str] = []
        for des_k, act_k, axis_name in (
            ("ATT.DesRoll", "ATT.Roll", "Roll横滚"),
            ("ATT.DesPitch", "ATT.Pitch", "Pitch俯仰"),
        ):
            try:
                s_des = store.series(des_k, max_points=4000, drop_nan=True)
                s_act = store.series(act_k, max_points=4000, drop_nan=True)
                if s_des.n > 10 and s_act.n == s_des.n:
                    err = np.abs(np.asarray(s_des.v) - np.asarray(s_act.v))
                    i_max = int(np.nanargmax(err))
                    rmse = float(np.sqrt(np.nanmean(err ** 2)))
                    out.append(
                        f"  * {axis_name}期望跟踪绝对误差 |{des_k}-{act_k}|: "
                        f"RMSE={rmse:.2f} deg, 最大偏差={float(err[i_max]):.2f} deg (@{float(s_des.t[i_max]):.1f}s)"
                    )
            except Exception:
                pass

        motor_series = []
        for mk in ("RCOU.C1", "RCOU.C2", "RCOU.C3", "RCOU.C4"):
            try:
                ms = store.series(mk, max_points=4000, drop_nan=True)
                if ms.n > 20:
                    motor_series.append((mk, ms))
            except Exception:
                pass
        if len(motor_series) >= 4:
            try:
                n_len = min(ms.n for _, ms in motor_series)
                mat = np.vstack([np.asarray(ms.v[:n_len], dtype=np.float64) for _, ms in motor_series])
                t_arr = np.asarray(motor_series[0][1].t[:n_len], dtype=np.float64)
                active = np.mean(mat, axis=0) > 1150.0
                if np.any(active):
                    spread = np.max(mat[:, active], axis=0) - np.min(mat[:, active], axis=0)
                    t_act = t_arr[active]
                    idx_sp = int(np.argmax(spread))
                    mean_per_motor = [f"{mk.split('.')[1]}={float(np.mean(mat[i, active])):.0f}" for i, (mk, _) in enumerate(motor_series)]
                    out.append(
                        f"  * 四轴电机飞行段输出均衡度 (PWM>1150): 各电机均值[{', '.join(mean_per_motor)}], "
                        f"平均极差={float(np.mean(spread)):.0f} PWM, 最大极差={float(spread[idx_sp]):.0f} PWM (@{float(t_act[idx_sp]):.1f}s)"
                    )
            except Exception:
                pass

        try:
            vx = store.series("VIBE.VibeX", max_points=4000, drop_nan=True)
            vy = store.series("VIBE.VibeY", max_points=4000, drop_nan=True)
            vz = store.series("VIBE.VibeZ", max_points=4000, drop_nan=True)
            if vx.n > 10 and vx.n == vy.n == vz.n:
                vnorm = np.sqrt(np.asarray(vx.v) ** 2 + np.asarray(vy.v) ** 2 + np.asarray(vz.v) ** 2)
                idx_v = int(np.nanargmax(vnorm))
                over30 = float(np.mean((np.asarray(vx.v) > 30) | (np.asarray(vy.v) > 30) | (np.asarray(vz.v) > 30)) * 100.0)
                out.append(
                    f"  * 震动超标占比 (任一轴 > 30 m/s²): {over30:.1f}%, "
                    f"三轴合成峰值={float(vnorm[idx_v]):.1f} m/s² (@{float(vx.t[idx_v]):.1f}s)"
                )
        except Exception:
            pass

        return out

    def build_dynamic_query_context(
        self,
        user_question: str,
        explicit_keys: Optional[Sequence[str]] = None,
        explicit_window: Optional[Tuple[float, float]] = None,
    ) -> str:
        store = self.ctx.store
        if not store.available:
            return ""

        extra_blocks: List[str] = []

        ui_state_lines = [
            "=== 当前用户界面实时状态 ===",
            f"- 当前时间线游标位置: {self.current_time:.2f}s"
            + (f" (当前时刻飞行模式: {self.current_mode})" if self.current_mode else ""),
        ]
        sel_t0 = self.selection.get("t0")
        sel_t1 = self.selection.get("t1")
        whole_log = bool(self.selection.get("whole_log", True))
        if not whole_log and sel_t0 is not None and sel_t1 is not None:
            ui_state_lines.append(f"- 用户当前在曲线上框选的时间区间: {float(sel_t0):.2f}s – {float(sel_t1):.2f}s")
        extra_blocks.append("\n".join(ui_state_lines))

        target_window: Optional[Tuple[float, float]] = explicit_window
        if target_window is None:
            range_match = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:s|秒)?\s*(?:到|至|~|-|—|–)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?",
                user_question,
                re.IGNORECASE,
            )
            if range_match:
                a, b = float(range_match.group(1)), float(range_match.group(2))
                if a != b:
                    target_window = (min(a, b), max(a, b))
            else:
                point_match = re.search(r"(?:第|t\s*=?\s*)?(\d+(?:\.\d+)?)\s*(?:s|秒)", user_question, re.IGNORECASE)
                if point_match:
                    center = float(point_match.group(1))
                    target_window = (max(0.0, center - 10.0), center + 10.0)
                elif not whole_log and sel_t0 is not None and sel_t1 is not None:
                    target_window = (float(sel_t0), float(sel_t1))

        matched_keys: List[str] = list(explicit_keys or [])
        try:
            all_keys = store.keys()
            all_types = store.types()
        except Exception:
            all_keys, all_types = [], []

        q_upper = user_question.upper()
        for tname in all_types:
            if tname.upper() in q_upper:
                type_keys = [k for k in all_keys if k.upper().startswith(tname.upper() + "#") or k.upper().startswith(tname.upper() + ".")]
                for tk in type_keys[:8]:
                    if tk not in matched_keys:
                        matched_keys.append(tk)

        if target_window is not None:
            wt0, wt1 = target_window
            default_win_keys = [
                "BAT.Volt", "BAT.Curr", "VIBE.VibeX", "VIBE.VibeY", "VIBE.VibeZ",
                "ATT.DesRoll", "ATT.Roll", "ATT.DesPitch", "ATT.Pitch",
                "CTUN.Alt", "CTUN.DAlt", "CTUN.ThO", "GPS.Status", "GPS.NSats", "GPS.Spd",
                "RCOU.C1", "RCOU.C2", "RCOU.C3", "RCOU.C4",
            ]
            for dk in default_win_keys:
                if dk not in matched_keys:
                    matched_keys.append(dk)

            win_lines = [f"=== 针对目标时间区间 [{wt0:.2f}s – {wt1:.2f}s] 的局部精细提取 ==="]
            try:
                win_events = store.events(t0=wt0, t1=wt1) or []
                if win_events:
                    win_lines.append("【区间内事件】")
                    for e in win_events[:25]:
                        win_lines.append(
                            f"  - t={float(e.get('t') or 0.0):.2f}s [{e.get('kind')}] {e.get('display') or e.get('decoded') or e.get('text')}"
                        )
            except Exception:
                pass

            win_lines.append("【区间内关键通道统计与时序采样】")
            for mk in matched_keys[:18]:
                slice_line = self._extract_series_slice_text(store, mk, wt0, wt1, n_samples=12)
                if slice_line:
                    win_lines.append(slice_line)
            extra_blocks.append("\n".join(win_lines))
        elif matched_keys:
            sp_lines = ["=== 用户问题提及通道的专项详细数据 ==="]
            for mk in matched_keys[:15]:
                slice_line = self._extract_series_slice_text(store, mk, None, None, n_samples=15)
                if slice_line:
                    sp_lines.append(slice_line)
            extra_blocks.append("\n".join(sp_lines))

        return "\n\n".join(extra_blocks)

    def _extract_series_slice_text(
        self,
        store,
        key: str,
        t0: Optional[float],
        t1: Optional[float],
        n_samples: int = 12,
    ) -> Optional[str]:
        try:
            s = store.series(key, t0=t0, t1=t1, max_points=2000, drop_nan=True)
        except Exception:
            return None
        if s is None or s.n == 0 or s.v.size == 0:
            return None
        v = np.asarray(s.v, dtype=np.float64)
        t = np.asarray(s.t, dtype=np.float64)
        mask = np.isfinite(v) & np.isfinite(t)
        if not np.any(mask):
            return None
        vf, tf = v[mask], t[mask]
        imin, imax = int(np.argmin(vf)), int(np.argmax(vf))
        unit = s.unit or ""
        idxs = np.linspace(0, len(vf) - 1, num=min(n_samples, len(vf)), dtype=int)
        pts = ", ".join(f"{tf[i]:.1f}s:{_fmt_num(vf[i])}" for i in idxs)
        return (
            f"  * {s.key} ({unit}): 区间均值={_fmt_num(np.mean(vf))}, "
            f"最小={_fmt_num(vf[imin])}(@{tf[imin]:.1f}s), 最大={_fmt_num(vf[imax])}(@{tf[imax]:.1f}s) | "
            f"趋势采样: [{pts}]"
        )


# ============================================================================
# 3. 异步流式大模型调用引擎 (LLMWorker) —— 节流防卡顿 + 防截断保护
# ============================================================================

SYSTEM_PROMPT_TEMPLATE = """你是内置在 **APMLogViewer (ArduPilot/ArduCopter DataFlash 日志分析软件)** 中的专业无人机飞控日志 AI 诊断专家。
无论用户当前在软件的哪个界面（曲线、轨迹地图、告警、参数等），你都能直接读取当前打开的这份日志数据并给出准确、清晰的诊断解释。

{thinking_instruction}

### 回答准则：
1. **严格基于实测日志数据**：所有结论必须引用下方【当前打开的日志实测数据】中的真实数值、通道名称（如 `VIBE.VibeZ`、`BAT.Volt`、`ATT.Roll`、`RCOU.C1`）和精确发生时刻（如 `125.4s` 或 `80.0s-95.0s`）。切勿编造日志中不存在的数据。
2. **时间戳与通道格式规范（支持宿主一键点击跳转）**：
   - 提到具体时刻时，请统一写作 `125.4s`（数字+s），用户点击该时间即可将软件游标和地图直接定位到该秒数；
   - 提到时间段时，请写作 `80.0s-95.0s`，用户点击即可放大该区间曲线；
   - 提到飞控通道时，请写作标准 `TYPE.Field` 格式（如 `BAT.Volt`、`VIBE.VibeZ`、`ATT.DesRoll`、`CTUN.ThO`），用户点击即可将该通道加入曲线视图。
3. **结构清晰、直击重点**：先给出核心识别结论，再列出关键证据（时间点 + 通道数值变化 + 对比正常范围），最后给出排查或调参建议。
4. **思考与正式回答完整性（重要）**：若启用了思考过程，请保持思考精炼聚焦（不要逐行抄写原始日志数据），思考结束后**必须立即输出完整的正式诊断正文**，绝不可只输出思考内容而省略正式解答。
5. **按需二次查数能力（可选）**：如果用户询问了某个非常具体的冷门通道或具体时间区间，而当前摘要中恰好缺少该通道的细节，你可以在回复末尾单独附带一行指令：
   `<QUERY_SERIES keys="通道1,通道2" t0="起始秒" t1="结束秒"/>`
   插件会自动从日志提取对应时序后再为你补充数据。若当前数据已足够回答，请直接给出完整解答，不要输出该标签。

---
### 【当前打开的日志实测数据】
{log_digest}

{dynamic_context}
"""


def build_thinking_payload_params(config: AIConfig) -> Tuple[str, Dict[str, Any]]:
    """
    针对 OpenAI 兼容格式构造模型名与思考等级附加字段。
    返回: (effective_model_name, extra_payload_fields)
    """
    model = config.model.strip()
    level = config.thinking_level.strip().lower()
    provider = config.provider.strip().lower()
    extra: Dict[str, Any] = {}

    if level == "auto" or level not in VALID_THINKING_IDS:
        return model, extra

    lvl_info = get_thinking_level_info(level)
    budget = lvl_info.get("budget")
    url_lower = config.base_url.lower()
    is_custom = (provider == "custom")

    if provider == "deepseek" or (is_custom and "api.deepseek.com" in url_lower):
        if level == "off":
            if model.lower() == "deepseek-reasoner":
                model = "deepseek-chat"
        elif level in ("low", "medium", "high"):
            if model.lower() == "deepseek-chat":
                model = "deepseek-reasoner"
        return model, extra

    if provider in ("qwen", "siliconflow") or (is_custom and ("dashscope.aliyuncs.com" in url_lower or "siliconflow.cn" in url_lower)):
        if level == "off":
            extra["enable_thinking"] = False
        elif level in ("low", "medium", "high"):
            extra["enable_thinking"] = True
            if budget:
                extra["thinking_budget"] = int(budget)
        return model, extra

    if provider in ("doubao", "zhipu") or (is_custom and ("volces.com" in url_lower or "bigmodel.cn" in url_lower)):
        if level == "off":
            extra["thinking"] = {"type": "disabled"}
        elif level in ("low", "medium", "high"):
            extra["thinking"] = {"type": "enabled"}
            if provider == "doubao" or "volces.com" in url_lower:
                extra["reasoning_effort"] = level
        return model, extra

    if level in ("low", "medium", "high"):
        extra["reasoning_effort"] = level

    return model, extra


def build_antigravity_messages_request(
    config: AIConfig,
    messages: List[Dict[str, str]],
    include_thinking: bool = True,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """
    构造 Antigravity 兼容格式 —— `/v1/messages` 请求。
    保证 `max_tokens` 远大于 `thinking.budget_tokens`（至少额外预留 8192 tokens 给正式回答），避免思考阶段耗尽配额。
    """
    base = config.base_url.strip().rstrip("/")
    if base.endswith("/v1/messages") or base.endswith("/messages"):
        endpoint = base
    elif base.endswith("/v1"):
        endpoint = base + "/messages"
    else:
        endpoint = base + "/v1/messages"

    api_key = config.api_key.strip() or "antigravity"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "User-Agent": "APMLogViewer-AIAssistant/1.4 (Antigravity-Compatible)",
    }

    sys_parts: List[str] = []
    conv_msgs: List[Dict[str, str]] = []
    for m in messages:
        role = m.get("role", "user")
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        if role == "system":
            sys_parts.append(content)
        else:
            norm_role = "assistant" if role == "assistant" else "user"
            if conv_msgs and conv_msgs[-1]["role"] == norm_role:
                conv_msgs[-1]["content"] += "\n\n" + content
            else:
                conv_msgs.append({"role": norm_role, "content": content})

    if not conv_msgs or conv_msgs[0]["role"] != "user":
        conv_msgs.insert(0, {"role": "user", "content": "请开始分析。"})

    level = config.thinking_level.strip().lower()
    lvl_info = get_thinking_level_info(level)
    budget = lvl_info.get("budget")
    base_max = max(16384, int(config.max_tokens or 16384))

    payload: Dict[str, Any] = {
        "model": config.model.strip(),
        "messages": conv_msgs,
        "stream": bool(config.stream),
    }
    if sys_parts:
        payload["system"] = "\n\n".join(sys_parts)

    if include_thinking and level in ("low", "medium", "high") and budget and budget > 0:
        effective_max = max(base_max, int(budget) + 8192)
        payload["max_tokens"] = effective_max
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": int(budget),
        }
    else:
        payload["max_tokens"] = base_max
        payload["temperature"] = float(config.temperature)

    return endpoint, headers, payload


def build_antigravity_gemini_request(
    config: AIConfig,
    messages: List[Dict[str, str]],
    include_thinking: bool = True,
    use_v1internal_envelope: bool = False,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """
    构造 Antigravity 兼容格式 —— Gemini 原生协议 (`/v1beta/models/{model}:streamGenerateContent`)
    及 `v1internal:streamGenerateContent` 请求。
    保证 `maxOutputTokens` 充足（>= 32768），防止 `thinkingBudget` 占满 `maxOutputTokens` 导致只输出思考内容。
    """
    base = config.base_url.strip().rstrip("/")
    model = config.model.strip()
    is_stream = bool(config.stream)

    if "v1internal" in base.lower():
        use_v1internal_envelope = True

    if use_v1internal_envelope:
        action = "streamGenerateContent?alt=sse" if is_stream else "generateContent"
        if ":streamGenerateContent" in base or ":generateContent" in base:
            endpoint = base
        elif base.endswith("/v1internal"):
            endpoint = f"{base}:{action}"
        else:
            clean_root = base[:-3] if base.endswith("/v1") else base
            endpoint = f"{clean_root}/v1internal:{action}"
    else:
        action = "streamGenerateContent?alt=sse" if is_stream else "generateContent"
        if ":streamGenerateContent" in base or ":generateContent" in base:
            endpoint = base
        else:
            clean_root = base[:-3] if base.endswith("/v1") else base
            if clean_root.endswith("/v1beta"):
                endpoint = f"{clean_root}/models/{model}:{action}"
            elif "/models/" in clean_root:
                endpoint = f"{clean_root}:{action}"
            else:
                endpoint = f"{clean_root}/v1beta/models/{model}:{action}"

    api_key = config.api_key.strip() or "antigravity"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
        "x-api-key": api_key,
        "User-Agent": "APMLogViewer-AIAssistant/1.4 (Antigravity-Gemini)",
    }

    sys_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        text = str(m.get("content", "")).strip()
        if not text:
            continue
        if role == "system":
            sys_parts.append(text)
        else:
            g_role = "model" if role == "assistant" else "user"
            if contents and contents[-1]["role"] == g_role:
                contents[-1]["parts"][0]["text"] += "\n\n" + text
            else:
                contents.append({"role": g_role, "parts": [{"text": text}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "请开始分析。"}]})

    level = config.thinking_level.strip().lower()
    lvl_info = get_thinking_level_info(level)
    budget = lvl_info.get("budget")

    # Gemini 的 maxOutputTokens 包含 thinkingBudget + 正文输出，因此必须保证远大于 thinkingBudget
    min_required_out = max(32768, int(config.max_tokens or 16384), int(budget or 0) + 16384)
    gen_cfg: Dict[str, Any] = {
        "maxOutputTokens": min_required_out,
        "temperature": float(config.temperature),
    }

    if include_thinking and level != "auto" and level in VALID_THINKING_IDS:
        if level == "off":
            gen_cfg["thinkingConfig"] = {
                "includeThoughts": False,
                "thinkingBudget": 0,
            }
        elif level in ("low", "medium", "high") and budget:
            gen_cfg["thinkingConfig"] = {
                "includeThoughts": True,
                "thinkingBudget": int(budget),
            }

    inner_req: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": gen_cfg,
    }
    if sys_parts:
        inner_req["systemInstruction"] = {
            "role": "user",
            "parts": [{"text": "\n\n".join(sys_parts)}],
        }

    if use_v1internal_envelope:
        payload: Dict[str, Any] = {
            "project": "antigravity",
            "model": model,
            "request": inner_req,
        }
    else:
        payload = inner_req

    return endpoint, headers, payload


def parse_antigravity_packet(pkt: Dict[str, Any]) -> Tuple[str, str]:
    """
    统一解析 Antigravity 各类响应包（兼容 `/v1/messages`、Gemini `candidates`、`v1internal` Envelope 及 OpenAI 代理包）。
    注意：在 Gemini 响应中，只有 `part.get("thought") is True` 才是思考内容；
    普通回答块也可能附带 `thoughtSignature` 签名供多轮对话校验，不能将仅带 `thoughtSignature` 的正文块误判为思考内容。
    """
    if not isinstance(pkt, dict):
        return "", ""

    r_out: List[str] = []
    c_out: List[str] = []

    evt_type = pkt.get("type")
    if evt_type == "content_block_start":
        cb = pkt.get("content_block") or {}
        if cb.get("type") == "thinking" and cb.get("thinking"):
            r_out.append(str(cb["thinking"]))
        elif cb.get("type") == "text" and cb.get("text"):
            c_out.append(str(cb["text"]))
        return "".join(r_out), "".join(c_out)

    if evt_type == "content_block_delta":
        delta = pkt.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "thinking_delta" or (dtype is None and "thinking" in delta):
            r_out.append(str(delta.get("thinking") or ""))
        elif dtype == "text_delta" or (dtype is None and "text" in delta):
            c_out.append(str(delta.get("text") or ""))
        return "".join(r_out), "".join(c_out)

    if isinstance(pkt.get("content"), list):
        for blk in pkt["content"]:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "thinking":
                r_out.append(str(blk.get("thinking") or blk.get("text") or ""))
            elif btype == "text":
                c_out.append(str(blk.get("text") or ""))
        return "".join(r_out), "".join(c_out)

    target_obj = pkt.get("response") if isinstance(pkt.get("response"), dict) else pkt
    candidates = target_obj.get("candidates")
    if isinstance(candidates, list) and candidates:
        cand0 = candidates[0] if isinstance(candidates[0], dict) else {}
        content_obj = cand0.get("content") if isinstance(cand0.get("content"), dict) else {}
        parts = content_obj.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                ptext = str(part.get("text") or "")
                if not ptext:
                    continue
                # 仅当明确标记 thought=True 时归入思考内容（或者仅有 thoughtSignature 且无 thought=False 但首块显式为 thought）
                if part.get("thought") is True:
                    r_out.append(ptext)
                else:
                    c_out.append(ptext)
            return "".join(r_out), "".join(c_out)

    choices = pkt.get("choices")
    if isinstance(choices, list) and choices:
        ch0 = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch0.get("delta") or ch0.get("message") or {}
        if isinstance(delta, dict):
            r_piece = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thought") or ""
            c_piece = delta.get("content") or ""
            if r_piece:
                r_out.append(str(r_piece))
            if c_piece:
                c_out.append(str(c_piece))

    return "".join(r_out), "".join(c_out)


def format_reply_with_thinking(reasoning_text: str, answer_text: str) -> str:
    """将模型返回的思考过程与正式回答组装成统一的 <think>...</think> 标记文本。"""
    r_clean = reasoning_text.strip()
    a_clean = answer_text.strip()
    if r_clean:
        if not a_clean.startswith("<think>"):
            return f"<think>\n{r_clean}\n</think>\n\n{a_clean}"
    return a_clean


class LLMWorker(QtCore.QThread):
    """
    在后台线程中调用大模型接口：
    - 流式信号采用 160ms 节流（避免高频刷新主线程 QTextBrowser 导致界面卡顿）；
    - OpenAI 兼容格式默认不强加低 `max_tokens` 限制，防止长思考模型在思考途中因 `finish_reason=length` 提前停止。
    """

    chunk_received = QtCore.Signal(str)
    finished_reply = QtCore.Signal(str)
    failed_error = QtCore.Signal(str)

    # 流式 UI 刷新最小间隔（秒），约 6 FPS，彻底避免主界面卡顿
    STREAM_THROTTLE_SEC = 0.16

    def __init__(
        self,
        config: AIConfig,
        messages: List[Dict[str, str]],
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.messages = messages
        self._abort = False
        self._last_emit_ts: float = 0.0

    def request_abort(self) -> None:
        self._abort = True

    @staticmethod
    def build_endpoint(base_url: str) -> str:
        url = base_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if not url.endswith("/v1") and not url.endswith("/v3") and not url.endswith("/v4"):
            parsed_parts = url.split("/")
            if len(parsed_parts) <= 3:
                url = url + "/v1"
        return url + "/chat/completions"

    def _build_openai_request(self, include_thinking_params: bool = True) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        endpoint = self.build_endpoint(self.config.base_url)
        api_key = self.config.api_key.strip() or "ollama"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "User-Agent": "APMLogViewer-AIAssistant/1.4",
        }
        effective_model, thinking_extra = build_thinking_payload_params(self.config)
        if not include_thinking_params:
            effective_model = self.config.model.strip()
            thinking_extra = {}

        # 注意：在 OpenAI 兼容接口（尤其是 DeepSeek R1 / Qwen / Antigravity 反代 / o3 等推理模型）中，
        # 不传过小的 max_tokens，让服务端使用模型原生最大输出长度（如 16K/32K/64K），避免思考到一半触发 length 截断。
        payload: Dict[str, Any] = {
            "model": effective_model,
            "messages": self.messages,
            "stream": bool(self.config.stream),
        }
        if "reasoner" not in effective_model.lower():
            payload["temperature"] = float(self.config.temperature)

        if include_thinking_params and thinking_extra:
            payload.update(thinking_extra)
        return endpoint, headers, payload

    def _build_request_plan(self) -> List[Tuple[str, Dict[str, str], Dict[str, Any]]]:
        fmt = self.config.api_format.strip().lower()
        url_lower = self.config.base_url.strip().lower()
        model_lower = self.config.model.strip().lower()

        if fmt == "antigravity_gemini":
            is_v1int = "v1internal" in url_lower
            return [
                build_antigravity_gemini_request(self.config, self.messages, include_thinking=True, use_v1internal_envelope=is_v1int),
                build_antigravity_gemini_request(self.config, self.messages, include_thinking=False, use_v1internal_envelope=is_v1int),
            ]

        if fmt == "antigravity_messages":
            return [
                build_antigravity_messages_request(self.config, self.messages, include_thinking=True),
                build_antigravity_messages_request(self.config, self.messages, include_thinking=False),
            ]

        if fmt == "antigravity" or self.config.provider.startswith("antigravity"):
            if "v1internal" in url_lower:
                return [
                    build_antigravity_gemini_request(self.config, self.messages, include_thinking=True, use_v1internal_envelope=True),
                    build_antigravity_gemini_request(self.config, self.messages, include_thinking=False, use_v1internal_envelope=True),
                ]
            if "/v1beta" in url_lower or ":generatecontent" in url_lower or ":streamgeneratecontent" in url_lower:
                return [
                    build_antigravity_gemini_request(self.config, self.messages, include_thinking=True, use_v1internal_envelope=False),
                    build_antigravity_gemini_request(self.config, self.messages, include_thinking=False, use_v1internal_envelope=False),
                ]
            if url_lower.endswith("/chat/completions"):
                return [
                    self._build_openai_request(include_thinking_params=True),
                    self._build_openai_request(include_thinking_params=False),
                ]
            plan = [
                build_antigravity_messages_request(self.config, self.messages, include_thinking=True),
                build_antigravity_messages_request(self.config, self.messages, include_thinking=False),
            ]
            if "gemini" in model_lower:
                plan.append(build_antigravity_gemini_request(self.config, self.messages, include_thinking=True, use_v1internal_envelope=False))
            plan.append(self._build_openai_request(include_thinking_params=True))
            return plan

        plan = [self._build_openai_request(include_thinking_params=True)]
        _, extra_fields = build_thinking_payload_params(self.config)
        if extra_fields:
            plan.append(self._build_openai_request(include_thinking_params=False))
        return plan

    def _emit_throttled_preview(self, reasoning_parts: List[str], content_parts: List[str], force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_emit_ts) < self.STREAM_THROTTLE_SEC:
            return
        self._last_emit_ts = now
        combined_preview = format_reply_with_thinking(
            "".join(reasoning_parts),
            "".join(content_parts),
        )
        if combined_preview:
            self.chunk_received.emit(combined_preview)

    def _process_sse_line(
        self,
        line: str,
        reasoning_parts: List[str],
        content_parts: List[str],
    ) -> bool:
        """处理单行 SSE 数据，返回 True 表示收到结束标记 [DONE] 或 message_stop。"""
        s = line.strip()
        if not s or s.startswith("event:") or s.startswith(":"):
            return False
        if not s.startswith("data:"):
            return False
        data_str = s[5:].strip()
        if not data_str:
            return False
        if data_str == "[DONE]":
            return True
        try:
            pkt = json.loads(data_str)
            if isinstance(pkt, list) and pkt:
                pkt = pkt[0]
            if not isinstance(pkt, dict):
                return False
            if pkt.get("type") == "message_stop":
                return True
            r_piece, c_piece = parse_antigravity_packet(pkt)
            if r_piece:
                reasoning_parts.append(r_piece)
            if c_piece:
                content_parts.append(c_piece)
            if r_piece or c_piece:
                self._emit_throttled_preview(reasoning_parts, content_parts, force=False)
        except Exception:
            pass
        return False

    def _parse_full_json_body(
        self,
        body: str,
        reasoning_parts: List[str],
        content_parts: List[str],
    ) -> None:
        pkt = json.loads(body)
        if isinstance(pkt, list):
            for item in pkt:
                if isinstance(item, dict):
                    rp, cp = parse_antigravity_packet(item)
                    if rp:
                        reasoning_parts.append(rp)
                    if cp:
                        content_parts.append(cp)
        elif isinstance(pkt, dict):
            rp, cp = parse_antigravity_packet(pkt)
            if rp:
                reasoning_parts.append(rp)
            if cp:
                content_parts.append(cp)
        self._emit_throttled_preview(reasoning_parts, content_parts, force=True)

    def _execute_request_qtnetwork(
        self,
        endpoint: str,
        headers: Dict[str, str],
        data_bytes: bytes,
    ) -> Tuple[str, str]:
        """使用宿主内置的 PySide6.QtNetwork 发送请求（按字节行缓冲，防止多字节中文 UTF-8 跨包乱码或截断）。"""
        nam = QtNetwork.QNetworkAccessManager()
        qreq = QtNetwork.QNetworkRequest(QtCore.QUrl(endpoint))
        for k, v in headers.items():
            qreq.setRawHeader(k.encode("utf-8"), str(v).encode("utf-8"))

        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        raw_buffer = bytearray()
        line_byte_buf = bytearray()

        loop = QtCore.QEventLoop()
        reply = nam.post(qreq, QtCore.QByteArray(data_bytes))

        def _on_ready_read() -> None:
            if self._abort:
                reply.abort()
                loop.quit()
                return
            chunk_bytes = bytes(reply.readAll())
            if not chunk_bytes:
                return
            raw_buffer.extend(chunk_bytes)
            ct = str(reply.header(QtNetwork.QNetworkRequest.KnownHeaders.ContentTypeHeader) or "").lower()
            if self.config.stream and ("text/event-stream" in ct or "alt=sse" in endpoint.lower()):
                line_byte_buf.extend(chunk_bytes)
                while b"\n" in line_byte_buf:
                    nl_pos = line_byte_buf.find(b"\n")
                    one_line_bytes = bytes(line_byte_buf[:nl_pos])
                    del line_byte_buf[: nl_pos + 1]
                    one_line = one_line_bytes.decode("utf-8", errors="replace")
                    if self._process_sse_line(one_line, reasoning_parts, content_parts):
                        break

        reply.readyRead.connect(_on_ready_read)
        reply.finished.connect(loop.quit)
        QtCore.QTimer.singleShot(300 * 1000, loop.quit)
        loop.exec()

        rem = bytes(reply.readAll())
        if rem:
            raw_buffer.extend(rem)
            line_byte_buf.extend(rem)

        status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        err_code = reply.error()
        err_str = reply.errorString()
        reply.deleteLater()

        body_str = raw_buffer.decode("utf-8", errors="replace")
        if status and int(status) >= 400:
            raise _HttpStatusError(int(status), err_str or f"HTTP {status}", body_str[:400])
        if err_code != QtNetwork.QNetworkReply.NetworkError.NoError and not self._abort:
            if not reasoning_parts and not content_parts:
                raise RuntimeError(f"{err_str} ({body_str[:240]})")

        rem_text = line_byte_buf.decode("utf-8", errors="replace")
        if rem_text.strip() and (reasoning_parts or content_parts or "data:" in rem_text):
            for rest_line in rem_text.splitlines():
                self._process_sse_line(rest_line, reasoning_parts, content_parts)
        elif not reasoning_parts and not content_parts and body_str.strip():
            self._parse_full_json_body(body_str, reasoning_parts, content_parts)

        return "".join(reasoning_parts), "".join(content_parts)

    def _execute_request(
        self,
        endpoint: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Tuple[str, str]:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        if not HAS_URLLIB or urllib is None:
            if QtNetwork is not None:
                return self._execute_request_qtnetwork(endpoint, headers, data_bytes)
            raise RuntimeError("当前运行环境缺少 urllib 与 QtNetwork 网络模块。")

        req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")
        ssl_ctx = ssl.create_default_context() if ssl is not None else None

        reasoning_parts: List[str] = []
        content_parts: List[str] = []

        try:
            with urllib.request.urlopen(req, timeout=300, context=ssl_ctx) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if self.config.stream and ("text/event-stream" in content_type or "alt=sse" in endpoint.lower()):
                    for raw_line in resp:
                        if self._abort:
                            break
                        line = raw_line.decode("utf-8", errors="replace")
                        if self._process_sse_line(line, reasoning_parts, content_parts):
                            break
                else:
                    body = resp.read().decode("utf-8", errors="replace")
                    self._parse_full_json_body(body, reasoning_parts, content_parts)
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            raise _HttpStatusError(int(he.code), str(he.reason), err_body)

        return "".join(reasoning_parts), "".join(content_parts)

    def run(self) -> None:
        if not self.config.is_configured():
            self.failed_error.emit("尚未配置 API Key，请先点击右上角「⚙️ API设置」完成配置。")
            return

        request_plan = self._build_request_plan()
        last_error_msg = ""

        for idx, (endpoint, headers, payload) in enumerate(request_plan):
            try:
                r_text, c_text = self._execute_request(endpoint, headers, payload)
                # 若模型把正式回答也放在了 reasoning_text 或中断前只有思考文本，确保正常返回展示
                final_combined = format_reply_with_thinking(r_text, c_text)
                self.finished_reply.emit(final_combined)
                return
            except _HttpStatusError as he:
                last_error_msg = f"HTTP {he.code} 请求失败 ({endpoint})：\n{he.body or he.message}"
                if idx + 1 < len(request_plan) and he.code in (400, 404, 415, 422, 500, 501):
                    continue
                self.failed_error.emit(last_error_msg)
                return
            except Exception as exc:
                if HAS_URLLIB and urllib is not None and isinstance(exc, urllib.error.URLError):
                    self.failed_error.emit(f"网络连接失败 ({endpoint})：{exc.reason}")
                    return
                last_error_msg = f"调用 AI 接口异常 ({endpoint})：{exc}"
                if idx + 1 < len(request_plan):
                    continue
                self.failed_error.emit(last_error_msg)
                return


# ============================================================================
# 4. 多 API 账号管理与多模型动态发现对话框 (ApiConfigDialog)
# ============================================================================

class ApiConfigDialog(QtWidgets.QDialog):
    """
    支持保存多个 API 配置、输入 API Key 后自动/手动拉取该 API 提供的多种模型，
    并可为每个 API 维护多个可选模型（供对话页面直接像 Antigravity 一样自由切换）。
    """

    config_saved = QtCore.Signal()

    def __init__(
        self,
        config: AIConfig,
        logger=None,
        first_launch: bool = False,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.logger = logger
        self._test_worker: Optional[LLMWorker] = None
        self._discover_worker: Optional[ModelDiscoveryWorker] = None
        self._loading_profile_ui: bool = False

        self._working_profiles: List[APIProfile] = [
            APIProfile.from_dict(p.to_dict()) for p in self.config.profiles
        ]
        if not self._working_profiles:
            self._working_profiles = [APIProfile()]
        self._current_idx: int = 0
        for i, p in enumerate(self._working_profiles):
            if p.id == self.config.active_profile_id:
                self._current_idx = i
                break

        self.setWindowTitle(
            "AI 识别 - 多 API 账号与多模型管理" if not first_launch else "欢迎使用 AI 识别 — 首次启动请配置 API Key 与模型"
        )
        self.setMinimumWidth(500)
        self.resize(600, 520)
        self.setModal(True)

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(14, 14, 14, 14)

        banner = QtWidgets.QLabel(
            "<b>🤖 多 API 与多模型管理（支持 OpenAI 兼容格式 & Antigravity 兼容格式）</b><br/>"
            "<span style='color:#666;'>支持保存多个 API 账号，每个 API 可关联多种模型；输入 Key 后可点「🔄 拉取模型」。"
            "保存后可在聊天页面上方直接下拉切换任意模型进行提问。</span>"
        )
        banner.setWordWrap(True)
        root.addWidget(banner)

        # 顶部：已保存的 API 账号切换与增删工具条
        prof_bar = QtWidgets.QHBoxLayout()
        prof_bar.addWidget(QtWidgets.QLabel("<b>📚 已保存 API：</b>"))
        self.profile_selector_combo = QtWidgets.QComboBox()
        prof_bar.addWidget(self.profile_selector_combo, 1)

        self.add_prof_btn = QtWidgets.QPushButton("➕ 新增 API")
        self.add_prof_btn.clicked.connect(self._on_add_profile)
        prof_bar.addWidget(self.add_prof_btn)

        self.del_prof_btn = QtWidgets.QPushButton("🗑️ 删除")
        self.del_prof_btn.clicked.connect(self._on_delete_profile)
        prof_bar.addWidget(self.del_prof_btn)
        root.addLayout(prof_bar)

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        self.profile_name_edit = QtWidgets.QLineEdit()
        self.profile_name_edit.setPlaceholderText("例如：Antigravity 本地网关 / DeepSeek 官方")
        if hasattr(self.profile_name_edit, "textChanged"):
            self.profile_name_edit.textChanged.connect(self._on_profile_name_edited)
        form.addRow("API 备注名：", self.profile_name_edit)

        self.provider_combo = QtWidgets.QComboBox()
        for preset in PROVIDER_PRESETS:
            self.provider_combo.addItem(preset["name"], preset["id"])
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("服务商预设：", self.provider_combo)

        self.format_combo = QtWidgets.QComboBox()
        for fmt_item in API_FORMATS:
            self.format_combo.addItem(fmt_item["name"], fmt_item["id"])
        form.addRow("协议格式：", self.format_combo)

        self.base_url_edit = QtWidgets.QLineEdit()
        self.base_url_edit.setPlaceholderText("例如 http://127.0.0.1:8045/v1 或 https://api.deepseek.com/v1")
        form.addRow("接口地址：", self.base_url_edit)

        key_row = QtWidgets.QHBoxLayout()
        self.api_key_edit = QtWidgets.QLineEdit()
        self.api_key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("请输入 API Key")
        self.toggle_key_btn = QtWidgets.QPushButton("显示")
        self.toggle_key_btn.setCheckable(True)
        self.toggle_key_btn.setFixedWidth(48)
        self.toggle_key_btn.toggled.connect(self._on_toggle_key_echo)

        self.fetch_models_btn = QtWidgets.QPushButton("🔄 拉取可用模型")
        self.fetch_models_btn.setToolTip("根据当前 Base URL 和 API Key 自动向接口请求该 API 提供的全部模型列表")
        self.fetch_models_btn.clicked.connect(self._on_fetch_models_clicked)
        if hasattr(self.api_key_edit, "editingFinished"):
            self.api_key_edit.editingFinished.connect(self._on_api_key_editing_finished)

        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.toggle_key_btn)
        key_row.addWidget(self.fetch_models_btn)
        form.addRow("API Key：", key_row)

        model_sel_row = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        if hasattr(self.model_combo, "setEditable"):
            self.model_combo.setEditable(True)
        self.model_combo.currentIndexChanged.connect(self._on_model_combo_changed)
        model_sel_row.addWidget(self.model_combo, 1)

        self.model_edit = QtWidgets.QLineEdit()
        self.model_edit.setPlaceholderText("或手填新模型名点『添加』")
        self.add_model_btn = QtWidgets.QPushButton("➕ 添加")
        self.add_model_btn.clicked.connect(self._on_add_custom_model)
        model_sel_row.addWidget(self.model_edit, 1)
        model_sel_row.addWidget(self.add_model_btn)
        form.addRow("默认模型：", model_sel_row)

        self.models_pool_edit = QtWidgets.QPlainTextEdit()
        self.models_pool_edit.setPlaceholderText("该 API 的可选模型库（逗号或换行分隔，均会出现在聊天页顶部的模型下拉框中）")
        self.models_pool_edit.setMaximumHeight(60)
        form.addRow("可选模型库：", self.models_pool_edit)

        self.thinking_combo = QtWidgets.QComboBox()
        for t_item in THINKING_LEVELS:
            self.thinking_combo.addItem(f"🧠 {t_item['name']}", t_item["id"])
        t_idx = self.thinking_combo.findData(self.config.thinking_level)
        if t_idx >= 0:
            self.thinking_combo.setCurrentIndex(t_idx)
        form.addRow("思考等级：", self.thinking_combo)

        opt_row = QtWidgets.QHBoxLayout()
        self.temp_spin = QtWidgets.QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 1.5)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(1)
        self.temp_spin.setValue(self.config.temperature)
        self.stream_chk = QtWidgets.QCheckBox("启用流式输出 (已做防卡顿节流优化)")
        self.stream_chk.setChecked(self.config.stream)
        opt_row.addWidget(QtWidgets.QLabel("温度:"))
        opt_row.addWidget(self.temp_spin)
        opt_row.addSpacing(12)
        opt_row.addWidget(self.stream_chk)
        opt_row.addStretch(1)
        form.addRow("生成参数：", opt_row)

        root.addLayout(form)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        btn_row = QtWidgets.QHBoxLayout()
        self.test_btn = QtWidgets.QPushButton("🔌 测试当前 API 连接")
        self.test_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self.test_btn)
        btn_row.addStretch(1)

        self.cancel_btn = QtWidgets.QPushButton("稍后配置" if first_launch else "取消")
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QtWidgets.QPushButton("💾 保存并启用")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)
        root.addLayout(btn_row)

        self._refresh_profile_selector_combo()
        self.profile_selector_combo.currentIndexChanged.connect(self._on_profile_selector_changed)
        self._load_profile_into_form(self._current_idx)

    def _parse_models_pool_text(self) -> List[str]:
        raw = self.models_pool_edit.toPlainText()
        parts = re.split(r"[\n,;，；]+", raw)
        out: List[str] = []
        for p in parts:
            s = p.strip()
            if s and s not in out:
                out.append(s)
        return out

    def _set_models_pool_ui(self, models: List[str], selected_model: str) -> None:
        cleaned: List[str] = []
        for m in models:
            ms = str(m).strip()
            if ms and ms not in cleaned:
                cleaned.append(ms)
        if selected_model and selected_model not in cleaned:
            cleaned.insert(0, selected_model)
        if not cleaned:
            cleaned = ["deepseek-chat"]
        if not selected_model:
            selected_model = cleaned[0]

        self.models_pool_edit.setPlainText(", ".join(cleaned))
        if hasattr(self.model_combo, "clear"):
            self.model_combo.clear()
        else:
            self.model_combo._items = []
        for m in cleaned:
            self.model_combo.addItem(m, m)
        idx = self.model_combo.findData(selected_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_edit.setText(selected_model)

    def _save_form_to_current_profile(self) -> None:
        if self._loading_profile_ui or not (0 <= self._current_idx < len(self._working_profiles)):
            return
        prof = self._working_profiles[self._current_idx]
        prof.name = self.profile_name_edit.text().strip() or prof.name
        prof.provider = str(self.provider_combo.currentData() or "custom")
        prof.api_format = str(self.format_combo.currentData() or "openai")
        prof.base_url = self.base_url_edit.text().strip()
        prof.api_key = self.api_key_edit.text().strip()

        pool = self._parse_models_pool_text()
        cur_m = self.model_edit.text().strip() or str(self.model_combo.currentData() or "")
        if cur_m and cur_m not in pool:
            pool.insert(0, cur_m)
        prof.models = pool
        prof.default_model = cur_m or (pool[0] if pool else "deepseek-chat")
        prof.ensure_valid_models()

    def _refresh_profile_selector_combo(self) -> None:
        self._loading_profile_ui = True
        try:
            if hasattr(self.profile_selector_combo, "clear"):
                self.profile_selector_combo.clear()
            else:
                self.profile_selector_combo._items = []
            for i, p in enumerate(self._working_profiles):
                key_tag = "✅已填Key" if p.is_configured() else "⚠️待填Key"
                fmt_short = get_api_format_info(p.api_format)["short"]
                label = f"{i + 1}. {p.name} ({fmt_short} · {len(p.models)}个模型 · {key_tag})"
                self.profile_selector_combo.addItem(label, p.id)
            if 0 <= self._current_idx < len(self._working_profiles):
                self.profile_selector_combo.setCurrentIndex(self._current_idx)
            self.del_prof_btn.setEnabled(len(self._working_profiles) > 1)
        finally:
            self._loading_profile_ui = False

    def _load_profile_into_form(self, index: int) -> None:
        if not (0 <= index < len(self._working_profiles)):
            return
        self._loading_profile_ui = True
        try:
            self._current_idx = index
            prof = self._working_profiles[index]
            prof.ensure_valid_models()

            self.profile_name_edit.setText(prof.name)
            p_idx = self.provider_combo.findData(prof.provider)
            if p_idx >= 0:
                self.provider_combo.setCurrentIndex(p_idx)
            f_idx = self.format_combo.findData(prof.api_format)
            if f_idx >= 0:
                self.format_combo.setCurrentIndex(f_idx)
            self.base_url_edit.setText(prof.base_url)
            self.api_key_edit.setText(prof.api_key)
            preset = get_provider_preset(prof.provider)
            self.api_key_edit.setPlaceholderText(preset.get("placeholder_key", "sk-..."))
            self._set_models_pool_ui(prof.models, prof.default_model)
        finally:
            self._loading_profile_ui = False

    def _on_profile_selector_changed(self, new_index: int) -> None:
        if self._loading_profile_ui or new_index < 0:
            return
        self._save_form_to_current_profile()
        self._load_profile_into_form(new_index)
        self._refresh_profile_selector_combo()

    def _on_profile_name_edited(self, _text: str) -> None:
        if self._loading_profile_ui:
            return
        if 0 <= self._current_idx < len(self._working_profiles):
            self._working_profiles[self._current_idx].name = self.profile_name_edit.text().strip() or "未命名 API"

    def _on_add_profile(self) -> None:
        self._save_form_to_current_profile()
        preset = PROVIDER_PRESETS[0]
        new_prof = APIProfile(
            id=_gen_short_id("api"),
            name=f"API #{len(self._working_profiles) + 1} ({preset['id']})",
            provider=preset["id"],
            api_format=preset["api_format"],
            base_url=preset["base_url"],
            api_key="",
            models=list(preset["models"]),
            default_model=preset["model"],
        )
        self._working_profiles.append(new_prof)
        self._current_idx = len(self._working_profiles) - 1
        self._refresh_profile_selector_combo()
        self._load_profile_into_form(self._current_idx)
        self.status_label.setText(
            "<span style='color:#0275d8;'>✨ 已新增一个 API 配置槽位，请选择服务商预设、填写 API Key 并拉取或选择模型。</span>"
        )

    def _on_delete_profile(self) -> None:
        if len(self._working_profiles) <= 1:
            return
        removed = self._working_profiles.pop(self._current_idx)
        self._current_idx = max(0, min(self._current_idx, len(self._working_profiles) - 1))
        self._refresh_profile_selector_combo()
        self._load_profile_into_form(self._current_idx)
        self.status_label.setText(f"<span style='color:#e67e22;'>已移除 API 配置「{_html_escape(removed.name)}」。</span>")

    def _on_toggle_key_echo(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if checked else QtWidgets.QLineEdit.EchoMode.Password
        )
        self.toggle_key_btn.setText("隐藏" if checked else "显示")

    def _on_provider_changed(self, index: int) -> None:
        if self._loading_profile_ui:
            return
        pid = str(self.provider_combo.itemData(index) or "custom")
        preset = get_provider_preset(pid)
        if pid != "custom":
            self.base_url_edit.setText(preset["base_url"])
            preset_fmt = preset.get("api_format", "openai")
            fidx = self.format_combo.findData(preset_fmt)
            if fidx >= 0:
                self.format_combo.setCurrentIndex(fidx)
            preset_models = list(preset.get("models") or [preset["model"]])
            self._set_models_pool_ui(preset_models, preset["model"])
            cur_name = self.profile_name_edit.text().strip()
            if not cur_name or any(cur_name.startswith(p["name"][:6]) or cur_name.startswith("API") for p in PROVIDER_PRESETS):
                self.profile_name_edit.setText(preset["name"].split(" (")[0])
        self.api_key_edit.setPlaceholderText(preset.get("placeholder_key", "sk-..."))

    def _on_model_combo_changed(self, index: int) -> None:
        if self._loading_profile_ui or index < 0:
            return
        m_val = str(self.model_combo.itemData(index) or "")
        if m_val:
            self.model_edit.setText(m_val)

    def _on_add_custom_model(self) -> None:
        new_m = self.model_edit.text().strip()
        if not new_m:
            return
        pool = self._parse_models_pool_text()
        if new_m not in pool:
            pool.insert(0, new_m)
        self._set_models_pool_ui(pool, new_m)
        self.status_label.setText(f"<span style='color:#28a745;'>✅ 已将模型 <b>{_html_escape(new_m)}</b> 添加到当前 API 的可选模型库。</span>")

    def _on_api_key_editing_finished(self) -> None:
        if self._loading_profile_ui:
            return
        key_val = self.api_key_edit.text().strip()
        url_val = self.base_url_edit.text().strip()
        if key_val and url_val and (self._discover_worker is None or not self._discover_worker.isRunning()):
            self._on_fetch_models_clicked()

    def _on_fetch_models_clicked(self) -> None:
        base_url = self.base_url_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        api_fmt = str(self.format_combo.currentData() or "openai")
        prov = str(self.provider_combo.currentData() or "custom")

        if not base_url:
            self.status_label.setText("<span style='color:#d9534f;'>❌ 请先填写接口地址 (Base URL)。</span>")
            return
        if not api_key and prov != "ollama":
            self.status_label.setText("<span style='color:#d9534f;'>❌ 请先输入 API Key，再拉取该 API 支持的模型列表。</span>")
            return

        self.fetch_models_btn.setEnabled(False)
        self.status_label.setText("<span style='color:#0275d8;'>⏳ 正在向接口请求可用模型列表 (/v1/models · /v1beta/models)...</span>")

        self._discover_worker = ModelDiscoveryWorker(base_url, api_key, api_fmt, prov, self)
        self._discover_worker.models_fetched.connect(self._on_models_fetched_ok)
        self._discover_worker.fetch_failed.connect(self._on_models_fetched_fail)
        self._discover_worker.start()

    def _on_models_fetched_ok(self, fetched_models: List[str]) -> None:
        self.fetch_models_btn.setEnabled(True)
        if not fetched_models:
            return
        cur_selected = self.model_edit.text().strip()
        chosen = cur_selected if cur_selected in fetched_models else fetched_models[0]
        self._set_models_pool_ui(fetched_models, chosen)
        preview = ", ".join(fetched_models[:6]) + (f" 等共 {len(fetched_models)} 个模型" if len(fetched_models) > 6 else "")
        self.status_label.setText(
            f"<span style='color:#28a745;'>✅ 成功从接口拉取到 {len(fetched_models)} 个可用模型：{_html_escape(preview)}</span>"
        )

    def _on_models_fetched_fail(self, msg: str) -> None:
        self.fetch_models_btn.setEnabled(True)
        self.status_label.setText(f"<span style='color:#e67e22;'>ℹ️ {_html_escape(msg)}</span>")

    def _collect_temp_config(self) -> AIConfig:
        self._save_form_to_current_profile()
        cur_prof = self._working_profiles[self._current_idx]
        return AIConfig(
            thinking_level="off",
            temperature=float(self.temp_spin.value()),
            max_tokens=self.config.max_tokens,
            stream=False,
            always_on_top=self.config.always_on_top,
            first_prompt_shown=True,
            profiles=[APIProfile.from_dict(cur_prof.to_dict())],
            active_profile_id=cur_prof.id,
            active_model=cur_prof.default_model,
        )

    def _on_test_connection(self) -> None:
        tmp_cfg = self._collect_temp_config()
        if not tmp_cfg.is_configured():
            self.status_label.setText("<span style='color:#d9534f;'>❌ 请先填写完整的 Base URL、Model 和 API Key。</span>")
            return

        fmt_info = get_api_format_info(tmp_cfg.api_format)
        self.test_btn.setEnabled(False)
        self.status_label.setText(
            f"<span style='color:#0275d8;'>⏳ 正在通过 [{_html_escape(fmt_info['short'])}] 测试模型 <b>{_html_escape(tmp_cfg.model)}</b>...</span>"
        )
        test_msgs = [{"role": "user", "content": "请回复“连接成功”四个字。"}]
        self._test_worker = LLMWorker(tmp_cfg, test_msgs, self)
        self._test_worker.finished_reply.connect(self._on_test_ok)
        self._test_worker.failed_error.connect(self._on_test_fail)
        self._test_worker.start()

    def _on_test_ok(self, text: str) -> None:
        self.test_btn.setEnabled(True)
        clean_preview = re.sub(r"<think>.*?</think>", "", text or "OK", flags=re.DOTALL).strip()[:60]
        self.status_label.setText(f"<span style='color:#28a745;'>✅ 连接测试成功！模型回复：{_html_escape(clean_preview)}</span>")

    def _on_test_fail(self, err: str) -> None:
        self.test_btn.setEnabled(True)
        self.status_label.setText(f"<span style='color:#d9534f;'>❌ {_html_escape(err[:240])}</span>")

    def _on_save(self) -> None:
        self._save_form_to_current_profile()
        any_configured = any(p.is_configured() for p in self._working_profiles)
        if not any_configured:
            QtWidgets.QMessageBox.warning(
                self,
                "配置不完整",
                "请至少为一个 API 填写有效的 API Key、接口地址 (Base URL) 和模型名称后再保存。",
            )
            return

        cur_prof = self._working_profiles[self._current_idx]
        if not cur_prof.is_configured():
            for p in self._working_profiles:
                if p.is_configured():
                    cur_prof = p
                    break

        self.config.profiles = [APIProfile.from_dict(p.to_dict()) for p in self._working_profiles]
        self.config.active_profile_id = cur_prof.id
        self.config.active_model = cur_prof.default_model
        self.config.thinking_level = str(self.thinking_combo.currentData() or "medium")
        self.config.temperature = float(self.temp_spin.value())
        self.config.stream = bool(self.stream_chk.isChecked())
        self.config.first_prompt_shown = True

        self.config.save(self.logger)
        self.config_saved.emit()
        self.accept()


# ============================================================================
# 5. 富文本 Markdown 与交互式链接渲染器 (支持思考折叠/展开 + 极速流式预览)
# ============================================================================

def _extract_think_and_main(text: str) -> Tuple[str, str, bool]:
    """
    从回复文本中分离思考内容与正式回答正文。
    返回: (think_text, main_text, is_think_closed)
    兼容 `<think>...</think>` 与 `<thinking>...</thinking>`。
    """
    think_chunks: List[str] = []
    remaining = text

    # 匹配已闭合的 <think>...</think> 或 <thinking>...</thinking>
    pattern = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>\s*", re.DOTALL | re.IGNORECASE)
    while True:
        m = pattern.search(remaining)
        if not m:
            break
        t_part = m.group(1).strip()
        if t_part:
            think_chunks.append(t_part)
        remaining = remaining[: m.start()] + remaining[m.end() :]

    # 检查是否还有未闭合的 <think> 或 <thinking>（流式思考进行中）
    open_match = re.search(r"<think(?:ing)?>", remaining, re.IGNORECASE)
    if open_match:
        t_unclosed = remaining[open_match.end() :].strip()
        if t_unclosed:
            think_chunks.append(t_unclosed)
        remaining = remaining[: open_match.start()].strip()
        return "\n\n".join(think_chunks), remaining, False

    return "\n\n".join(think_chunks), remaining.strip(), True


def render_streaming_preview_fast(text: str) -> str:
    """
    轻量级极速流式渲染器（在流式打字期间调用，不做复杂的全局通道/时间正则扫描，保证 0 卡顿）：
    - 思考进行中时仅展示最近 10 行思考内容与字数统计；
    - 正式正文开始输出后自动收起思考框，突出显示正式诊断回答。
    """
    think_raw, main_raw, is_closed = _extract_think_and_main(text)
    thinking_html = ""

    if think_raw:
        think_len = len(think_raw)
        if not is_closed and not main_raw:
            # 思考还在进行中，只截取最后 10 行展示，避免长文本重排卡顿
            t_lines = think_raw.splitlines()
            tail_lines = t_lines[-10:] if len(t_lines) > 10 else t_lines
            prefix_hint = f"... (前略 {len(t_lines) - 10} 行)<br/>" if len(t_lines) > 10 else ""
            tail_escaped = prefix_hint + _html_escape("\n".join(tail_lines)).replace("\n", "<br/>")
            thinking_html = (
                f"<div style='margin-bottom:8px;padding:7px 10px;border-left:3px solid #8e44ad;"
                f"background:rgba(142,68,173,0.08);border-radius:4px;color:#777;font-size:12px;'>"
                f"<div style='font-weight:bold;color:#8e44ad;margin-bottom:3px;'>🧠 正在思考推导中... (已生成 {think_len} 字)</div>"
                f"<div style='line-height:1.4;'>{tail_escaped}</div></div>"
            )
        else:
            # 思考已结束，开始输出正式正文：折叠思考框为单行摘要
            thinking_html = (
                f"<div style='margin-bottom:8px;padding:5px 10px;border-left:3px solid #8e44ad;"
                f"background:rgba(142,68,173,0.08);border-radius:4px;color:#8e44ad;font-size:12px;'>"
                f"🧠 <b>思考推导已完成</b>（共 {think_len} 字，生成完毕后可点击展开查看）</div>"
            )

    if not main_raw:
        return thinking_html

    main_escaped = _html_escape(main_raw).replace("\n", "<br/>")
    return thinking_html + f"<div style='font-size:13px;line-height:1.55;'>{main_escaped}</div>"


def render_interactive_html(
    text: str,
    known_channel_prefixes: Sequence[str] = (),
    msg_index: Optional[int] = None,
    think_expanded: bool = False,
) -> str:
    """
    将已完成的消息文本转换为带交互链接的 HTML：
    - <think>...</think> -> 若同时存在正式回答正文，默认折叠为可点击展开的「🧠 思考推导过程」摘要条；
      若用户点击了展开（或没有正式正文），则展示完整思考内容。
    - 80.0s-95.0s        -> <a href="zoom://80.0,95.0">🔍 80.0s-95.0s</a>
    - 125.4s             -> <a href="seek://125.4">⏱ 125.4s</a>
    - VIBE.VibeZ         -> <a href="channel://VIBE.VibeZ">📈 VIBE.VibeZ</a>
    """
    think_raw, main_text, _ = _extract_think_and_main(text)
    thinking_html = ""

    if think_raw:
        think_len = len(think_raw)
        # 如果没有正式正文（例如用户中途停止），则强制展开显示思考内容，避免页面空白
        show_full_think = think_expanded or (not main_text.strip())
        toggle_link = ""
        if msg_index is not None and main_text.strip():
            action_text = "🔼 点击收起思考过程" if show_full_think else "🔽 点击展开思考过程"
            toggle_link = (
                f" &nbsp;<a href='think://{msg_index}' "
                f"style='color:#8e44ad;text-decoration:underline;font-weight:normal;'>[{action_text}]</a>"
            )

        if show_full_think:
            think_escaped = _html_escape(think_raw).replace("\n", "<br/>")
            thinking_html = (
                f"<div style='margin-bottom:8px;padding:8px 10px;border-left:3px solid #8e44ad;"
                f"background:rgba(142,68,173,0.08);border-radius:4px;color:#666;font-size:12px;'>"
                f"<div style='font-weight:bold;color:#8e44ad;margin-bottom:4px;'>🧠 思考推导过程 (共 {think_len} 字){toggle_link}</div>"
                f"<div style='line-height:1.45;'>{think_escaped}</div></div>"
            )
        else:
            preview_line = _html_escape(think_raw.splitlines()[0][:65]) + ("..." if len(think_raw) > 65 else "")
            thinking_html = (
                f"<div style='margin-bottom:8px;padding:6px 10px;border-left:3px solid #8e44ad;"
                f"background:rgba(142,68,173,0.08);border-radius:4px;color:#666;font-size:12px;'>"
                f"<span style='font-weight:bold;color:#8e44ad;'>🧠 思考推导过程 (共 {think_len} 字)</span>："
                f"<span style='color:#888;'>{preview_line}</span>{toggle_link}</div>"
            )

    if not main_text.strip():
        return thinking_html

    escaped = _html_escape(main_text)

    code_blocks: List[str] = []

    def _save_code_block(m: re.Match) -> str:
        idx = len(code_blocks)
        code_content = m.group(1)
        code_blocks.append(
            f"<pre style='background:rgba(128,128,128,0.14);padding:8px;border-radius:6px;"
            f"font-family:Consolas,monospace;font-size:12px;'>{code_content}</pre>"
        )
        return f"@@CODE_BLOCK_{idx}@@"

    escaped = re.sub(r"```(?:\w+)?\n(.*?)```", _save_code_block, escaped, flags=re.DOTALL)

    escaped = re.sub(
        r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?)\s*(?:s|秒)?\s*(?:–|—|-|~|至)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)(?![A-Za-z0-9_])",
        lambda m: (
            f"<a href='zoom://{m.group(1)},{m.group(2)}' "
            f"style='color:#e67e22;text-decoration:none;font-weight:bold;' "
            f"title='点击在曲线中放大 {m.group(1)}s – {m.group(2)}s'>🔍{m.group(1)}s–{m.group(2)}s</a>"
        ),
        escaped,
    )

    escaped = re.sub(
        r"(?<![A-Za-z0-9_./:-])(?:t\s*=\s*)?(\d+(?:\.\d+)?)\s*(?:s|秒)(?![A-Za-z0-9_])",
        lambda m: (
            f"<a href='seek://{m.group(1)}' "
            f"style='color:#2f6fed;text-decoration:none;font-weight:bold;' "
            f"title='点击将游标/地图定位到 {m.group(1)}s'>⏱{m.group(1)}s</a>"
        ),
        escaped,
    )

    default_prefixes = {
        "BAT", "VIBE", "ATT", "RATE", "CTUN", "POS", "GPS", "GPA",
        "RCOU", "RCIN", "POWR", "MCU", "XKF1", "XKF2", "XKF3", "XKF4",
        "NKF1", "NKF4", "MAG", "BARO", "PSCD", "PIDR", "PIDP", "PIDY", "SYN",
    }
    all_pfx = default_prefixes | {p.upper() for p in known_channel_prefixes}

    def _replace_channel(m: re.Match) -> str:
        full_key = m.group(0)
        t_part = m.group(1).upper()
        if t_part in all_pfx:
            return (
                f"<a href='channel://{full_key}' "
                f"style='color:#16a085;text-decoration:none;font-weight:bold;font-family:Consolas,monospace;' "
                f"title='点击将通道 {full_key} 添加到曲线视图'>📈{full_key}</a>"
            )
        return full_key

    escaped = re.sub(r"\b([A-Z][A-Z0-9]{1,5})(?:#\d+)?\.([A-Za-z0-9_]{1,16})\b", _replace_channel, escaped)

    lines = escaped.splitlines()
    out_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#### "):
            out_lines.append(f"<div style='font-weight:bold;font-size:13px;margin-top:8px;margin-bottom:2px;'>{stripped[5:]}</div>")
        elif stripped.startswith("### "):
            out_lines.append(f"<div style='font-weight:bold;font-size:14px;margin-top:10px;margin-bottom:3px;color:#2f6fed;'>{stripped[4:]}</div>")
        elif stripped.startswith("## "):
            out_lines.append(f"<div style='font-weight:bold;font-size:15px;margin-top:10px;margin-bottom:4px;color:#2f6fed;'>{stripped[3:]}</div>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            item_body = stripped[2:]
            out_lines.append(f"<div style='margin-left:12px;margin-bottom:3px;'>• {item_body}</div>")
        else:
            out_lines.append(line + "<br/>")

    joined = "\n".join(out_lines)
    joined = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", joined)
    joined = re.sub(
        r"`([^`]+)`",
        r"<code style='background:rgba(128,128,128,0.16);padding:1px 4px;border-radius:3px;font-family:Consolas,monospace;'>\1</code>",
        joined,
    )

    for idx, block_html in enumerate(code_blocks):
        joined = joined.replace(f"@@CODE_BLOCK_{idx}@@", block_html)

    return thinking_html + joined


# ============================================================================
# 6. 核心共享会话控制器 (AIChatSession) —— 同步顶部悬浮窗、标签页、多 API 模型选择与思考等级
# ============================================================================

class AIChatSession(QtCore.QObject):
    """
    管理对话历史、多 API 模型切换、思考等级切换、LLM 工作线程以及双端界面同步。
    """

    history_changed = QtCore.Signal()
    streaming_updated = QtCore.Signal(str)
    busy_changed = QtCore.Signal(bool)
    status_updated = QtCore.Signal(str)
    thinking_level_changed = QtCore.Signal(str)
    model_selection_changed = QtCore.Signal(str, str)

    def __init__(self, ctx, config: AIConfig, extractor: LogKnowledgeExtractor) -> None:
        super().__init__()
        self.ctx = ctx
        self.config = config
        self.extractor = extractor
        self.chat_log: List[Dict[str, str]] = []
        self._worker: Optional[LLMWorker] = None
        self._streaming_buffer: str = ""
        self._pending_second_pass: bool = False
        self._last_user_question: str = ""

    @property
    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def set_thinking_level(self, level_id: str) -> None:
        lvl = level_id.strip().lower()
        if lvl not in VALID_THINKING_IDS:
            return
        if self.config.thinking_level != lvl:
            self.config.thinking_level = lvl
            self.config.save(self.ctx.log)
            lvl_info = get_thinking_level_info(lvl)
            self.thinking_level_changed.emit(lvl)
            self.status_updated.emit(f"已切换模型思考等级为：🧠 {lvl_info['name']}")

    def set_active_model(self, profile_id: str, model_name: str) -> None:
        if self.config.set_active_selection(profile_id, model_name):
            self.config.save(self.ctx.log)
            prof = self.config.get_active_profile()
            fmt_short = get_api_format_info(prof.api_format)["short"]
            self.model_selection_changed.emit(prof.id, self.config.model)
            self.status_updated.emit(
                f"已切换提问模型为：🤖 [{prof.name}] {self.config.model} ({fmt_short})"
            )

    def add_system_welcome(self) -> None:
        if self.chat_log:
            return
        lvl_info = get_thinking_level_info(self.config.thinking_level)
        prof = self.config.get_active_profile()
        fmt_info = get_api_format_info(prof.api_format)
        if self.ctx.store.available:
            name = self.ctx.store.name()
            stats = self.ctx.store.stats() or {}
            dur = float(stats.get("duration") or 0.0)
            fw = stats.get("firmware") or "ArduPilot"
            n_alerts = len(self.extractor.latest_alerts)
            msg = (
                f"已连接当前日志 **{name}**（固件 `{fw}`，时长 `{dur:.1f}s`，命中告警 `{n_alerts}` 条；"
                f"当前模型：**[{prof.name}] {self.config.model}** ｜ 思考等级：**{lvl_info['short']}**）。\n"
                f"您可以通过上方工具栏直接切换 **不同 API 下的不同 AI 模型** 与 **思考等级（关闭/低/中/高）** 进行提问！例如：\n"
                f"- 「帮我全面体检这份日志，有没有安全隐患？」\n"
                f"- 「为什么飞行中途触发了告警？分析一下原因和发生时刻」\n"
                f"- 「检查一下震动 VIBE.VibeZ 和电池电压 BAT.Volt 是否正常」\n"
                f"- 点击我回复中的蓝色时间（如 `10.0s`）或绿色通道名（如 `BAT.Volt`）可直接联动软件定位与看图。"
            )
        else:
            msg = (
                f"欢迎使用 **AI 智能日志识别助手**！（当前模型：**[{prof.name}] {self.config.model}** · {fmt_info['short']} ｜ 思考等级：**{lvl_info['short']}**）\n"
                "- 打开任意 `.BIN` 飞控日志后，我会自动读取全量日志统计、飞行模式、事件、告警及各子系统时序数据。\n"
                "- 点击右上角 **「⚙️ API设置」** 可添加并保存**多个 API 账号**、自动拉取每个 API 支持的**多种模型**；在页面上方 **「🤖 模型」** 下拉框中可随时切换不同模型提问。"
            )
        self.chat_log.append({
            "role": "system_note",
            "content": msg,
            "ts": time.strftime("%H:%M:%S"),
        })
        self.history_changed.emit()

    def clear_history(self) -> None:
        if self.is_busy and self._worker:
            self._worker.request_abort()
        self.chat_log.clear()
        self.add_system_welcome()

    def ask(
        self,
        question: str,
        explicit_keys: Optional[Sequence[str]] = None,
        explicit_window: Optional[Tuple[float, float]] = None,
        parent_widget: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        q = question.strip()
        if not q:
            return
        if self.is_busy:
            self.status_updated.emit("AI 正在思考上一条问题，请稍候或点击停止。")
            return

        if not self.config.is_configured():
            dlg = ApiConfigDialog(self.config, logger=self.ctx.log, first_launch=False, parent=parent_widget)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or not self.config.is_configured():
                return
            self.thinking_level_changed.emit(self.config.thinking_level)
            self.model_selection_changed.emit(self.config.active_profile_id, self.config.model)

        self._last_user_question = q
        self._pending_second_pass = False
        lvl_info = get_thinking_level_info(self.config.thinking_level)
        prof = self.config.get_active_profile()
        self.chat_log.append({
            "role": "user",
            "content": q,
            "ts": time.strftime("%H:%M:%S"),
            "model_label": f"[{prof.name}] {self.config.model}",
            "thinking_level": lvl_info["short"],
        })
        self.history_changed.emit()

        self._start_llm_call(q, explicit_keys=explicit_keys, explicit_window=explicit_window)

    def _start_llm_call(
        self,
        user_question: str,
        explicit_keys: Optional[Sequence[str]] = None,
        explicit_window: Optional[Tuple[float, float]] = None,
        extra_lookup_result: str = "",
    ) -> None:
        log_digest = self.extractor.get_base_digest()
        dynamic_ctx = self.extractor.build_dynamic_query_context(
            user_question,
            explicit_keys=explicit_keys,
            explicit_window=explicit_window,
        )
        if extra_lookup_result:
            dynamic_ctx = dynamic_ctx + "\n\n" + extra_lookup_result

        lvl_info = get_thinking_level_info(self.config.thinking_level)
        prof = self.config.get_active_profile()
        fmt_info = get_api_format_info(prof.api_format)
        sys_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            thinking_instruction=lvl_info.get("prompt_hint") or "",
            log_digest=log_digest,
            dynamic_context=dynamic_ctx,
        )

        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
        recent_turns = [m for m in self.chat_log if m["role"] in ("user", "assistant")][-10:]
        for item in recent_turns:
            clean_hist = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>\s*", "", item["content"], flags=re.DOTALL | re.IGNORECASE).strip()
            messages.append({"role": item["role"], "content": clean_hist or item["content"]})

        if self.config.is_antigravity_format():
            effective_model = self.config.model.strip()
        else:
            effective_model, _ = build_thinking_payload_params(self.config)

        self._streaming_buffer = ""
        self.busy_changed.emit(True)
        self.status_updated.emit(
            f"正在结合日志分析（[{prof.name}] {effective_model} · {fmt_info['short']} ｜ 思考：{lvl_info['short']}）..."
        )

        self._worker = LLMWorker(self.config, messages, self)
        self._worker.chunk_received.connect(self._on_worker_chunk)
        self._worker.finished_reply.connect(self._on_worker_finished)
        self._worker.failed_error.connect(self._on_worker_error)
        self._worker.start()

    def stop_generation(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.request_abort()
            self.status_updated.emit("已中止生成。")

    def _on_worker_chunk(self, piece: str) -> None:
        self._streaming_buffer = piece
        self.streaming_updated.emit(self._streaming_buffer)

    def _on_worker_finished(self, full_text: str) -> None:
        reply_text = (full_text or self._streaming_buffer).strip()
        self._streaming_buffer = ""

        query_match = re.search(
            r"<QUERY_SERIES\s+keys=[\"']([^\"']+)[\"'](?:\s+t0=[\"']([^\"']*)[\"'])?(?:\s+t1=[\"']([^\"']*)[\"'])?\s*/?>",
            reply_text,
            re.IGNORECASE,
        )
        if query_match and not self._pending_second_pass and self.ctx.store.available:
            self._pending_second_pass = True
            raw_keys = [k.strip() for k in query_match.group(1).split(",") if k.strip()]
            t0_str = (query_match.group(2) or "").strip()
            t1_str = (query_match.group(3) or "").strip()
            win: Optional[Tuple[float, float]] = None
            try:
                if t0_str and t1_str:
                    win = (float(t0_str), float(t1_str))
            except Exception:
                win = None

            self.status_updated.emit(f"AI 正在下钻调取通道 {', '.join(raw_keys[:4])} 的详细时序...")
            extra_lines = [f"=== 应 AI 请求补充的精细通道时序 ({', '.join(raw_keys)}) ==="]
            for rk in raw_keys[:10]:
                sl = self.extractor._extract_series_slice_text(
                    self.ctx.store,
                    rk,
                    win[0] if win else None,
                    win[1] if win else None,
                    n_samples=20,
                )
                if sl:
                    extra_lines.append(sl)
                else:
                    extra_lines.append(f"  * {rk}: 当前日志中未找到该通道或无有效数据")
            self._start_llm_call(
                self._last_user_question,
                explicit_keys=raw_keys,
                explicit_window=win,
                extra_lookup_result="\n".join(extra_lines),
            )
            return

        clean_reply = re.sub(r"<QUERY_SERIES[^>]*>", "", reply_text).strip()
        lvl_info = get_thinking_level_info(self.config.thinking_level)
        prof = self.config.get_active_profile()
        if clean_reply:
            self.chat_log.append({
                "role": "assistant",
                "content": clean_reply,
                "ts": time.strftime("%H:%M:%S"),
                "model_label": f"[{prof.name}] {self.config.model}",
                "thinking_level": lvl_info["short"],
            })
        self.busy_changed.emit(False)
        self.history_changed.emit()
        self.status_updated.emit(
            f"AI 识别完成（模型：[{prof.name}] {self.config.model} ｜ 思考：{lvl_info['short']} ｜ 点击回复中时间或通道可直接联动）。"
        )

    def _on_worker_error(self, err_msg: str) -> None:
        self._streaming_buffer = ""
        self.chat_log.append({
            "role": "system_note",
            "content": f"⚠️ **请求失败**：{err_msg}\n请点击右上角「⚙️ API设置」检查当前选中的 API 配置、密钥或切换其它模型。",
            "ts": time.strftime("%H:%M:%S"),
        })
        self.busy_changed.emit(False)
        self.history_changed.emit()
        self.status_updated.emit("AI 请求失败，请检查配置或切换模型。")

    def export_markdown(self) -> str:
        lvl_info = get_thinking_level_info(self.config.thinking_level)
        prof = self.config.get_active_profile()
        fmt_info = get_api_format_info(prof.api_format)
        lines = [
            "# APMLogViewer AI 诊断报告",
            f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 日志文件：{self.ctx.store.name() if self.ctx.store.available else '未打开日志'}",
            f"- 当前 API：{prof.name} ({fmt_info['name']})",
            f"- 当前模型：{self.config.model} ({prof.base_url})",
            f"- 思考等级：{lvl_info['name']}",
            "",
            "## 一、对话诊断记录",
            "",
        ]
        for item in self.chat_log:
            role = item.get("role")
            ts = item.get("ts", "")
            m_lbl = item.get("model_label", "")
            t_lvl = item.get("thinking_level", "")
            meta_parts = [x for x in (m_lbl, f"思考:{t_lvl}" if t_lvl else "") if x]
            meta_tag = f" [{' · '.join(meta_parts)}]" if meta_parts else ""
            content = item.get("content", "")
            if role == "user":
                lines.append(f"### 🧑 用户提问 ({ts}){meta_tag}\n\n{content}\n")
            elif role == "assistant":
                lines.append(f"### 🤖 AI 诊断分析 ({ts}){meta_tag}\n\n{content}\n")
            else:
                lines.append(f"> ℹ️ 系统提示 ({ts})：{content}\n")

        if self.ctx.store.available:
            lines.append("\n## 二、附：日志实测数据摘要\n")
            lines.append("```text")
            lines.append(self.extractor.get_base_digest())
            lines.append("```")
        return "\n".join(lines)


# ============================================================================
# 7. 可复用 AI 对话面板组件 (AIChatWidget) —— 紧凑自适应布局 + 零卡顿缓存渲染
# ============================================================================

QUICK_PROMPTS: List[Tuple[str, str]] = [
    ("🩺 全盘体检", "请对当前打开的这份日志做一次全面体检：总结飞行概况、检查各子系统（电源、震动、姿态跟踪、GPS、电机输出、EKF）是否正常，并指出所有异常时间点与改进建议。"),
    ("⚠️ 告警解读", "请逐条解读当前日志触发的所有告警和关键错误事件（ERR/EV/MSG），结合对应时刻前后的通道数据说明根本原因。"),
    ("📳 震动分析", "请深入分析这份日志的震动水平（VIBE.VibeX/Y/Z 与 Clip0/1/2），指出最大震动发生的时刻与飞行阶段，并结合 INS_ 滤波参数给出减震或陷波滤波建议。"),
    ("🔋 电池评估", "请分析电池电压（BAT.Volt / VoltR）、电流（BAT.Curr）与油门输出（CTUN.ThO / RCOU），评估是否存在大电流压降、动力不足或电机输出不均衡问题。"),
    ("🎯 姿态跟踪", "请对比期望姿态与实际姿态（ATT.DesRoll/Roll、DesPitch/Pitch）以及高度跟踪（CTUN.DAlt/Alt），评估 PID 控制表现与超调情况。"),
    ("📍 当前游标", "请重点分析我当前时间线游标所在时刻（或当前曲线框选区间）前后发生了什么，各关键通道有什么变化？"),
]


class AIChatWidget(QtWidgets.QWidget):
    """
    通用 AI 识别对话组件：
    - 布局经过紧凑自适应优化：普通窗口大小下绝不溢出或遮挡；
    - 历史消息 HTML 缓存 (`_cached_history_html`) + 流式轻量预览，流式打字丝滑不卡顿；
    - 支持点击展开/折叠思考推导过程 (`think://<idx>`)。
    """

    request_open_floating = QtCore.Signal()

    def __init__(
        self,
        ctx,
        session: AIChatSession,
        is_floating_mode: bool = False,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.session = session
        self.is_floating_mode = is_floating_mode
        self._syncing_thinking_combo = False
        self._syncing_model_combo = False
        self._cached_history_html: str = ""
        self._expanded_think_indices: Set[int] = set()

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 1) 第一行（紧凑控制栏）：左对齐 [🤖模型下拉框] + [🧠思考下拉框]，右对齐紧凑功能按钮
        top_bar = QtWidgets.QHBoxLayout()
        top_bar.setSpacing(6)

        model_lbl = QtWidgets.QLabel("🤖 模型:")
        model_lbl.setToolTip("直接切换已配置的任意 API 及其旗下的任意模型进行提问")
        top_bar.addWidget(model_lbl)

        self.model_selector_combo = QtWidgets.QComboBox()
        self.model_selector_combo.setMinimumWidth(140)
        self.model_selector_combo.setMaximumWidth(250)
        if hasattr(QtWidgets.QComboBox, "SizeAdjustPolicy"):
            self.model_selector_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        self.model_selector_combo.setToolTip(
            "像 Antigravity 一样直接在此选择不同 API 下的不同 AI 模型进行提问。\n"
            "如需添加更多 API 或拉取新模型，请点击右侧「⚙️ API设置」。"
        )
        self.model_selector_combo.currentIndexChanged.connect(self._on_page_model_combo_changed)
        top_bar.addWidget(self.model_selector_combo)

        think_lbl = QtWidgets.QLabel("🧠 思考:")
        think_lbl.setToolTip("随时切换 AI 模型的思考深度")
        top_bar.addWidget(think_lbl)

        self.thinking_combo = QtWidgets.QComboBox()
        self.thinking_combo.setMinimumWidth(88)
        self.thinking_combo.setMaximumWidth(120)
        self.thinking_combo.setToolTip(
            "切换模型的思考等级：\n"
            "• 自动：使用模型默认配置\n"
            "• 关闭思考：极速直接回答\n"
            "• 低度思考 (Low)：轻量快速推理\n"
            "• 中度思考 (Medium)：均衡多维分析\n"
            "• 深度思考 (High)：深度归因推导"
        )
        for t_item in THINKING_LEVELS:
            self.thinking_combo.addItem(t_item["short"], t_item["id"])
        cur_idx = self.thinking_combo.findData(self.session.config.thinking_level)
        if cur_idx >= 0:
            self.thinking_combo.setCurrentIndex(cur_idx)
        self.thinking_combo.currentIndexChanged.connect(self._on_thinking_combo_changed)
        top_bar.addWidget(self.thinking_combo)

        # 中间弹性留白，防止下拉框被过度拉伸，同时让右侧按钮紧凑排列
        top_bar.addStretch(1)

        if not self.is_floating_mode:
            self.float_btn = QtWidgets.QPushButton("🗗 悬浮窗")
            self.float_btn.setToolTip("弹出可置顶的独立悬浮窗口，在看曲线或看地图时也能随时提问")
            self.float_btn.clicked.connect(self.request_open_floating.emit)
            top_bar.addWidget(self.float_btn)

        self.export_btn = QtWidgets.QPushButton("💾 导出")
        self.export_btn.setToolTip("将当前 AI 诊断对话导出为 Markdown 报告")
        self.export_btn.clicked.connect(self._on_export_clicked)
        top_bar.addWidget(self.export_btn)

        self.clear_btn = QtWidgets.QPushButton("🗑️ 清空")
        self.clear_btn.setToolTip("清空当前对话历史")
        self.clear_btn.clicked.connect(self.session.clear_history)
        top_bar.addWidget(self.clear_btn)

        self.cfg_btn = QtWidgets.QPushButton("⚙️ API设置")
        self.cfg_btn.setToolTip("管理多个 API 账号、拉取可用模型列表、设置协议格式 (OpenAI / Antigravity)")
        self.cfg_btn.clicked.connect(self._on_open_config)
        top_bar.addWidget(self.cfg_btn)

        root.addLayout(top_bar)

        # 2) 第二行（紧凑日志状态 + 快捷诊断栏）：左侧自适应日志状态标签，右侧精简快捷按钮
        sub_bar = QtWidgets.QHBoxLayout()
        sub_bar.setSpacing(5)

        self.log_badge = QtWidgets.QLabel()
        self.log_badge.setMinimumWidth(100)
        if hasattr(QtWidgets, "QSizePolicy"):
            self.log_badge.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
        self.log_badge.setStyleSheet("font-weight:bold;font-size:12px;")
        sub_bar.addWidget(self.log_badge, 1)

        for btn_title, prompt_text in QUICK_PROMPTS:
            b = QtWidgets.QPushButton(btn_title)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setToolTip(prompt_text)
            b.setStyleSheet("padding: 2px 6px; font-size: 12px;")
            b.clicked.connect(lambda _c=False, p=prompt_text: self._send_quick_prompt(p))
            sub_bar.addWidget(b)

        root.addLayout(sub_bar)

        # 3) 富文本对话展示区
        self.chat_browser = QtWidgets.QTextBrowser()
        self.chat_browser.setOpenLinks(False)
        self.chat_browser.setOpenExternalLinks(False)
        self.chat_browser.anchorClicked.connect(self._on_anchor_clicked)
        root.addWidget(self.chat_browser, 1)

        # 4) 实时状态提示条
        self.status_bar_lbl = QtWidgets.QLabel("就绪：可在上方切换模型与思考等级，按 Ctrl+Enter 发送提问。")
        self.status_bar_lbl.setMinimumWidth(100)
        self.status_bar_lbl.setStyleSheet("color:#888;font-size:12px;")
        root.addWidget(self.status_bar_lbl)

        # 5) 底部输入框与发送按钮
        input_lay = QtWidgets.QHBoxLayout()
        input_lay.setSpacing(6)
        self.input_edit = QtWidgets.QPlainTextEdit()
        self.input_edit.setPlaceholderText(
            "在此输入问题（AI 将直接读取当前打开的日志并给出解释；按 Ctrl+Enter 快速发送）..."
        )
        self.input_edit.setMaximumHeight(68)
        self.input_edit.installEventFilter(self)
        input_lay.addWidget(self.input_edit, 1)

        btn_col = QtWidgets.QVBoxLayout()
        btn_col.setSpacing(4)
        self.send_btn = QtWidgets.QPushButton("🚀 发送识别")
        self.send_btn.setMinimumHeight(32)
        self.send_btn.setMinimumWidth(88)
        self.send_btn.setStyleSheet("font-weight:bold;")
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_col.addWidget(self.send_btn)

        self.stop_btn = QtWidgets.QPushButton("⏹ 停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.session.stop_generation)
        btn_col.addWidget(self.stop_btn)
        input_lay.addLayout(btn_col)

        root.addLayout(input_lay)

        self.session.history_changed.connect(self._on_history_changed)
        self.session.streaming_updated.connect(self._on_streaming_chunk)
        self.session.busy_changed.connect(self._on_busy_changed)
        self.session.status_updated.connect(self.status_bar_lbl.setText)
        self.session.thinking_level_changed.connect(self._on_session_thinking_level_changed)
        self.session.model_selection_changed.connect(self._on_session_model_selection_changed)

        self.refresh_model_selector_combo()
        self.update_log_badge()
        self._on_history_changed()

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self.input_edit and event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event  # type: ignore
            if key_event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                mods = key_event.modifiers()
                if mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.MetaModifier):
                    self._on_send_clicked()
                    return True
        return super().eventFilter(obj, event)

    def refresh_model_selector_combo(self) -> None:
        self._syncing_model_combo = True
        try:
            if hasattr(self.model_selector_combo, "clear"):
                self.model_selector_combo.clear()
            else:
                self.model_selector_combo._items = []

            choices = self.session.config.list_all_model_choices()
            active_pid = self.session.config.active_profile_id
            active_mod = self.session.config.model
            target_idx = 0

            for idx, ch in enumerate(choices):
                token = f"{ch['profile_id']}::{ch['model']}"
                self.model_selector_combo.addItem(ch["label"], token)
                if ch["profile_id"] == active_pid and ch["model"] == active_mod:
                    target_idx = idx

            if choices:
                self.model_selector_combo.setCurrentIndex(target_idx)
        finally:
            self._syncing_model_combo = False

    def _on_page_model_combo_changed(self, index: int) -> None:
        if self._syncing_model_combo or index < 0:
            return
        token = str(self.model_selector_combo.itemData(index) or "")
        if "::" not in token:
            return
        pid, mod_name = token.split("::", 1)
        self.session.set_active_model(pid, mod_name)
        self.update_log_badge()

    def _on_session_model_selection_changed(self, _profile_id: str, _model_name: str) -> None:
        self.refresh_model_selector_combo()
        self.update_log_badge()

    def _on_thinking_combo_changed(self, index: int) -> None:
        if self._syncing_thinking_combo:
            return
        lvl_id = str(self.thinking_combo.itemData(index) or "medium")
        self.session.set_thinking_level(lvl_id)
        self.update_log_badge()

    def _on_session_thinking_level_changed(self, level_id: str) -> None:
        self._syncing_thinking_combo = True
        try:
            idx = self.thinking_combo.findData(level_id)
            cur_idx = self.thinking_combo.currentIndex() if hasattr(self.thinking_combo, "currentIndex") else -1
            if idx >= 0 and cur_idx != idx:
                self.thinking_combo.setCurrentIndex(idx)
        finally:
            self._syncing_thinking_combo = False
        self.update_log_badge()

    def update_log_badge(self) -> None:
        store = self.ctx.store
        cfg_ok = "✅已就绪" if self.session.config.is_configured() else "⚠️未配Key"
        prof = self.session.config.get_active_profile()
        if self.session.config.is_antigravity_format():
            effective_model = self.session.config.model.strip()
        else:
            effective_model, _ = build_thinking_payload_params(self.session.config)
        lvl_info = get_thinking_level_info(self.session.config.thinking_level)
        fmt_info = get_api_format_info(prof.api_format)
        if store.available:
            stats = store.stats() or {}
            dur = float(stats.get("duration") or 0.0)
            fw = stats.get("firmware") or "?"
            cur_t = self.session.extractor.current_time
            short_text = f"📂 {store.name()} ({dur:.1f}s) ｜ 游标:{cur_t:.1f}s ｜ {cfg_ok}"
            full_tip = (
                f"当前日志: {store.name()} ({fw}, {dur:.1f}s)\n"
                f"游标时刻: {cur_t:.2f}s\n"
                f"当前 API: {prof.name} ({fmt_info['name']})\n"
                f"当前模型: {effective_model} (思考等级: {lvl_info['name']})"
            )
            self.log_badge.setText(short_text)
            self.log_badge.setToolTip(full_tip)
        else:
            self.log_badge.setText(f"📂 未打开日志 ｜ {cfg_ok} [{prof.name} · {effective_model}]")
            self.log_badge.setToolTip(f"当前未打开 .BIN 日志\n当前 API: {prof.name} ({fmt_info['name']})\n模型: {effective_model}")

    def _send_quick_prompt(self, prompt_text: str) -> None:
        self.session.ask(prompt_text, parent_widget=self)

    def _on_send_clicked(self) -> None:
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        self.input_edit.clear()
        self.session.ask(text, parent_widget=self)

    def _on_open_config(self) -> None:
        dlg = ApiConfigDialog(self.session.config, logger=self.ctx.log, first_launch=False, parent=self)
        dlg.config_saved.connect(lambda: self.session.thinking_level_changed.emit(self.session.config.thinking_level))
        dlg.config_saved.connect(
            lambda: self.session.model_selection_changed.emit(
                self.session.config.active_profile_id, self.session.config.model
            )
        )
        dlg.config_saved.connect(self.update_log_badge)
        dlg.exec()
        self.refresh_model_selector_combo()
        self._on_session_thinking_level_changed(self.session.config.thinking_level)

    def _on_export_clicked(self) -> None:
        default_name = "AI日志诊断报告.md"
        if self.ctx.store.available:
            default_name = f"{Path(self.ctx.store.name()).stem}_AI诊断报告.md"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出 AI 诊断报告",
            default_name,
            "Markdown 文档 (*.md);;文本文件 (*.txt)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.session.export_markdown(), encoding="utf-8")
            self.ctx.control.status(f"AI 诊断报告已导出至 {Path(path).name}")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "导出失败", f"写入文件失败：{exc}")

    def _on_busy_changed(self, busy: bool) -> None:
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def _rebuild_history_cache(self) -> None:
        """仅在历史消息发生增删或用户点击展开/折叠思考块时重算一次历史 HTML，流式期间直接复用。"""
        try:
            known_types = self.ctx.store.types() if self.ctx.store.available else []
        except Exception:
            known_types = []

        blocks: List[str] = []
        for idx, item in enumerate(self.session.chat_log):
            role = item.get("role")
            ts = item.get("ts", "")
            m_lbl = item.get("model_label", "")
            t_lvl = item.get("thinking_level", "")
            meta_str = ""
            if m_lbl:
                meta_str += f" · 🤖 {_html_escape(m_lbl)}"
            if t_lvl:
                meta_str += f" · 🧠 {_html_escape(t_lvl)}"
            content = item.get("content", "")
            is_expanded = idx in self._expanded_think_indices
            body_html = render_interactive_html(
                content,
                known_types,
                msg_index=idx,
                think_expanded=is_expanded,
            )

            if role == "user":
                blocks.append(
                    f"<div style='margin:8px 0;padding:10px 12px;border-left:4px solid #2f6fed;"
                    f"background:rgba(47,111,237,0.09);border-radius:4px;'>"
                    f"<div style='font-size:11px;color:#2f6fed;font-weight:bold;margin-bottom:4px;'>🧑 提问 ({ts}{meta_str})</div>"
                    f"<div style='font-size:13px;line-height:1.5;'>{body_html}</div></div>"
                )
            elif role == "assistant":
                blocks.append(
                    f"<div style='margin:8px 0;padding:10px 12px;border-left:4px solid #16a085;"
                    f"background:rgba(22,160,133,0.08);border-radius:4px;'>"
                    f"<div style='font-size:11px;color:#16a085;font-weight:bold;margin-bottom:4px;'>🤖 AI 识别解释 ({ts}{meta_str})</div>"
                    f"<div style='font-size:13px;line-height:1.55;'>{body_html}</div></div>"
                )
            else:
                blocks.append(
                    f"<div style='margin:6px 0;padding:8px 12px;border-left:3px solid #f0a500;"
                    f"background:rgba(240,165,0,0.08);border-radius:4px;font-size:12px;'>"
                    f"{body_html}</div>"
                )

        self._cached_history_html = "".join(blocks)

    def _on_history_changed(self) -> None:
        self._rebuild_history_cache()
        self.refresh_chat_view(streaming_tail="")

    def _on_streaming_chunk(self, streaming_text: str) -> None:
        self.refresh_chat_view(streaming_tail=streaming_text)

    def refresh_chat_view(self, streaming_tail: str = "") -> None:
        if not self._cached_history_html and self.session.chat_log:
            self._rebuild_history_cache()

        if streaming_tail:
            lvl_info = get_thinking_level_info(self.session.config.thinking_level)
            prof = self.session.config.get_active_profile()
            stream_html = render_streaming_preview_fast(streaming_tail)
            tail_block = (
                f"<div style='margin:8px 0;padding:10px 12px;border-left:4px solid #16a085;"
                f"background:rgba(22,160,133,0.08);border-radius:4px;'>"
                f"<div style='font-size:11px;color:#16a085;font-weight:bold;margin-bottom:4px;'>"
                f"🤖 AI 正在识别 ([{_html_escape(prof.name)}] {_html_escape(self.session.config.model)} · 🧠 {lvl_info['short']})...</div>"
                f"<div style='font-size:13px;line-height:1.55;'>{stream_html} ▌</div></div>"
            )
            self.chat_browser.setHtml(self._cached_history_html + tail_block)
        else:
            self.chat_browser.setHtml(self._cached_history_html)

        sb = self.chat_browser.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _on_anchor_clicked(self, url: QtCore.QUrl) -> None:
        raw = url.toString()
        if raw.startswith("think://"):
            idx_str = raw[len("think://"):].strip("/")
            try:
                msg_idx = int(idx_str)
                if msg_idx in self._expanded_think_indices:
                    self._expanded_think_indices.remove(msg_idx)
                else:
                    self._expanded_think_indices.add(msg_idx)
                # 保持当前滚动条位置附近刷新
                self._rebuild_history_cache()
                self.chat_browser.setHtml(self._cached_history_html)
            except Exception:
                pass
        elif raw.startswith("seek://"):
            val_str = raw[len("seek://"):].strip("/")
            try:
                t_val = float(val_str)
                ok = self.ctx.control.seek(t_val)
                self.ctx.control.status(f"AI识别联动：已定位游标至 {t_val:.2f}s" if ok else f"定位 {t_val:.2f}s 失败（未打开日志或越界）")
            except Exception:
                pass
        elif raw.startswith("zoom://"):
            val_str = raw[len("zoom://"):].strip("/")
            try:
                parts = val_str.split(",")
                t0, t1 = float(parts[0]), float(parts[1])
                self.ctx.control.select_tab("曲线")
                self.ctx.control.zoom(t0, t1)
                self.ctx.control.seek(t0)
                self.ctx.control.status(f"AI识别联动：已放大曲线区间 {t0:.1f}s – {t1:.1f}s")
            except Exception:
                pass
        elif raw.startswith("channel://"):
            ch_key = raw[len("channel://"):].strip("/")
            ok = self.ctx.control.add_channel(ch_key)
            self.ctx.control.status(f"AI识别联动：已将通道 {ch_key} 添加到曲线视图" if ok else f"当前日志中未找到通道 {ch_key}")


class FloatingAIDialog(QtWidgets.QDialog):
    """
    从软件顶部工具栏「🤖 AI识别」按钮呼出的非模态可置顶对话框。
    """

    def __init__(
        self,
        ctx,
        session: AIChatSession,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        flags = QtCore.Qt.WindowType.Window | QtCore.Qt.WindowType.WindowCloseButtonHint | QtCore.Qt.WindowType.WindowMinimizeButtonHint
        if session.config.always_on_top:
            flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.ctx = ctx
        self.session = session

        self.setWindowTitle("🤖 AI识别 — 跨界面飞控日志智能助手 (Ctrl+Shift+A)")
        self.setMinimumWidth(480)
        self.resize(620, 660)
        self.setModal(False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        pin_bar = QtWidgets.QHBoxLayout()
        pin_info = QtWidgets.QLabel(
            "💡 <b>跨界面模式</b>：可在主窗浏览「曲线 / 地图 / 告警」的同时在此切换模型与提问。"
        )
        pin_info.setStyleSheet("font-size:12px;color:#777;")
        pin_bar.addWidget(pin_info, 1)

        self.pin_chk = QtWidgets.QCheckBox("📌 始终置顶")
        self.pin_chk.setChecked(self.session.config.always_on_top)
        self.pin_chk.toggled.connect(self._on_pin_toggled)
        pin_bar.addWidget(self.pin_chk)
        lay.addLayout(pin_bar)

        self.chat_widget = AIChatWidget(ctx, session, is_floating_mode=True, parent=self)
        lay.addWidget(self.chat_widget, 1)

    def _on_pin_toggled(self, checked: bool) -> None:
        self.session.config.always_on_top = checked
        self.session.config.save(self.ctx.log)
        pos = self.pos()
        flags = self.windowFlags()
        if checked:
            flags |= QtCore.Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~QtCore.Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.move(pos)
        self.show()


# ============================================================================
# 8. 宿主顶部工具栏按钮注入与生命周期管理 (HostToolbarIntegration)
# ============================================================================

class HostToolbarIntegration(QtCore.QObject):
    """
    在宿主主窗口顶部工具栏挂载「🤖 AI识别」按钮，确保用户在软件的任何界面都能一键唤起 AI 识别对话框，
    并在插件停用/重载时干净卸载。
    """

    def __init__(
        self,
        ctx,
        config: AIConfig,
        session: AIChatSession,
        tab_page: QtWidgets.QWidget,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.config = config
        self.session = session
        self.tab_page = tab_page
        self.floating_dlg: Optional[FloatingAIDialog] = None
        self._injected_actions: List[Tuple[QtWidgets.QWidget, QtGui.QAction]] = []
        self._injected_widgets: List[QtWidgets.QWidget] = []
        self._shortcut: Optional[QtGui.QShortcut] = None
        self._cleaned_up: bool = False

    def setup_post_register(self) -> None:
        if self._cleaned_up:
            return
        main_win = self._find_main_window()
        if main_win is not None:
            self._inject_top_button(main_win)

        if not self.config.is_configured():
            self.config.first_prompt_shown = True
            dlg = ApiConfigDialog(
                self.config,
                logger=self.ctx.log,
                first_launch=True,
                parent=main_win or self.tab_page,
            )
            dlg.config_saved.connect(lambda: self.session.thinking_level_changed.emit(self.config.thinking_level))
            dlg.config_saved.connect(
                lambda: self.session.model_selection_changed.emit(
                    self.config.active_profile_id, self.config.model
                )
            )
            dlg.config_saved.connect(self.session.history_changed.emit)
            dlg.exec()

    def _find_main_window(self) -> Optional[QtWidgets.QMainWindow]:
        win = self.tab_page.window()
        if isinstance(win, QtWidgets.QMainWindow):
            return win
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                if isinstance(w, QtWidgets.QMainWindow):
                    return w
        return None

    def _inject_top_button(self, main_win: QtWidgets.QMainWindow) -> None:
        toolbars = main_win.findChildren(QtWidgets.QToolBar)
        if toolbars:
            tb = toolbars[0]
            sep = tb.addSeparator()
            self._injected_actions.append((tb, sep))

            ai_btn = QtWidgets.QToolButton(tb)
            ai_btn.setText("🤖 AI识别")
            ai_btn.setToolTip("打开 AI 智能日志识别悬浮对话框 (Ctrl+Shift+A)\n无论在哪个界面，均可直接选择不同 AI 模型针对当前打开的日志提问")
            ai_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            ai_btn.setStyleSheet(
                "QToolButton { font-weight: bold; padding: 4px 10px; border-radius: 4px; "
                "background-color: rgba(47, 111, 237, 0.16); }"
                "QToolButton:hover { background-color: rgba(47, 111, 237, 0.28); }"
            )
            ai_btn.clicked.connect(self.toggle_floating_dialog)
            act = tb.addWidget(ai_btn)
            self._injected_actions.append((tb, act))
            self._injected_widgets.append(ai_btn)
        else:
            menu_bar = main_win.menuBar()
            if menu_bar is not None:
                act = menu_bar.addAction("🤖 AI识别")
                act.triggered.connect(self.toggle_floating_dialog)
                self._injected_actions.append((menu_bar, act))

        try:
            self._shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+A"), main_win)
            self._shortcut.activated.connect(self.toggle_floating_dialog)
        except Exception:
            pass

    def open_floating_dialog(self, initial_question: str = "") -> None:
        if self._cleaned_up:
            return
        if self.floating_dlg is None:
            main_win = self._find_main_window()
            self.floating_dlg = FloatingAIDialog(self.ctx, self.session, parent=main_win)
        self.floating_dlg.chat_widget.refresh_model_selector_combo()
        self.floating_dlg.chat_widget.update_log_badge()
        self.floating_dlg.show()
        self.floating_dlg.raise_()
        self.floating_dlg.activateWindow()
        self.floating_dlg.chat_widget.input_edit.setFocus()
        if initial_question:
            self.session.ask(initial_question, parent_widget=self.floating_dlg)

    def toggle_floating_dialog(self) -> None:
        if self.floating_dlg is not None and self.floating_dlg.isVisible():
            self.floating_dlg.raise_()
            self.floating_dlg.activateWindow()
            self.floating_dlg.chat_widget.input_edit.setFocus()
        else:
            self.open_floating_dialog()

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.session.is_busy:
            self.session.stop_generation()

        if self._shortcut is not None:
            try:
                self._shortcut.setEnabled(False)
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

        for container, act in self._injected_actions:
            try:
                container.removeAction(act)
            except Exception:
                pass
        self._injected_actions.clear()

        for w in self._injected_widgets:
            try:
                w.hide()
                w.deleteLater()
            except Exception:
                pass
        self._injected_widgets.clear()

        if self.floating_dlg is not None:
            try:
                self.floating_dlg.close()
                self.floating_dlg.deleteLater()
            except Exception:
                pass
            self.floating_dlg = None


# ============================================================================
# 9. 插件入口 register(ctx)
# ============================================================================

def register(ctx) -> None:
    if getattr(ctx, "api_version", 1) != 1:
        raise RuntimeError(f"AI识别插件需要接口 v1，当前宿主接口 v{getattr(ctx, 'api_version', '?')}")

    config = AIConfig.load(logger=ctx.log)
    extractor = LogKnowledgeExtractor(ctx)
    session = AIChatSession(ctx, config, extractor)
    session.add_system_welcome()

    # 1) 注册顶部「AI识别」标签页
    tab_widget = AIChatWidget(ctx, session, is_floating_mode=False)
    ctx.register_tab("AI识别", tab_widget)

    # 2) 顶部工具栏「🤖 AI识别」全局按钮与首次启动自动弹窗控制器
    integration = HostToolbarIntegration(ctx, config, session, tab_widget)
    tab_widget.request_open_floating.connect(lambda: integration.open_floating_dialog())
    tab_widget.destroyed.connect(lambda *_args: integration.cleanup())

    # 3) 注册曲线右键工具：「AI 识别选中区间」
    def ai_plot_selection_tool(inner_ctx, sel) -> None:
        keys_list = list(getattr(sel, "keys", ()) or ())
        t0 = float(getattr(sel, "t0", 0.0))
        t1 = float(getattr(sel, "t1", t0))
        whole = bool(getattr(sel, "whole", False))
        lane_label = getattr(sel, "lane_label", "当前通道")
        scope_desc = f"当前视图区间 {t0:.1f}s–{t1:.1f}s" if whole else f"框选区间 {t0:.1f}s–{t1:.1f}s"
        keys_desc = ", ".join(keys_list) if keys_list else lane_label
        question = (
            f"请详细识别并分析曲线道「{lane_label}」（通道：{keys_desc}）在 {scope_desc} 内的变化特征、"
            f"极值/异常波动原因，以及同一时间段内飞控其它相关通道和事件的表现。"
        )
        integration.open_floating_dialog()
        session.ask(
            question,
            explicit_keys=keys_list,
            explicit_window=(t0, t1),
            parent_widget=integration.floating_dlg or tab_widget,
        )
        return None

    ctx.register_plot_tool("🤖 AI 识别选中区间", ai_plot_selection_tool)

    # 4) 注册导出器：「AI 诊断对话报告 (.md)」
    def export_ai_report(store, path: str) -> str:
        return session.export_markdown()

    ctx.register_exporter("AI 诊断对话报告 (.md)", export_ai_report)

    # 5) 订阅宿主事件信号，实时同步日志状态与游标上下文
    def on_log_loaded(info: Any) -> None:
        extractor.clear_cache()
        tab_widget.update_log_badge()
        if integration.floating_dlg is not None:
            integration.floating_dlg.chat_widget.update_log_badge()
        if isinstance(info, dict):
            name = info.get("name") or (ctx.store.name() if ctx.store.available else "新日志")
            stats = info.get("stats") or {}
            fw = stats.get("firmware") or "ArduPilot"
            dur = float(stats.get("duration") or 0.0)
            n_alerts = int(info.get("n_alerts") or 0)
            session.chat_log.append({
                "role": "system_note",
                "content": (
                    f"📂 已切换并加载日志 **{name}**（固件 `{fw}`，时长 `{dur:.1f}s`，告警 `{n_alerts}` 条）。"
                    f"现在可以直接选择模型针对这份日志提问！"
                ),
                "ts": time.strftime("%H:%M:%S"),
            })
            session.history_changed.emit()

    def on_log_closed() -> None:
        extractor.clear_cache()
        extractor.latest_alerts.clear()
        tab_widget.update_log_badge()
        if integration.floating_dlg is not None:
            integration.floating_dlg.chat_widget.update_log_badge()

    def on_alerts_updated(alerts: Any) -> None:
        extractor.on_alerts_updated(alerts)

    def on_time_changed(t: float) -> None:
        extractor.current_time = float(t)
        tab_widget.update_log_badge()
        if integration.floating_dlg is not None and integration.floating_dlg.isVisible():
            integration.floating_dlg.chat_widget.update_log_badge()

    def on_mode_changed(mode: str) -> None:
        extractor.current_mode = str(mode or "")

    def on_selection_changed(sel_dict: Any) -> None:
        if isinstance(sel_dict, dict):
            extractor.selection = dict(sel_dict)

    def on_plugins_changed(plugin_list: Any) -> None:
        if not isinstance(plugin_list, list):
            return
        for item in plugin_list:
            if isinstance(item, dict) and item.get("id") == ctx.plugin_id:
                if item.get("status") != "enabled" and item.get("status") != "已启用":
                    integration.cleanup()

    ctx.signals.log_loaded.connect(on_log_loaded)
    ctx.signals.log_closed.connect(on_log_closed)
    ctx.signals.alerts_updated.connect(on_alerts_updated)
    ctx.signals.time_changed.connect(on_time_changed)
    ctx.signals.mode_changed.connect(on_mode_changed)
    ctx.signals.selection_changed.connect(on_selection_changed)
    ctx.signals.plugins_changed.connect(on_plugins_changed)

    # 6) 延迟挂载顶部工具栏按钮与首次启动自动弹出 API Key 配置会话框
    QtCore.QTimer.singleShot(350, integration.setup_post_register)
    ctx.log.info("AI识别插件 (v%s) 已完成注册", __version__)