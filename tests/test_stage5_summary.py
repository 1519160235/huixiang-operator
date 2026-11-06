# -*- coding: utf-8 -*-
"""
回响插件：第五阶段「感想总结发帖」自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. 字数统计口径：count_chars 只数非空白字符；count_paragraphs 只数非空段。
2. 字数收敛：clamp_chars 切在句读处、不超上限。
3. 段数收敛：fit_summary_shape 段数过多合并、过少拆分，最终落在 2–4 段、≤400 字。
4. 触发判定：count / time / both 三种方式，边界与「无素材不触发」。
5. 形状校验：check_summary_shape 字数不足 / 超限 / 段数不足 / 超限。
6. 浏览摘要：BrowseDigestStore 按 post_id 去重、pending 只取未消费、mark_summarized 消费。
7. 素材整理：build_summary_material 带分区前缀、去空行。
8. 总结稿转帖：summary_to_draft 默认纯文字动态 28，AI 味分带入。

【验收对齐（docs/开发路线.md 阶段 5）】
- 一篇总结帖字数落在 200–400、段数 2–4；
- 过 AI 味阈值（正文 60）；
- 回读 verified=true（真实发送待服务器人工点头，此处只验纯逻辑）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state  # noqa: E402
from layers import create, summarize  # noqa: E402
from models import BrowseItem, LinkTag, SummaryDraft  # noqa: E402


# ===================== 字数 / 段数统计 =====================

class TestCountHelpers(unittest.TestCase):
    def test_count_chars_ignores_whitespace(self) -> None:
        self.assertEqual(create.count_chars("你好 世界\n第二段"), 7)

    def test_count_paragraphs(self) -> None:
        self.assertEqual(create.count_paragraphs("第一段\n第二段\n\n第三段"), 3)
        self.assertEqual(create.count_paragraphs(""), 0)

    def test_clamp_chars_within_limit(self) -> None:
        text = "一二三四五，六七八九十"
        self.assertEqual(create.count_chars(create.clamp_chars(text, 5)) <= 5, True)

    def test_clamp_chars_breaks_on_punctuation(self) -> None:
        text = "第一句说完了，第二句还没说完就超了"
        clipped = create.clamp_chars(text, 8)
        self.assertTrue(clipped.endswith("，") or create.count_chars(clipped) <= 8)


# ===================== 段数 / 字数收敛 =====================

class TestFitSummaryShape(unittest.TestCase):
    def _long_text(self, paragraphs: int, per: int = 60) -> str:
        sentence = "今天刷到不少东西，有几个点挺有意思的，"
        return "\n".join(sentence * (per // len(sentence) + 1) for _ in range(paragraphs))

    def test_merge_too_many_paragraphs(self) -> None:
        text = "\n".join(["第一段内容一二三四五。", "第二段内容一二三四五。", "第三段内容一二三四五。",
                          "第四段内容一二三四五。", "第五段内容一二三四五。"])
        result = create.fit_summary_shape(text, 1, 400, 2, 4)
        self.assertLessEqual(create.count_paragraphs(result), 4)

    def test_split_too_few_paragraphs(self) -> None:
        text = self._long_text(1, 120)
        result = create.fit_summary_shape(text, 1, 400, 3, 4)
        self.assertGreaterEqual(create.count_paragraphs(result), 3)

    def test_respects_max_chars(self) -> None:
        text = self._long_text(3, 200)
        result = create.fit_summary_shape(text, 1, 400, 2, 4)
        self.assertLessEqual(create.count_chars(result), 400)

    def test_empty_input(self) -> None:
        self.assertEqual(create.fit_summary_shape("", 200, 400, 2, 4), "")

    def test_typical_output_in_range(self) -> None:
        # 模拟一段真实长度的总结：3 段、每段约 90 字。
        body = self._long_text(3, 90)
        result = create.fit_summary_shape(body, 200, 400, 2, 4)
        self.assertGreaterEqual(create.count_chars(result), 200)
        self.assertLessEqual(create.count_chars(result), 400)
        self.assertGreaterEqual(create.count_paragraphs(result), 2)
        self.assertLessEqual(create.count_paragraphs(result), 4)


# ===================== 触发判定 =====================

class TestShouldSummarize(unittest.TestCase):
    def test_no_material_never_triggers(self) -> None:
        ok, reason = summarize.should_summarize(trigger_by="both", pending_count=0, last_summary_ts=0)
        self.assertFalse(ok)
        self.assertIn("没有待总结", reason)

    def test_count_mode_reaches_threshold(self) -> None:
        ok, _ = summarize.should_summarize(
            trigger_by="count", count_threshold=10, pending_count=10, last_summary_ts=1
        )
        self.assertTrue(ok)

    def test_count_mode_below_threshold(self) -> None:
        ok, _ = summarize.should_summarize(
            trigger_by="count", count_threshold=10, pending_count=9, last_summary_ts=1
        )
        self.assertFalse(ok)

    def test_time_mode_due(self) -> None:
        ok, _ = summarize.should_summarize(
            trigger_by="time", time_hours=24, pending_count=1, since_last_hours=25, last_summary_ts=1
        )
        self.assertTrue(ok)

    def test_time_mode_not_due(self) -> None:
        ok, _ = summarize.should_summarize(
            trigger_by="time", time_hours=24, pending_count=1, since_last_hours=2, last_summary_ts=1
        )
        self.assertFalse(ok)

    def test_both_either(self) -> None:
        by_count, _ = summarize.should_summarize(
            trigger_by="both", count_threshold=5, time_hours=24, pending_count=6,
            since_last_hours=0.5, last_summary_ts=1,
        )
        self.assertTrue(by_count)
        by_time, _ = summarize.should_summarize(
            trigger_by="both", count_threshold=5, time_hours=24, pending_count=2,
            since_last_hours=30, last_summary_ts=1,
        )
        self.assertTrue(by_time)
        neither, _ = summarize.should_summarize(
            trigger_by="both", count_threshold=5, time_hours=24, pending_count=2,
            since_last_hours=1, last_summary_ts=1,
        )
        self.assertFalse(neither)


# ===================== 形状校验 =====================

class TestCheckSummaryShape(unittest.TestCase):
    def _cfg(self) -> dict:
        return {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4}

    def test_ok(self) -> None:
        s = SummaryDraft(text="a" * 300, chars=300, paragraphs=3)
        ok, _ = summarize.check_summary_shape(s, self._cfg())
        self.assertTrue(ok)

    def test_too_short(self) -> None:
        s = SummaryDraft(text="a" * 120, chars=120, paragraphs=3)
        ok, message = summarize.check_summary_shape(s, self._cfg())
        self.assertFalse(ok)
        self.assertIn("字数不足", message)

    def test_too_long(self) -> None:
        s = SummaryDraft(text="a" * 500, chars=500, paragraphs=3)
        ok, message = summarize.check_summary_shape(s, self._cfg())
        self.assertFalse(ok)
        self.assertIn("字数超限", message)

    def test_too_few_paragraphs(self) -> None:
        s = SummaryDraft(text="a" * 300, chars=300, paragraphs=1)
        ok, message = summarize.check_summary_shape(s, self._cfg())
        self.assertFalse(ok)
        self.assertIn("段数不足", message)

    def test_too_many_paragraphs(self) -> None:
        s = SummaryDraft(text="a" * 300, chars=300, paragraphs=6)
        ok, message = summarize.check_summary_shape(s, self._cfg())
        self.assertFalse(ok)
        self.assertIn("段数超限", message)


# ===================== 浏览摘要存储 =====================

class TestBrowseDigestStore(unittest.TestCase):
    def _items(self) -> list[BrowseItem]:
        return [
            BrowseItem(post_id="1", title="帖子一", excerpt="摘要一", partition_name="数码"),
            BrowseItem(post_id="2", title="帖子二", excerpt="摘要二", partition_name="AI"),
            BrowseItem(post_id="1", title="帖子一（重复）", excerpt="摘要一", partition_name="数码"),
        ]

    def test_dedup_by_post_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.BrowseDigestStore(Path(tmp))
            added = store.add_many(self._items())
            self.assertEqual(added, 2)
            self.assertEqual(store.pending_count(), 2)

    def test_pending_excludes_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.BrowseDigestStore(Path(tmp))
            store.add_many(self._items())
            store.mark_summarized(["1"])
            self.assertEqual(store.pending_count(), 1)
            self.assertEqual([row["post_id"] for row in store.pending()], ["2"])

    def test_last_summary_ts_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.BrowseDigestStore(Path(tmp))
            self.assertEqual(store.last_summary_ts(), 0.0)
            store.mark_summarized([])
            self.assertGreater(store.last_summary_ts(), 0)

    def test_max_entries_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.BrowseDigestStore(Path(tmp), max_entries=5)
            store.add_many([BrowseItem(post_id=str(i), title=f"t{i}") for i in range(10)])
            self.assertLessEqual(store.pending_count(), 5)


# ===================== 素材整理 / 转帖 =====================

class TestSummaryMaterial(unittest.TestCase):
    def test_material_has_partition_prefix(self) -> None:
        material = create.build_summary_material(
            [{"title": "标题", "excerpt": "摘要内容", "partition_name": "数码"}]
        )
        self.assertIn("【数码】", material)
        self.assertIn("标题", material)

    def test_material_skips_empty(self) -> None:
        material = create.build_summary_material([{"title": "", "excerpt": ""}, {"title": "有效"}])
        self.assertEqual(material, "有效")

    def test_summary_to_draft_is_dynamic(self) -> None:
        summary = SummaryDraft(title="标题", text="正文", ai_flavor_score=12.0)
        draft = summarize.summary_to_draft(summary)
        self.assertEqual(draft.link_tag, LinkTag.DYNAMIC)
        self.assertEqual(draft.images, [])
        self.assertEqual(draft.ai_flavor_score, 12.0)
        self.assertTrue(draft.polished)


# ===================== 端到端（演练） =====================

class TestRunSummaryOnce(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, tmp: str, summary_text: str) -> SimpleNamespace:
        ctx = SimpleNamespace()
        ctx.config = {
            "guard": {"dry_run_no_quota_cost": True, "daily_quota_post": 3, "de_ai_flavor": True},
            "summarize": {
                "enabled": True,
                "trigger_by": "count",
                "count_threshold": 2,
                "min_chars": 200,
                "max_chars": 400,
                "min_paragraphs": 2,
                "max_paragraphs": 4,
            },
            "publish": {"max_topics": 5},
        }
        ctx.store = state.StoreContainer(Path(tmp))
        ctx.generate_text = lambda prompt: summary_text
        return ctx

    async def test_dry_run_summary_passes_shape(self) -> None:
        # 造一段 3 段、约 250 字的总结正文（每段约 85 字）。
        sentence = "今天刷了半天小黑盒，看到几个帖子挺有意思的，说不上多深刻，但确实值得记一下，"
        para = sentence * 3
        body = "\n".join([para, para, para])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题在这\n" + body)
            ctx.store.digest.add_many(
                [BrowseItem(post_id="1", title="帖子一"), BrowseItem(post_id="2", title="帖子二")]
            )
            result = await summarize.run_summary_once(ctx, mode=__import__("models").RunMode.DRY_RUN)
            self.assertTrue(result.get("ok"), result)
            self.assertGreaterEqual(result["chars"], 200)
            self.assertLessEqual(result["chars"], 400)
            self.assertGreaterEqual(result["paragraphs"], 2)
            self.assertLessEqual(result["paragraphs"], 4)

    async def test_no_material_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题\n正文")
            result = await summarize.run_summary_once(ctx, mode=__import__("models").RunMode.DRY_RUN)
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("code"), "no_material")

    async def test_shape_violation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题\n太短了")
            ctx.store.digest.add_many([BrowseItem(post_id="1", title="帖子一")])
            result = await summarize.run_summary_once(ctx, mode=__import__("models").RunMode.DRY_RUN)
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("code"), "summary_shape")


# ===================== summary_ready 实时判定（回归：since_hours 变量名笔误） =====================

class TestSummaryReady(unittest.TestCase):
    def _ctx(self, tmp: str, **over) -> SimpleNamespace:
        section = {"enabled": True, "trigger_by": "count", "count_threshold": 2, "time_hours": 24}
        section.update(over)
        ctx = SimpleNamespace()
        ctx.config = {"summarize": section}
        ctx.store = state.StoreContainer(Path(tmp))
        return ctx

    def test_count_mode_triggers_with_enough_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            ctx.store.digest.add_many([BrowseItem(post_id=str(i)) for i in range(3)])
            ok, reason = summarize.summary_ready(ctx, ctx.store.digest)
            self.assertTrue(ok, reason)

    def test_count_mode_blocks_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            ctx.store.digest.add_many([BrowseItem(post_id="1")])
            ok, reason = summarize.summary_ready(ctx, ctx.store.digest)
            self.assertFalse(ok, reason)

    def test_time_mode_first_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, trigger_by="time")
            ctx.store.digest.add_many([BrowseItem(post_id="1")])
            ok, reason = summarize.summary_ready(ctx, ctx.store.digest)
            self.assertTrue(ok, reason)
            self.assertIn("还没总结过", reason)

    def test_disabled_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, enabled=False)
            ctx.store.digest.add_many([BrowseItem(post_id="1")])
            ok, reason = summarize.summary_ready(ctx, ctx.store.digest)
            self.assertFalse(ok)
            self.assertIn("未开启", reason)

    def test_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            ok, reason = summarize.summary_ready(ctx, None)
            self.assertFalse(ok)
            self.assertIn("未初始化", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)