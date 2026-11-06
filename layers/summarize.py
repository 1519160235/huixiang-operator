# -*- coding: utf-8 -*-
"""
回响插件：总结层（阶段 5 落地，「感想总结发帖」子流水线）。

【职责边界】
只做编排，不新增业务能力：把「刷过什么」变成「发一篇总结帖」，串起
接入层（浏览素材）→ 创作层（生成 200–400 字 / 2–4 段）→ 护栏层（正文 60 阈值总闸）
→ 发布层（发帖 + 回读核验）。

【触发判定（纯逻辑，可测）】
should_summarize：
- count：未被总结消费的浏览条数 ≥ count_threshold；
- time：距上次总结 ≥ time_hours 小时，且至少有一条待总结素材；
- both：上面任一成立。

【素材来源】
state.BrowseDigestStore 记录刷过的帖子（post_id / 标题 / 摘要 / 分区）。
总结成功后把这些素材标记为已消费，避免同一批内容被反复总结。
"""

from __future__ import annotations

import time
from typing import Any

from models import LinkTag, PostDraft, RunMode, SummaryDraft

from . import create, guard, publish, sense
from ._cfg import norm_choice as _norm_choice, read as _read_section_value, section as _section

# 护栏判「太像机器」/ 篇幅不合规时，最多重写几轮。
# 每轮都是一次完整的大模型调用，别设太大（烧额度也慢）；3 轮不达标就如实失败。
REWRITE_ROUNDS = 2


def _summarize_cfg(ctx: Any) -> dict[str, Any]:
    """读取 [summarize] 段配置（缺省值与 config_model 一致）。"""
    defaults: dict[str, Any] = {
        "enabled": True,
        "trigger_by": "count",
        "count_threshold": 10,
        "time_hours": 24,
        "min_chars": 200,
        "max_chars": 400,
        "min_paragraphs": 2,
        "max_paragraphs": 4,
    }
    section = _section(ctx, "summarize")
    for key in defaults:
        defaults[key] = _read_section_value(section, key, defaults[key])
    return defaults


def should_summarize(
    *,
    trigger_by: str = "count",
    count_threshold: int = 10,
    time_hours: int = 24,
    pending_count: int = 0,
    since_last_hours: float = 0.0,
    last_summary_ts: float = 0.0,
) -> tuple[bool, str]:
    """判断是否该发一篇感想总结（纯逻辑，可测）。

    :return: (是否触发, 中文原因)
    """
    if pending_count <= 0:
        return False, "没有待总结的浏览素材"
    by_count = pending_count >= max(1, int(count_threshold))
    if last_summary_ts <= 0:
        by_time = pending_count > 0
    else:
        by_time = since_last_hours >= max(1, int(time_hours))
    mode = str(trigger_by or "count")
    if mode == "count":
        return (True, f"已刷 {pending_count} 条，达到帖数阈值 {count_threshold}") if by_count else (
            False,
            f"已刷 {pending_count} 条，未到帖数阈值 {count_threshold}",
        )
    if mode == "time":
        if last_summary_ts <= 0:
            return (True, f"还没总结过，已攒 {pending_count} 条素材") if by_time else (
                False,
                "没有待总结的浏览素材",
            )
        return (True, f"距上次总结已 {since_last_hours:.1f} 小时") if by_time else (
            False,
            f"距上次总结 {since_last_hours:.1f} 小时，未到时间窗口 {time_hours} 小时",
        )
    if by_count:
        return True, f"已刷 {pending_count} 条，达到帖数阈值 {count_threshold}"
    if by_time:
        return True, f"距上次总结已 {since_last_hours:.1f} 小时"
    return False, "帖数与时间两个条件都未满足"


def summary_ready(ctx: Any, store: Any = None, now: float | None = None) -> tuple[bool, str]:
    """从配置与存储读实时状态，判断是否该发总结（不含生成）。"""
    cfg = _summarize_cfg(ctx)
    if not bool(cfg.get("enabled", True)):
        return False, "感想总结开关未开启"
    if store is None:
        return False, "浏览摘要存储未初始化"
    now_ts = now if now is not None else time.time()
    last_ts = store.last_summary_ts()
    since_hours = ((now_ts - last_ts) / 3600.0) if last_ts > 0 else 0.0
    return should_summarize(
        trigger_by=_norm_choice("trigger_by", cfg.get("trigger_by"), "count"),
        count_threshold=int(cfg.get("count_threshold", 10)),
        time_hours=int(cfg.get("time_hours", 24)),
        pending_count=store.pending_count(),
        since_last_hours=since_hours,
        last_summary_ts=last_ts,
    )


def summary_to_draft(summary: SummaryDraft, partition: Any = None, topic_ids: list[int] | None = None) -> PostDraft:
    """把 SummaryDraft 转成可走发布层的 PostDraft（总结帖默认纯文字动态 28）。"""
    return PostDraft(
        title=summary.title,
        text=summary.text,
        link_tag=LinkTag.DYNAMIC,
        partition=partition,
        topic_ids=list(topic_ids or summary.topic_ids or []),
        images=[],
        polished=True,
        ai_flavor_score=summary.ai_flavor_score,
    )


def check_summary_shape(summary: SummaryDraft, cfg: dict[str, Any]) -> tuple[bool, str]:
    """校验总结字数与段数是否落在约束内（纯逻辑，可测）。"""
    min_chars = int(cfg.get("min_chars", 200))
    max_chars = int(cfg.get("max_chars", 400))
    min_par = int(cfg.get("min_paragraphs", 2))
    max_par = int(cfg.get("max_paragraphs", 4))
    chars = summary.chars or create.count_chars(summary.text)
    paragraphs = summary.paragraphs or create.count_paragraphs(summary.text)
    if chars < min_chars:
        return False, f"总结字数不足（{chars} < {min_chars}）"
    if chars > max_chars:
        return False, f"总结字数超限（{chars} > {max_chars}）"
    if paragraphs < min_par:
        return False, f"总结段数不足（{paragraphs} < {min_par}）"
    if paragraphs > max_par:
        return False, f"总结段数超限（{paragraphs} > {max_par}）"
    return True, f"总结字数段数合规（{chars} 字 / {paragraphs} 段）"


async def run_summary_once(ctx: Any, partition: Any = None, mode: RunMode = RunMode.DRY_RUN) -> dict:
    """跑一遍感想总结子流水线：取素材 → 生成 → 校验形状 → 护栏 → 发布 → 回读 → 消费素材。

    【真机踩坑 2026-11-06 用户报障】
    「写得跟机器一样，提示建议重写，然后就没动静了；过了会儿又变成演练」——
    根因有两个：
    1. 护栏判 `ai_flavor_block` 时只**返回**一句「建议重写」，**没有任何代码真的去重写**，
       于是这一轮直接以失败结束，用户看到的就是「提示完就没下文」。
    2. 那道失败记录写的是 worker 自己的 mode；而 worker 的 mode 又是在启动时固化的，
       切「自动」后旧 worker 还在跑演练 —— 日志里就出现「前面自动、后面又演练」。
    这里补上真正的重写闭环：AI 味超标 → 带反馈重新生成，最多 REWRITE_ROUNDS 轮。
    """
    store = getattr(getattr(ctx, "store", None), "digest", None)
    cfg = _summarize_cfg(ctx)
    if not bool(cfg.get("enabled", True)):
        return {"ok": False, "code": "summary_disabled", "message": "感想总结开关未开启"}
    if store is None:
        return {"ok": False, "code": "no_digest_store", "message": "浏览摘要存储未初始化"}
    items = store.pending()
    if not items:
        return {"ok": False, "code": "no_material", "message": "没有待总结的浏览素材"}

    trace = await sense.read_life_trace(ctx)
    summary = await create.generate_summary(ctx, items, trace)

    # 形状不合规 / AI 味超标都可能靠重写解决，统一放一个循环里。
    # 护栏的其他闸门（额度、间隔、去重、时段）重写也没用，直接失败返回。
    rewrite_hint = ""
    previous_text = ""
    attempts: list[dict] = []
    for round_no in range(REWRITE_ROUNDS + 1):
        if rewrite_hint:
            summary = await create.generate_summary(
                ctx,
                items,
                trace,
                rewrite_hint=rewrite_hint,
                previous_text=previous_text,
            )

        ok, message = check_summary_shape(summary, cfg)
        if not ok:
            attempts.append({"round": round_no, "code": "summary_shape", "message": message})
            if round_no >= REWRITE_ROUNDS:
                return {
                    "ok": False,
                    "code": "summary_shape",
                    "message": message,
                    "chars": summary.chars,
                    "paragraphs": summary.paragraphs,
                    "rewrites": round_no,
                }
            # 字数/段数不够 → 用「写长一点」的提示重写，而不是换话题。
            rewrite_hint = f"篇幅不合规（{message}）"
            previous_text = summary.text
            continue

        draft = summary_to_draft(summary, partition=partition)
        verdict = await guard.is_publishable(ctx, draft, mode)
        if verdict.ok:
            break

        attempts.append({"round": round_no, "code": verdict.code, "message": verdict.message})
        # 只有「文字太像机器」值得重写。额度/间隔/去重/时段重写一百遍也一样。
        if verdict.code != "ai_flavor_block":
            return {"ok": False, "code": verdict.code, "message": verdict.message}
        if round_no >= REWRITE_ROUNDS:
            # 把最终分数和字数一起带出来：用户看到「重写 N 次仍不达标」时，
            # 需要知道是「差一点」还是「模型根本写不像人」，否则只能干瞪眼。
            return {
                "ok": False,
                "code": verdict.code,
                "message": f"{verdict.message}（已重写 {round_no} 次仍不达标）",
                "chars": summary.chars,
                "paragraphs": summary.paragraphs,
                "ai_flavor_score": summary.ai_flavor_score,
                "rewrites": round_no,
            }
        rewrite_hint = "文字太像机器、AI 味太重"
        previous_text = summary.text
    else:  # pragma: no cover —— 循环必然 break 或 return，这里只是兜底。
        return {"ok": False, "code": "summary_loop_exhausted", "message": "重写循环异常结束"}

    result = await publish.publish_post(ctx, draft, dry_run=(mode is RunMode.DRY_RUN))
    if mode is RunMode.DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "note": result.note,
            "chars": summary.chars,
            "paragraphs": summary.paragraphs,
            "rewrites": len(attempts),
        }
    if not result.post_id:
        return {"ok": False, "code": "no_post_id", "message": "总结帖未返回帖子 ID"}
    verify = await publish.verify_post_published(ctx, result.post_id)
    if verify.ok:
        await guard.reserve_quota(ctx, mode, "post")
        store.mark_summarized([str(i.get("post_id", "")) for i in items])
        return {
            "ok": True,
            "post_id": result.post_id,
            "verified": True,
            "chars": summary.chars,
            "paragraphs": summary.paragraphs,
            "rewrites": len(attempts),
        }
    return {
        "ok": False,
        "code": verify.code,
        "post_id": result.post_id,
        "verified": False,
        "message": verify.message,
    }


__all__ = [
    "should_summarize",
    "summary_ready",
    "summary_to_draft",
    "check_summary_shape",
    "run_summary_once",
]
