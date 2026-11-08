# -*- coding: utf-8 -*-
"""模型动态解析测试（2026-11-07）。

背景：用户报障「中转站一个密钥可能有几个模型，不能把模型名写死」。
真机踩到 `feiyu-gemini-flash` 下线改名成 `gemini-3.8-flash`，旧名直接 404。
这里锁定「配置名失效时怎么自动找替代」的行为，防止改回去写死。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import Runtime  # noqa: E402


class _FakeLlm:
    def __init__(self, models):
        self._models = models
        self.calls = 0

    async def get_available_models(self):
        self.calls += 1
        return list(self._models)


class _FakePlugin:
    def __init__(self, text_model=""):
        self._cfg = SimpleNamespace(
            model=SimpleNamespace(text_model=text_model, temperature=None, max_tokens=None)
        )

    def cfg(self):
        return self._cfg

    def store(self):
        return SimpleNamespace()


def _make_rt(models, text_model=""):
    host = SimpleNamespace(llm=_FakeLlm(models), logger=SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None
    ))
    return Runtime(_FakePlugin(text_model), host, Path(tempfile.mkdtemp()))


class TestResolveModelName(unittest.IsolatedAsyncioTestCase):
    async def test_configured_model_available_kept(self) -> None:
        rt = _make_rt(["gemini-3.8-flash", "glm-4-flash"], "gemini-3.8-flash")
        self.assertEqual(await rt._resolve_model_name("gemini-3.8-flash"), "gemini-3.8-flash")

    async def test_stale_model_falls_back_to_family(self) -> None:
        # 真机场景：配置 feiyu-gemini-flash 已下线，可用的是 gemini-3.8-flash。
        rt = _make_rt(["gemini-3.8-flash", "glm-4-flash"])
        got = await rt._resolve_model_name("feiyu-gemini-flash")
        self.assertEqual(got, "gemini-3.8-flash")

    async def test_prefers_higher_version(self) -> None:
        rt = _make_rt(["gemini-2.5-flash", "gemini-3.8-flash"])
        self.assertEqual(await rt._resolve_model_name("feiyu-gemini-flash"), "gemini-3.8-flash")

    async def test_falls_back_by_preference_when_family_missing(self) -> None:
        # 没有 gemini 系时按偏好退到 glm。
        rt = _make_rt(["glm-4-flash", "glm-5.3-flash"])
        self.assertEqual(await rt._resolve_model_name("feiyu-gemini-flash"), "glm-5.3-flash")

    async def test_no_available_models_keeps_configured(self) -> None:
        # 拿不到列表（老宿主/接口异常）时不能乱改，维持原配置。
        rt = _make_rt([])
        self.assertEqual(await rt._resolve_model_name("feiyu-gemini-flash"), "feiyu-gemini-flash")

    async def test_nothing_matchable_returns_empty(self) -> None:
        # 全都不匹配时返回空串 = 不指定模型，交给任务默认顺序，避免整条链路废掉。
        rt = _make_rt(["some-random-model"])
        self.assertEqual(await rt._resolve_model_name("feiyu-gemini-flash"), "")

    async def test_models_list_cached(self) -> None:
        rt = _make_rt(["gemini-3.8-flash"])
        await rt._available_models()
        await rt._available_models()
        # 只向宿主要一次，避免每轮生成都打一次能力调用。
        self.assertEqual(rt.host.llm.calls, 1)


if __name__ == "__main__":
    unittest.main()