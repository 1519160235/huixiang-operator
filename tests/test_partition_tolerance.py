# -*- coding: utf-8 -*-
"""分区目录拉取失败不能拖垮浏览 / 总结（真机 2026-11-06 CommentRejectedError）。"""
from __future__ import annotations

import asyncio
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


class _BrokenPartitions:
    """模拟上游拒绝：TOPIC_INDEX 抛 CommentRejectedError。"""

    async def fetch_partitions(self, ctx):
        raise RuntimeError("CommentRejectedError: 上游拒绝")

    async def refresh_partitions(self, ctx, cache=None, force=False):
        raise RuntimeError("CommentRejectedError: 上游拒绝")


class TestPartitionFailureTolerance(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, tmp: str):
        inst = plugin_mod.create_plugin()
        inst._set_context(SimpleNamespace(
            paths=SimpleNamespace(data_dir=Path(tmp), state_dir=Path(tmp),
                                  runtime_dir=Path(tmp)),
            logger=__import__("logging").getLogger("t"),
            send=SimpleNamespace(text=lambda *a, **k: None, image=lambda *a, **k: None),
            api=None, llm=None, config=None,
        ))
        inst.set_plugin_config({
            "plugin": {"config_version": "1.0.0"},
            "basic": {"enabled": True, "mode": "演练"},
            "browse": {"partition_mode": "全部分区", "posts_per_round": 5},
            "publish": {"default_partition": "不指定分区"},
        })
        inst._store = __import__("state").StoreContainer(Path(tmp))
        return inst

    async def test_browse_survives_partition_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._plugin(tmp)
            rt = inst.runtime()
            original = plugin_mod.inbound.refresh_partitions

            async def broken(*a, **k):
                raise RuntimeError("CommentRejectedError")

            plugin_mod.inbound.refresh_partitions = broken
            try:
                result = await inst.run_browse_once(rt, RunMode.DRY_RUN)
                self.assertIsInstance(result, dict)
                self.assertIn("count", result, "浏览不该因为分区拉取失败而整轮抛异常")
            finally:
                plugin_mod.inbound.refresh_partitions = original

    async def test_pick_partition_survives_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._plugin(tmp)
            rt = inst.runtime()
            original = plugin_mod.inbound.fetch_partitions

            async def broken(*a, **k):
                raise RuntimeError("CommentRejectedError")

            plugin_mod.inbound.fetch_partitions = broken
            try:
                result = await inst._pick_partition(rt)
                self.assertIsNone(result, "拉不到分区时应返回 None，而不是抛异常")
            finally:
                plugin_mod.inbound.fetch_partitions = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
