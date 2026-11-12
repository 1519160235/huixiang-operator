# -*- coding: utf-8 -*-
"""
回响插件：后台运营 Worker（阶段 6 落地）。

【职责边界】
只做调度，不做业务：按配置间隔依次跑「浏览 → 互动 → 感想总结」三条子流水线，
每一步都把结果写进操作记录（state.ActionLogStore），供 WebUI 查看。

【硬约束】
- 默认演练：worker 启动时必须显式传入 mode，plugin 只会在 basic.mode == "auto"
  且 basic.enabled == true 时传 RunMode.REAL，其余一律演练。
- 单轮异常不退出循环：网络抖动 / 上游改字段都不该让 worker 死掉，只记一条失败日志。
- 作息联动：睡觉 / 小睡时段整轮跳过（sense.should_pause_for_routine）。
- 可随时停止：stop() 置位并在下一轮边界退出，不强杀正在跑的协程。

【可测性】
循环体 `run_round()` 与调度 `OpsWorker` 分离：单测直接调 run_round，
不需要真起 asyncio 任务、也不等真实间隔。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from models import RunMode
from layers import sense
from layers._cfg import read_int, section

logger = logging.getLogger(__name__)

# 表情白名单刷新周期：一天一次足够（新表情不是天天有）。
EMOJI_REFRESH_SECONDS = 24 * 3600
# 话题池采样周期：话题变化比表情慢，一天一次也够。
HASHTAG_SAMPLE_SECONDS = 24 * 3600


class OpsWorker:
    """后台运营 Worker：按间隔跑「浏览 → 互动 → 总结」。"""

    def __init__(
        self,
        plugin: Any,
        ctx: Any,
        mode: RunMode = RunMode.DRY_RUN,
        interval_seconds: int = 300,
    ) -> None:
        self.plugin = plugin
        self.ctx = ctx
        self.mode = mode
        self.interval_seconds = max(10, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_round_at: str = ""
        self.round_count = 0
        self.last_result: dict[str, Any] = {}

    # ===================== 生命周期 =====================

    @property
    def running(self) -> bool:
        """worker 是否在跑（任务存在且未结束）。"""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动后台循环（幂等：已在跑就不重复起）。"""
        if self.running:
            logger.info("回响：后台 worker 已在运行，不重复启动。")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "回响：后台 worker 已启动，模式=%s，间隔=%s 秒。",
            self.mode.value,
            self.interval_seconds,
        )

    async def stop(self) -> None:
        """停止后台循环（置位 + 等当前轮跑完）。"""
        self._stopping = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 —— 停止时不关心退出原因
            pass
        logger.info("回响：后台 worker 已停止。")

    async def _loop(self) -> None:
        """主循环：跑一轮 → 等间隔 → 再跑，直到 stop()。"""
        while not self._stopping:
            try:
                await self.run_round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单轮异常绝不能打死 worker。
                logger.warning("回响：worker 单轮异常，已忽略并继续下一轮：%s", exc)
                self._log("worker_round", False, f"单轮异常：{type(exc).__name__}")
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise

    # ===================== 单轮 =====================

    async def run_round(self) -> dict[str, Any]:
        """跑一轮：作息判定 → 浏览 → 互动 → 总结。返回本轮结果。

        【真机踩坑 2026-11-06 用户报障「前面自动、后面又变演练」】
        mode 原来只在构造时传入一次，之后完全不变。配置改成「自动」后虽然会尝试
        重建 worker，但重建是异步任务、且失败时异常被吞掉，旧 worker 会继续用
        构造时的 DRY_RUN 跑下去 —— 日志里就是「先自动、后演练」的错乱。
        所以每轮开头都按当前配置重新判定一次 mode，worker 不再持有过期的模式。
        """
        from state import now_iso

        self.mode = self._current_mode()

        self.round_count += 1
        self.last_round_at = now_iso()
        trace = await sense.read_life_trace(self.ctx)
        if sense.should_pause_for_routine(trace, self._pause_on_sleep()):
            reason = sense.routine_pause_reason(trace)
            self._log("worker_round", True, reason)
            self.last_result = {"ok": True, "paused": True, "message": reason}
            return self.last_result

        result: dict[str, Any] = {"ok": True, "round": self.round_count}
        result["browse"] = await self._step("browse", self.plugin.run_browse_once)
        result["interact"] = await self._step("interact", self.plugin.run_interact_once)
        result["summary"] = await self._step("summary", self.plugin.run_summary_once)
        result["emoji_refresh"] = await self._maybe_refresh_emoji()
        result["hashtag_sample"] = await self._maybe_sample_hashtags()
        self.last_result = result
        return result

    async def _maybe_sample_hashtags(self) -> dict[str, Any]:
        """每天从推荐流里采一次话题（hashtag）到池子。

        小黑盒没有公开的话题列表接口，`/bbs/app/api/topic/index/` 返回的是分区树。
        所以照表情白名单的办法：别人帖子上挂的 hashtags 一定真实存在，抄下来即可。
        失败只记 debug，不影响发帖（没话题就按没有发）。
        """
        store = getattr(getattr(self.ctx, "store", None), "cursor", None)
        if store is None:
            return {"ok": False, "code": "no_cursor"}
        now = time.time()
        try:
            last = float(store.get("hashtags_sampled_at").value or 0)
        except Exception:  # noqa: BLE001
            last = 0.0
        if last and now - last < HASHTAG_SAMPLE_SECONDS:
            return {"ok": True, "skipped": True, "code": "not_due"}

        from layers import hashtags

        try:
            outcome = await hashtags.sample_from_feed(self.ctx, limit=2)
        except Exception as exc:  # noqa: BLE001 —— 采样失败绝不影响主流程。
            logger.debug("回响：话题采样失败（已忽略）：%s", exc)
            return {"ok": False, "code": "sample_error"}
        if outcome.get("ok"):
            try:
                from models import Cursor

                store.set(Cursor(name="hashtags_sampled_at", value=str(now)))
            except Exception:  # noqa: BLE001
                pass
        return outcome

    async def _maybe_refresh_emoji(self) -> dict[str, Any]:
        """每 24 小时采一次真实评论，把新表情并入白名单。

        小黑盒会持续上新表情，写死的名单迟早过期。采样只在刷过的帖子上做，
        不额外发请求探接口；任何失败都只降级成一条 debug，不影响发帖。
        """
        store = getattr(getattr(self.ctx, "store", None), "cursor", None)
        if store is None:
            return {"ok": False, "code": "no_cursor"}
        last = 0.0
        try:
            cursor = store.get("emoji_whitelist_refreshed_at")
            if cursor.value:
                last = float(cursor.value)
        except Exception:  # noqa: BLE001
            last = 0.0
        now = time.time()
        if last and now - last < EMOJI_REFRESH_SECONDS:
            return {"ok": True, "skipped": True, "code": "not_due"}

        from layers import emoji_refresh

        try:
            outcome = await emoji_refresh.refresh_once(self.ctx)
        except Exception as exc:  # noqa: BLE001 —— 采样失败绝不影响主流程。
            logger.debug("回响：表情白名单刷新失败（已忽略）：%s", exc)
            return {"ok": False, "code": "refresh_error"}
        if outcome.get("ok"):
            try:
                from models import Cursor

                store.set(Cursor(name="emoji_whitelist_refreshed_at", value=str(now)))
            except Exception:  # noqa: BLE001
                pass
            if outcome.get("added"):
                self._log("emoji_refresh", True, f"新增 {outcome['added']} 个表情", outcome)
        return outcome

    async def _step(self, name: str, runner: Callable[..., Awaitable[dict]]) -> dict[str, Any]:
        """跑一个子步骤：成功失败都写操作记录，绝不向外抛异常。"""
        try:
            outcome = await runner(self.ctx, self.mode)
        except Exception as exc:  # noqa: BLE001 —— 子步骤失败只降级，不影响后续步骤。
            # 【真机缺陷 2026-11-12】以前只记类名（"执行异常：CommentRejectedError"），
            # 排查时完全不知道平台到底说了什么，只能事后扒接口。把原始消息带上。
            self._log(name, False, f"执行异常：{type(exc).__name__}: {str(exc)[:160]}")
            return {"ok": False, "code": "exception", "message": str(exc)[:200]}
        if not isinstance(outcome, dict):
            self._log(name, False, "返回值不是对象")
            return {"ok": False, "code": "bad_return"}
        ok = bool(outcome.get("ok", False))
        message = str(outcome.get("message") or outcome.get("note") or outcome.get("code") or "")
        self._log(name, ok, message, outcome)
        return outcome

    # ===================== 辅助 =====================

    def _current_mode(self) -> RunMode:
        """按插件当前配置重新判定运行模式（每轮都调，避免模式过期）。

        插件实现 `_runtime_mode()` 时以它为准（那里统一处理了 enabled / 中英文
        归一化）；拿不到就退回构造时传入的 mode，绝不让判定失败打断整轮。
        """
        plugin = self.plugin
        resolver = getattr(plugin, "_runtime_mode", None)
        if callable(resolver):
            try:
                mode = resolver()
                if isinstance(mode, RunMode):
                    return mode
            except Exception as exc:  # noqa: BLE001
                logger.warning("回响：重新判定运行模式失败，沿用 %s：%s", self.mode.value, exc)
        return self.mode

    def _pause_on_sleep(self) -> bool:
        from layers._cfg import read_bool, section

        return read_bool(section(self.ctx, "sense"), "pause_on_sleep", True)

    def _log(self, action: str, ok: bool, message: str, detail: dict | None = None) -> None:
        """写操作记录（失败不影响主流程）。"""
        store = getattr(getattr(self.ctx, "store", None), "action_log", None)
        if store is None:
            return
        try:
            store.add(action, ok=ok, mode=self.mode, message=message, detail=detail or {})
        except Exception:  # noqa: BLE001 —— 记录失败只丢日志。
            logger.warning("回响：操作记录写入失败（已忽略）。")


__all__ = ["OpsWorker"]
