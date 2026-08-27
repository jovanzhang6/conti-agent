from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

from .errors import ConfigurationError


VALID_PROTOCOLS = {"openai", "openai-compat", "anthropic", "fake"}
VALID_PERMISSION_MODES = {"read_only", "workspace", "approved", "trusted"}


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key_env: str = ""
    context_window: int = 0
    max_output_tokens: int = 8192

    def resolve_api_key(self) -> str:
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "")


@dataclass
class RuntimeConfig:
    permission_mode: str = "workspace"
    max_tool_iterations: int = 32
    history_limit: int = 120


@dataclass
class ProfileConfig:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "read_only"
    max_tool_iterations: int = 12
    allow_spawn: bool = False


@dataclass
class HookConfig:
    event: str
    command: list[str]
    match_tool: str | None = None
    timeout_ms: int = 5000
    continue_on_error: bool = False


@dataclass
class ExternalServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    providers: list[ProviderConfig] = field(default_factory=list)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    profiles: list[ProfileConfig] = field(default_factory=list)
    hooks: list[HookConfig] = field(default_factory=list)
    external_servers: list[ExternalServerConfig] = field(default_factory=list)
    skills_enabled: bool = True
    hooks_enabled: bool = True
    profiles_enabled: bool = True
    external_tools_enabled: bool = True
    collaboration_enabled: bool = True


def _require(raw: dict[str, Any], key: str, context: str) -> Any:
    if key not in raw:
        raise ConfigurationError(f"{context} 缺少字段：{key}")
    return raw[key]


def _parse_provider(raw: dict[str, Any]) -> ProviderConfig:
    config = ProviderConfig(
        name=_require(raw, "name", "provider"),
        protocol=_require(raw, "protocol", "provider"),
        base_url=_require(raw, "base_url", "provider"),
        model=_require(raw, "model", "provider"),
        api_key_env=raw.get("api_key_env", ""),
        context_window=int(raw.get("context_window", 0)),
        max_output_tokens=int(raw.get("max_output_tokens", 8192)),
    )
    if config.protocol not in VALID_PROTOCOLS:
        raise ConfigurationError(f"不支持的 provider 协议：{config.protocol}")
    return config


def _parse_profile(raw: dict[str, Any]) -> ProfileConfig:
    return ProfileConfig(
        name=_require(raw, "name", "profile"),
        description=raw.get("description", ""),
        system_prompt=raw.get("system_prompt", ""),
        allowed_tools=list(raw.get("allowed_tools", [])),
        permission_mode=raw.get("permission_mode", "read_only"),
        max_tool_iterations=int(raw.get("max_tool_iterations", 12)),
        allow_spawn=bool(raw.get("allow_spawn", False)),
    )


def _parse_hook(raw: dict[str, Any]) -> HookConfig:
    return HookConfig(
        event=_require(raw, "event", "hook"),
        command=list(_require(raw, "command", "hook")),
        match_tool=raw.get("match_tool"),
        timeout_ms=int(raw.get("timeout_ms", 5000)),
        continue_on_error=bool(raw.get("continue_on_error", False)),
    )


def _parse_external(raw: dict[str, Any]) -> ExternalServerConfig:
    return ExternalServerConfig(
        name=_require(raw, "name", "external_server"),
        command=list(_require(raw, "command", "external_server")),
        env=dict(raw.get("env", {})),
    )


def load_single(path: Path) -> AppConfig:
    """加载并校验一份 TOML 配置。"""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置不存在：{path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"配置解析失败：{path}: {exc}") from exc

    runtime_raw = raw.get("runtime", {})
    mode = runtime_raw.get("permission_mode", "workspace")
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigurationError(f"未知权限模式：{mode}")
    providers = [_parse_provider(item) for item in raw.get("provider", [])]
    names = [provider.name for provider in providers]
    if len(names) != len(set(names)):
        raise ConfigurationError("provider 名称必须唯一")
    return AppConfig(
        providers=providers,
        runtime=RuntimeConfig(
            permission_mode=mode,
            max_tool_iterations=int(runtime_raw.get("max_tool_iterations", 32)),
            history_limit=int(runtime_raw.get("history_limit", 120)),
        ),
        profiles=[_parse_profile(item) for item in raw.get("profile", [])],
        hooks=[_parse_hook(item) for item in raw.get("hook", [])],
        external_servers=[_parse_external(item) for item in raw.get("external_server", [])],
        skills_enabled=bool(raw.get("extensions", {}).get("skills", True)),
        hooks_enabled=bool(raw.get("extensions", {}).get("hooks", True)),
        profiles_enabled=bool(raw.get("extensions", {}).get("profiles", True)),
        external_tools_enabled=bool(raw.get("extensions", {}).get("external_tools", True)),
        collaboration_enabled=bool(raw.get("extensions", {}).get("collaboration", True)),
    )


def merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    """用局部配置覆盖同层同名字段，列表按名称替换或追加。"""
    for provider in override.providers:
        for index, item in enumerate(base.providers):
            if item.name == provider.name:
                base.providers[index] = provider
                break
        else:
            base.providers.append(provider)
    for profile in override.profiles:
        base.profiles = [item for item in base.profiles if item.name != profile.name]
        base.profiles.append(profile)
    base.hooks.extend(override.hooks)
    base.external_servers.extend(override.external_servers)
    base.runtime = override.runtime if override.runtime != RuntimeConfig() else base.runtime
    for key in ("skills_enabled", "hooks_enabled", "profiles_enabled",
                "external_tools_enabled", "collaboration_enabled"):
        setattr(base, key, getattr(override, key))
    return base


def load_config(path: Path | None = None) -> AppConfig:
    """默认按 项目局部 > 项目 > 用户目录 的顺序合并配置。"""
    if path is not None:
        return load_single(path)
    candidates = [
        Path.home() / ".conti-agent" / "config.toml",
        Path.cwd() / ".conti" / "config.toml",
        Path.cwd() / ".conti" / "config.local.toml",
    ]
    config: AppConfig | None = None
    for candidate in candidates:
        if candidate.exists():
            loaded = load_single(candidate)
            config = loaded if config is None else merge_config(config, loaded)
    if config is None:
        raise ConfigurationError("未找到 .conti/config.toml 或用户级配置")
    return config
