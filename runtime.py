# -*- coding: utf-8 -*-
"""回响插件：宿主运行时适配层。

【为什么需要这一层】
六块业务代码（layers/*）只认一个「薄上下文」：`.config`（配置对象）、`.paths.data_dir`、
`.store`（状态存储）、`.api.call`（A 类宿主能力）、`.xhh_client`（B 类直连客户端）、
`.generate_text`（大模型）。真实 MaiBot 的 `PluginContext` 只有 `api / llm / send / paths`，
且配置是插件基类的 `self.config` 属性、不是 ctx 上的东西。

本模块把两者对接起来：把 `MaiBotPlugin` 实例 + `PluginContext` 包成一个薄上下文，
业务层完全不用知道宿主长什么样。这样单测里用 `SimpleNamespace` 也能跑同一套代码。

【真实 SDK 契约（2026-11-04 在部署环境实测核对）】
- `maibot_sdk.__version__ == 2.21.0`
- `PluginContext.paths` 是 `PluginPaths(data_dir: Path, runtime_dir: Path)`
- `PluginContext.api.call(api_name, *, version="", **kwargs)` —— A 类能力
- `PluginContext.llm.generate(prompt, model="", temperature=None, max_tokens=None, ...)` -> dict
- 配置在插件基类：`self.config`（property，返回 PluginConfigBase 子类实例）
- 日志用 `self.ctx.logger`
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 宿主模型任务名。
# 【真机踩坑（2026-11-06）】`ctx.llm.generate(task_name=...)` 的 task_name **不是自由标签**，
# 必须是 model_config.toml 里已声明的 `[model_task_config.XXX]` 段名，否则直接报
# `未找到名为 XXX 的模型配置`，整条内容生成链路全废。
# 服务器上现有的合法名字：web_search / replyer / planner / memory / utils / learner /
# fast_model / emoji / vlm / voice / embedding / image_embedding。
# 写内容属于「回复生成」这一类，用 replyer 最贴切。
DEFAULT_MODEL_TASK = "replyer"

# 兜底用的任务名：replyer 不可用时依次尝试。
FALLBACK_MODEL_TASKS: tuple[str, ...] = ("replyer", "utils", "fast_model", "planner")

# 默认写作用的具体模型名（空字符串=不指定，沿用任务里的优先级顺序）。
# 【真机踩坑 2026-11-06】宿主 model_config.toml 里 `replyer` 的 model_list 是
# ["ds-official", "feiyu-gemini-flash"]，DeepSeek 排第一，所以只传 task_name 时
# 内容一定是 DeepSeek 生成的。用户明确要求「不要 DeepSeek，用 GM」，
# 必须用 model_name 直选具体模型（SDK 契约：传 model_name 一律直选，绕过任务优先级）。
DEFAULT_MODEL_NAME = "feiyu-gemini-flash"

# 单次生成给宿主留多久（毫秒）。
# 【真机踩坑 2026-11-06】宿主 cap.call 的默认超时只有 30000ms，写 200~400 字的
# 帖子经常跑不完，日志里直接是 `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`，
# 上层只看到「生成失败」，用户看到的就是「提示重写之后没动静」。
# SDK 的 llm.generate 支持 `timeout_ms=`，它会被当成这次 RPC 的等待预算
# （life-frequency 插件里同款用法），这里给足 180 秒。
DEFAULT_TIMEOUT_MS = 180_000


class _Paths:
    """路径代理：同时提供 data_dir 与 state_dir（业务层两种叫法都认）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(data_dir)


class _ApiProxy:
    """A 类宿主能力代理：把 ctx.api.call(name, **kwargs) 原样透传。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def call(self, api_name: str, **kwargs: Any) -> Any:
        api = getattr(self._ctx, "api", None)
        if api is None or not hasattr(api, "call"):
            raise RuntimeError("宿主未提供 ctx.api.call 能力")
        # 真实签名是 call(api_name, *, version="", **kwargs)，直接透传即可。
        return await api.call(api_name, **kwargs)


class Runtime:
    """薄上下文：业务层唯一依赖的运行时对象。

    :param plugin: 插件实例（提供 config / store / 日志）。
    :param host_ctx: 真实 `PluginContext`；单测可传 SimpleNamespace。
    :param data_dir: 数据目录（已解析好的绝对路径）。
    """

    def __init__(self, plugin: Any, host_ctx: Any, data_dir: Path) -> None:
        self.plugin = plugin
        self.host = host_ctx
        self.config = plugin.cfg()
        self.paths = _Paths(data_dir)
        self.store = plugin.store()
        self.logger = getattr(host_ctx, "logger", logger)
        self.api = _ApiProxy(host_ctx)
        self._xhh_client: Any = None
        self._xhh_error: str = ""

    # ===================== A 类：大模型 =====================

    async def generate_text(self, prompt: str) -> str:
        """经宿主大模型生成文本（真实 SDK：ctx.llm.generate）。

        task_name 必须是宿主 model_config.toml 里已声明的任务名（见模块顶部注释）。

        【真机踩坑 2026-11-06】只传 task_name 时，宿主会按该任务 model_list 的
        顺序取模型；本机 `replyer` 是 ["ds-official", "feiyu-gemini-flash"]，
        DeepSeek 永远排第一 —— 用户改了「用 GM」也照样生成 DeepSeek 内容。
        解法：用 `model_name=` 直选具体模型（SDK 契约里 model_name 一律直选，
        即使与任务同名也不当任务解析），从而绕过任务里的优先级顺序。
        """
        llm = getattr(self.host, "llm", None)
        if llm is None or not hasattr(llm, "generate"):
            raise NotImplementedError("宿主未提供 ctx.llm.generate 能力")

        model_name, temperature, max_tokens = self._text_model_options()

        last_error: Exception | None = None
        empty_tasks: list[str] = []
        for task in FALLBACK_MODEL_TASKS:
            kwargs: dict[str, Any] = {"task_name": task, "timeout_ms": DEFAULT_TIMEOUT_MS}
            if model_name:
                kwargs["model_name"] = model_name
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            try:
                result = await llm.generate(prompt, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 换下一个任务名再试。
                last_error = exc
                self.logger.debug("回响：模型任务 %s 调用失败，换下一个：%s", task, exc)
                continue
            text = _extract_text(result)
            if text.strip():
                return text
            empty_tasks.append(task)
        if last_error is not None:
            raise last_error
        # 宿主在某些失败场景下不抛异常、只回一个空结果（真机实测：任务名不存在时就是这样）。
        # 这里必须显式报错，否则上层只会看到「生成 0 字」，误判成模型话少。
        raise RuntimeError(
            f"宿主大模型未返回内容（已尝试任务：{'、'.join(empty_tasks) or '无'}，"
            f"模型：{model_name or '跟随任务默认'}）。"
            "请检查 model_config.toml 里这些任务的 model_list 是否配了可用模型。"
        )

    def _text_model_options(self) -> tuple[str, float | None, int | None]:
        """读「模型设置」段：返回 (模型名, 温度, max_tokens)。

        配置缺失 / 读取失败时退回 DEFAULT_MODEL_NAME，绝不因为一个配置项读不到
        就把整条生成链路搞崩。
        """
        try:
            model_cfg = getattr(self.config, "model", None)
            if model_cfg is None:
                return DEFAULT_MODEL_NAME, None, None
            raw = str(getattr(model_cfg, "text_model", "") or "").strip()
            # 「跟随宿主默认」是界面上的选项，实际含义是「不指定模型」。
            name = "" if raw in ("", "跟随宿主默认") else raw
            temp = getattr(model_cfg, "temperature", None)
            tokens = getattr(model_cfg, "max_tokens", None)
            return (
                name,
                float(temp) if isinstance(temp, (int, float)) else None,
                int(tokens) if isinstance(tokens, (int, float)) and int(tokens) > 0 else None,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("回响：读取模型设置失败，退回默认模型 %s：%s", DEFAULT_MODEL_NAME, exc)
            return DEFAULT_MODEL_NAME, None, None

    # ===================== B 类：小黑盒直连客户端 =====================

    @property
    def xhh_client(self) -> Any:
        """懒加载小黑盒直连客户端（CommunityClient）。

        未登录 / 缺依赖时返回 None，由各层按「没有客户端」降级处理，
        不让「没登录」把整个插件加载搞崩。
        """
        if self._xhh_client is not None or self._xhh_error:
            return self._xhh_client
        try:
            from auth import load_credentials
            from community_client import CommunityClient

            credentials = load_credentials(self.paths.data_dir)
            self._xhh_client = CommunityClient(
                profile_id=str(self.config.basic.profile_id or "default"),
                credentials=credentials,
            )
        except Exception as exc:  # noqa: BLE001 —— 未登录 / 缺 httpx 都只是「暂无客户端」。
            self._xhh_error = f"{type(exc).__name__}: {exc}"
            self._xhh_client = None
            self.logger.info("回响：小黑盒客户端未就绪（%s），需要先扫码登录。", self._xhh_error)
        return self._xhh_client

    def reset_xhh_client(self) -> None:
        """登录成功后清掉缓存，下次访问重建客户端。"""
        self._xhh_client = None
        self._xhh_error = ""


def _extract_text(result: Any) -> str:
    """从宿主 llm.generate 的返回里取正文。

    真实 SDK 返回 dict，常见字段：text / content / response / message / answer。
    兼容 dict 里再套一层 data / result。
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result or "")
    for key in ("text", "content", "response", "answer", "message", "output"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("data", "result"):
        nested = result.get(key)
        if isinstance(nested, (dict, str)):
            text = _extract_text(nested)
            if text.strip():
                return text
    return ""


__all__ = ["Runtime", "_Paths", "_ApiProxy"]
