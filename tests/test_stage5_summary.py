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
        # 造一段 3 段、约 250 字的总结正文。注意：每段内容必须互不相同——
        # 总结链路新增了段级/句级去重，若用同一句话重复拼接会被当重复折叠掉。
        body = "\n".join(
            [
                "中午刷了会小黑盒，首页给我推了个新游戏评测，数值一项项拆开对比，作者写得挺较真，我耐着性子看到结论就划走了，确实有点东西。",
                "后来又刷到一条晒桌面搭的帖子，灯带一开氛围感直接拉满，评论区一堆人问链接求清单，我就看看热闹，没跟着下单。",
                "再往下翻基本都是老梗图和开箱视频，有个开箱贴摆了满满一桌子的货，我扫了两眼就关了，说不上多有意思。",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题在这\n" + body)
            ctx.store.digest.add_many(
                [
                    BrowseItem(post_id="1", title="新游戏评测：数值一项项拆开对比，作者挺较真"),
                    BrowseItem(post_id="2", title="晒桌面搭：灯带一开氛围感直接拉满"),
                    BrowseItem(post_id="3", title="开箱视频：摆了满满一桌子的货"),
                ]
            )
            result = await summarize.run_summary_once(
                ctx,
                mode=__import__("models").RunMode.DRY_RUN,
                format_id="daily_log",  # 汇总类动机：走 200–400 字的长帖
                check_want=False,  # 这些用例测的是生成形状，不测「想不想发」
            )
            self.assertTrue(result.get("ok"), result)
            # 篇幅不设限（用户 2026-11-08：「字数多少也就不要设置了」），
            # 只要发出去了、正文非空就算通过。
            self.assertGreater(result["chars"], 0)

    async def test_no_material_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题\n正文")
            result = await summarize.run_summary_once(
                ctx, mode=__import__("models").RunMode.DRY_RUN, check_want=False
            )
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("code"), "no_material")

    async def test_short_post_allowed(self) -> None:
        # 用户 2026-11-08：「字数多少也就不要设置了」——写一句也算数，不再判失败。
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "标题\n太短了")
            ctx.store.digest.add_many([BrowseItem(post_id="1", title="帖子一")])
            result = await summarize.run_summary_once(
                ctx, mode=__import__("models").RunMode.DRY_RUN, check_want=False
            )
            self.assertTrue(result.get("ok"), result)


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

class TestPostPermissionCheck(unittest.IsolatedAsyncioTestCase):
    """发帖预检（2026-11-07）。

    【记录一次误判】我最初拿 `bbs_basic_permission == 0` 判定「没有发帖权限」，
    被用户当场纠正：该账号明明能发。真机复测也证实 `link_tag=27` 能过校验。
    `bbs_basic_permission` 是站务类权限，不是普通发帖闸门。

    真正的坑是 `link_tag=28`（纯动态）被服务端一律拒绝，
    现在统一用 27 发布，所以预检永远放行。
    """

    async def test_always_allows(self) -> None:
        from layers import publish

        class _Client:
            async def _request(self, name):
                # 就算权限字段是 0，也不该拦——那不是发帖闸门。
                return type("R", (), {"payload": {"bbs_basic_permission": 0}})()

        class _Ctx:
            xhh_client = _Client()

        ok, why = await publish.check_post_permission(_Ctx())
        self.assertTrue(ok, "bbs_basic_permission=0 不代表不能发帖，不能误拦")
        self.assertEqual(why, "")

    async def test_no_client_allows(self) -> None:
        from layers import publish

        class _Ctx:
            xhh_client = None

        ok, _ = await publish.check_post_permission(_Ctx())
        self.assertTrue(ok)


class TestStripEnglishThought(unittest.TestCase):
    """剥英文思考（2026-11-07 用户报障「总结到底是写什么玩意」）。

    真实故障输出：总结正文开头是模型的英文思考，还夹着中英混排的候选标题：
        The task involves generating a
        Title options considered: "順手刷了会儿动态" or "刷了会儿动态"
        Focusing on how diverse online content can be...
    结果被当成正文（甚至被当成标题）发了出去。
    """

    BAD = (
        'The task involves generating a\n'
        'Title options considered: "順手刷了会儿动态" or "刷了会儿动态"\n'
        'Focusing on how diverse online content can be, with people engaged in various activities.\n'
        '\n'
        '顺手刷了会儿动态，发现大家各忙各的。\n'
        '\n'
        '有人泡在游戏评测里较真数值。\n'
    )

    def test_strips_english_thought_block(self) -> None:
        got = create._strip_english_thought(self.BAD)
        self.assertTrue(got.startswith("顺手刷了会儿动态"), got[:40])
        self.assertNotIn("Title options considered", got)
        self.assertNotIn("Focusing on", got)

    def test_title_not_english_thought(self) -> None:
        # 关键回归：标题绝不能再是英文思考。
        title, body = create._split_title_body(self.BAD)
        self.assertNotIn("Title options", title)
        self.assertNotIn("The task involves", title)

    def test_prose_first_line_not_taken_as_title(self) -> None:
        # 模型没给标题时，首句是正文，不能被当成标题吃掉。
        title, body = create._split_title_body("顺手刷了会儿动态，发现大家各忙各的。\n\n第二段内容。")
        self.assertEqual(title, "", "带句号的首句应判为正文，不是标题")
        self.assertIn("顺手刷了会儿动态", body)
        # 兜底标题从正文提炼。
        self.assertEqual(create._derive_title_from_body(body), "顺手刷了会儿动态")

    def test_pure_chinese_untouched(self) -> None:
        self.assertEqual(create._strip_english_thought("第一段\n第二段"), "第一段\n第二段")

    def test_keeps_paragraphs_for_long_text(self) -> None:
        # keep_paragraphs=True 时不能把多段压平（压平会触发「段数不足」重写）。
        text = "第一段。\n\n第二段。\n\n第三段。"
        self.assertEqual(create.count_paragraphs(create.polish_chinese(text, keep_paragraphs=True)), 3)


class TestNowContext(unittest.TestCase):
    """真实时间注入（2026-11-07 用户报障「中午还没到却写大半夜」）。"""

    def test_midday_period(self) -> None:
        from datetime import datetime

        ctx = create._now_context(datetime(2026, 11, 7, 11, 23))
        self.assertIn("中午", ctx)
        self.assertIn("2026 年 11 月 7 日", ctx)
        self.assertIn("星期六", ctx)

    def test_late_night_period(self) -> None:
        from datetime import datetime

        self.assertIn("凌晨", create._now_context(datetime(2026, 11, 7, 2, 0)))

    def test_evening_period(self) -> None:
        from datetime import datetime

        self.assertIn("晚上", create._now_context(datetime(2026, 11, 7, 19, 0)))


class TestDedupeSummary(unittest.TestCase):
    """总结去重（2026-11-07 用户报障「第一二行重复」）。"""

    def test_dedupes_near_duplicate_paragraphs(self) -> None:
        text = (
            "中午随手刷了会儿，看到几个帖子。\n"
            "中午随手刷了会儿，看到几个帖子。\n"
            "另外一条讲了个新游戏。"
        )
        got = create.dedupe_summary(text)
        self.assertEqual(got.count("中午随手刷了会儿"), 1)

    def test_preserves_paragraph_structure(self) -> None:
        text = "第一段内容写点什么。\n第二段是完全不同的东西。\n第三段又是新的。"
        self.assertEqual(create.count_paragraphs(create.dedupe_summary(text)), 3)

    def test_dedupes_sentences_within_paragraph(self) -> None:
        text = "运气真好，工坊开箱出了金光。运气真挺好的，工坊开箱就出了金光。"
        got = create.dedupe_summary(text)
        self.assertLess(create.count_chars(got), create.count_chars(text))


class TestStripTitleFromBody(unittest.TestCase):
    """标题-正文首句重复剥离（2026-11-07 用户报障）。"""

    def test_strips_leading_repeat(self) -> None:
        got = create._strip_title_from_body(
            "大半夜随手刷了会儿",
            "大半夜随手刷了会儿，大家的生活都挺有意思的。\n后面还有一段。",
        )
        self.assertFalse(got.startswith("大半夜随手刷了会儿"))
        self.assertTrue(got.startswith("大家的生活"))

    def test_title_equals_first_line(self) -> None:
        got = create._strip_title_from_body("随手刷动态", "随手刷动态\n后面写正文")
        self.assertTrue(got.startswith("后面写正文"))

    def test_no_repeat_untouched(self) -> None:
        body = "今天刷到一个有意思的帖子。\n第二段。"
        self.assertEqual(create._strip_title_from_body("随便一个标题", body), body)

    def test_short_title_untouched(self) -> None:
        body = "短。"
        self.assertEqual(create._strip_title_from_body("短", body), body)


class TestPadSummaryNoAiFlavor(unittest.TestCase):
    """凑字数兜底不再带「大家…都…」上帝视角总结腔（2026-11-07）。"""

    def test_filler_has_no_god_view(self) -> None:
        text = "短" * 20
        items = [{"title": "标题一"}, {"title": "标题二"}]
        got = create.pad_summary_to_min(text, items, min_chars=200, max_chars=400)
        self.assertNotIn("大家都", got)
        self.assertNotIn("大家最近", got)
        self.assertNotIn("看着都", got)


class TestSummaryPrompt(unittest.IsolatedAsyncioTestCase):
    """总结提示词注入真实时间 + 去 AI 腔 + 禁标题正文重复（2026-11-07）。"""

    async def test_prompt_has_time_and_bans(self) -> None:
        captured = {}

        class _Ctx:
            config = {
                "summarize": {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4},
                "create": {"tone": "平和"},
            }

            async def generate_text(self, prompt):
                captured["prompt"] = prompt
                return "随手刷了会儿\n刷到个新游戏评测，讲得挺细。\n另外还有个帖子挺逗。\n"

        items = [{"title": "帖子一", "excerpt": "摘要", "partition_name": "数码", "post_id": "1"}]
        await create.generate_summary(_Ctx(), items, None, max_attempts=1)

        prompt = captured["prompt"]
        self.assertIn("现在是", prompt)
        self.assertIn("禁止编造", prompt)
        self.assertIn("大家的生活都挺有意思的", prompt)  # 明确点名的禁句
        self.assertIn("标题和正文第一句不要重复", prompt)


class TestSummaryWithImage(unittest.IsolatedAsyncioTestCase):
    """总结帖配图（2026-11-07 用户报障「发帖子要配图，图没了」）。

    根因：summary_to_draft 写死 images=[]，且 worker 只跑「浏览/互动/总结」，
    普通发帖不在调度里 —— 用户看到的帖子就是总结帖，所以永远没图。
    """

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
            "image": {"with_image": True, "image_count": 1},
            "publish": {"max_topics": 5},
        }
        ctx.store = state.StoreContainer(Path(tmp))
        ctx.generate_text = lambda prompt: summary_text
        return ctx

    async def test_dry_run_attaches_image(self) -> None:
        from models import ActionState, ImageAsset, PublishResult, RunMode

        captured: dict = {}

        async def _fake_images(ctx, draft, count=1):
            return [ImageAsset(local_ref="hx_img_test.png")]

        async def _fake_publish(ctx, draft, dry_run=False):
            captured["draft"] = draft
            return PublishResult(state=ActionState.VERIFIED, note="演练：已组装")

        body = "\n".join(
            [
                "中午刷了会小黑盒，首页给我推了个新游戏评测，数值一项项拆开对比，作者写得挺较真，我耐着性子看到结论就划走了。",
                "后来又刷到一条晒桌面搭的帖子，灯带一开氛围感直接拉满，评论区一堆人问链接求清单，我就看看热闹。",
                "再往下翻基本都是老梗图和开箱视频，有个开箱贴摆了满满一桌子的货，我扫了两眼就关了，说不上多有意思。",
            ]
        )
        original_images = summarize.create.generate_images
        original_publish = summarize.publish.publish_post
        summarize.create.generate_images = _fake_images
        summarize.publish.publish_post = _fake_publish
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = self._ctx(tmp, "标题在这\n" + body)
                ctx.store.digest.add_many(
                    [
                        BrowseItem(post_id="1", title="新游戏评测：数值一项项拆开对比，作者挺较真"),
                        BrowseItem(post_id="2", title="晒桌面搭：灯带一开氛围感直接拉满"),
                        BrowseItem(post_id="3", title="开箱视频：摆了满满一桌子的货"),
                    ]
                )
                result = await summarize.run_summary_once(ctx, mode=RunMode.DRY_RUN, check_want=False)
                self.assertTrue(result.get("ok"), result)
                self.assertEqual(len(captured["draft"].images), 1)
        finally:
            summarize.create.generate_images = original_images
            summarize.publish.publish_post = original_publish


class TestSummaryGrounding(unittest.TestCase):
    """总结必须只写真实刷到的内容（2026-11-07 用户报障「你没刷到这个怎么能说呢」）。

    真机故障：模型拿到 12 条真实素材（GTA6 / FF7 打折 / MC 涨价…），
    却写出「活侠传改存档」「工程软件报错」「霍格沃茨支持 PS5」三条，
    200 条浏览记录里一条都没有。
    """

    MATERIAL = (
        "【游戏资讯】前R星技术总监：AI移植或许会倒逼《GTA6》PC版提前上线：前Rockstar North技术总监\n"
        "【游戏商城】跌破史低！券后仅32元，《最终幻想7 重制版》这波不冲等何时？\n"
        "【盒友杂谈】mc涨价了：这两天刚发生活费想补票的 微软你没有心\n"
    )

    def test_flags_unseen_game(self) -> None:
        got = create.summary_grounding_issues(
            "还看到霍格沃茨之遗居然都支持5了，等会儿回去更新驱动。",
            self.MATERIAL,
        )
        self.assertTrue(got)

    def test_flags_unseen_work_in_brackets(self) -> None:
        got = create.summary_grounding_issues("刷到《活侠传》改存档的帖子。", self.MATERIAL)
        self.assertIn("《活侠传》", got)

    def test_passes_grounded_text(self) -> None:
        body = (
            "刷到有人在聊《GTA6》PC版可能提前上线，前R星技术总监说的。\n"
            "又看到《最终幻想7 重制版》券后32块，有点心动。"
        )
        self.assertEqual(create.summary_grounding_issues(body, self.MATERIAL), [])

    def test_material_includes_more_items(self) -> None:
        items = [{"title": f"帖子{i}"} for i in range(40)]
        material = create.build_summary_material(items)
        self.assertEqual(len(material.splitlines()), 24)


class TestSummaryGroundingLoop(unittest.IsolatedAsyncioTestCase):
    """连续几轮都在编 → 本轮不发（宁可空着，也不发假总结）。"""

    async def test_hallucinated_summary_not_returned(self) -> None:
        class _Ctx:
            config = {
                "summarize": {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4},
                "create": {"tone": "平和"},
            }

            async def generate_text(self, prompt):
                return (
                    "周六傍晚刷了会儿\n"
                    "还看到霍格沃茨之遗居然都支持5了。\n"
                    "后面又刷到活侠传改存档的帖子，挺有意思。"
                )

        items = [
            {
                "title": "前R星技术总监：AI移植或许会倒逼《GTA6》PC版提前上线",
                "excerpt": "前Rockstar North技术总监Obbe Vermeij在Rock Paper Shotgun采访里说",
                "post_id": "1",
            }
        ]
        draft = await create.generate_summary(_Ctx(), items, None, max_attempts=2)
        self.assertEqual(draft.text, "", "编造内容不该被当成可发草稿")
        self.assertEqual(draft.chars, 0)
