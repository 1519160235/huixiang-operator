# -*- coding: utf-8 -*-
"""发帖动机（为什么发）与体裁（怎么写）。

【真机缺陷 2026-11-08 用户反馈】
「为什么是发感想呢？我当时的想法是，哎他觉得好玩他就发一个，或者是发一个日记什么的。」

问题在于：插件只有一条发帖路径 —— 刷够 N 条就写一篇「感想总结」。
真实的社区用户不是这么发的：刷到个好玩的就随手发一条，有时候发张图配一句话，
有时候写点自己的日常，只有偶尔才会梳理一堆帖子写篇长的。

所以这里定义「发帖动机」，每次发帖先选**为什么发**，再决定要什么素材：

- `single`：只需要一条素材（刷到好玩的、想吐槽的、想安利的、想问的）
- `multi` ：需要一批素材（刷帖汇总，偶尔才来一次）
- `life`  ：不需要帖子素材，写自己的状态（日记/碎碎念）

动机按「最久没用过」轮换，并给 `multi`（汇总）加配额 —— 默认每 3 篇最多 1 篇汇总，
其余都是单条驱动，这样才像个人在发帖，而不是每天交作业。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

# 每种动机的素材类型与写法。
POST_FORMATS: tuple[dict[str, Any], ...] = (
    {
        "id": "fun_share",
        "name": "刷到好玩的",
        "material": "single",
        "instruction": (
            "你刚刷到一条挺有意思的帖子，想随手发一条说说它。"
            "就聊这一条，别扯别的；可以复述一下那个点，再说你的反应。"
        ),
        "title_hint": "直接点那件事或你的反应，例如「这操作我是真没想到」。",
    },
    {
        "id": "hot_take",
        "name": "想吐槽",
        "material": "single",
        "instruction": (
            "你刷到一条让你想说两句的帖子，就着它吐槽一句，态度可以有，"
            "但只打一个点，别展开成大道理。"
        ),
        "title_hint": "短、有态度，例如「这也能吹」。",
    },
    {
        "id": "ask",
        "name": "想问一句",
        "material": "single",
        "instruction": (
            "拿刷到的东西当由头，问社区一个问题。先说你的情况，再真的问出去，别自问自答。"
        ),
        "title_hint": "用问句，例如「这价格你们真会买吗」。",
    },
    {
        "id": "recommend",
        "name": "想安利",
        "material": "single",
        "instruction": (
            "刷到个你觉得不错的东西，想分享出去。说清楚好在哪、适合谁，别写成测评稿。"
        ),
        "title_hint": "给具体理由，例如「这游戏的光影值回票价」。",
    },
    {
        "id": "self_record",
        "name": "发点自己的",
        "material": "life",
        "instruction": (
            "不聊刷到的帖子，就写你自己这会儿的状态：在干嘛、什么心情、想到什么。"
            "像发朋友圈那样随手写两句，可以很短。"
        ),
        "title_hint": "以自己为主语，例如「今天什么都没干成」。",
    },
    {
        "id": "short_meme",
        "name": "就一句话",
        "material": "single",
        "instruction": (
            "两三句话的短帖，玩梗或者纯吐槽，不讲道理、不总结。允许不完整。"
        ),
        "title_hint": "一句话梗，越短越好。",
    },
    {
        "id": "daily_log",
        "name": "刷帖随手记",
        "material": "multi",
        "instruction": (
            "把这一轮刷到的东西拢一拢，写成一篇随手记。"
            "**只挑一两件最想说的**，其余一笔带过或者干脆不提，不要按顺序挨个点。"
        ),
        "title_hint": "可以带时间或动作，但最近用过这个句式就换。",
    },
    {
        "id": "compare",
        "name": "两相对比",
        "material": "multi",
        "instruction": (
            "挑两条形成反差的帖子放一起说，最后给一句你自己的判断。两条都点到，但别平均用力。"
        ),
        "title_hint": "两边都点到，例如「一个在省钱一个在烧钱」。",
    },
)

_FORMAT_BY_ID = {f["id"]: f for f in POST_FORMATS}

# 汇总类（要一批素材）的配额：最近 3 篇里最多 1 篇汇总。
# 真实用户不会天天交总结作业，多数时候是刷到什么说什么。
MULTI_WINDOW = 3
MULTI_LIMIT = 1


def get_format(format_id: str) -> dict[str, Any]:
    """按 id 取动机；未知 id 退回第一个。"""
    return _FORMAT_BY_ID.get(str(format_id or ""), POST_FORMATS[0])


def _is_multi(fmt: dict[str, Any]) -> bool:
    return str(fmt.get("material", "")) == "multi"


def pick_format(recent_formats: Sequence[str], *, has_material: bool = True) -> dict[str, Any]:
    """挑一个最近没用过的动机。

    :param recent_formats: 最近用过的动机 id（旧的在前）。
    :param has_material: 有没有待总结的帖子素材。没有就只从 `life` 类里挑。

    规则：
    1. 没有帖子素材 → 只考虑 `life`（自己的状态），不硬编刷帖内容；
    2. 最近 3 篇里已经有汇总（multi）→ 这次排掉汇总；
    3. 其余按「最久没用过」排序，取第一个。
    """
    recent = [str(x or "") for x in (recent_formats or []) if str(x or "")]
    pool = [f for f in POST_FORMATS if has_material or f["material"] == "life"]
    if not pool:
        pool = list(POST_FORMATS)

    window = recent[-MULTI_WINDOW:]
    multi_used = sum(1 for fid in window if _is_multi(_FORMAT_BY_ID.get(fid, {})))
    if multi_used >= MULTI_LIMIT:
        narrowed = [f for f in pool if not _is_multi(f)]
        if narrowed:
            pool = narrowed

    used = set(recent[-len(pool):])
    for fmt in pool:
        if fmt["id"] not in used:
            return fmt

    last_pos: dict[str, int] = {}
    for idx, fid in enumerate(recent):
        last_pos[fid] = idx
    return min(pool, key=lambda f: last_pos.get(f["id"], -1))


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", str(text or ""))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def title_similarity(a: str, b: str) -> float:
    """标题相似度（字符二元组 Jaccard，0~1）。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def similar_title(title: str, recent_titles: Sequence[str], threshold: float = 0.55) -> str:
    """返回最近标题里跟它最像的那条（超过阈值才算）；没有就返回空串。"""
    best = ""
    best_score = 0.0
    for old in recent_titles or []:
        score = title_similarity(title, old)
        if score > best_score:
            best, best_score = str(old), score
    return best if best_score >= threshold else ""


def format_brief(fmt: dict[str, Any]) -> str:
    """拼给模型看的一段话：这篇为什么发、怎么写、标题往哪起。"""
    return (
        f"这一篇是「{fmt.get('name', '')}」：{fmt.get('instruction', '')}\n"
        f"标题方向：{fmt.get('title_hint', '')}\n"
    )


def recent_titles_block(recent_titles: Sequence[str]) -> str:
    """把最近发过的标题列给模型，明确禁止重复或换汤不换药。"""
    rows = [str(t).strip() for t in (recent_titles or []) if str(t).strip()]
    if not rows:
        return ""
    listed = "\n".join(f"- {t}" for t in rows[:6])
    return (
        "\n你最近已经发过这几篇，标题分别是：\n"
        f"{listed}\n"
        "**这一篇的标题不许和上面任何一条重复，也不许只改一两个字换汤不换药**；"
        "句式、开头、落点都要换。正文也别再重复它们聊过的角度。\n"
    )


__all__ = [
    "POST_FORMATS",
    "MULTI_WINDOW",
    "MULTI_LIMIT",
    "get_format",
    "pick_format",
    "title_similarity",
    "similar_title",
    "format_brief",
    "recent_titles_block",
]
