# -*- coding: utf-8 -*-
"""配置切换「演练 → 自动」后，worker 必须按新模式重启（真机 2026-11-06）。"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plugin as plugin_mod  # noqa: E402
from models import RunMode  # noqa: E402


class _FakeWorker:
    def __init__(self, mode):
        self.mode = mode
        self.running = True
        self.interval_seconds = 300

    def start(self):
        self.running = True

    async def stop(self):
        self.running = False


class TestModeSwitch(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, tmp: str, mode: str):
        inst = plugin_mod.create_plugin()
        inst._set_context(SimpleNamespace(
            paths=SimpleNamespace(data_dir=Path(tmp), state_dir=Path(tmp),
                                  runtime_dir=Path(tmp)),
            logger=logging.getLogger("t"),
            send=SimpleNamespace(text=lambda *a, **k: None),
            api=None, llm=None, config=None,
        ))
        inst.set_plugin_config({
            "plugin": {"config_version": "1.0.0"},
            "basic": {"enabled": True, "mode": mode},
            "browse": {"browse_interval_seconds": 300},
        })
        inst._store = __import__("state").StoreContainer(Path(tmp))
        return inst

    async def test_runtime_mode_follows_chinese_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._plugin(tmp, "演练")
            self.assertIs(inst._runtime_mode(), RunMode.DRY_RUN)
            inst.set_plugin_config({
                "plugin": {"config_version": "1.0.0"},
                "basic": {"enabled": True, "mode": "自动"},
                "browse": {"browse_interval_seconds": 300},
            })
            self.assertIs(inst._runtime_mode(), RunMode.REAL, "切成「自动」必须变成真实模式")

    async def test_worker_restarts_when_mode_changes(self) -> None:
        """worker 已在跑但模式变了 → 必须停旧启新，不能直接 return。"""
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._plugin(tmp, "演练")
            old = _FakeWorker(RunMode.DRY_RUN)
            inst._worker = old

            created = []
            original_cls = plugin_mod.OpsWorker

            def fake_worker(*a, **kw):
                w = _FakeWorker(kw.get("mode", RunMode.DRY_RUN))
                created.append(w)
                return w

            plugin_mod.OpsWorker = fake_worker
            try:
                inst.set_plugin_config({
                    "plugin": {"config_version": "1.0.0"},
                    "basic": {"enabled": True, "mode": "自动"},
                    "browse": {"browse_interval_seconds": 300},
                })
                inst._start_worker()
                await asyncio.sleep(0.05)  # 让 _restart_worker 的 task 跑完
                self.assertFalse(old.running, "旧 worker 应该被停掉")
                self.assertTrue(created, "应该用新模式创建新 worker")
                self.assertIs(created[0].mode, RunMode.REAL, "新 worker 必须是真实模式")
            finally:
                plugin_mod.OpsWorker = original_cls


if __name__ == "__main__":
    unittest.main(verbosity=2)
