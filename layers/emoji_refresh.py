# -*- coding: utf-8 -*-
"""表情白名单自动扩充。

【为什么要这个】
小黑盒会持续上新表情。代码里写死的白名单迟早过期 —— 过期后新表情一律被当成
「编造的名字」删掉，用户会觉得「怎么又不带表情了」。

【怎么采】
表情名不是我们猜的，是**从真实评论里采样**的：普通用户能发出来、能正常显示的表情，
名字一定在客户端内置表里。扫最近刷到的帖子评论区，把出现的 `[cube_xxx]` 收进白名单即可。

只增不减：某次采样没出现不代表表情失效（可能只是没人用），所以从不删除已有条目。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from layers import emojis

logger = logging.getLogger(__name__)

MARK_RE = re.compile(r"\[cube_[^\]\s]{1,24}\]")


def _data_dir(ctx: Any) -> str:
    paths = getattr(ctx, "paths", None)
    return str(getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None) or "")


async def _post_comments(client: Any, post_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """拉一个帖子的评论（只读）。失败返回空列表。"""
    from vendor.endpoints import EndpointName

    try:
        resp = await client._request(
            EndpointName.POST_TREE,
            params={"link_id": post_id, "page": 1, "limit": max(1, min(limit, 50))},
        )
    except Exception as exc:  # noqa: BLE001 —— 单个帖子失败不影响整体采样。
        logger.debug("回响：采样表情时拉取帖子 %s 失败：%s", post_id, exc)
        return []
    body = resp.payload.get("result") or {}
    rows: list[dict[str, Any]] = []
    for group in body.get("comments") or []:
        if isinstance(group, dict) and isinstance(group.get("comment"), list):
            rows.extend([x for x in group["comment"] if isinstance(x, dict)])
    return rows


async def refresh_once(ctx: Any, *, posts: int = 8, comments_per_post: int = 30) -> dict[str, Any]:
    """扫最近刷到的帖子评论，把新出现的表情名并入白名单。

    :return: {"ok", "scanned", "found", "added"}
    """
    client = getattr(ctx, "xhh_client", None)
    if client is None:
        return {"ok": False, "code": "no_client", "message": "未登录，无法采样表情"}

    digest = getattr(getattr(ctx, "store", None), "digest", None)
    if digest is None:
        return {"ok": False, "code": "no_digest", "message": "浏览摘要存储未初始化"}

    items = []
    getter = getattr(digest, "recent_post_ids", None)
    if callable(getter):
        items = [{"post_id": pid} for pid in getter(max(1, posts))]
    else:
        items = list(digest.pending(limit=max(1, posts)))
    post_ids = [str(it.get("post_id", "")) for it in items if isinstance(it, dict) and it.get("post_id")]
    if not post_ids:
        return {"ok": False, "code": "no_posts", "message": "还没刷到帖子，没有采样样本"}

    found: set[str] = set()
    for pid in post_ids:
        for row in await _post_comments(client, pid, comments_per_post):
            for mark in MARK_RE.findall(str(row.get("text") or "")):
                found.add(mark.strip("[]"))

    added = emojis.merge_allowed(sorted(found), _data_dir(ctx))
    if added:
        logger.info("回响：表情白名单新增 %d 个（%s）", len(added), "、".join(added[:6]))
    return {
        "ok": True,
        "scanned": len(post_ids),
        "found": len(found),
        "added": len(added),
        "names": added,
    }


__all__ = ["refresh_once", "MARK_RE"]
