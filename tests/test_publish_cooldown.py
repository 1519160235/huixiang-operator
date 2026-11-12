# -*- coding: utf-8 -*-
"""回响插件：2026-11-12「发帖被拒还一直生图白扣钱」回归测试。

【事故】小黑盒整天回 `failed: 发布功能暂不可用`，156 次发帖 0 成功；
但生图在发帖之前，每轮失败后草稿丢弃、下一轮重新画，当天堆了 328 张图、
API 后台白扣 5 块钱。

【契约】
1. 平台级拒绝 → 冷却，冷却期内在 check_post_permission 就拦下整轮（零生成、零生图）；
2. 连续拒绝指数退避，上限 6 小时；
3. 冷却到期后先用纯文字探测（should_skip_image=True），成功才恢复生图；
4. 能自己修的错误（分区/标题/独占）不触发熔断；
5. 发布成功清除状态。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import cooldown, publish  # noqa: E402

NOW = 1_700_000_000.0


class _Ctx:
    def __init__(self, path: Path, client=None) -> None:
        self.paths = SimpleNamespace(data_dir=path)
        self.xhh_client = client
        self.store = None
        self.config = None
        self.logger = None


class TestCooldownCore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = _Ctx(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_classify_only_platform_level_errors(self) -> None:
        self.assertEqual(cooldown.classify("小黑盒 API 返回非成功状态 failed: 发布功能暂不可用"), "platform")
        # 真机 15:31 拿到的原始报错：评论功能也被停用，必须同样熔断
        self.assertEqual(cooldown.classify("小黑盒 API 返回非成功状态 failed: 评论功能暂不可用"), "platform")
        self.assertEqual(cooldown.classify("回复功能暂不可用"), "platform")
        self.assertEqual(cooldown.classify("发帖被拒：小黑盒限流了，频率过快"), "rate_limit")
        # 这些是本插件能自己修的，绝不能熔断
        for msg in ("错误的动态分区", "标题最多输入30字", "只能单独选择 盒友杂谈 分区", "网络超时"):
            self.assertEqual(cooldown.classify(msg), "", msg)

    def test_platform_rejection_blocks_following_rounds(self) -> None:
        state = cooldown.record_rejection(self.ctx, "failed: 发布功能暂不可用", now=NOW)
        self.assertEqual(state["consecutive"], 1)
        self.assertEqual(state["cooldown_sec"], 30 * 60)

        blocked, why = cooldown.is_blocked(self.ctx, now=NOW + 60)
        self.assertTrue(blocked)
        self.assertIn("发布冷却中", why)
        # 冷却期内连探测都不做：should_skip_image 此时无意义（整轮已被拦），
        # 但必须不能误判成「正常状态」。
        self.assertFalse(cooldown.should_skip_image(self.ctx, now=NOW + 60))

    def test_exponential_backoff_capped(self) -> None:
        secs = []
        for i in range(6):
            st = cooldown.record_rejection(
                self.ctx, "发布功能暂不可用", now=NOW + i * 7 * 3600)
            secs.append(st["cooldown_sec"])
        self.assertEqual(secs[0], 30 * 60)
        self.assertEqual(secs[1], 60 * 60)
        self.assertEqual(secs[2], 2 * 3600)
        self.assertEqual(secs[3], 4 * 3600)
        self.assertEqual(secs[4], 6 * 3600, "必须封顶")
        self.assertEqual(secs[5], 6 * 3600)

    def test_expired_cooldown_becomes_text_only_probe(self) -> None:
        cooldown.record_rejection(self.ctx, "发布功能暂不可用", now=NOW)
        blocked, _ = cooldown.is_blocked(self.ctx, now=NOW + 31 * 60)
        self.assertFalse(blocked, "冷却到期应允许再试一次")
        self.assertTrue(
            cooldown.should_skip_image(self.ctx, now=NOW + 31 * 60),
            "探测轮必须跳过生图，不然又白画一张",
        )

    def test_clear_resets_everything(self) -> None:
        cooldown.record_rejection(self.ctx, "发布功能暂不可用", now=NOW)
        cooldown.clear(self.ctx)
        self.assertFalse(cooldown.is_blocked(self.ctx, now=NOW + 1)[0])
        self.assertFalse(cooldown.should_skip_image(self.ctx, now=NOW + 1))

    def test_rate_limit_uses_short_cooldown(self) -> None:
        st = cooldown.record_rejection(self.ctx, "发帖被拒：频率过快", now=NOW)
        self.assertEqual(st["cooldown_sec"], 10 * 60)

    def test_normal_error_is_ignored(self) -> None:
        self.assertIsNone(cooldown.record_rejection(self.ctx, "标题最多输入30字", now=NOW))
        self.assertFalse(cooldown.is_blocked(self.ctx, now=NOW)[0])


class TestPublishIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_permission_check_blocks_during_cooldown(self) -> None:
        ctx = _Ctx(self.path)
        ok, _ = asyncio.run(publish.check_post_permission(ctx))
        self.assertTrue(ok, "无状态时不能误杀正常发帖")

        cooldown.record_rejection(ctx, "发布功能暂不可用", now=NOW)
        # 用真实时间登记，确保检查时仍在冷却
        cooldown.record_rejection(ctx, "发布功能暂不可用")
        ok, why = asyncio.run(publish.check_post_permission(ctx))
        self.assertFalse(ok)
        self.assertIn("发布冷却中", why)

    def test_publish_failure_records_and_success_clears(self) -> None:
        class _FailClient:
            async def publish_post(self, **kwargs):
                raise RuntimeError("小黑盒 API 返回非成功状态 failed: 发布功能暂不可用")

        class _OkClient:
            async def publish_post(self, **kwargs):
                return "123456"

        from models import PostDraft

        draft = PostDraft(title="测试标题", text="测试正文")
        ctx = _Ctx(self.path, _FailClient())
        with self.assertRaises(Exception):
            asyncio.run(publish.publish_post(ctx, draft))
        self.assertEqual(cooldown._read(ctx).get("consecutive"), 1, "发布失败必须登记冷却")

        ctx.xhh_client = _OkClient()
        result = asyncio.run(publish.publish_post(ctx, draft))
        self.assertEqual(result.post_id, "123456")
        self.assertEqual(cooldown._read(ctx), {}, "发布成功必须清除冷却")


class TestInteractAndLog(unittest.TestCase):
    """评论被拒和异常记录：别再把整轮搞崩、别再只记一个类名。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = _Ctx(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cooldown_skips_whole_interact_round(self) -> None:
        from unittest.mock import patch
        from layers import interact
        from models import RunMode

        called: list[str] = []

        async def fake_notif(ctx, mode):
            called.append("notif")
            return {"handled": 0, "sent_items": []}

        async def fake_feeds(ctx, mode):
            called.append("feeds")
            return {"comments_sent": 0}

        cooldown.record_rejection(self.ctx, "发布功能暂不可用")
        with patch.object(interact, "respond_to_notifications", fake_notif), \
             patch.object(interact, "interact_with_feeds", fake_feeds):
            result = asyncio.run(interact.respond_and_interact(self.ctx, RunMode.REAL))
        self.assertEqual(result.get("code"), "publish_cooldown")
        self.assertEqual(called, [], "冷却期内一条评论都不该生成")

    def test_dry_run_is_not_blocked(self) -> None:
        from unittest.mock import patch
        from layers import interact
        from models import RunMode

        called: list[str] = []

        async def fake_notif(ctx, mode):
            called.append("notif")
            return {"handled": 0, "sent_items": [], "skipped": []}

        async def fake_feeds(ctx, mode):
            called.append("feeds")
            return {"comments_sent": 0, "skipped": [], "sent_items": []}

        cooldown.record_rejection(self.ctx, "发布功能暂不可用")
        with patch.object(interact, "respond_to_notifications", fake_notif), \
             patch.object(interact, "interact_with_feeds", fake_feeds):
            result = asyncio.run(interact.respond_and_interact(self.ctx, RunMode.DRY_RUN))
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(called), 2, "演练模式不该被真实平台的冷却误拦")

    def test_worker_logs_original_exception_message(self) -> None:
        import worker as worker_mod

        class _FakeWorker:
            _step = worker_mod.OpsWorker._step

            def __init__(self):
                self.logged = None
                self.ctx = self  # _step 只把 ctx/mode 透传给 runner，占位即可
                self.mode = None

            def _log(self, name, ok, message, detail=None):
                self.logged = (name, ok, message)

        async def boom(ctx, mode):
            raise RuntimeError("小黑盒 API 返回非成功状态 failed: 发布功能暂不可用")

        fw = _FakeWorker()
        out = asyncio.run(fw._step("interact", boom))
        self.assertFalse(out["ok"])
        self.assertIn("发布功能暂不可用", fw.logged[2], "记录里必须能看到平台原始原因")
        self.assertIn("RuntimeError", fw.logged[2])


if __name__ == "__main__":
    unittest.main()
