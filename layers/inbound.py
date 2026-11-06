# -*- coding: utf-8 -*-
"""
回响插件：接入层（六块第一块，落地版）。

【职责边界】
接入层负责「与小黑盒的会话与信息流接入」：
- 扫码登录与登录态检查（委托 auth.py）；
- 收 @ / 回复通知（B 类直连，阶段 4 落地，区分 @ 与回复）；
- 主动浏览（全分区走推荐流 FEED，指定分区走 TOPIC_FEED）；
- 分区目录拉取 + 定时刷新（保证分区目录最新，不写死 Literal）。

【依赖口径】
B 类直连：通过 ctx.xhh_client（CommunityClient）走 vendor 直连客户端，不经 MCP 8757 中转。
分区动态拉取统一走 TOPIC_INDEX → topics_list 树，不写死 Literal。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from models import BrowseItem, Cursor, Partition
from layers._cfg import norm_choice as _norm_choice, read_int as _read_int, section as _section


def _data_dir(ctx: Any) -> Path:
    paths = getattr(ctx, "paths", None)
    if paths is not None:
        d = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
        if d:
            return Path(d)
    return Path(".")


def _client(ctx: Any) -> Any:
    return getattr(ctx, "xhh_client", None)


# ===================== 登录 =====================

async def login_qr(ctx: Any) -> Any:
    """发起扫码登录（委托 auth.QrLoginService），返回登录状态快照。"""
    from auth import QrLoginService

    directory = _data_dir(ctx)
    service = getattr(ctx, "qr_service", None)
    if service is None:
        service = QrLoginService(directory=directory)
        ctx.qr_service = service
    await service.start()
    return service.snapshot()


async def check_login_state(ctx: Any) -> Any:
    """检查当前登录态是否有效（委托 auth.load_credentials，失败返回 None）。"""
    from auth import load_credentials

    try:
        return load_credentials(_data_dir(ctx))
    except Exception:
        return None


# ===================== 通知（阶段 4 落地） =====================

async def fetch_notifications(ctx: Any, event_type: str = "reply", cursor: Cursor | None = None) -> list[Any]:
    """拉取 @ / 回复通知（B 类直连），返回 vendor.models.Notification 对象列表。

    :param event_type: "mention"（@，message_type=16）或 "reply"（回复，list_type=0）。
    """
    client = _client(ctx)
    if client is None:
        return []
    from vendor.models import NotificationType

    vtype = NotificationType.MENTION if event_type == "mention" else NotificationType.REPLY
    try:
        page = await client.fetch_notifications(vtype, cursor=str(cursor.value) if cursor else "")
    except Exception:
        return []
    result: list[Any] = []
    for wrapper in page.items:
        if isinstance(wrapper, dict):
            notif = wrapper.get("notification")
            if notif is not None:
                result.append(notif)
    return result


# ===================== 浏览（阶段 4 落地：分区过滤） =====================

def resolve_browse_partitions(
    partitions: list[Partition],
    partition_mode: str = "all",
    selected_partitions: list[str] | None = None,
) -> list[Partition]:
    """按配置决定要浏览哪些分区（纯逻辑，可测）。

    all：返回全部分区（不限制）。
    selected：只返回 selected_partitions 指定的分区（按 id 或准确名称匹配，名称以运行时目录为准）。
    勾选了 selected 但没填清单时，退回全部分区，避免「什么也不刷」。

    注意：partition_mode 可能来自配置界面（中文「全部分区」/「仅指定分区」），
    也可能来自老配置（all / selected），统一用 norm_choice 归一化。
    """
    mode = _norm_choice("partition_mode", partition_mode, "all")
    if mode != "selected":
        return list(partitions)
    selected = [str(s).strip() for s in (selected_partitions or []) if str(s).strip()]
    if not selected:
        return list(partitions)
    result: list[Partition] = []
    for p in partitions:
        if str(p.id) in selected or p.name in selected:
            result.append(p)
    return result


def _to_browse_item(raw: dict[str, Any]) -> BrowseItem:
    author = raw.get("author") or {}
    return BrowseItem(
        post_id=str(raw.get("post_id", "")),
        title=str(raw.get("title", "")),
        excerpt=str(raw.get("content", ""))[:200],
        author_name=str((author or {}).get("nickname", "")),
        heat=float(raw.get("popularity_score", 0) or 0),
        raw=raw,
    )


async def _browse_partition(ctx: Any, partition: Partition, limit: int = 10) -> list[BrowseItem]:
    """拉指定分区的动态流（TOPIC_FEED），失败返回空、不抛异常。"""
    client = _client(ctx)
    if client is None:
        return []
    from vendor.endpoints import EndpointName
    from vendor.parsers import ResponseShapeError, parse_feed

    try:
        response = await client._request(
            EndpointName.TOPIC_FEED,
            params={"topic_id": partition.id, "limit": max(1, limit), "offset": 0},
        )
        raw_items = parse_feed(response.payload).items
    except (Exception, ResponseShapeError):  # noqa: BLE001 —— 分区流拉取失败只降级返回空。
        return []
    return [_to_browse_item(raw) for raw in raw_items if isinstance(raw, dict)]


async def browse_feeds(
    ctx: Any,
    partitions: list[Partition],
    cursor: Cursor | None = None,
    partition_mode: str = "all",
    selected_partitions: list[str] | None = None,
    posts_per_round: int = 10,
) -> list[BrowseItem]:
    """主动浏览分区动态。

    all：走推荐流 FEED（不限制分区）；selected：只刷勾选的分区（逐个 TOPIC_FEED）。
    partition_mode 兼容中文界面值与老英文值。
    """
    client = _client(ctx)
    if client is None:
        return []
    mode = _norm_choice("partition_mode", partition_mode, "all")
    target = resolve_browse_partitions(partitions, mode, selected_partitions)
    if mode == "selected" and target:
        items: list[BrowseItem] = []
        for p in target:
            items.extend(await _browse_partition(ctx, p, posts_per_round))
        return items
    offset = 0
    if cursor is not None and cursor.value.isdigit():
        offset = int(cursor.value)
    try:
        page = await client.fetch_feed(offset=offset)
    except Exception:  # noqa: BLE001 —— 推荐流拉取失败降级返回空。
        return []
    return [_to_browse_item(raw) for raw in page.items if isinstance(raw, dict)]


# ===================== 分区目录（动态拉取 + 定时刷新） =====================

async def fetch_partitions(ctx: Any) -> list[Partition]:
    """拉取分区目录（TOPIC_INDEX → topics_list 树，动态拉取、不写死 Literal）。"""
    client = _client(ctx)
    if client is None:
        return []
    from vendor.endpoints import EndpointName

    try:
        response = await client._request(EndpointName.TOPIC_INDEX, params={"type": "list"})
    except Exception:  # noqa: BLE001 —— 拉取失败降级返回空。
        return []
    payload = response.payload
    result = payload.get("result", payload.get("data", payload))
    if not isinstance(result, dict):
        return []
    tree = result.get("topics_list") or result.get("list") or result.get("topics")
    if not isinstance(tree, list):
        return []
    partitions: list[Partition] = []
    for group in tree:
        if not isinstance(group, dict):
            continue
        children = group.get("children") or group.get("sub") or group.get("topics") or group.get("list") or []
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            tid = child.get("topic_id") or child.get("id")
            name = child.get("name") or child.get("title") or ""
            if tid and name:
                topic_ids = child.get("topic_ids") or child.get("topics") or []
                parsed_ids = [int(t) for t in topic_ids if str(t).isdigit()] if isinstance(topic_ids, list) else []
                partitions.append(Partition(id=int(tid), name=str(name), topic_ids=parsed_ids))
    return partitions


def should_refresh_partitions(last_refresh_at: str, interval_minutes: int, now: float | None = None) -> bool:
    """判断分区目录是否该定时刷新（纯逻辑，可测）。

    :param last_refresh_at: 上次刷新时间（ISO 字符串，state.now_iso 格式）。
    :param interval_minutes: 刷新间隔（分钟）。
    :param now: 当前时间戳（测试注入用），缺省用 time.time()。
    """
    if not last_refresh_at:
        return True
    now_ts = now if now is not None else time.time()
    try:
        last_ts = _parse_iso_to_ts(last_refresh_at)
    except (TypeError, ValueError, OverflowError):
        return True
    return (now_ts - last_ts) >= max(0, int(interval_minutes)) * 60


def _parse_iso_to_ts(value: str) -> float:
    text = str(value).replace("Z", "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return datetime.fromisoformat(text).timestamp()


async def refresh_partitions(ctx: Any, cache: Any = None, force: bool = False) -> list[Partition]:
    """定时重测分区，保证分区目录最新，并回写缓存；未到刷新间隔时直接读缓存。"""
    interval = _read_int(_section(ctx, "browse"), "refresh_partitions_interval_minutes", 360)
    if not force and cache is not None:
        last = cache.last_refresh_at() if hasattr(cache, "last_refresh_at") else ""
        if last and not should_refresh_partitions(last, interval):
            cached = cache.get() if hasattr(cache, "get") else []
            if cached:
                return cached
    partitions = await fetch_partitions(ctx)
    if cache is not None and hasattr(cache, "set"):
        cache.set(partitions)
    return partitions


__all__ = [
    "login_qr",
    "check_login_state",
    "fetch_notifications",
    "resolve_browse_partitions",
    "browse_feeds",
    "fetch_partitions",
    "should_refresh_partitions",
    "refresh_partitions",
]
