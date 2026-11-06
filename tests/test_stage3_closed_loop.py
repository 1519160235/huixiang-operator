# -*- coding: utf-8 -*-
"""
回响插件：第三阶段最小闭环自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. 中文口语清洗：去楼层词 / 破折号转逗号 / 去句号 / 段间不空行；
2. AI 味打分（直译 aiflavor.ts，0–100）；
3. 配图降级：图文(27) → 动态(28)；
4. 护栏六道闸：额度 / 间隔 / 去重 / AI 味 / 熔断，及演练不占额度；
5. 状态存储：额度分桶隔离、去重按 mode 隔离、熔断冷却；
6. 发布线格式：topicIds 逗号字符串（最多 5 个）、link_tag 数值、img 段内嵌。

【验收对齐（docs/开发路线.md 阶段 3）】
- 连打十次演练额度不变（check_quota 演练直放行，real 桶始终 0）；
- 不用 search_posts 当判据（回读走 fetch_thread_context，此处仅验纯逻辑）；
- topicIds 是数字字符串、最多 5 个话题。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state  # noqa: E402
from layers import create, guard, publish  # noqa: E402
from models import (  # noqa: E402
    Cursor,
    ImageAsset,
    LinkTag,
    Partition,
    PostDraft,
    RunMode,
)


def _make_ctx(data_dir: str, config: dict | None = None) -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.config = config or {"guard": {"dry_run_no_quota_cost": True, "daily_quota_post": 3, "de_ai_flavor": True}}
    ctx.store = state.StoreContainer(Path(data_dir))
    return ctx


# ===================== 中文口语清洗 =====================

class TestPolishChinese(unittest.TestCase):
    def test_remove_floor_word(self) -> None:
        self.assertEqual(create.polish_chinese("1楼 说得对"), "说得对")

    def test_remove_floor_hash(self) -> None:
        self.assertEqual(create.polish_chinese("#3 顶"), "顶")

    def test_dash_to_comma(self) -> None:
        self.assertEqual(create.polish_chinese("好——真的"), "好，真的")

    def test_remove_period(self) -> None:
        self.assertEqual(create.polish_chinese("你好。"), "你好")

    def test_no_blank_line(self) -> None:
        self.assertEqual(create.polish_chinese("第一段\n\n第二段"), "第一段\n第二段")


# ===================== AI 味打分 =====================

class TestAiFlavorScore(unittest.TestCase):
    def test_empty_is_zero(self) -> None:
        self.assertEqual(create.ai_flavor_score(""), 0)

    def test_floor_word_hits_threshold(self) -> None:
        # 楼层版位词一个就顶到 45（评论阈值）。
        self.assertGreaterEqual(create.ai_flavor_score("楼主说得对"), 45)

    def test_spec_word_adds_score(self) -> None:
        self.assertGreater(create.ai_flavor_score("必须校验这些参数"), 0)

    def test_dash_adds_score(self) -> None:
        self.assertGreater(create.ai_flavor_score("其实——"), create.ai_flavor_score("其实"))

    def test_polite_word_adds_score(self) -> None:
        self.assertGreater(create.ai_flavor_score("感谢分享，希望对你有帮助"), 0)


# ===================== 配图降级 =====================

class TestFallbackTextOnly(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_clears_images_and_downgrades(self) -> None:
        draft = PostDraft(link_tag=LinkTag.IMAGE_TEXT, images=[ImageAsset()])
        result = await create.fallback_text_only(draft)
        self.assertEqual(result.images, [])
        self.assertEqual(result.link_tag, LinkTag.DYNAMIC)


# ===================== 护栏六道闸 =====================

class TestGuard(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_does_not_cost_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            day = state.today_str()
            for _ in range(10):
                result = await guard.check_quota(ctx, RunMode.DRY_RUN, "post")
                self.assertTrue(result.ok)
            self.assertEqual(ctx.store.quota.get(RunMode.DRY_RUN, day).used, 0)
            self.assertEqual(ctx.store.quota.get(RunMode.REAL, day).used, 0)

    async def test_real_quota_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            ctx.store.quota.incr(RunMode.REAL, state.today_str(), 3)
            result = await guard.check_quota(ctx, RunMode.REAL, "post")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "quota_exhausted")

    async def test_dedup_isolated_by_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            fp = state.make_fingerprint({"title": "x"})
            ctx.store.dedup.mark(fp, RunMode.DRY_RUN)
            self.assertTrue(ctx.store.dedup.seen(fp, RunMode.DRY_RUN))
            self.assertFalse(ctx.store.dedup.seen(fp, RunMode.REAL))
            result = await guard.check_dedup(ctx, fp, RunMode.REAL)
            self.assertTrue(result.ok)

    async def test_ai_flavor_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            blocked = await guard.check_ai_flavor(ctx, 70, threshold=60)
            self.assertFalse(blocked.ok)
            passed = await guard.check_ai_flavor(ctx, 50, threshold=60)
            self.assertTrue(passed.ok)

    async def test_captcha_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            ctx.store.circuit.trip()
            blocked = await guard.check_captcha(ctx)
            self.assertFalse(blocked.ok)
            ctx.store.circuit.reset()
            passed = await guard.check_captcha(ctx)
            self.assertTrue(passed.ok)

    async def test_interval_post_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            ctx.store.cursor.set(Cursor(name="last_post_at", value=str(time.time())))
            result = await guard.check_interval(ctx, RunMode.REAL)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "post_gap")


# ===================== 状态存储隔离 =====================

class TestStateStore(unittest.TestCase):
    def test_quota_bucket_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.QuotaStore(Path(tmp))
            day = "2026-11-04"
            store.incr(RunMode.DRY_RUN, day, 5)
            self.assertEqual(store.get(RunMode.REAL, day).used, 0)
            self.assertEqual(store.get(RunMode.DRY_RUN, day).used, 5)

    def test_circuit_breaker_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.CircuitBreakerStore(Path(tmp), cooldown_minutes=5)
            self.assertFalse(store.is_open())
            store.trip()
            self.assertTrue(store.is_open())
            store.reset()
            self.assertFalse(store.is_open())


# ===================== 发布线格式 =====================

class TestPublishWire(unittest.TestCase):
    def test_topic_ids_wire_format(self) -> None:
        ctx = SimpleNamespace(config={"publish": {"max_topics": 5}})
        draft = PostDraft(topic_ids=[12, 34, 56, 78, 90, 100])
        self.assertEqual(publish._topic_ids_str(ctx, draft), "12,34,56,78,90")

    def test_topic_ids_partition_first(self) -> None:
        ctx = SimpleNamespace(config={"publish": {"max_topics": 5}})
        draft = PostDraft(topic_ids=[34, 56], partition=Partition(id=12))
        self.assertEqual(publish._topic_ids_str(ctx, draft), "12,34,56")

    def test_effective_link_tag(self) -> None:
        with_image = PostDraft(link_tag=LinkTag.IMAGE_TEXT, images=[ImageAsset()])
        self.assertEqual(publish._effective_link_tag(with_image), LinkTag.IMAGE_TEXT)
        no_image = PostDraft(link_tag=LinkTag.IMAGE_TEXT)
        self.assertEqual(publish._effective_link_tag(no_image), LinkTag.DYNAMIC)

    def test_build_segments_embeds_img(self) -> None:
        draft = PostDraft(
            text="正文",
            images=[ImageAsset(remote_ref="https://cdn.max-c.com/x.png", width=100, height=200)],
        )
        segments = publish._build_segments(draft)
        self.assertEqual(segments[0], {"type": "text", "text": "正文"})
        self.assertEqual(segments[1]["type"], "img")
        self.assertEqual(segments[1]["url"], "https://cdn.max-c.com/x.png")
        self.assertEqual(segments[1]["width"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
