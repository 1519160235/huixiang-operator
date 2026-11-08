# -*- coding: utf-8 -*-
"""回响插件：「想不想发」判断单测。

【背景】真机 2026-11-08 用户要求：
「不是要他总结，他觉得好玩他就发帖，他觉得不好玩就不发帖。」
「就跟你发抖音、发朋友圈、发 QQ 空间，有分享就发呗，乐意发发呗，
没什么特定要求，爱发发呗，不发不发呗。」

原来的逻辑是「到时段 + 有素材 → 必然生成一条发出去」，等于交作业。
现在生成前先问一句「你想发吗」，不想发就直接跳过。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import create, voices  # noqa: E402


class ParseWantTest(unittest.TestCase):
    def test_want(self) -> None:
        want, reason = create._parse_want("想发\n那个显卡帖子太离谱了，想说两句")
        self.assertTrue(want)
        self.assertIn("显卡", reason)

    def test_not_want(self) -> None:
        want, reason = create._parse_want("不想发\n今天刷到的都挺平的")
        self.assertFalse(want)
        self.assertIn("平", reason)

    def test_not_want_not_misread_as_want(self) -> None:
        # 「不想发」里含「想发」子串，必须先判否定词。
        want, _ = create._parse_want("不想发")
        self.assertFalse(want)

    def test_empty_means_no(self) -> None:
        # 解析不出来宁可不发。
        self.assertFalse(create._parse_want("")[0])
        self.assertFalse(create._parse_want("嗯……")[0])

    def test_missing_second_line_ok(self) -> None:
        want, reason = create._parse_want("想发")
        self.assertTrue(want)
        self.assertEqual(reason, "")


class DecideWantTest(unittest.TestCase):
    ITEMS = [{"post_id": "1", "title": "显卡改修", "excerpt": "有人把显卡改成了轿车"}]

    def _ctx(self, reply: str):
        class Ctx:
            async def generate_text(self, prompt: str) -> str:
                self.prompt = prompt
                return reply

        ctx = Ctx()
        ctx.prompt = ""
        return ctx

    def test_decide_want(self) -> None:
        ctx = self._ctx("想发\n这改修也太离谱了")
        out = asyncio.run(create.decide_want_to_post(ctx, items=self.ITEMS, fmt=voices.get_format("fun_share")))
        self.assertTrue(out["want"])
        self.assertIn("离谱", out["reason"])

    def test_decide_not_want(self) -> None:
        ctx = self._ctx("不想发\n没什么想说的")
        out = asyncio.run(create.decide_want_to_post(ctx, items=self.ITEMS, fmt=voices.get_format("fun_share")))
        self.assertFalse(out["want"])

    def test_prompt_mentions_no_pressure(self) -> None:
        # 提示词要明确「没人逼你」，否则模型会为了发帖硬找理由。
        ctx = self._ctx("不想发")
        asyncio.run(create.decide_want_to_post(ctx, items=self.ITEMS, fmt=voices.get_format("fun_share")))
        self.assertIn("没人逼你", ctx.prompt)
        self.assertIn("朋友圈", ctx.prompt)
        self.assertIn("不要为了发帖而找理由", ctx.prompt)

    def test_no_items_and_not_life_returns_false(self) -> None:
        ctx = self._ctx("想发")
        out = asyncio.run(create.decide_want_to_post(ctx, items=[], fmt=voices.get_format("fun_share")))
        self.assertFalse(out["want"])

    def test_llm_failure_passes_through(self) -> None:
        # 判断失败不能把发帖卡死，按原逻辑放行。
        class Ctx:
            async def generate_text(self, prompt: str) -> str:
                raise RuntimeError("模型挂了")

        out = asyncio.run(create.decide_want_to_post(Ctx(), items=self.ITEMS, fmt=voices.get_format("fun_share")))
        self.assertTrue(out["want"])

    def test_no_llm_passes_through(self) -> None:
        class Ctx:
            pass

        out = asyncio.run(create.decide_want_to_post(Ctx(), items=self.ITEMS, fmt=voices.get_format("fun_share")))
        self.assertTrue(out["want"])


if __name__ == "__main__":
    unittest.main()
