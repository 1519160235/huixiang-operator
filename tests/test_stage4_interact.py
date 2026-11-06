# -*- coding: utf-8 -*-
"""
回响插件：第四阶段「评论 / 浏览 / 互动」自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. 楼中楼 rootId：reply_floor 不带 rootId 拒绝；带 rootId 正确传 root_comment_id / parent_comment_id；
   reply_notification 按通知有无 rootId 分流（回复→楼中楼 / @ 帖子→顶层评论）。
2. 同一通知不重复处理：NotificationStore seen/mark + respond_to_notifications 编排去重。
3. 浏览分区勾选后只刷勾选分区：resolve_browse_partitions。
4. 分区列表定时刷新：should_refresh_partitions。
5. 评论护栏：AI 味 45 阈值、评论额度、评论间隔。
6. 纯逻辑：should_reply_notification、pick_comment_targets。

【验收对齐（docs/开发路线.md 阶段 4）】
- 带 rootId 回楼中楼成功，不再出 error root；
- 同一通知不重复处理；
- 浏览分区勾选后只刷勾选分区；
- 分区列表定时刷新，新增分区不用改代码。
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state  # noqa: E402
from layers import guard, inbound, interact, publish  # noqa: E402
from models import (  # noqa: E402
    ActionState,
    CommentTarget,
    Cursor,
    Partition,
    RunMode,
)


def _make_ctx(data_dir: str, config: dict | None = None) -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.config = config or {
        "guard": {"dry_run_no_quota_cost": True, "daily_quota_comment": 20, "de_ai_flavor": True},
        "interact": {"comment_interval_seconds": 600, "reply_mentions": True, "reply_replies": True},
    }
    ctx.store = state.StoreContainer(Path(data_dir))
    return ctx


# ===================== 楼中楼 rootId（验收：带 rootId 回楼中楼不再 error root） =====================

class _FakeClient:
    def __init__(self) -> None:
        self.routes: list = []

    async def send_comment(self, route: object, text: str) -> SimpleNamespace:
        self.routes.append(route)
        return SimpleNamespace(external_comment_id="c1")


class TestReplyFloor(unittest.IsolatedAsyncioTestCase):
    def test_reply_floor_without_root_rejected(self) -> None:
        target = CommentTarget(post_id="123", root_id=None)
        result = _run_async(publish.reply_floor(None, target, "hi", dry_run=True))
        self.assertEqual(result.state, ActionState.FAILED)

    async def test_reply_floor_passes_root_and_parent(self) -> None:
        client = _FakeClient()
        ctx = SimpleNamespace(xhh_client=client)
        target = CommentTarget(post_id="123", root_id="456", parent_id="789")
        await publish.reply_floor(ctx, target, "hi")
        self.assertEqual(len(client.routes), 1)
        route = client.routes[0]
        self.assertEqual(route.root_comment_id, "456")
        self.assertEqual(route.parent_comment_id, "789")

    async def test_reply_notification_thread_passes_root_id(self) -> None:
        client = _FakeClient()
        ctx = SimpleNamespace(xhh_client=client)
        notif = SimpleNamespace(post_id="123", root_comment_id="456", parent_comment_id="789")
        await publish.reply_notification(ctx, notif, "hi")
        route = client.routes[0]
        self.assertEqual(route.root_comment_id, "456")
        self.assertEqual(route.parent_comment_id, "789")

    async def test_reply_notification_mention_is_top_level(self) -> None:
        client = _FakeClient()
        ctx = SimpleNamespace(xhh_client=client)
        notif = SimpleNamespace(post_id="123", root_comment_id="", parent_comment_id="")
        await publish.reply_notification(ctx, notif, "hi")
        route = client.routes[0]
        self.assertEqual(route.root_comment_id, "")
        self.assertEqual(route.parent_comment_id, "")


def _run_async(awaitable):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


# ===================== 同一通知不重复处理（验收点） =====================

class TestNotificationStore(unittest.TestCase):
    def test_seen_after_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.NotificationStore(Path(tmp))
            self.assertFalse(store.seen("n1"))
            store.mark("n1")
            self.assertTrue(store.seen("n1"))
            self.assertFalse(store.seen("n2"))


class _FakeNotif:
    def __init__(self, nid: str, post_id: str, root_id: str, parent_id: str, event_type: str) -> None:
        self.notification_id = nid
        self.external_event_id = nid
        self.post_id = post_id
        self.root_comment_id = root_id
        self.parent_comment_id = parent_id
        self.event_type = event_type


class _FakeNotifyClient:
    def __init__(self) -> None:
        self.comments: list = []

    async def fetch_notifications(self, event_type: object, cursor: str = "") -> SimpleNamespace:
        if event_type.value == "mention":
            return SimpleNamespace(items=[{"notification": _FakeNotif("n1", "p1", "", "", "mention")}])
        return SimpleNamespace(items=[{"notification": _FakeNotif("n2", "p2", "r2", "p2", "reply")}])

    async def send_comment(self, route: object, text: str) -> SimpleNamespace:
        self.comments.append(route)
        return SimpleNamespace(external_comment_id="c1")


class TestRespondToNotifications(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, tmp: str) -> SimpleNamespace:
        ctx = SimpleNamespace()
        ctx.config = {
            "guard": {"dry_run_no_quota_cost": True, "daily_quota_reply": 15, "de_ai_flavor": True},
            "interact": {"reply_mentions": True, "reply_replies": True, "comment_interval_seconds": 600},
        }
        ctx.store = state.StoreContainer(Path(tmp))
        ctx.xhh_client = _FakeNotifyClient()
        ctx.generate_text = lambda prompt: "短评"
        return ctx

    async def test_same_notification_not_processed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            first = await interact.respond_to_notifications(ctx, RunMode.DRY_RUN)
            self.assertEqual(first["handled"], 2)
            second = await interact.respond_to_notifications(ctx, RunMode.DRY_RUN)
            self.assertEqual(second["handled"], 0)


# ===================== 浏览分区勾选（验收点：勾选后只刷勾选分区） =====================

class TestResolveBrowsePartitions(unittest.TestCase):
    @staticmethod
    def _parts() -> list[Partition]:
        return [
            Partition(id=1, name="数码"),
            Partition(id=2, name="AI"),
            Partition(id=3, name="盒友杂谈"),
        ]

    def test_all_returns_all(self) -> None:
        self.assertEqual(len(inbound.resolve_browse_partitions(self._parts(), "all")), 3)

    def test_selected_by_id(self) -> None:
        result = inbound.resolve_browse_partitions(self._parts(), "selected", ["1", "3"])
        self.assertEqual([p.id for p in result], [1, 3])

    def test_selected_by_name(self) -> None:
        result = inbound.resolve_browse_partitions(self._parts(), "selected", ["数码"])
        self.assertEqual([p.name for p in result], ["数码"])

    def test_selected_empty_returns_all(self) -> None:
        self.assertEqual(len(inbound.resolve_browse_partitions(self._parts(), "selected", [])), 3)


# ===================== 分区定时刷新（验收点：定时刷新，新增分区不用改代码） =====================

class TestShouldRefreshPartitions(unittest.TestCase):
    def test_empty_should_refresh(self) -> None:
        self.assertTrue(inbound.should_refresh_partitions("", 360))

    def test_not_due_yet(self) -> None:
        base = datetime(2026, 1, 1, 0, 0, 0)
        last = base.strftime("%Y-%m-%dT%H:%M:%S")
        last_ts = base.timestamp()
        self.assertFalse(inbound.should_refresh_partitions(last, 360, now=last_ts + 100))

    def test_due(self) -> None:
        base = datetime(2026, 1, 1, 0, 0, 0)
        last = base.strftime("%Y-%m-%dT%H:%M:%S")
        last_ts = base.timestamp()
        self.assertTrue(inbound.should_refresh_partitions(last, 360, now=last_ts + 360 * 60 + 1))


# ===================== 评论护栏（验收：AI 味 45 / 评论额度 / 评论间隔） =====================

class TestCommentGuard(unittest.IsolatedAsyncioTestCase):
    async def test_comment_ai_flavor_threshold_45(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            blocked = await guard.is_commentable(ctx, "text", 50, RunMode.DRY_RUN, action="comment")
            self.assertFalse(blocked.ok)
            self.assertEqual(blocked.code, "ai_flavor_block")
            passed = await guard.is_commentable(ctx, "text", 40, RunMode.DRY_RUN, action="comment")
            self.assertTrue(passed.ok)

    async def test_comment_quota_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            ctx.store.quota.incr(RunMode.REAL, state.today_str(), 20)
            result = await guard.is_commentable(ctx, "好", 0, RunMode.REAL, action="comment")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "quota_exhausted")

    async def test_comment_interval_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            ctx.store.cursor.set(Cursor(name="last_comment_at", value=str(time.time())))
            result = await guard.check_comment_interval(ctx, RunMode.REAL, "comment")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "comment_gap")


# ===================== 互动纯逻辑 =====================

class TestShouldReplyNotification(unittest.TestCase):
    def test_mention_gated(self) -> None:
        notif = SimpleNamespace(event_type="mention")
        self.assertTrue(interact.should_reply_notification(notif, True, True))
        self.assertFalse(interact.should_reply_notification(notif, False, True))

    def test_reply_gated(self) -> None:
        notif = SimpleNamespace(event_type="reply")
        self.assertTrue(interact.should_reply_notification(notif, True, True))
        self.assertFalse(interact.should_reply_notification(notif, True, False))

    def test_other_ignored(self) -> None:
        notif = SimpleNamespace(event_type="proactive_feed")
        self.assertFalse(interact.should_reply_notification(notif, True, True))


class TestPickCommentTargets(unittest.TestCase):
    def test_dedup_and_limit(self) -> None:
        items = [
            SimpleNamespace(post_id="1"),
            SimpleNamespace(post_id="1"),  # 重复
            SimpleNamespace(post_id="2"),
            SimpleNamespace(post_id="3"),
            SimpleNamespace(post_id="4"),
        ]
        picked = interact.pick_comment_targets(items, 3, random.Random(42))
        self.assertEqual(len(picked), 3)
        ids = [i.post_id for i in picked]
        self.assertEqual(len(set(ids)), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
