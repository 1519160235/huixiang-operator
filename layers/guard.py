# -*- coding: utf-8 -*-
"""
回响插件：护栏层（六块第四块，落地版）。

【职责边界】
决定「能不能发、要不要拦」，但不产出内容、不执行发布：
- 额度、间隔、去重三道常规闸门；
- AI 味拦截（分值来自创作层 ai_flavor_score，0–100）；
- 验证码熔断（冷却 5 分钟）；
- 演练不占额度（dry_run_no_quota_cost 开关，演练直接放行、不读写额度桶、不写 post_gap）；
- 发布前 is_publishable 敏感内容总闸。

【阈值来源（直译 MCP 护栏四件套，常量见 contracts.py）】
- gap.ts      ：发帖间隔 30 分钟；
- aiflavor.ts ：评论 ≥45 / 正文 ≥60 判机器；
- captcha.ts  ：验证码熔断 5 分钟；
- ratelimit.ts：单操作 5 秒、每小时 30 次（进程内限流）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import state as state_mod
from contracts import (
    GUARD_AI_BODY_THRESHOLD,
    GUARD_AI_COMMENT_THRESHOLD,
    GUARD_POST_INTERVAL_MINUTES,
)
from models import Cursor, PostDraft, RunMode, VerifyResult
from layers._cfg import (
    read as _read,
    read_bool as _read_bool,
    read_int as _read_int,
    read_str as _read_str,
    section as _section,
)

# ===================== 进程内限流（直译 ratelimit.ts） =====================
# 写操作粗粒度限流：单操作 5 秒、每小时 30 次；进程内即可（重启清零）。
_RATE_MIN_INTERVAL_SECONDS = 5.0
_RATE_MAX_PER_HOUR = 30
_rate_lock = asyncio.Lock()
_rate_last: dict[str, float] = {}
_rate_stamps: list[float] = []


async def _check_rate(action: str) -> str:
    """返回空串=通过，否则为中文拒绝原因。"""
    async with _rate_lock:
        now = time.time()
        global _rate_stamps
        _rate_stamps = [s for s in _rate_stamps if now - s < 3600]
        if len(_rate_stamps) >= _RATE_MAX_PER_HOUR:
            return f"本小时写操作已达上限（{_RATE_MAX_PER_HOUR} 次），请稍后再试"
        last = _rate_last.get(action)
        if last is not None and now - last < _RATE_MIN_INTERVAL_SECONDS:
            wait = int(_RATE_MIN_INTERVAL_SECONDS - (now - last)) + 1
            return f"操作过于频繁，请 {wait} 秒后再试"
        return ""


async def _record_rate(action: str) -> None:
    async with _rate_lock:
        now = time.time()
        _rate_last[action] = now
        _rate_stamps.append(now)


# ===================== 配置 / 存储访问辅助 =====================

def _guard_cfg(ctx: Any) -> Any:
    return _section(ctx, "guard")


def _dry_run_no_quota_cost(ctx: Any) -> bool:
    """读取演练不占额度开关（默认 true，务必显式读取）。"""
    return _read_bool(_guard_cfg(ctx), "dry_run_no_quota_cost", True)


def _store(ctx: Any, name: str) -> Any:
    store = getattr(ctx, "store", None)
    if store is None:
        return None
    return getattr(store, name, None)


_QUOTA_FIELD = {
    "post": "daily_quota_post",
    "comment": "daily_quota_comment",
    "like": "daily_quota_like",
    "reply": "daily_quota_reply",
}

# 动作类型 → 间隔游标名（发帖 / 评论 / 回复 / 点赞各自记录「上次时间」）。
_CURSOR_FIELD = {
    "post": "last_post_at",
    "comment": "last_comment_at",
    "reply": "last_reply_at",
    "like": "last_like_at",
}


def _quota_limit(ctx: Any, action: str) -> int:
    return _read_int(_guard_cfg(ctx), _QUOTA_FIELD.get(action, "daily_quota_post"), 0)


def _ai_flavor_threshold(ctx: Any) -> float:
    """正文 AI 味阈值（0-100），从 config [guard].ai_flavor_threshold 读，缺省 60。"""
    try:
        return float(_read(_guard_cfg(ctx), "ai_flavor_threshold", GUARD_AI_BODY_THRESHOLD))
    except (TypeError, ValueError):
        return float(GUARD_AI_BODY_THRESHOLD)


def _interact_cfg(ctx: Any) -> Any:
    return _section(ctx, "interact")


def _comment_interval(ctx: Any) -> int:
    """评论 / 回复间隔（秒），从 [interact].comment_interval_seconds 读，缺省 600。"""
    return _read_int(_interact_cfg(ctx), "comment_interval_seconds", 600)


# ===================== 六道闸门 =====================

async def check_quota(ctx: Any, mode: RunMode, action: str = "post") -> VerifyResult:
    """额度闸门：演练且 dry_run_no_quota_cost 为真时直接放行，不读写额度桶。"""
    if mode is RunMode.DRY_RUN and _dry_run_no_quota_cost(ctx):
        return VerifyResult(ok=True, code="dry_run_no_quota", message="演练模式，不占额度，直接放行")
    quota = _store(ctx, "quota")
    if quota is None:
        return VerifyResult(ok=True, code="quota_store_unavailable", message="额度存储未初始化，跳过额度校验")
    day = state_mod.today_str()
    limit = _quota_limit(ctx, action)
    if limit <= 0:
        return VerifyResult(ok=True, code="quota_unlimited", message="未配置额度上限，跳过额度校验")
    if quota.is_exhausted(mode, day, limit, action):
        return VerifyResult(ok=False, code="quota_exhausted", message=f"今日{action}额度已用尽（{limit}）")
    return VerifyResult(ok=True, code="quota_ok", message="额度充足")


async def check_interval(ctx: Any, mode: RunMode) -> VerifyResult:
    """间隔 / 限流闸门：演练直接放行不写 post_gap；否则限流 + 30 分钟发帖间隔。"""
    if mode is RunMode.DRY_RUN and _dry_run_no_quota_cost(ctx):
        return VerifyResult(ok=True, code="dry_run_no_quota", message="演练模式，不记录间隔，直接放行")
    rate_msg = await _check_rate("post")
    if rate_msg:
        return VerifyResult(ok=False, code="rate_limited", message=rate_msg)
    cursor_store = _store(ctx, "cursor")
    if cursor_store is not None:
        last = cursor_store.get("last_post_at")
        if last.value:
            try:
                last_ts = float(last.value)
                elapsed = time.time() - last_ts
                if elapsed < GUARD_POST_INTERVAL_MINUTES * 60:
                    left = int((GUARD_POST_INTERVAL_MINUTES * 60 - elapsed) / 60) + 1
                    return VerifyResult(ok=False, code="post_gap", message=f"刚发过帖，还要等约 {left} 分钟")
            except ValueError:
                pass
    return VerifyResult(ok=True, code="interval_ok", message="间隔与限流通过")


async def check_dedup(ctx: Any, fingerprint: str, mode: RunMode) -> VerifyResult:
    """去重闸门：同一内容（按 mode 隔离）不重复发布。"""
    dedup = _store(ctx, "dedup")
    if dedup is None:
        return VerifyResult(ok=True, code="dedup_store_unavailable", message="去重存储未初始化")
    if dedup.seen(fingerprint, mode):
        return VerifyResult(ok=False, code="duplicate", message="这条内容已发过，去重拦截")
    return VerifyResult(ok=True, code="dedup_ok", message="未发过")


async def check_ai_flavor(ctx: Any, score: float, threshold: float | None = None) -> VerifyResult:
    """AI 味拦截：分值过高说明文字太像机器，拦下重写。"""
    de_ai = _read_bool(_guard_cfg(ctx), "de_ai_flavor", True)
    if not de_ai:
        return VerifyResult(ok=True, code="ai_flavor_off", message="去 AI 味已关闭")
    limit = threshold if threshold is not None else _ai_flavor_threshold(ctx)
    if float(score) >= limit:
        return VerifyResult(ok=False, code="ai_flavor_block", message=f"文字太像机器（AI 味 {float(score):.0f} ≥ {limit}），建议重写")
    return VerifyResult(ok=True, code="ai_flavor_ok", message="AI 味通过")


async def check_time_window(
    ctx: Any, now_minutes: int | None = None, mode: RunMode = RunMode.REAL
) -> VerifyResult:
    """发帖时段闸门（已停用）。

    【2026-11-08 用户要求「既然都说了他是自己想发，那就不要设置什么时间段了」】
    原来这里有 `post_time_window` / `post_slots` 两套时段互相打架，
    现在时段整个取消：什么时候发由「想不想发」+ 生活状态决定。
    这个函数保留成恒放行，是为了不破坏既有的护栏链调用顺序；
    真正的闸门只剩额度（`daily_quota_post`）和生活状态。
    """
    return VerifyResult(ok=True, code="no_time_window", message="不限时段，想发就发")


async def check_captcha(ctx: Any) -> VerifyResult:
    """验证码熔断：冷却期内一律拦截。"""
    circuit = _store(ctx, "circuit")
    if circuit is None:
        return VerifyResult(ok=True, code="circuit_store_unavailable", message="熔断存储未初始化")
    if circuit.is_open():
        return VerifyResult(ok=False, code="captcha_block", message="验证码熔断中，冷却期内暂停发布")
    return VerifyResult(ok=True, code="captcha_ok", message="熔断正常")


async def reserve_quota(ctx: Any, mode: RunMode, action: str = "post") -> VerifyResult:
    """占用一次额度（发布成功后调用）；演练不占真实额度、不写 post_gap。"""
    if mode is RunMode.DRY_RUN and _dry_run_no_quota_cost(ctx):
        return VerifyResult(ok=True, code="dry_run_no_quota", message="演练模式，不占额度")
    quota = _store(ctx, "quota")
    if quota is not None:
        # 【2026-11-07 修正】按动作分桶：评论不能把发帖额度吃掉（原来共用一个计数器）。
        quota.incr(mode, state_mod.today_str(), 1, action)
    cursor_store = _store(ctx, "cursor")
    if cursor_store is not None:
        cursor_store.set(Cursor(name=_CURSOR_FIELD.get(action, "last_post_at"), value=str(time.time())))
    await _record_rate(action)
    return VerifyResult(ok=True, code="quota_reserved", message="已占用额度")


async def is_publishable(ctx: Any, draft: PostDraft, mode: RunMode = RunMode.DRY_RUN) -> VerifyResult:
    """发布前总闸：串联五道闸门，任一不过即拦截。返回 detail 携带去重指纹。"""
    fingerprint = state_mod.make_fingerprint(
        {"title": draft.title, "text": draft.text, "topic_ids": sorted(draft.topic_ids)}
    )
    checks = [
        await check_quota(ctx, mode, "post"),
        await check_interval(ctx, mode),
        await check_dedup(ctx, fingerprint, mode),
        await check_ai_flavor(ctx, draft.ai_flavor_score, GUARD_AI_BODY_THRESHOLD),
        await check_captcha(ctx),
        await check_time_window(ctx, mode=mode),
    ]
    for result in checks:
        if not result.ok:
            return result
    return VerifyResult(ok=True, code="publishable", message="护栏闸门全部通过", detail={"fingerprint": fingerprint})


async def check_comment_interval(ctx: Any, mode: RunMode, action: str = "comment") -> VerifyResult:
    """评论 / 回复间隔闸门：演练直接放行；否则限流 + [interact].comment_interval_seconds 间隔。"""
    if mode is RunMode.DRY_RUN and _dry_run_no_quota_cost(ctx):
        return VerifyResult(ok=True, code="dry_run_no_quota", message="演练模式，不记录评论间隔，直接放行")
    rate_msg = await _check_rate(action)
    if rate_msg:
        return VerifyResult(ok=False, code="rate_limited", message=rate_msg)
    interval = _comment_interval(ctx)
    cursor_store = _store(ctx, "cursor")
    if cursor_store is not None and interval > 0:
        cursor_name = _CURSOR_FIELD.get(action, "last_comment_at")
        last = cursor_store.get(cursor_name)
        if last.value:
            try:
                elapsed = time.time() - float(last.value)
                if elapsed < interval:
                    left = int(interval - elapsed) + 1
                    return VerifyResult(ok=False, code="comment_gap", message=f"评论间隔未到，还要等约 {left} 秒")
            except ValueError:
                pass
    return VerifyResult(ok=True, code="comment_interval_ok", message="评论间隔与限流通过")


async def is_commentable(
    ctx: Any, text: str, score: float, mode: RunMode = RunMode.DRY_RUN, action: str = "comment"
) -> VerifyResult:
    """评论 / 回复前总闸：串联评论额度 + 评论间隔 + 去重 + AI 味(45) + 熔断。"""
    fingerprint = state_mod.make_fingerprint({"kind": action, "text": str(text)})
    checks = [
        await check_quota(ctx, mode, action),
        await check_comment_interval(ctx, mode, action),
        await check_dedup(ctx, fingerprint, mode),
        await check_ai_flavor(ctx, score, GUARD_AI_COMMENT_THRESHOLD),
        await check_captcha(ctx),
    ]
    for result in checks:
        if not result.ok:
            return result
    return VerifyResult(ok=True, code="commentable", message="评论护栏全部通过", detail={"fingerprint": fingerprint})


async def reset_circuit_breaker(ctx: Any) -> VerifyResult:
    """手动解除验证码熔断（WebUI reset_circuit_breaker 调用）。"""
    circuit = _store(ctx, "circuit")
    if circuit is not None:
        circuit.reset()
    return VerifyResult(ok=True, code="circuit_reset", message="熔断已解除")


__all__ = [
    "check_quota",
    "check_interval",
    "check_dedup",
    "check_ai_flavor",
    "check_captcha",
    "check_time_window",
    "reserve_quota",
    "is_publishable",
    "check_comment_interval",
    "is_commentable",
    "reset_circuit_breaker",
    "GUARD_AI_COMMENT_THRESHOLD",
]
