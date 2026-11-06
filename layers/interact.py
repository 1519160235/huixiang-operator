# -*- coding: utf-8 -*-
"""
回响插件：互动层（阶段 4 落地，六块编排的「互动」子流水线）。

【职责边界】
只编排既有六块，不新增业务能力：把接入层拉到的通知 / 浏览项，串成
「创作层生成 → 护栏层判定 → 发布层发送」的互动闭环。两条子流水线：
1. respond_to_notifications：收 @ / 回复通知 → 去重 → 生成回复 → 过评论护栏 → 回帖（楼中楼带 rootId）。
2. interact_with_feeds：主动浏览一批帖子 → 挑可评论项 → 生成评论 → 过评论护栏 → 发顶层评论。

【纯逻辑（可测）】
- should_reply_notification：按 reply_mentions / reply_replies 决定是否响应某条通知。
- pick_comment_targets：从浏览项按 post_id 去重、随机抽样出要评论的帖子。
"""

from __future__ import annotations

import random
from typing import Any

from models import CommentTarget, RunMode

from . import create, guard, inbound, publish, sense
from ._cfg import read_bool as _read_bool, read_int as _read_int, read_list as _read_list, read_str as _read_str, section as _section


def _interact_flags(ctx: Any) -> tuple[bool, bool]:
    section = _section(ctx, "interact")
    return _read_bool(section, "reply_mentions", True), _read_bool(section, "reply_replies", True)


def _comment_enabled(ctx: Any) -> bool:
    return _read_bool(_section(ctx, "interact"), "comment_enabled", True)


def _max_comments_per_round(ctx: Any) -> int:
    return _read_int(_section(ctx, "interact"), "max_comments_per_round", 3)


def _browse_args(ctx: Any) -> tuple[str, list[str], int]:
    browse = _section(ctx, "browse")
    return (
        _read_str(browse, "partition_mode", "all"),
        _read_list(browse, "selected_partitions", []),
        _read_int(browse, "posts_per_round", 10),
    )


# ===================== 纯逻辑 =====================

def should_reply_notification(notif: Any, reply_mentions: bool = True, reply_replies: bool = True) -> bool:
    """按开关决定是否响应某条通知。"""
    event_type = str(getattr(notif, "event_type", "") or "")
    if event_type == "mention":
        return bool(reply_mentions)
    if event_type == "reply":
        return bool(reply_replies)
    return False  # proactive_feed 等非回复类通知不响应


def pick_comment_targets(
    browse_items: list[Any], max_comments: int = 3, rng: random.Random | None = None
) -> list[Any]:
    """从浏览项挑出要评论的帖子：按 post_id 去重，随机抽样 max_comments 个。"""
    rng = rng or random.Random()
    unique: list[Any] = []
    seen: set[str] = set()
    for item in browse_items:
        pid = str(getattr(item, "post_id", "") or "")
        if pid and pid not in seen:
            seen.add(pid)
            unique.append(item)
    if len(unique) <= max_comments:
        return unique
    return rng.sample(unique, max_comments)


# ===================== 编排 =====================

async def respond_to_notifications(ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
    """收 @ / 回复通知 → 去重 → 生成回复 → 过评论护栏 → 回帖（楼中楼带 rootId）。"""
    store = getattr(ctx, "store", None)
    notification_store = getattr(store, "notification", None) if store is not None else None
    reply_mentions, reply_replies = _interact_flags(ctx)
    notifs: list[Any] = []
    if reply_mentions:
        notifs.extend(await inbound.fetch_notifications(ctx, "mention"))
    if reply_replies:
        notifs.extend(await inbound.fetch_notifications(ctx, "reply"))
    trace = await sense.read_life_trace(ctx)
    handled = 0
    for notif in notifs:
        nid = str(getattr(notif, "notification_id", "") or getattr(notif, "external_event_id", "") or "")
        if not nid:
            continue
        if notification_store is not None and notification_store.seen(nid):
            continue
        if not should_reply_notification(notif, reply_mentions, reply_replies):
            if notification_store is not None:
                notification_store.mark(nid)  # 不需要响应的通知也标记，避免反复拉取
            continue
        target = CommentTarget(post_id=str(getattr(notif, "post_id", "") or ""))
        text = await create.generate_comment(ctx, target, trace)
        score = create.ai_flavor_score(text)
        verdict = await guard.is_commentable(ctx, text, score, mode, action="reply")
        if not verdict.ok:
            continue
        await publish.reply_notification(ctx, notif, text, dry_run=(mode is RunMode.DRY_RUN))
        if notification_store is not None:
            notification_store.mark(nid)
        if mode is not RunMode.DRY_RUN:
            await guard.reserve_quota(ctx, mode, "reply")
        handled += 1
    return {"ok": True, "handled": handled}


async def interact_with_feeds(ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
    """主动浏览一批帖子 → 挑可评论项 → 生成评论 → 过评论护栏 → 发顶层评论。"""
    if not _comment_enabled(ctx):
        return {"comments_sent": 0}
    store = getattr(ctx, "store", None)
    partition_cache = getattr(store, "partition", None) if store is not None else None
    partitions = await inbound.refresh_partitions(ctx, partition_cache)
    partition_mode, selected, posts_per_round = _browse_args(ctx)
    items = await inbound.browse_feeds(
        ctx, partitions, partition_mode=partition_mode, selected_partitions=selected, posts_per_round=posts_per_round
    )
    if not items:
        return {"comments_sent": 0}
    digest = getattr(store, "digest", None) if store is not None else None
    if digest is not None:
        digest.add_many(items)
    targets = pick_comment_targets(items, _max_comments_per_round(ctx))
    trace = await sense.read_life_trace(ctx)
    sent = 0
    for item in targets:
        target = CommentTarget(post_id=str(getattr(item, "post_id", "") or ""))
        if not target.post_id:
            continue
        text = await create.generate_comment(ctx, target, trace)
        score = create.ai_flavor_score(text)
        verdict = await guard.is_commentable(ctx, text, score, mode, action="comment")
        if not verdict.ok:
            continue
        await publish.publish_comment(ctx, target, text, dry_run=(mode is RunMode.DRY_RUN))
        if mode is not RunMode.DRY_RUN:
            await guard.reserve_quota(ctx, mode, "comment")
        sent += 1
    return {"comments_sent": sent}


async def respond_and_interact(ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
    """互动主入口：先响应通知，再主动浏览并互动评论。"""
    notification_result = await respond_to_notifications(ctx, mode)
    browse_result = await interact_with_feeds(ctx, mode)
    return {
        "ok": True,
        "notifications_handled": notification_result.get("handled", 0),
        "comments_sent": browse_result.get("comments_sent", 0),
    }


__all__ = [
    "should_reply_notification",
    "pick_comment_targets",
    "respond_to_notifications",
    "interact_with_feeds",
    "respond_and_interact",
]
