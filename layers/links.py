# -*- coding: utf-8 -*-
"""
回响插件：外围联动层（阶段 7 落地）。

【职责边界】
把「已经发生的事」播报出去，不参与任何判定、不改内容、不影响发帖闭环：
- 语音播报（voice_broadcast）
- QQ 空间同步（qzone_sync）
- 日记联动（diary_link）

【依赖口径】
三个出口全部属于 A 类宿主能力，统一走 `ctx.api.call(插件ID.名称, 参数)` 复用现有插件，
接口名常量登记在 contracts.py。任一出口失败只记中文日志，绝不向上抛异常——
「逐个卸掉联动，发帖闭环仍然通」是阶段 7 的验收线。

【开关】
[links] 段三个开关默认全关；关闭的出口连 api.call 都不会发起，直接短路返回「已关闭」。
"""

from __future__ import annotations

import logging
from typing import Any

import contracts
from layers._cfg import read_bool as _read_bool, section as _section

logger = logging.getLogger(__name__)


def link_enabled(ctx: Any, name: str) -> bool:
    """读取单个联动开关（默认 false）。"""
    return _read_bool(_section(ctx, "links"), name, False)


def enabled_links(ctx: Any) -> list[str]:
    """返回当前开启的联动出口名清单（顺序固定，便于测试与展示）。"""
    names = ("voice_broadcast", "qzone_sync", "diary_link")
    return [name for name in names if link_enabled(ctx, name)]


def _call_api(ctx: Any) -> Any:
    api = getattr(ctx, "api", None)
    if api is None or not hasattr(api, "call"):
        return None
    return api


def build_broadcast_text(title: str, post_id: str = "", extra: str = "") -> str:
    """拼播报文案（纯逻辑，可测）：全中文，带帖子 ID 便于回查。"""
    parts = [f"刚在小黑盒发了一条：{title}".strip()]
    if post_id:
        parts.append(f"帖子号 {post_id}")
    if extra:
        parts.append(str(extra).strip())
    return "，".join(p for p in parts if p)


async def _notify(ctx: Any, api_name: str, label: str, payload: dict[str, Any]) -> dict[str, Any]:
    """统一调用一个通知出口：关闭 → 短路；失败 → 只记日志。"""
    api = _call_api(ctx)
    if api is None:
        logger.debug("回响：宿主未提供 ctx.api.call，%s 联动跳过。", label)
        return {"ok": False, "code": "no_api_channel", "message": f"宿主无 A 类调用通道，{label}跳过"}
    try:
        await api.call(api_name, **payload)
        return {"ok": True, "code": "sent", "message": f"{label}已发出"}
    except Exception as exc:  # noqa: BLE001 —— 联动失败绝不阻断主闭环。
        logger.warning("回响：%s 联动失败（已忽略）：%s", label, exc)
        return {"ok": False, "code": "link_failed", "message": f"{label}失败：{type(exc).__name__}"}


async def broadcast_voice(ctx: Any, text: str) -> dict[str, Any]:
    """语音播报（[links].voice_broadcast）。"""
    if not link_enabled(ctx, "voice_broadcast"):
        return {"ok": False, "code": "disabled", "message": "语音播报未开启"}
    return await _notify(ctx, contracts.API_NOTIFY_VOICE, "语音播报", {"text": text})


async def sync_qzone(ctx: Any, text: str, post_id: str = "") -> dict[str, Any]:
    """QQ 空间同步（[links].qzone_sync）。"""
    if not link_enabled(ctx, "qzone_sync"):
        return {"ok": False, "code": "disabled", "message": "QQ 空间同步未开启"}
    return await _notify(ctx, contracts.API_NOTIFY_QQZONE, "QQ 空间同步", {"text": text, "post_id": post_id})


async def write_diary(ctx: Any, text: str, tag: str = "小黑盒") -> dict[str, Any]:
    """日记联动（[links].diary_link）。"""
    if not link_enabled(ctx, "diary_link"):
        return {"ok": False, "code": "disabled", "message": "日记联动未开启"}
    return await _notify(ctx, contracts.API_DIARY_LINK, "日记联动", {"text": text, "tag": tag})


async def announce_published(ctx: Any, title: str, post_id: str = "", summary: str = "") -> dict[str, Any]:
    """发帖成功后的统一播报：按开启的开关逐个调用，互不影响。

    :return: {"enabled": [...], "results": {出口名: 结果}}
    """
    names = enabled_links(ctx)
    if not names:
        return {"enabled": [], "results": {}}
    text = build_broadcast_text(title, post_id, summary)
    results: dict[str, Any] = {}
    if "voice_broadcast" in names:
        results["voice_broadcast"] = await broadcast_voice(ctx, text)
    if "qzone_sync" in names:
        results["qzone_sync"] = await sync_qzone(ctx, text, post_id)
    if "diary_link" in names:
        results["diary_link"] = await write_diary(ctx, text)
    return {"enabled": names, "results": results}


__all__ = [
    "link_enabled",
    "enabled_links",
    "build_broadcast_text",
    "broadcast_voice",
    "sync_qzone",
    "write_diary",
    "announce_published",
]
