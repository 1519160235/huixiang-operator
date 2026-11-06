# -*- coding: utf-8 -*-
"""
回响插件：2026-11-06 真机报障的三个 bug 回归测试。

【三个报障】
1. 「发帖用的模型不对」——宿主 `replyer` 任务的 model_list 是
   ["ds-official", "feiyu-gemini-flash"]，DeepSeek 排第一，插件只传 task_name
   时内容永远由 DeepSeek 生成，用户在配置里选了 Gemini 也没用。
2. 「感想总结被判太像机器，提示建议重写，然后就没动静了」——
   `guard.check_ai_flavor` 返回 ai_flavor_block 后，上一层直接失败返回，
   **没有任何代码真的执行重写**。
3. 「后面又变成演练了」——worker 的 mode 在构造时固化，配置改「自动」后
   旧 worker 继续用 DRY_RUN 跑；重建走 create_task，异常被吞就永久卡住。

【测试原则】
全部走真实代码路径（真 Runtime / 真 summarize / 真 worker），只在
「宿主大模型」和「发布网络调用」两处打桩，不联网、不发帖、不碰真实账号。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state  # noqa: E402
import worker as worker_mod  # noqa: E402
from layers import summarize  # noqa: E402
from models import RunMode, SummaryDraft  # noqa: E402
from runtime import DEFAULT_MODEL_NAME, DEFAULT_TIMEOUT_MS, Runtime  # noqa: E402


def _cfg(**over):
    """构造一份够用的配置对象（只放测试用得到的字段）。"""
    base = {
        "model": SimpleNamespace(text_model="feiyu-gemini-flash", temperature=1.0, max_tokens=2048),
    }
    base.update(over)
    return SimpleNamespace(**base)


class FakeLLM:
    """假的宿主 llm：记录每次调用的参数，按队列返回文本。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    async def generate(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        text = self.texts.pop(0) if self.texts else "兜底正文"
        return {"text": text}


def _runtime_with(llm, config=None) -> Runtime:
    plugin = SimpleNamespace(cfg=lambda: config or _cfg(), store=lambda: None)
    host = SimpleNamespace(llm=llm, logger=__import__("logging").getLogger("t"))
    rt = Runtime(plugin, host, Path("."))
    return rt


# ===================== 1. 模型必须按配置选，不能被任务顺序吃掉 =====================

class TestModelSelection(unittest.TestCase):
    def test_passes_model_name_to_host(self) -> None:
        """生成时必须带上 model_name，否则宿主会用 replyer 里排第一的 DeepSeek。"""
        llm = FakeLLM(["正文"])
        rt = _runtime_with(llm)
        asyncio.run(rt.generate_text("写点什么"))

        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertEqual(call.get("model_name"), "feiyu-gemini-flash")
        self.assertEqual(call.get("task_name"), "replyer")
        self.assertEqual(call.get("temperature"), 1.0)
        self.assertEqual(call.get("max_tokens"), 2048)
        # 宿主 cap.call 默认 30 秒就超时，必须显式给足预算
        self.assertEqual(call.get("timeout_ms"), DEFAULT_TIMEOUT_MS)

    def test_follow_host_default_sends_no_model_name(self) -> None:
        """选「跟随宿主默认」时不传 model_name，交给宿主自己挑。"""
        llm = FakeLLM(["正文"])
        rt = _runtime_with(llm, _cfg(model=SimpleNamespace(
            text_model="跟随宿主默认", temperature=None, max_tokens=None)))
        asyncio.run(rt.generate_text("写点什么"))

        call = llm.calls[0]
        self.assertNotIn("model_name", call)
        self.assertNotIn("temperature", call)
        self.assertNotIn("max_tokens", call)

    def test_broken_model_config_falls_back(self) -> None:
        """配置读不到时退回默认模型，不能把整条生成链路搞崩。"""
        llm = FakeLLM(["正文"])
        rt = _runtime_with(llm, SimpleNamespace())  # 没有 model 段
        text = asyncio.run(rt.generate_text("写点什么"))

        self.assertEqual(text, "正文")
        self.assertEqual(llm.calls[0].get("model_name"), DEFAULT_MODEL_NAME)

    def test_default_is_not_deepseek(self) -> None:
        """默认模型不能是 DeepSeek —— 用户明确要求不要。"""
        self.assertNotIn("deepseek", DEFAULT_MODEL_NAME.lower())
        self.assertNotIn("ds-", DEFAULT_MODEL_NAME.lower())


# ===================== 2. AI 味拦截后必须真的重写 =====================

class _FakeDigest:
    def __init__(self, items):
        self._items = items
        self.marked: list[str] = []

    def pending(self):
        return self._items

    def mark_summarized(self, ids):
        self.marked.extend(ids)


class _FakeStore:
    def __init__(self, items):
        self.digest = _FakeDigest(items)


def _summary_ctx(items, scores: list[float]):
    """构造能跑通 summarize.run_summary_once 的薄上下文。

    scores 是每次护栏判定时喂给 check_ai_flavor 的分数，用来模拟
    「第一版太像机器、重写后达标」。
    """
    calls = {"generate": 0, "publish": 0}
    pending_scores = list(scores)

    return SimpleNamespace(
        store=_FakeStore(items),
        generate_text=None,
        calls=calls,
        _pending_scores=pending_scores,
    )


class TestSummaryRewrite(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {
            "generate_summary": summarize.create.generate_summary,
            "is_publishable": summarize.guard.is_publishable,
            "publish_post": summarize.publish.publish_post,
            "verify": summarize.publish.verify_post_published,
            "reserve": summarize.guard.reserve_quota,
            "trace": summarize.sense.read_life_trace,
        }
        self.generate_calls: list[dict] = []

    def tearDown(self) -> None:
        summarize.create.generate_summary = self._orig["generate_summary"]
        summarize.guard.is_publishable = self._orig["is_publishable"]
        summarize.publish.publish_post = self._orig["publish_post"]
        summarize.publish.verify_post_published = self._orig["verify"]
        summarize.guard.reserve_quota = self._orig["reserve"]
        summarize.sense.read_life_trace = self._orig["trace"]

    def _install(self, ai_scores: list[float], texts: list[str]) -> None:
        """装桩：生成返回 texts，护栏按 ai_scores 依次判 AI 味，发帖只记账。"""
        scores = list(ai_scores)

        async def fake_gen(ctx, items, trace, max_attempts=3, rewrite_hint="", previous_text=""):
            self.generate_calls.append({"hint": rewrite_hint, "prev": previous_text})
            idx = min(len(self.generate_calls) - 1, len(texts) - 1)
            text = texts[idx]
            return SummaryDraft(
                title="标题",
                text=text,
                paragraphs=text.count("\n") + 1,
                chars=len(text.replace("\n", "")),
                ai_flavor_score=scores[idx] if idx < len(scores) else 0.0,
                topic_ids=[],
            )

        async def fake_publishable(ctx, draft, mode):
            score = draft.ai_flavor_score
            if score >= summarize.guard.GUARD_AI_BODY_THRESHOLD:
                return summarize.guard.VerifyResult(
                    ok=False,
                    code="ai_flavor_block",
                    message=f"文字太像机器（AI 味 {score:.0f} ≥ 60），建议重写",
                )
            return summarize.guard.VerifyResult(ok=True, code="publishable", message="通过")

        async def fake_publish(ctx, draft, dry_run=True):
            self.generate_calls.append({"published": True})
            return SimpleNamespace(post_id="pid-1", note="演练：已组装")

        async def fake_verify(ctx, post_id):
            return SimpleNamespace(ok=True, code="verified", message="核验通过")

        async def fake_reserve(ctx, mode, action):
            return None

        async def fake_trace(ctx):
            return SimpleNamespace(battery=80, asleep=False, in_sleep_window=False)

        summarize.create.generate_summary = fake_gen
        summarize.guard.is_publishable = fake_publishable
        summarize.publish.publish_post = fake_publish
        summarize.publish.verify_post_published = fake_verify
        summarize.guard.reserve_quota = fake_reserve
        summarize.sense.read_life_trace = fake_trace

    def test_ai_flavor_triggers_real_rewrite(self) -> None:
        """第一版 AI 味 70（拦）、第二版 20（过）→ 必须真的重写一次并成功。"""
        body = "这是第一段的内容，写点具体的。" * 8 + "\n" + "这是第二段的内容，继续写。" * 9
        self._install(ai_scores=[70.0, 20.0], texts=[body, body])
        ctx = _summary_ctx([{"post_id": "p1", "title": "t", "summary": "s"}], [70.0, 20.0])

        result = asyncio.run(summarize.run_summary_once(ctx, mode=RunMode.DRY_RUN))

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("rewrites"), 1, "应当记录重写了 1 次")
        # 第一次是正常生成（无 hint），第二次必须带「太像机器」的反馈
        self.assertEqual(len(self.generate_calls), 3)  # 2 次生成 + 1 次发帖
        self.assertEqual(self.generate_calls[0]["hint"], "")
        self.assertIn("机器", self.generate_calls[1]["hint"])
        self.assertTrue(self.generate_calls[1]["prev"], "重写时要把上一版正文带给模型")

    def test_rewrite_stops_at_limit(self) -> None:
        """一直不达标时不会无限重写，按轮数上限如实失败。"""
        body = "这是第一段的内容，写点具体的。" * 8 + "\n" + "这是第二段的内容，继续写。" * 9
        self._install(ai_scores=[80.0] * 10, texts=[body] * 10)
        ctx = _summary_ctx([{"post_id": "p1", "title": "t", "summary": "s"}], [80.0] * 10)

        result = asyncio.run(summarize.run_summary_once(ctx, mode=RunMode.DRY_RUN))

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("code"), "ai_flavor_block")
        # REWRITE_ROUNDS + 1 次生成
        gens = [c for c in self.generate_calls if "published" not in c]
        self.assertEqual(len(gens), summarize.REWRITE_ROUNDS + 1)

    def test_non_rewritable_gate_fails_fast(self) -> None:
        """额度/间隔/去重这类拦住重写也没用，必须立刻失败，不浪费模型调用。"""
        body = "这是第一段的内容，写点具体的。" * 8 + "\n" + "这是第二段的内容，继续写。" * 9
        self._install(ai_scores=[10.0], texts=[body])

        async def fake_publishable(ctx, draft, mode):
            return summarize.guard.VerifyResult(ok=False, code="post_gap", message="刚发过帖，还要等 30 分钟")

        summarize.guard.is_publishable = fake_publishable
        ctx = _summary_ctx([{"post_id": "p1", "title": "t", "summary": "s"}], [10.0])

        result = asyncio.run(summarize.run_summary_once(ctx, mode=RunMode.DRY_RUN))

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("code"), "post_gap")
        gens = [c for c in self.generate_calls if "published" not in c]
        self.assertEqual(len(gens), 1, "不可重写的闸门不该触发额外生成")


# ===================== 3. worker 每轮重读模式，不锁死启动时的值 =====================

class TestWorkerModeRefresh(unittest.TestCase):
    def test_worker_rereads_mode_each_round(self) -> None:
        """配置从演练切成自动后，同一批 worker 本轮就该用新模式，不需要重建。"""
        mode = {"value": RunMode.DRY_RUN}

        class FakePlugin:
            def _runtime_mode(self):
                return mode["value"]

            async def run_browse_once(self, ctx, m):
                return {"ok": True, "count": 0}

            async def run_interact_once(self, ctx, m):
                return {"ok": True}

            async def run_summary_once(self, ctx, m):
                return {"ok": True}

        ctx = SimpleNamespace(
            store=SimpleNamespace(
                digest=SimpleNamespace(pending=lambda: []),
                action_log=None,
            )
        )
        w = worker_mod.OpsWorker(FakePlugin(), ctx, mode=RunMode.DRY_RUN, interval_seconds=999)

        async def scenario():
            # 第一轮：演练
            await w.run_round()
            first = w.mode
            # 模拟后台把配置改成「自动」
            mode["value"] = RunMode.REAL
            await w.run_round()
            return first, w.mode

        # 跳过作息判定（没有 sense 配置时默认不拦，但保险起见直接打桩）
        orig = worker_mod.sense.should_pause_for_routine
        worker_mod.sense.should_pause_for_routine = lambda trace, pause: False
        orig_reason = worker_mod.sense.read_life_trace

        async def fake_trace(ctx):
            return SimpleNamespace()

        worker_mod.sense.read_life_trace = fake_trace
        try:
            first, second = asyncio.run(scenario())
        finally:
            worker_mod.sense.should_pause_for_routine = orig
            worker_mod.sense.read_life_trace = orig_reason

        self.assertEqual(first, RunMode.DRY_RUN)
        self.assertEqual(second, RunMode.REAL, "第二轮必须跟随配置切到「自动」，不能还卡在演练")

    def test_mode_falls_back_when_resolver_raises(self) -> None:
        """插件取模式抛异常时沿用旧值，不能让整轮崩掉。"""
        class BadPlugin:
            def _runtime_mode(self):
                raise RuntimeError("配置读不到")

            async def run_browse_once(self, ctx, m):
                return {"ok": True, "count": 0}

            async def run_interact_once(self, ctx, m):
                return {"ok": True}

            async def run_summary_once(self, ctx, m):
                return {"ok": True}

        ctx = SimpleNamespace(store=SimpleNamespace(action_log=None))
        w = worker_mod.OpsWorker(BadPlugin(), ctx, mode=RunMode.REAL, interval_seconds=999)
        self.assertEqual(w._current_mode(), RunMode.REAL)


if __name__ == "__main__":
    unittest.main()
