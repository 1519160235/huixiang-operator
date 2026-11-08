# -*- coding: utf-8 -*-
"""回响插件：发帖时机单测（时段窗口 / 间隔 / 生活状态）。

【背景】真机 2026-11-08 用户反馈：「额度三个没错，MCP 时代是早中晚各一个，
现在想什么时候发什么时候发，配合情绪插件」。于是从「每天硬额度」改成
「早/中/晚窗口，每窗口最多一条，窗口内有料就发」。
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import schedule  # noqa: E402

SLOTS = "08:00-11:00,12:00-14:00,19:00-23:00"


class ParseSlotsTest(unittest.TestCase):
    def test_normal(self) -> None:
        self.assertEqual(schedule.parse_slots("08:00-11:00,19:00-23:00"), [(480, 660), (1140, 1380)])

    def test_bad_pieces_skipped(self) -> None:
        slots = schedule.parse_slots("08:00-11:00,乱写,12:00-12:00,14:00-13:00")
        self.assertEqual(slots, [(480, 660)])

    def test_empty_falls_back_to_default(self) -> None:
        self.assertEqual(schedule.parse_slots(""), schedule.parse_slots(schedule.DEFAULT_SLOTS))

    def test_default_is_single_wide_window(self) -> None:
        # 默认是「白天到深夜一整段」，不是切成三段 —— 用户要的是想发就发。
        self.assertEqual(len(schedule.parse_slots(schedule.DEFAULT_SLOTS)), 1)


class CurrentSlotTest(unittest.TestCase):
    def test_hit(self) -> None:
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 9, 30), SLOTS), "08:00-11:00")
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 20, 0), SLOTS), "19:00-23:00")

    def test_miss(self) -> None:
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 15, 0), SLOTS), "")
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 3, 0), SLOTS), "")

    def test_cross_midnight(self) -> None:
        slots = "22:00-02:00"
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 23, 30), slots), "22:00-02:00")
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 1, 0), slots), "22:00-02:00")
        self.assertEqual(schedule.current_slot(datetime(2026, 11, 8, 12, 0), slots), "")

    def test_slot_key_has_date(self) -> None:
        key = schedule.slot_key(datetime(2026, 11, 8, 9, 30), SLOTS)
        self.assertEqual(key, "2026-11-08|08:00-11:00")
        self.assertEqual(schedule.slot_key(datetime(2026, 11, 8, 15, 0), SLOTS), "")


class ShouldPostNowTest(unittest.TestCase):
    def _call(self, **kwargs):
        base = dict(
            now=datetime(2026, 11, 8, 9, 30),
            slots_raw=SLOTS,
            posted_slots=[],
            last_post_ts=0.0,
            today_count=0,
            hard_limit=5,
            min_gap_minutes=30,
            trace=None,
        )
        base.update(kwargs)
        return schedule.should_post_now(**base)

    def test_ok(self) -> None:
        ok, reason = self._call()
        self.assertTrue(ok, reason)

    def test_no_time_window_limit(self) -> None:
        # 用户 2026-11-08：「既然都说了他是自己想发，那就不要设置什么时间段了」
        # 任何时间点都不该被时段挡住。
        for hour in (3, 9, 15, 23):
            ok, reason = self._call(now=datetime(2026, 11, 8, hour, 0))
            self.assertTrue(ok, f"{hour} 点被拦了：{reason}")

    def test_slot_already_used_still_allowed(self) -> None:
        # 「每段一条」的限制也取消了，发过还能再发（上限交给每日额度）。
        ok, reason = self._call(posted_slots=["2026-11-08|08:00-11:00"])
        self.assertTrue(ok, reason)

    def test_hard_limit(self) -> None:
        ok, reason = self._call(today_count=5, hard_limit=5)
        self.assertFalse(ok)
        self.assertIn("硬上限", reason)

    def test_min_gap_no_longer_blocks(self) -> None:
        # 最小间隔也取消了：想连发就发，防失控靠每日额度。
        ok, reason = self._call(last_post_ts=time.time() - 60, min_gap_minutes=30)
        self.assertTrue(ok, reason)

    def test_sleep_blocked(self) -> None:
        trace = SimpleNamespace(routine="深夜", social_battery=None, energy=None)
        ok, reason = self._call(trace=trace)
        self.assertFalse(ok)
        self.assertIn("不适合发帖", reason)

    def test_low_battery_blocked(self) -> None:
        trace = SimpleNamespace(routine="", social_battery=0.1, energy=None)
        ok, reason = self._call(trace=trace)
        self.assertFalse(ok)
        self.assertIn("社交电量", reason)

    def test_unknown_life_state_passes(self) -> None:
        # 没接生活插件时不能把人锁死。
        trace = SimpleNamespace(routine="", social_battery=None, energy=None)
        ok, _ = self._call(trace=trace)
        self.assertTrue(ok)


class SlotBookkeepingTest(unittest.TestCase):
    def test_can_post_in_slot(self) -> None:
        self.assertTrue(schedule.can_post_in_slot([], "2026-11-08|08:00-11:00"))
        self.assertFalse(schedule.can_post_in_slot(["2026-11-08|08:00-11:00"], "2026-11-08|08:00-11:00"))
        self.assertFalse(schedule.can_post_in_slot([], ""))

    def test_trim_posted(self) -> None:
        rows = [f"k{i}" for i in range(40)]
        self.assertEqual(len(schedule.trim_posted(rows)), schedule.MAX_POSTED_SLOTS)
        self.assertEqual(schedule.trim_posted(rows)[-1], "k39")


if __name__ == "__main__":
    unittest.main()
