# -*- coding: utf-8 -*-
"""回响插件：2026-11-12 真机缺陷「模型单点故障」回归测试。

【事故】
中转站模型 feiyu-gemini-flash 持续硬超时 / 返回空回复，插件 167 轮互动
连续 ok=False，一条评论都发不出去。根因不是模型名字写错（它是有效别名），
而是 generate_text 的兜底只换 task_name，每轮都强制 model_name=首选模型：
四次尝试全打同一个挂掉的模型，等于没有兜底。

【修复契约】
1. 第一次仍按用户配置直选模型（保留「用 GM、不用 DeepSeek」的偏好）；
2. 一旦失败，后续尝试不再指定模型，让宿主按 model_list 顺序回退到备用模型；
3. 兜底阶段缩短超时预算，避免每轮卡十几分钟；
4. 全都失败才报错，且错误信息写清尝试过哪些组合。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import (  # noqa: E402
    DEFAULT_MODEL_NAME,
    DEFAULT_TIMEOUT_MS,
    FALLBACK_TIMEOUT_MS,
    Runtime,
)


class ScriptedLLM:
    """按脚本返回结果：字符串=正常文本，None=空回复，Exception=抛错。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def generate(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        item = self.script.pop(0) if self.script else "兜底正文"
        if isinstance(item, BaseException):
            raise item
        return {"text": item if item is not None else ""}


def _runtime(llm, text_model=DEFAULT_MODEL_NAME) -> Runtime:
    cfg = SimpleNamespace(model=SimpleNamespace(
        text_model=text_model, temperature=1.0, max_tokens=2048))
    plugin = SimpleNamespace(cfg=lambda: cfg, store=lambda: None)
    host = SimpleNamespace(llm=llm, logger=logging.getLogger("t"))
    return Runtime(plugin, host, Path("."))


class TestModelFallback(unittest.TestCase):
    def test_first_success_never_falls_back(self) -> None:
        """首选模型正常时只调一次，参数完全不变。"""
        llm = ScriptedLLM(["正文"])
        text = asyncio.run(_runtime(llm).generate_text("写点什么"))

        self.assertEqual(text, "正文")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0].get("model_name"), DEFAULT_MODEL_NAME)
        self.assertEqual(llm.calls[0].get("timeout_ms"), DEFAULT_TIMEOUT_MS)

    def test_timeout_then_host_default_chain(self) -> None:
        """首选超时后，第二次必须去掉 model_name，让宿主切到备用模型。"""
        llm = ScriptedLLM([TimeoutError("触发硬超时 120s"), "备用模型写的正文"])
        text = asyncio.run(_runtime(llm).generate_text("写点什么"))

        self.assertEqual(text, "备用模型写的正文")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0].get("model_name"), DEFAULT_MODEL_NAME)
        self.assertNotIn("model_name", llm.calls[1], "兜底仍在指定同一个模型，等于没兜底")
        self.assertEqual(llm.calls[1].get("timeout_ms"), FALLBACK_TIMEOUT_MS)

    def test_empty_reply_then_host_default_chain(self) -> None:
        """真机更常见的是「不抛异常、只回空字符串」，也必须能降级。"""
        llm = ScriptedLLM([None, "备用模型写的正文"])
        text = asyncio.run(_runtime(llm).generate_text("写点什么"))

        self.assertEqual(text, "备用模型写的正文")
        self.assertNotIn("model_name", llm.calls[1])

    def test_all_fail_raises_with_attempts(self) -> None:
        """全挂才报错，且错误信息要说明试过哪些组合，便于复盘。"""
        llm = ScriptedLLM([TimeoutError("超时"), None, TimeoutError("又超时")])
        with self.assertRaises(Exception) as ctx:
            asyncio.run(_runtime(llm).generate_text("写点什么"))

        self.assertEqual(len(llm.calls), 3, "兜底次数应收敛，别每轮卡十几分钟")
        self.assertIn("超时", str(ctx.exception))
        # 最后一次也换了任务参数，但始终不再指定挂掉的模型
        self.assertNotIn("model_name", llm.calls[1])
        self.assertNotIn("model_name", llm.calls[2])
        self.assertEqual(llm.calls[2].get("task_name"), "utils")

    def test_follow_host_default_has_no_extra_retry_when_ok(self) -> None:
        """「跟随宿主默认」本来就不指定模型，成功时不应产生多余调用。"""
        llm = ScriptedLLM(["正文"])
        text = asyncio.run(_runtime(llm, "跟随宿主默认").generate_text("写点什么"))

        self.assertEqual(text, "正文")
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("model_name", llm.calls[0])


if __name__ == "__main__":
    unittest.main()
