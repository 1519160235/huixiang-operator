# -*- coding: utf-8 -*-
"""回响插件：发帖多样化单测（体裁轮换 / 标题去重 / 配图随内容变）。

【背景】真机 2026-11-08 用户报障：
1. 「连续发三个，题目是一模一样」——没有历史标题约束，每次同一道提示词；
2. 「配图大差不差」——配图提示词写死成「书桌 + 手机 + 饮料 + 台灯」。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import imagegen, voices  # noqa: E402
from layers import create as create_mod  # noqa: E402
import state  # noqa: E402


class FormatRotationTest(unittest.TestCase):
    def test_pick_avoids_recent(self) -> None:
        # 非汇总类动机必须连着挑不重复（汇总类有配额，见下一个用例）。
        singles = [f["id"] for f in voices.POST_FORMATS if f["material"] != "multi"]
        picked: list[str] = []
        for _ in range(len(singles)):
            fmt = voices.pick_format(picked)
            self.assertNotIn(fmt["id"], picked)
            picked.append(fmt["id"])
        self.assertEqual(len(set(picked)), len(singles))

    def test_multi_has_quota(self) -> None:
        # 最近 3 篇里已经有汇总，这次不能再给汇总。
        picked = ["daily_log"]
        for _ in range(3):
            fmt = voices.pick_format(picked)
            picked.append(fmt["id"])
        tail = picked[-3:]
        multis = [fid for fid in tail if fid in {"daily_log", "compare"}]
        self.assertLessEqual(len(multis), 1)

    def test_no_material_only_life(self) -> None:
        fmt = voices.pick_format([], has_material=False)
        self.assertEqual(fmt["material"], "life")

    def test_pick_after_all_used_returns_oldest(self) -> None:
        all_ids = [f["id"] for f in voices.POST_FORMATS]
        # 全部用过，最早用的是 all_ids[0]，应重新轮回到它。
        fmt = voices.pick_format(all_ids)
        self.assertEqual(fmt["id"], all_ids[0])

    def test_get_format_fallback(self) -> None:
        self.assertEqual(voices.get_format("not-exist")["id"], voices.POST_FORMATS[0]["id"])
        self.assertEqual(voices.get_format("")["id"], voices.POST_FORMATS[0]["id"])


class TitleDedupTest(unittest.TestCase):
    def test_similar_titles_detected(self) -> None:
        a = "周日早七点随手翻翻黑盒"
        b = "周日早上随手翻翻黑盒"
        self.assertGreater(voices.title_similarity(a, b), 0.55)

    def test_different_titles_pass(self) -> None:
        a = "这价格你们真会买吗"
        b = "影之刃零的光影值回票价"
        self.assertLess(voices.title_similarity(a, b), 0.3)

    def test_similar_title_returns_match(self) -> None:
        recent = ["周日早七点随手翻翻黑盒", "这价格你们真会买吗"]
        self.assertEqual(voices.similar_title("周日早七点随手翻翻黑盒", recent), recent[0])
        self.assertEqual(voices.similar_title("影之刃零的光影值回票价", recent), "")

    def test_recent_titles_block_lists_titles(self) -> None:
        block = voices.recent_titles_block(["标题一", "标题二"])
        self.assertIn("标题一", block)
        self.assertIn("不许", block)
        self.assertEqual(voices.recent_titles_block([]), "")


class PostHistoryStoreTest(unittest.TestCase):
    def test_add_and_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.PostHistoryStore(Path(tmp))
            store.add(post_id="1", title="第一篇", format_id="daily_log", chars=200)
            store.add(post_id="2", title="第二篇", format_id="ask", chars=210)
            self.assertEqual(store.recent_titles(5), ["第二篇", "第一篇"])
            self.assertEqual(store.recent(1)[0]["format"], "ask")
            # 体裁按时间正序，供 pick_format 判断谁最久没用。
            self.assertEqual(store.recent_formats(5), ["daily_log", "ask"])

    def test_max_entries_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.PostHistoryStore(Path(tmp), max_entries=3)
            for i in range(6):
                store.add(post_id=str(i), title=f"标题{i}", format_id="daily_log")
            titles = store.recent_titles(10)
            self.assertEqual(len(titles), 3)
            self.assertEqual(titles[0], "标题5")

    def test_container_wires_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            container = state.StoreContainer(Path(tmp))
            self.assertTrue(hasattr(container, "post_history"))
            container.post_history.add(title="x", format_id="ask")
            self.assertEqual(container.post_history.recent_titles(3), ["x"])


class ImagePromptVarietyTest(unittest.TestCase):
    def test_scene_passed_through_verbatim(self) -> None:
        scene = "厨房里煮着面，锅盖被顶起来"
        self.assertEqual(imagegen.build_image_prompt("标题", "正文", scene=scene), scene)

    def test_no_extra_rules_at_all(self) -> None:
        # 用户 2026-11-08：「规则是我定的，按他自己的人设生成照片就行了，
        # 想怎么发怎么发，只要不凭空捏造就行」「你怎么老想着做限制呢？」
        # 所以这里一条附加限制都不许有 —— 包括我之前自作主张加的「不要截图」。
        prompt = imagegen.build_image_prompt("标题", "正文", scene="随手拍的一张")
        for banned in (
            "严禁", "必须", "不要", "禁止", "截图", "插画", "3D",
            "自然光", "噪点", "文字", "水印", "logo",
        ):
            self.assertNotIn(banned, prompt, f"不该出现限制词：{banned}")

    def test_fallback_gives_post_content(self) -> None:
        # 兜底不再套我写的场景模板，直接把帖子交给生图模型自己判断。
        prompt = imagegen.build_image_prompt("养鱼日记", "我的鱼今天不吃食")
        self.assertIn("养鱼日记", prompt)
        self.assertIn("我的鱼今天不吃食", prompt)
        for banned in ("书桌", "台灯", "暖色", "手机随手拍"):
            self.assertNotIn(banned, prompt, f"兜底不该出现预设场景：{banned}")

    def test_fallback_differs_by_content(self) -> None:
        fish = imagegen.build_image_prompt("养鱼日记", "我的鱼今天不吃食")
        game = imagegen.build_image_prompt("打游戏", "开黑到凌晨")
        self.assertNotEqual(fish, game)

    def test_fallback_has_no_style_words(self) -> None:
        # 兜底也只是把帖子内容交出去，不掺我的风格词。
        prompt = imagegen.build_image_prompt("周六傍晚刷了会儿", "活侠传存档、工程软件报错")
        for banned in ("严禁", "不要", "必须", "自然光", "噪点", "随手拍"):
            self.assertNotIn(banned, prompt, f"不该出现限制/风格词：{banned}")

    def test_describe_scene_without_llm_returns_empty(self) -> None:
        import asyncio

        class Ctx:
            pass

        self.assertEqual(asyncio.run(imagegen.describe_scene(Ctx(), title="t", body="b")), "")

    def test_describe_scene_strips_prefix(self) -> None:
        import asyncio

        class Ctx:
            async def generate_text(self, prompt: str) -> str:
                return "配图：桌面上摊着一本翻开的书，旁边是凉掉的咖啡"

        out = asyncio.run(imagegen.describe_scene(Ctx(), title="t", body="b"))
        self.assertEqual(out, "桌面上摊着一本翻开的书，旁边是凉掉的咖啡")


class ImageSizeAndCompressTest(unittest.TestCase):
    """4K 生图：尺寸可配、超 5 MB 自动压。"""

    def test_generator_keeps_size(self) -> None:
        gen = imagegen.build_generator("http://x", "k", "m", "3840x2160")
        self.assertIsNotNone(gen)
        self.assertEqual(gen.size, "3840x2160")

    def test_small_image_untouched(self) -> None:
        raw = b"\xff\xd8" + b"0" * 100
        self.assertEqual(imagegen._fit_upload(raw), raw)

    def test_big_image_compressed_under_limit(self) -> None:
        import io
        import random

        from PIL import Image

        # 造一张真·大 PNG（逐像素随机，压不小），模拟 4K 生图 ~6 MB 的输出。
        size = 2880
        random.seed(0)
        raw = bytearray(random.getrandbits(8) for _ in range(size * size * 3))
        img = Image.frombytes("RGB", (size, size), bytes(raw))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        big = buf.getvalue()
        self.assertGreater(len(big), imagegen.MAX_UPLOAD_BYTES)
        out = imagegen._fit_upload(big)
        self.assertLessEqual(len(out), imagegen.MAX_UPLOAD_BYTES)
        self.assertEqual(out[:2], b"\xff\xd8", "应当转成 JPEG")


class SceneDescriptionLengthTest(unittest.TestCase):
    """配图描述不设限制：用户 2026-11-08「他描述 2000 个字也行啊，你怎么老想着做限制呢？」"""

    def test_prompt_has_no_length_cap(self) -> None:
        from layers.imagegen import _SCENE_PROMPT

        text = _SCENE_PROMPT.format(persona="", title="t", body="b")
        # 明确允许写长，且不再给字数区间。
        self.assertIn("想写多长写多长", text)
        self.assertIn("两千字", text)
        for banned in ("120–220", "80 字", "以内"):
            self.assertNotIn(banned, text)

    def test_prompt_has_no_checklist(self) -> None:
        # 不再列「必须写清镜头/主体/环境」这种清单，全交给模型。
        from layers.imagegen import _SCENE_PROMPT

        text = _SCENE_PROMPT.format(persona="", title="t", body="b")
        for banned in ("镜头怎么摆", "画面里有什么", "在哪儿拍的", "不强制"):
            self.assertNotIn(banned, text)

    def test_long_description_kept_intact(self) -> None:
        import asyncio

        long_text = "桌面上摊着练习册，台灯打着暖光。" * 200  # ~3600 字

        class Ctx:
            async def generate_text(self, prompt: str) -> str:
                return long_text

        out = asyncio.run(imagegen.describe_scene(Ctx(), title="t", body="b"))
        self.assertEqual(len(out), len(long_text), "不该截断")
        self.assertEqual(out, long_text)

    def test_prefix_stripped_only(self) -> None:
        import asyncio

        class Ctx:
            async def generate_text(self, prompt: str) -> str:
                return "配图：贴着桌面拍的，手机屏幕亮着"

        out = asyncio.run(imagegen.describe_scene(Ctx(), title="t", body="b"))
        self.assertEqual(out, "贴着桌面拍的，手机屏幕亮着")


if __name__ == "__main__":
    unittest.main()
