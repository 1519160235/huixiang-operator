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

import re
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

async def fetch_post_context(ctx: Any, post_id: str) -> Any:
    """按 post_id 回读帖子内容（标题 / 正文 / 配图），失败返回 None。

    【为什么需要】回复通知时，通知对象里只有「对方说了什么」，
    没有帖子本身的内容。不回查就只能对着评论原文硬接，接不出所以然。
    这里复用发布层的 `fetch_thread_context`（契约要求：核验一律用它，禁用 search_posts）。
    """
    pid = str(post_id or "").strip()
    if not pid:
        return None
    client = _client(ctx)
    if client is None:
        return None
    try:
        return await client.fetch_thread_context(pid)
    except Exception as exc:  # noqa: BLE001 —— 回查失败只降级，不拖垮回复流程。
        import logging

        logging.getLogger(__name__).info("回响：回查帖子内容失败（已降级）：%s", exc)
        return None


async def fetch_existing_comments(ctx: Any, post_id: str, limit: int = 8) -> list[str]:
    """拉这条帖子下**别人已经发过的**评论（纯文本，去掉表情标记）。

    【用户要求 2026-11-07】「你再看一下其他人评论的啥，你再结合一下，
    别人评论一个方面你一个人单独评论一个方面」。

    【为什么要做】真机看了评论区才发现：小黑盒评论区绝大多数是
    `来了来了[cube_喜欢]`、`看看可以`、`没搭子` 这种 2-6 字短句 + 表情，
    和我们写的「资讯稿式长句」完全不是一路 —— 这就是用户说的「生硬」。
    把真实评论喂给模型，它才知道该用什么长度和口气说话。

    接口契约（真机 2026-11-07 实测）：
      - `POST_TREE` 返回 `result.comments`，每条是 `{"comment": [ {text, user, ...} ]}`
        （文本在**嵌套的 comment 数组**里，直接读 `comments[i].text` 是空的）；
      - 评论里可能只有表情（如 `[cube_doge]`），要过滤掉，否则等于没信息。

    失败一律返回空列表，不拖垮评论主流程。
    """
    pid = str(post_id or "").strip()
    if not pid:
        return []
    client = _client(ctx)
    if client is None:
        return []
    try:
        from vendor.endpoints import EndpointName

        resp = await client._request(
            EndpointName.POST_TREE,
            params={"link_id": pid, "h_src": "", "page": 1, "limit": max(1, min(limit * 2, 30))},
        )
        payload = getattr(resp, "payload", None)
        body = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return []
        raw = body.get("comments") or body.get("items") or []
        if not isinstance(raw, list):
            return []

        out: list[str] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            # 文本在嵌套的 comment 数组里。
            inner = entry.get("comment")
            items = inner if isinstance(inner, list) else [entry]
            for one in items:
                if not isinstance(one, dict):
                    continue
                text = str(one.get("text") or one.get("content") or "").strip()
                if not text:
                    continue
                # 去掉纯表情评论（如 `[cube_doge]`），它对「别人在聊什么」没有信息量。
                bare = re.sub(r"\[[^\[\]]{1,20}\]", "", text).strip()
                if len(bare) < 2:
                    continue
                out.append(text[:80])
                if len(out) >= limit:
                    return out
        return out
    except Exception as exc:  # noqa: BLE001 —— 拉不到评论只降级，不影响发评论。
        import logging

        logging.getLogger(__name__).info("回响：拉取帖子已有评论失败（已降级）：%s", exc)
        return []


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
    # 图片地址：解析器已在 raw["image_urls"] 里给出（vendor/parsers.py），
    # 这里必须接住，否则评论/总结都拿不到图，只能对着标题瞎写。
    images = raw.get("image_urls") or raw.get("images") or []
    if not isinstance(images, list):
        images = []
    image_urls = [str(u).strip() for u in images if str(u or "").strip()]
    # 分区/话题标签：解析器已产出 section_names（topics + hashtags 合并），
    # 这里是判断「这是哪款游戏的帖子」的唯一可靠来源，不能丢。
    sections = raw.get("section_names") or []
    if not isinstance(sections, list):
        sections = []
    section_names = [str(s).strip() for s in sections if str(s or "").strip()]
    return BrowseItem(
        post_id=str(raw.get("post_id", "")),
        title=str(raw.get("title", "")),
        excerpt=str(raw.get("content", ""))[:200],
        author_name=str((author or {}).get("nickname", "")),
        heat=float(raw.get("popularity_score", 0) or 0),
        image_urls=image_urls[:6],   # 单帖最多取 6 张，够用且不撑爆上下文
        section_names=section_names[:8],
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
    # 同一个社区会同时出现在「推荐」和具体分组里（AI 既是推荐位也在盒友之家）。
    # 「推荐」只是首页聚合位，不代表归类，所以有具体分组时以具体分组为准。
    seen: dict[int, int] = {}
    for group in tree:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "").strip()
        is_recommend_slot = str(group.get("key") or "") == "rec" or group_name == "推荐"
        children = group.get("children") or group.get("sub") or group.get("topics") or group.get("list") or []
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            tid = child.get("topic_id") or child.get("id")
            name = child.get("name") or child.get("title") or ""
            # 小黑盒会用 valid=0 标记下架社区，别把它挂上去（会导致发帖被判分区错误）。
            if child.get("valid") in (0, False, "0"):
                continue
            if not (tid and name):
                continue
            topic_ids = child.get("topic_ids") or child.get("topics") or []
            parsed_ids = [int(t) for t in topic_ids if str(t).isdigit()] if isinstance(topic_ids, list) else []
            hot = child.get("hot") if isinstance(child.get("hot"), dict) else {}
            try:
                heat = float(hot.get("raw_hot_value") or 0)
            except (TypeError, ValueError):
                heat = 0.0
            pid = int(tid)
            item = Partition(
                id=pid,
                name=str(name),
                topic_ids=parsed_ids,
                group=group_name,
                heat=heat,
            )
            if pid in seen:
                idx = seen[pid]
                # 有了更具体的分组，就把「推荐」覆盖掉。
                if partitions[idx].group in {"", "推荐"} and not is_recommend_slot:
                    partitions[idx] = item
                continue
            seen[pid] = len(partitions)
            partitions.append(item)
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
    "fetch_post_context",
    "fetch_existing_comments",
]
