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

    # ---- 2026-11-07 段间不空行（[create].no_blank_line_between_paragraphs 生效） ----

    def test_blank_between_paragraphs_false(self) -> None:
        # no_blank_line_between_paragraphs=true → 段间不留空行。
        got = create.polish_chinese(
            "第一段\n\n第二段\n\n第三段",
            keep_paragraphs=True,
            blank_between_paragraphs=False,
        )
        self.assertEqual(got, "第一段\n第二段\n第三段")

    def test_blank_between_paragraphs_true(self) -> None:
        # 默认段间留一个空行。
        got = create.polish_chinese(
            "第一段\n\n第二段",
            keep_paragraphs=True,
            blank_between_paragraphs=True,
        )
        self.assertEqual(got, "第一段\n\n第二段")

    def test_keep_periods_for_long_text(self) -> None:
        # 长文保留句号：否则 300 字正文会变成一串逗号连到底的巨型长句。
        got = create.polish_chinese(
            "第一句。第二句。",
            keep_paragraphs=True,
            keep_periods=True,
        )
        self.assertEqual(got, "第一句。第二句。")


# ===================== 评论强清洗（2026-11-07 用户报障：重复/简繁混/英文） =====================

class TestSanitizeComment(unittest.TestCase):
    def test_dedupe_repeated_sentence(self) -> None:
        self.assertEqual(create.sanitize_comment("这游戏真不错。这游戏真不错。这游戏真不错。"), "这游戏真不错。")

    def test_dedupe_repeated_phrase_no_punct(self) -> None:
        self.assertEqual(create.sanitize_comment("支持一下支持一下支持一下支持一下"), "支持一下")

    def test_traditional_to_simplified(self) -> None:
        self.assertEqual(create.sanitize_comment("這個遊戲體驗很棒，隊友很強"), "这个游戏体验很棒，队友很强")

    def test_strip_english_sentence(self) -> None:
        self.assertEqual(create.sanitize_comment("The post title suggests showing off. 这皮肤真好看"), "这皮肤真好看")

    def test_strip_inline_english_word(self) -> None:
        self.assertEqual(create.sanitize_comment("我觉得 this game 真的很好玩"), "我觉得真的很好玩")

    def test_collapse_repeat_char(self) -> None:
        self.assertEqual(create.sanitize_comment("哈哈哈哈哈哈哈哈！！！太强了"), "哈哈！太强了")

    def test_keep_whitelist_term(self) -> None:
        self.assertEqual(create.sanitize_comment("PUBG 这波操作可以"), "PUBG这波操作可以")

    # ---- 2026-11-07 真机补充：模型自言自语与结构残渣 ----

    def test_pick_final_line_drops_draft(self) -> None:
        # 推理模型常输出「草稿行 + 定稿行」，评论应取最后的定稿句。
        raw = "紫衣角色显得拘谨营造了幽默感考虑将评论聚焦于场景\n紫衣服那个站得挺老实的，看来家教确实严"
        self.assertEqual(create.sanitize_comment(raw), "紫衣服那个站得挺老实的，看来家教确实严")

    def test_strip_quote_and_symbol_noise(self) -> None:
        raw = '一发入魂"\n运气真好，吸吸欧气，我工坊开出来的全都是喷漆'
        self.assertEqual(create.sanitize_comment(raw), "运气真好，吸吸欧气，我工坊开出来的全都是喷漆")

    def test_strip_self_talk_sentence(self) -> None:
        # 「考虑将评论聚焦于…」是模型在跟自己讨论写法，不是评论内容。
        raw = "这皮肤真好看考虑将评论聚焦于服装细节\n这皮肤真好看，我也想要一个"
        self.assertEqual(create.sanitize_comment(raw), "这皮肤真好看，我也想要一个")

    def test_normal_multiline_not_broken(self) -> None:
        # 正常单句不能被「取末句」规则破坏。
        self.assertEqual(create.sanitize_comment("陆家村这图我也卡在60%多，找那个监牢转了半天"),
                         "陆家村这图我也卡在60%多，找那个监牢转了半天")


# ===================== AI 味打分 =====================

class TestAiFlavorScore(unittest.TestCase):
    """AI 味打分（2026-11-07 按两个开源项目融合重写后的口径）。

    重写原因：老版本「单词命中即重罚」，实测 10 篇真实人类文章 4 篇被判 ≥45，
    同时 AI 腔评论「值得一提的是…」全打 0 分。新算法按「痕迹密度」评分。
    """

    def test_empty_is_zero(self) -> None:
        self.assertEqual(create.ai_flavor_score(""), 0)

    def test_pure_english_is_zero(self) -> None:
        # 纯英文不做中文 AI 味判断，直接放行。
        self.assertEqual(create.ai_flavor_score("GG WP nice play"), 0)

    def test_floor_word_not_scored(self) -> None:
        # 楼层词由 polish_chinese 清掉，不再由打分器重复惩罚。
        self.assertEqual(create.ai_flavor_score("楼主说得对"), 0)

    def test_technical_words_not_scored(self) -> None:
        # 「必须/校验/参数/机制/规范」是人类正式写作常用词，不是 AI 证据。
        self.assertEqual(create.ai_flavor_score("必须校验这些参数"), 0)

    def test_single_dash_not_scored(self) -> None:
        # 单次破折号是人类正常修辞，不构成 AI 证据（成簇才算）。
        self.assertEqual(
            create.ai_flavor_score("其实——"),
            create.ai_flavor_score("其实"),
        )

    def test_ai_marker_cluster_hits_threshold(self) -> None:
        # 「值得一提的是」这类讲义开场在短评论里就是高密度，应被判机器。
        self.assertGreaterEqual(
            create.ai_flavor_score("值得一提的是，这款游戏画面出色，带来了极致体验。"), 45
        )

    def test_ai_shell_pattern_hits(self) -> None:
        # 「不是 A 而是 B」+ 伪深度尾缀 + 路标词成簇 → 判机器。
        self.assertGreaterEqual(
            create.ai_flavor_score(
                "总而言之，这不仅仅是一次优化，而是对体验的全面升级，体现了团队的匠心。"
            ),
            45,
        )

    def test_customer_service_tone_scored(self) -> None:
        self.assertGreater(create.ai_flavor_score("感谢分享，希望对你有帮助"), 0)

    def test_human_comment_is_clean(self) -> None:
        # 真实人类随手评论不该被判机器（老版本误杀高发区）。
        for t in (
            "紫薯精站在教室里这",
            "吸一口欧气，我工坊开出来的",
            "这波操作可以啊，我上次也这么玩",
            "说实话这皮肤我不太喜欢",
        ):
            self.assertLess(create.ai_flavor_score(t), 45, t)


# ===================== 配图降级 =====================

class TestFallbackTextOnly(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_clears_images_keeps_27(self) -> None:
        # 【2026-11-07 修正】fallback 不再降级成 28（28 会被小黑盒拒），仍保持 27 发纯文字。
        draft = PostDraft(link_tag=LinkTag.IMAGE_TEXT, images=[ImageAsset()])
        result = await create.fallback_text_only(draft)
        self.assertEqual(result.images, [])
        self.assertEqual(result.link_tag, LinkTag.IMAGE_TEXT)


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
        # 【2026-11-07 回归】无图也必须用 27，不能再降级成 28。
        # 真机实测 28（纯动态）被小黑盒一律拒绝（任何分区都返回
        # `failed: 错误的动态分区`），而同样的分区用 27 就能过。
        # 用户当场纠正了「账号没发帖权限」的错误判断。
        with_image = PostDraft(link_tag=LinkTag.IMAGE_TEXT, images=[ImageAsset()])
        self.assertEqual(publish._effective_link_tag(with_image), LinkTag.IMAGE_TEXT)
        no_image = PostDraft(link_tag=LinkTag.IMAGE_TEXT)
        self.assertEqual(
            publish._effective_link_tag(no_image), LinkTag.IMAGE_TEXT,
            "无图也必须发 27，28 会被小黑盒拒绝",
        )
        dynamic_draft = PostDraft(link_tag=LinkTag.DYNAMIC)
        self.assertEqual(
            publish._effective_link_tag(dynamic_draft), LinkTag.IMAGE_TEXT,
            "即使草稿标了 28 也要纠正成 27",
        )

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


# ===================== 游戏语境识别（2026-11-07 用户报障「不涉及黑话就瞎评」） =====================

class TestGuessGameName(unittest.TestCase):
    """认游戏：决定评论用哪套圈内话。

    用户 2026-11-07 报障：「评论不涉及游戏黑话，人家一眼就辨别出来了」。
    """

    def test_main_partition_is_game(self) -> None:
        self.assertEqual(create._guess_game_name(["鬼谷八荒"], "8G显卡方案", "", ""), "鬼谷八荒")

    def test_alias_in_title(self) -> None:
        # 「博启」是博德之门3的圈内梗，标题里出现就应该认出来。
        self.assertEqual(
            create._guess_game_name(["盒友杂谈"], "可以和大肥鱼一起博启了？", "", ""), "博德之门3"
        )

    def test_novel_post_with_game_hashtag_not_misjudged(self) -> None:
        # 关键回归：网文帖子挂「原神」「鸣潮」标签只是顺带关联，不能判成游戏帖。
        got = create._guess_game_name(
            ["小说", "网文", "鸣潮", "原神"], "现在的多女主校园恋爱文都玩的这么花吗？！", "", ""
        )
        self.assertEqual(got, "", "泛分区帖子不该因为关联标签被判成游戏帖")

    def test_generic_partition_returns_empty(self) -> None:
        # 数码硬件帖子里提到芯片，不该硬认一个游戏。
        self.assertEqual(
            create._guess_game_name(["数码硬件"], "现在高通的芯片命名越来越看不懂了", "", ""), ""
        )

    def test_no_signal_returns_empty(self) -> None:
        self.assertEqual(create._guess_game_name([], "", "", ""), "")

    def test_safe_title_truncates_to_30(self) -> None:
        # 真机实测：图文帖（27）标题上限 30 字，超一个字整条失败。
        self.assertEqual(len(publish._safe_title("测" * 50)), 30)
        self.assertEqual(publish._safe_title("短标题"), "短标题")
        self.assertEqual(publish._safe_title("带\n换行"), "带 换行")


# ===================== 配图生图（2026-11-07 接入） =====================

class TestImageGen(unittest.TestCase):
    def test_build_generator_requires_all(self) -> None:
        from layers import imagegen

        self.assertIsNone(imagegen.build_generator("", "", ""))
        self.assertIsNone(imagegen.build_generator("http://x", "k", ""))
        self.assertIsNotNone(imagegen.build_generator("http://x", "k", "m"))

    def test_prompt_has_no_framework_name(self) -> None:
        # 真机缺陷 2026-11-07：图上出现「maibot 的生活分享」——框架名被当昵称画进图里。
        from layers import imagegen

        prompt = imagegen.build_image_prompt("周六傍晚刷了会儿小黑盒", "刷到有人改存档")
        self.assertNotIn("maibot", prompt.lower())
        self.assertNotIn("mybot", prompt.lower())

    def test_prompt_adds_no_rules(self) -> None:
        # 用户 2026-11-08：「规则是我定的……想怎么发怎么发，只要不凭空捏造就行」
        # 「你怎么老想着做限制呢？」—— 提示词里一条附加限制都不许有。
        from layers import imagegen

        prompt = imagegen.build_image_prompt("周六傍晚刷了会儿小黑盒", "刷到有人改存档")
        for banned in ("严禁", "不要", "必须", "截图", "编造", "插画", "3D", "自然光", "噪点"):
            self.assertNotIn(banned, prompt, f"不该出现限制词：{banned}")

    def test_scene_passed_through(self) -> None:
        # 有场景描述时原样透传，一个字的加工都不做。
        from layers import imagegen

        scene = "傍晚，窗台上的鱼缸，夕阳斜进来"
        self.assertEqual(imagegen.build_image_prompt("随便", "随便", scene=scene), scene)


class TestGenerateImages(unittest.IsolatedAsyncioTestCase):
    """生图 → 写文件 → 返回 ImageAsset（不真联网，mock 掉 imagegen）。"""

    async def test_generate_images_writes_assets(self) -> None:
        import layers.imagegen as imagegen

        async def _fake_gen(ctx, *, title, body, request_id="", model=""):
            return b"\x89PNG\r\n\x1a\nfake-image-bytes"

        original = imagegen.generate_image_bytes
        imagegen.generate_image_bytes = _fake_gen
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(tmp)))
                draft = PostDraft(title="标题", text="正文")
                assets = await create.generate_images(ctx, draft, count=2)
                self.assertEqual(len(assets), 2)
                for asset in assets:
                    self.assertTrue(asset.local_ref.endswith(".png"))
                    self.assertTrue((Path(tmp) / asset.local_ref).exists())
        finally:
            imagegen.generate_image_bytes = original

    async def test_generate_images_empty_on_failure(self) -> None:
        import layers.imagegen as imagegen

        async def _fake_fail(ctx, *, title, body, request_id="", model=""):
            return None

        original = imagegen.generate_image_bytes
        imagegen.generate_image_bytes = _fake_fail
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(tmp)))
                draft = PostDraft(title="标题", text="正文")
                assets = await create.generate_images(ctx, draft, count=1)
                self.assertEqual(assets, [])
        finally:
            imagegen.generate_image_bytes = original


# ===================== 额度按动作隔离（2026-11-07 发帖被评论挤爆） =====================

class TestQuotaIsolation(unittest.IsolatedAsyncioTestCase):
    """评论 / 回复不能吃掉发帖额度。

    真机现象：quota.json 里 `quota:real:2026-11-07` 是 37，而 daily_quota_post=3，
    发帖从早上起就被判「额度已用尽」，当天一篇帖都发不出去（自然也没配图）。
    根因是额度桶只按 mode+日期 计数，不区分动作。
    """

    def setUp(self) -> None:
        # reserve_quota 会写进程内限流状态，用例前后都清掉，避免污染其它测试。
        guard._rate_last.clear()
        guard._rate_stamps.clear()

    def tearDown(self) -> None:
        guard._rate_last.clear()
        guard._rate_stamps.clear()

    async def test_comment_does_not_eat_post_quota(self) -> None:
        cfg = {
            "guard": {
                "dry_run_no_quota_cost": True,
                "daily_quota_post": 3,
                "daily_quota_comment": 20,
                "de_ai_flavor": True,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp, cfg)
            for _ in range(10):
                await guard.reserve_quota(ctx, RunMode.REAL, "comment")
            result = await guard.check_quota(ctx, RunMode.REAL, "post")
            self.assertTrue(result.ok, f"评论不该挤占发帖额度：{result.message}")

    async def test_bucket_key_differs_by_action(self) -> None:
        post_key = state.quota_bucket_key(RunMode.REAL, "2026-11-07", "post")
        comment_key = state.quota_bucket_key(RunMode.REAL, "2026-11-07", "comment")
        self.assertNotEqual(post_key, comment_key)
        self.assertTrue(post_key.endswith(":post"))
        self.assertTrue(comment_key.endswith(":comment"))
