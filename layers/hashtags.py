# -*- coding: utf-8 -*-
"""话题标签（hashtag）。

【和分区不是一回事，之前搞混了】
真机读到一条真实帖子的结构：
    topics   = [{"topic_id": 806933, "name": "AI"}]          ← 这是**分区/社区**
    hashtags = [{"name": "deepseek", "hashtag_id": 689666}]  ← 这才是**话题**

小黑盒手机端发帖：先选分区（topics），选完之后 App 会推几个相关话题（hashtags）。
用户说的「自动推话题」指的是后者。

【话题从哪来】
小黑盒没有公开的话题列表接口（`/bbs/app/api/topic/index/` 返回的是分区树）。
所以照表情白名单的办法：**从真实帖子里采样** —— 刷 feed 时把别人帖子上的
`hashtags` 抄下来（名字 + hashtag_id），攒成池子，发帖时按正文匹配挑几个。
这样拿到的话题名和 id 一定是对的，不会编。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

MAX_PICK = 5


def normalize(name: str) -> str:
    """话题名归一化：去空格、去 # 前缀、转小写（小黑盒话题名大小写不敏感）。"""
    return str(name or "").strip().lstrip("#").replace(" ", "").lower()


def collect_from_post(post: Any) -> list[dict[str, Any]]:
    """从一条帖子数据里取出它带的话题（name + hashtag_id）。"""
    rows = []
    for item in (post or {}).get("hashtags") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            hid = int(item.get("hashtag_id") or 0)
        except (TypeError, ValueError):
            hid = 0
        if name and hid:
            rows.append({"name": name, "id": hid})
    return rows


def merge(pool: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把新采到的话题并进池子（按 id 去重，已有的名字更新为最新写法）。"""
    table: dict[int, dict[str, Any]] = {}
    for item in pool or []:
        try:
            hid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if hid:
            table[hid] = {"name": str(item.get("name") or ""), "id": hid}
    for item in incoming or []:
        try:
            hid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or "").strip()
        if hid and name:
            table[hid] = {"name": name, "id": hid}
    return sorted(table.values(), key=lambda x: x["id"])


def pick_hashtags(
    pool: Iterable[dict[str, Any]],
    text: str,
    *,
    limit: int = MAX_PICK,
) -> list[str]:
    """按正文内容从池子里挑话题名（发帖接口要的是名字数组）。

    匹配方式：话题名出现在正文里 → 直接选；否则看话题名和正文有没有
    2 字以上的公共片段（「deepseek」命中「DeepSeek Harness」这种）。
    宁缺勿滥：一个都匹配不上就不带话题，别硬塞不相关的。
    """
    body = normalize(text)
    if not body:
        return []
    scored: list[tuple[int, str]] = []
    for item in pool or []:
        name = str(item.get("name") or "").strip()
        key = normalize(name)
        if len(key) < 2:
            continue
        if key in body:
            scored.append((100 + len(key), name))
            continue
        # 英文/数字话题做子串命中即可；中文话题要求 2 字以上连续命中。
        if re.fullmatch(r"[a-z0-9#+._-]+", key) and len(key) >= 3 and key in body:
            scored.append((50 + len(key), name))
    scored.sort(reverse=True)
    picked: list[str] = []
    seen: set[str] = set()
    for _, name in scored:
        key = normalize(name)
        if key in seen:
            continue
        seen.add(key)
        picked.append(name)
        if len(picked) >= max(1, int(limit)):
            break
    return picked


class HashtagPool:
    """话题池的落盘存储（data/hashtags.json），只增不减。"""

    FILE = "hashtags.json"

    def __init__(self, data_dir: str | Path | None) -> None:
        self.path = (Path(data_dir) / self.FILE) if data_dir else None

    def load(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload.get("items") if isinstance(payload, dict) else payload
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def save(self, rows: list[dict[str, Any]]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"items": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def add_many(self, incoming: Iterable[dict[str, Any]]) -> int:
        """并入一批话题，返回新增条数。"""
        before = len(self.load())
        merged = merge(self.load(), incoming)
        if len(merged) != before:
            self.save(merged)
        return max(0, len(merged) - before)


async def sample_from_feed(ctx: Any, *, limit: int = 3) -> dict[str, Any]:
    """从推荐流里抄别人帖子带的话题，并进池子。

    【为什么要采样】小黑盒没有公开的话题列表接口（`/bbs/app/api/topic/index/`
    返回的是分区树，不是话题）。所以照表情白名单的做法：别人帖子上挂的
    `hashtags` 一定是真实存在的，抄下来就能用，不用猜、不会编。

    :param limit: 拉几页 feed（每页约 10 条帖子）。
    """
    client = getattr(ctx, "xhh_client", None)
    if client is None:
        return {"ok": False, "code": "no_client", "message": "未登录，无法采样话题"}

    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    pool = HashtagPool(data_dir)

    from vendor.endpoints import EndpointName

    found: list[dict[str, Any]] = []
    for page in range(max(1, int(limit))):
        try:
            resp = await client._request(
                EndpointName.FEED,
                params={"pull": 1, "offset": page * 10, "limit": 10},
            )
        except Exception:  # noqa: BLE001 —— 单页失败不影响整体。
            continue
        links = (resp.payload.get("result") or {}).get("links") or []
        for post in links:
            found.extend(collect_from_post(post))

    added = pool.add_many(found)
    return {"ok": True, "found": len(found), "added": added, "total": len(pool.load())}


__all__ = [
    "MAX_PICK",
    "HashtagPool",
    "collect_from_post",
    "merge",
    "normalize",
    "pick_hashtags",
    "sample_from_feed",
]
