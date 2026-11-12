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

import inspect
import logging
import random
from typing import Any

from models import CommentTarget, RunMode

from . import cooldown, create, guard, inbound, publish, sense
logger = logging.getLogger(__name__)

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

async def _reply_target(ctx: Any, notif: Any) -> CommentTarget:
    """把一条通知整理成「带上下文的回复目标」。

    通知本身只有「谁说了什么」，没有帖子内容。这里回查帖子，
    再按需识别配图，让回复能真正接上话。
    【真机缺陷 2026-11-06】原实现只取 post_id 就去生成回复，
    提示词里既没有帖子内容也没有对方评论，回复必然跑题。
    """
    post_id = str(getattr(notif, "post_id", "") or "")
    target = CommentTarget(
        post_id=post_id,
        # 对方说的那句话：回复要接得住，必须原样带给模型。
        comment_text=str(getattr(notif, "content", "") or ""),
        author_name=str(getattr(notif, "sender_nickname", "") or ""),
        root_id=str(getattr(notif, "root_comment_id", "") or "") or None,
        parent_id=str(getattr(notif, "parent_comment_id", "") or "") or None,
    )

    post_ctx = await inbound.fetch_post_context(ctx, post_id)
    if post_ctx is not None:
        target.title = str(getattr(post_ctx, "title", "") or "")
        target.body = str(getattr(post_ctx, "body", "") or "")
        images = list(getattr(post_ctx, "image_urls", []) or [])
        if images and len(target.body) < 30:
            describe = getattr(ctx, "describe_image", None)
            if callable(describe):
                try:
                    got = describe(images[0], target.title)
                    if inspect.isawaitable(got):
                        got = await got
                    target.image_desc = str(got or "").strip()
                except Exception:  # noqa: BLE001 —— 识图失败按没有图处理。
                    target.image_desc = ""
    # 回复同样要说圈内话：认游戏 + 捞宿主的黑话。
    # 通知回查拿不到分区标签，就靠标题/正文/识图描述推断。
    target.game_name = create._guess_game_name([], target.title, target.body, target.image_desc)
    lookup = getattr(ctx, "look_up_jargon", None)
    if callable(lookup):
        try:
            got = lookup([target.title, target.body, target.image_desc, target.comment_text])
            if inspect.isawaitable(got):
                got = await got
            target.jargon_hint = str(got or "").strip()
        except Exception:  # noqa: BLE001
            target.jargon_hint = ""
    return target


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
    # 【真机缺陷 2026-11-09 用户报障「一个评论重复了好几个，可能是别人连续评论了好多」】
    # 同一个人短时间内连发几条，会推来好几条通知。原来逐条独立回复，
    # 于是同一个帖子里出现「谢谢」「谢谢」「谢谢」这种连珠炮 —— 真人不会这么说话。
    # 现在的规矩：
    #   1. 同一个 (帖子, 楼层, 人) 这一轮只回一次，后面的先不追；
    #   2. 本轮已经说过的句子，生成时明确告诉模型「这些说过了，换个说法」。
    replied_keys: set[tuple[str, str, str]] = set()
    said_this_round: list[str] = []
    sent_items: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
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

        # 同一帖子里同一个人连着发的，只在第一层楼回一次，别一句一句追着说。
        key = (
            str(getattr(notif, "post_id", "") or ""),
            str(getattr(notif, "root_comment_id", "") or ""),
            str(getattr(notif, "sender_uid", "") or ""),
        )
        if key[0] and key in replied_keys:
            logger.info("回响：同一楼层这个人已经回过了，跳过这条（避免连珠炮）")
            if notification_store is not None:
                notification_store.mark(nid)
            continue

        target = await _reply_target(ctx, notif)
        text = await create.generate_comment(
            ctx, target, trace, for_reply=True, avoid_texts=said_this_round
        )
        if not str(text or "").strip():
            continue
        if key[0]:
            replied_keys.add(key)
        said_this_round.append(str(text).strip())
        score = create.ai_flavor_score(text)
        verdict = await guard.is_commentable(ctx, text, score, mode, action="reply")
        if not verdict.ok:
            continue
        try:
            await publish.reply_notification(ctx, notif, text, dry_run=(mode is RunMode.DRY_RUN))
        except Exception as exc:  # noqa: BLE001 —— 一条发不出去不该拖垮整轮互动。
            reason = str(exc)[:160] or type(exc).__name__
            logger.warning("回响：回复通知发送失败：%s", reason)
            skipped.append({"post_id": key[0], "reason": f"发送被拒：{reason}"})
            if cooldown.classify(reason):
                cooldown.record_rejection(ctx, reason)
                break  # 平台层面发不了，剩下的通知别再白生成一遍。
            continue
        if notification_store is not None:
            notification_store.mark(nid)
        if mode is not RunMode.DRY_RUN:
            await guard.reserve_quota(ctx, mode, "reply")
        handled += 1
        sent_items.append(
            {
                "post_id": str(getattr(notif, "post_id", "") or ""),
                "text": str(text)[:200],
                "kind": "reply",
            }
        )
    return {"ok": True, "handled": handled, "sent_items": sent_items, "skipped": skipped}


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
    # 【真机缺陷 2026-11-09 用户报障「有的时候评论记录里说正文没生成」】
    # 原来每条跳过都只是 `continue`，上层只看到 comments_sent=0，什么原因都不知道。
    # 现在把每次跳过记下来，用户能在记录里看到「这条为什么没评」。
    skipped: list[dict[str, str]] = []
    sent_items: list[dict[str, str]] = []

    def _skip(post_id: str, title: str, why: str) -> None:
        skipped.append({"post_id": post_id, "title": str(title or "")[:40], "reason": why})
        logger.info("回响：跳过评论（%s）：%s", why, str(title or post_id)[:40])

    for item in targets:
        # 先把帖子的标题 / 正文 / 配图内容凑齐，再交给模型写评论。
        # 【真机缺陷 2026-11-06】原来这里只传了一个 post_id，提示词里没有任何
        # 帖子内容，模型只能凭空编，导致「回复跟帖子一点关系都没有」。
        target = await create.enrich_comment_target(ctx, item)
        if not target.post_id:
            _skip("", getattr(item, "title", ""), "拿不到帖子 id")
            continue
        if not (target.title or target.body or target.image_desc):
            # 一点素材都没有，宁可跳过也不发无关内容。
            _skip(target.post_id, target.title, "帖子没有标题/正文/配图，没东西可接")
            continue
        text = await create.generate_comment(ctx, target, trace)
        if not str(text or "").strip():
            # 【2026-11-09】模型返回空、或清洗后为空（表情把整句吃掉了），
            # 以前只说「没生成」，现在说清是哪一种。
            _skip(target.post_id, target.title, "模型没返回正文（或清洗后为空）")
            continue
        score = create.ai_flavor_score(text)
        verdict = await guard.is_commentable(ctx, text, score, mode, action="comment")
        if not verdict.ok:
            _skip(target.post_id, target.title, f"护栏拦截：{verdict.message}")
            continue
        try:
            await publish.publish_comment(ctx, target, text, dry_run=(mode is RunMode.DRY_RUN))
        except Exception as exc:  # noqa: BLE001 —— 记录原因并继续下一条，别让整轮 ok=False。
            reason = str(exc)[:160] or type(exc).__name__
            _skip(target.post_id, target.title, f"发送被拒：{reason}")
            if cooldown.classify(reason):
                # 【2026-11-12】评论和发帖被同一账号限制挡住时，共用冷却：
                # 冷却期内连评论文本都不再生成，省 LLM 费用。
                cooldown.record_rejection(ctx, reason)
                break
            continue
        if mode is not RunMode.DRY_RUN:
            await guard.reserve_quota(ctx, mode, "comment")
        sent += 1
        # 【2026-11-10】评论发出去要留全文：这次「本体」翻车是靠事后扒接口才还原的，
        # 记录里只有 comments_sent=1，连评了哪条、说了什么都不知道。
        sent_items.append(
            {
                "post_id": target.post_id,
                "title": str(target.title or "")[:40],
                "text": str(text)[:200],
            }
        )
    return {"comments_sent": sent, "skipped": skipped, "sent_items": sent_items}


async def respond_and_interact(ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
    """互动主入口：先响应通知，再主动浏览并互动评论。"""
    # 【2026-11-12】平台把评论/发帖都回绝时，整轮直接跳过：
    # 否则每 2 分钟生成一批注定发不出去的评论，白扣 LLM 费用、记录里还只有一句类名。
    if mode is not RunMode.DRY_RUN:
        blocked, why = cooldown.is_blocked(ctx)
        if blocked:
            return {
                "ok": False, "code": "publish_cooldown", "message": why,
                "notifications_handled": 0, "comments_sent": 0,
                "skipped": [], "sent_items": [],
            }
    notification_result = await respond_to_notifications(ctx, mode)
    browse_result = await interact_with_feeds(ctx, mode)
    return {
        "ok": True,
        "notifications_handled": notification_result.get("handled", 0),
        "comments_sent": browse_result.get("comments_sent", 0),
        "skipped": (notification_result.get("skipped") or [])
        + (browse_result.get("skipped") or []),
        "sent_items": (notification_result.get("sent_items") or [])
        + (browse_result.get("sent_items") or []),
    }


__all__ = [
    "should_reply_notification",
    "pick_comment_targets",
    "respond_to_notifications",
    "interact_with_feeds",
    "respond_and_interact",
]
