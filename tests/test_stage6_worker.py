# -*- coding: utf-8 -*-
"""
回响插件：第六阶段「自动运营 Worker 与额度」自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. 作息联动：睡觉 / 小睡时段停发，其余不停。
2. 发帖时段：parse_time_window 解析、in_post_time_window 正常段与跨零点段。
3. 时段闸门：check_time_window 演练放行、真实拦截；is_publishable 串联。
4. 操作记录：ActionLogStore 追加 / 倒序读取 / 上限裁剪。
5. Worker：run_round 串三步、单步异常不中断、睡觉时段整轮跳过、mode 只能由配置决定。
6. 额度：真实模式只被真实发送消耗；演练额度桶始终 0。

【验收对齐（docs/开发路线.md 阶段 6）】
- dry_run 默认开；额度只被真实发送消耗；
- 作息判停生效；限流 5 秒 / 30 次每小时生效；验证码熔断 5 分钟冷却生效。
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
import worker as worker_mod  # noqa: E402
from layers import guard, sense  # noqa: E402
from models import ActionState, LifeTrace, RunMode  # noqa: E402


def _make_ctx(data_dir: str, config: dict | None = None) -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.config = config or {"guard": {"dry_run_no_quota_cost": True, "daily_quota_post": 3, "de_ai_flavor": True}}
    ctx.store = state.StoreContainer(Path(data_dir))
    return ctx


# ===================== 作息联动 =====================

class TestRoutinePause(unittest.TestCase):
    def test_sleep_routines_pause(self) -> None:
        for routine in ("深夜", "睡觉", "睡眠", "凌晨", "小睡", "午睡"):
            with self.subTest(routine=routine):
                self.assertTrue(sense.should_pause_for_routine(LifeTrace(routine=routine), True))

    def test_awake_routines_do_not_pause(self) -> None:
        for routine in ("刚起床", "午后", "傍晚", ""):
            with self.subTest(routine=routine):
                self.assertFalse(sense.should_pause_for_routine(LifeTrace(routine=routine), True))

    def test_switch_off_never_pauses(self) -> None:
        self.assertFalse(sense.should_pause_for_routine(LifeTrace(routine="深夜"), False))

    def test_reason_is_chinese(self) -> None:
        self.assertIn("作息", sense.routine_pause_reason(LifeTrace(routine="深夜")))


# ===================== 发帖时段（含跨零点） =====================

class TestTimeWindow(unittest.TestCase):
    def test_parse_normal(self) -> None:
        self.assertEqual(sense.parse_time_window("09:00-22:00"), (540, 1320))

    def test_parse_bad_uses_default(self) -> None:
        self.assertEqual(sense.parse_time_window("乱写的"), (540, 1320))

    def test_inside_normal_window(self) -> None:
        self.assertTrue(sense.in_post_time_window("09:00-22:00", now_minutes=10 * 60))
        self.assertFalse(sense.in_post_time_window("09:00-22:00", now_minutes=7 * 60))

    def test_cross_midnight_window(self) -> None:
        # 22:00-06:00：23 点在里面、12 点在外面。
        self.assertTrue(sense.in_post_time_window("22:00-06:00", now_minutes=23 * 60))
        self.assertTrue(sense.in_post_time_window("22:00-06:00", now_minutes=2 * 60))
        self.assertFalse(sense.in_post_time_window("22:00-06:00", now_minutes=12 * 60))

    def test_equal_bounds_means_always(self) -> None:
        self.assertTrue(sense.in_post_time_window("00:00-00:00", now_minutes=3 * 60))


class TestTimeWindowGuard(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_ignores_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp, {"guard": {"dry_run_no_quota_cost": True}, "publish": {"post_time_window": "09:00-10:00"}})
            result = await guard.check_time_window(ctx, now_minutes=3 * 60, mode=RunMode.DRY_RUN)
            self.assertTrue(result.ok)

    async def test_real_blocks_outside_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp, {"guard": {"dry_run_no_quota_cost": True}, "publish": {"post_time_window": "09:00-10:00"}})
            result = await guard.check_time_window(ctx, now_minutes=3 * 60, mode=RunMode.REAL)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "outside_time_window")

    async def test_real_passes_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp, {"guard": {"dry_run_no_quota_cost": True}, "publish": {"post_time_window": "09:00-10:00"}})
            result = await guard.check_time_window(ctx, now_minutes=9 * 60 + 30, mode=RunMode.REAL)
            self.assertTrue(result.ok)


# ===================== 操作记录 =====================

class TestActionLogStore(unittest.TestCase):
    def test_add_and_recent_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.ActionLogStore(Path(tmp))
            store.add("browse", ok=True, message="浏览 10 条")
            store.add("post", ok=False, mode=RunMode.REAL, message="回读失败")
            rows = store.recent()
            self.assertEqual(rows[0]["action"], "post")
            self.assertEqual(rows[0]["mode"], "real")
            self.assertFalse(rows[0]["ok"])
            self.assertEqual(store.count(), 2)

    def test_trim_to_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = state.ActionLogStore(Path(tmp), max_entries=3)
            for i in range(10):
                store.add(f"a{i}", ok=True)
            self.assertEqual(store.count(), 3)
            self.assertEqual([r["action"] for r in store.recent()], ["a9", "a8", "a7"])


# ===================== Worker =====================

class _FakePlugin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_on: str = ""

    async def run_browse_once(self, ctx, mode):
        self.calls.append("browse")
        if self.fail_on == "browse":
            raise RuntimeError("模拟浏览异常")
        return {"ok": True, "count": 3}

    async def run_interact_once(self, ctx, mode):
        self.calls.append("interact")
        if self.fail_on == "interact":
            raise RuntimeError("模拟互动异常")
        return {"ok": True, "notifications_handled": 1, "comments_sent": 2}

    async def run_summary_once(self, ctx, mode):
        self.calls.append("summary")
        if self.fail_on == "summary":
            raise RuntimeError("模拟总结异常")
        return {"ok": False, "code": "not_triggered", "message": "素材不够"}


class TestOpsWorker(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, tmp: str, routine: str = "午后") -> SimpleNamespace:
        ctx = SimpleNamespace()
        ctx.config = {"sense": {"pause_on_sleep": True}}
        ctx.store = state.StoreContainer(Path(tmp))
        ctx.life_state = routine
        # sense.read_life_trace 读 ctx.api.call / life_state.json；此处直接 monkeypatch 掉。
        return ctx

    async def test_round_runs_three_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            fake = _FakePlugin()
            w = worker_mod.OpsWorker(fake, ctx, mode=RunMode.DRY_RUN, interval_seconds=10)
            original = sense.read_life_trace
            sense.read_life_trace = lambda _ctx: _async_return(LifeTrace(routine="午后"))
            try:
                result = await w.run_round()
            finally:
                sense.read_life_trace = original
            self.assertTrue(result["ok"])
            self.assertEqual(fake.calls, ["browse", "interact", "summary"])
            self.assertEqual(ctx.store.action_log.count(), 3)

    async def test_step_exception_does_not_stop_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            fake = _FakePlugin()
            fake.fail_on = "interact"
            w = worker_mod.OpsWorker(fake, ctx, mode=RunMode.DRY_RUN, interval_seconds=10)
            original = sense.read_life_trace
            sense.read_life_trace = lambda _ctx: _async_return(LifeTrace(routine="午后"))
            try:
                result = await w.run_round()
            finally:
                sense.read_life_trace = original
            self.assertFalse(result["interact"]["ok"])
            # 后面的总结步骤仍然执行。
            self.assertIn("summary", fake.calls)

    async def test_sleep_round_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            fake = _FakePlugin()
            w = worker_mod.OpsWorker(fake, ctx, mode=RunMode.DRY_RUN, interval_seconds=10)
            original = sense.read_life_trace
            sense.read_life_trace = lambda _ctx: _async_return(LifeTrace(routine="深夜"))
            try:
                result = await w.run_round()
            finally:
                sense.read_life_trace = original
            self.assertTrue(result.get("paused"))
            self.assertEqual(fake.calls, [])

    async def test_running_false_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            w = worker_mod.OpsWorker(_FakePlugin(), ctx, mode=RunMode.DRY_RUN, interval_seconds=10)
            self.assertFalse(w.running)


def _async_return(value):
    async def _inner():
        return value

    return _inner()


# ===================== 运行模式判定（演练默认） =====================

_SDK_AVAILABLE = True
try:  # 没有 maibot_sdk 时跳过（插件类本身依赖 SDK，业务层不依赖）
    import maibot_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False


@unittest.skipUnless(_SDK_AVAILABLE, "本地未装 maibot_sdk，跳过插件类用例")
class TestRuntimeMode(unittest.TestCase):
    """按真实 SDK 的注入方式构造插件：set_plugin_config(dict) + 带 [plugin].config_version。

    注意：SDK 的 set_plugin_config 会校验 [plugin].config_version，缺了直接抛
    PluginConfigVersionError——这是真机上的硬约束，测试必须照做。
    """

    def _plugin(self, config: dict | None) -> object:
        import plugin as plugin_mod

        p = plugin_mod.HuiXiangPlugin()
        if config is not None:
            p.set_plugin_config({**config, "plugin": {"config_version": "1.0.0"}})
        return p

    def test_disabled_is_dry_run(self) -> None:
        p = self._plugin({"basic": {"enabled": False, "mode": "auto"}})
        self.assertEqual(p._runtime_mode(), RunMode.DRY_RUN)

    def test_enabled_dry_run_mode(self) -> None:
        p = self._plugin({"basic": {"enabled": True, "mode": "dry_run"}})
        self.assertEqual(p._runtime_mode(), RunMode.DRY_RUN)

    def test_enabled_auto_mode(self) -> None:
        p = self._plugin({"basic": {"enabled": True, "mode": "auto"}})
        self.assertEqual(p._runtime_mode(), RunMode.REAL)

    def test_no_config_defaults_to_dry_run(self) -> None:
        p = self._plugin(None)
        self.assertEqual(p._runtime_mode(), RunMode.DRY_RUN)


# ===================== 额度只被真实消耗 =====================

class TestQuotaOnlyReal(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_round_keeps_real_bucket_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            day = state.today_str()
            for _ in range(5):
                await guard.check_quota(ctx, RunMode.DRY_RUN, "post")
                await guard.check_comment_interval(ctx, RunMode.DRY_RUN, "comment")
            self.assertEqual(ctx.store.quota.get(RunMode.REAL, day).used, 0)
            self.assertEqual(ctx.store.quota.get(RunMode.DRY_RUN, day).used, 0)

    async def test_real_reserve_increments_real_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            day = state.today_str()
            await guard.reserve_quota(ctx, RunMode.REAL, "post")
            self.assertEqual(ctx.store.quota.get(RunMode.REAL, day).used, 1)
            self.assertEqual(ctx.store.quota.get(RunMode.DRY_RUN, day).used, 0)


# ===================== 限流 / 熔断（阶段 6 验收：5 秒 / 30 次每小时；5 分钟冷却） =====================

class TestRateLimitAndCircuit(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 每个用例前清空进程内限流状态，避免用例之间互相影响。
        guard._rate_last.clear()
        guard._rate_stamps.clear()

    def tearDown(self) -> None:
        guard._rate_last.clear()
        guard._rate_stamps.clear()

    async def test_second_write_within_5s_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            first = await guard.check_comment_interval(ctx, RunMode.REAL, "comment")
            self.assertTrue(first.ok)
            await guard.reserve_quota(ctx, RunMode.REAL, "comment")
            second = await guard.check_comment_interval(ctx, RunMode.REAL, "comment")
            self.assertFalse(second.ok)
            self.assertIn(second.code, {"rate_limited", "comment_gap"})

    async def test_hourly_cap_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            guard._rate_stamps.extend([time.time()] * 30)
            result = await guard.check_comment_interval(ctx, RunMode.REAL, "comment")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "rate_limited")

    async def test_captcha_cooldown_5_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(tmp)
            self.assertEqual(ctx.store.circuit.cooldown_seconds, 5 * 60)
            ctx.store.circuit.trip()
            blocked = await guard.check_captcha(ctx)
            self.assertFalse(blocked.ok)
            # 把熔断时间往前拨 6 分钟，冷却应已过。
            ctx.store.circuit._kv.write({"tripped_at": time.time() - 6 * 60})
            passed = await guard.check_captcha(ctx)
            self.assertTrue(passed.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
