# -*- coding: utf-8 -*-
"""回响插件：真机联调回归测试（2026-11-06）。

【这两个 bug 都是「本地测试全过、真机一跑就废」的典型】
1. 社交电量兜底成 0.0 → 没接生活频率时永远「不宜开口」，发帖流水线全被挡死；
2. 模型话太少 → 总结只有 120 字，重试提示词与首次完全相同，压不出更多内容。

本文件把这两条钉死，防止以后改回去。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import create, sense  # noqa: E402
from models import LifeTrace  # noqa: E402


# ===================== 社交电量「未知」不能当「没电」 =====================

class TestSocialBatteryUnknown(unittest.IsolatedAsyncioTestCase):
    """真机踩坑：LifeTrace.social_battery 默认 0.0，导致 can_speak_now 恒返回 False。"""

    def _ctx(self, **sense_cfg) -> SimpleNamespace:
        ctx = SimpleNamespace()
        ctx.config = {"sense": {"pause_on_sleep": True, "social_battery_threshold": 40, **sense_cfg}}
        return ctx

    async def test_none_battery_does_not_block(self) -> None:
        """社交电量为 None（未接生活频率）→ 不拦截。"""
        trace = LifeTrace(routine="午后", social_battery=None)
        self.assertTrue(await sense.can_speak_now(self._ctx(), trace))

    async def test_zero_battery_blocks(self) -> None:
        """明确读到 0 电量 → 拦截。"""
        trace = LifeTrace(routine="午后", social_battery=0.0)
        self.assertFalse(await sense.can_speak_now(self._ctx(), trace))

    async def test_low_battery_blocks(self) -> None:
        trace = LifeTrace(routine="午后", social_battery=0.2)
        self.assertFalse(await sense.can_speak_now(self._ctx(), trace))

    async def test_enough_battery_passes(self) -> None:
        trace = LifeTrace(routine="午后", social_battery=0.8)
        self.assertTrue(await sense.can_speak_now(self._ctx(), trace))

    async def test_sleep_still_blocks(self) -> None:
        """睡觉时段即使电量未知也要拦。"""
        trace = LifeTrace(routine="深夜", social_battery=None)
        self.assertFalse(await sense.can_speak_now(self._ctx(), trace))

    def test_default_is_none_not_zero(self) -> None:
        """默认值必须是 None（未知），不能是 0.0（没电）。"""
        self.assertIsNone(LifeTrace().social_battery)
        self.assertIsNone(LifeTrace().energy)


# ===================== 总结字数不足要能补到下限 =====================

class TestPadSummary(unittest.TestCase):
    """真机踩坑：模型只写 120 字，重试也压不出更多，整条总结链路判失败。"""

    ITEMS = [
        {"title": "谷歌新规：学校若禁用Gemini将失去搜索访问权限"},
        {"title": "再见了所有的chatgpt plus"},
        {"title": "什么DeepSeek它居然认识自己"},
        {"title": "大肥鱼你要干什么"},
        {"title": "我们是否高估了AI低估了自己"},
        {"title": "每日梗图：一代人有一代人的鸡蛋领"},
    ]

    def test_pads_short_text_to_min(self) -> None:
        short = "今天刷了会儿小黑盒，看到几个帖子挺有意思的，有聊AI的也有吐槽日常的，看着挺放松。"
        out = create.pad_summary_to_min(short, self.ITEMS, 200, 400)
        self.assertGreaterEqual(create.count_chars(out), 200, f"补足后仍不足：{create.count_chars(out)}")
        self.assertLessEqual(create.count_chars(out), 400)

    def test_leaves_long_text_untouched(self) -> None:
        long_text = "字" * 250
        self.assertEqual(create.pad_summary_to_min(long_text, self.ITEMS, 200, 400), long_text)

    def test_no_material_returns_original(self) -> None:
        short = "太短了"
        self.assertEqual(create.pad_summary_to_min(short, [], 200, 400), short)

    def test_does_not_repeat_same_list(self) -> None:
        """补足内容不能把同一批标题列两遍（读起来太机械）。"""
        short = "今天刷了会儿小黑盒，随便看看。"
        out = create.pad_summary_to_min(short, self.ITEMS, 200, 400)
        # 同一个标题最多出现一次
        for item in self.ITEMS[:3]:
            title = item["title"]
            self.assertLessEqual(out.count(title), 1, f"标题重复出现：{title}")

    def test_respects_max_chars(self) -> None:
        """素材很长时也不能突破上限。"""
        many = [{"title": "很长的标题" * 8} for _ in range(12)]
        out = create.pad_summary_to_min("短", many, 200, 260)
        self.assertLessEqual(create.count_chars(out), 260)


# ===================== 总结重试提示词要带「上次差多少」 =====================

class TestSummaryRetryPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_retry_prompt_mentions_shortfall(self) -> None:
        """第二次生成的提示词必须提到「上一版多少字」，否则模型会一直给同样长度。"""
        prompts: list[str] = []

        class FakeCtx:
            config = {"summarize": {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4}}

        async def fake_generate(ctx_arg, prompt: str) -> str:
            prompts.append(prompt)
            return "标题\n短。"  # 每次都很短，逼它走重试

        original = create._generate_text
        create._generate_text = fake_generate
        try:
            await create.generate_summary(
                FakeCtx(),
                [{"title": "测试素材"}],
                LifeTrace(routine="午后"),
                max_attempts=3,
            )
        finally:
            create._generate_text = original

        self.assertGreaterEqual(len(prompts), 2, "应该至少重试一次")
        self.assertIn("上一版", prompts[1], "重试提示词里没提上一版字数，模型不会改长度")


if __name__ == "__main__":
    unittest.main(verbosity=2)
