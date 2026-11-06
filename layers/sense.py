# -*- coding: utf-8 -*-
"""
回响插件：感知层（六块第二块，落地版）。

【职责边界】
只读「现在是什么状态、外面有什么可聊的」，不产出任何内容、不决定是否发布：
- 读生活轨迹（作息 / 情绪 / 体力 / 社交电量）；
- 判断现在能不能开口（依据社交电量与作息）；
- 拉热门话题与有奖话题（B 类直连，最小闭环阶段返回空、留接口）。

【复用约定】
生活轨迹属 A 类宿主能力：优先 ctx.api.call 复用 life-frequency，失败回退直读 life_state.json；
热门 / 有奖话题属 B 类直连，最小闭环阶段接口保留、待接入真实话题接口。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import contracts
import state as state_mod
from layers._cfg import read as _read, read_bool as _read_bool, read_int as _read_int, section as _section
from models import Cursor, LifeTrace, Topic


async def read_life_trace(ctx: Any) -> LifeTrace:
    """读取当前生活轨迹：优先 ctx.api.call，失败回退直读 life_state.json。"""
    api = getattr(ctx, "api", None)
    if api is not None and hasattr(api, "call"):
        try:
            result = await api.call(contracts.API_LIFE_CONTEXT)
            if isinstance(result, dict):
                return LifeTrace(
                    routine=str(result.get("routine", result.get("作息", ""))),
                    mood=str(result.get("mood", result.get("情绪", ""))),
                    energy=float(result.get("energy", result.get("体力", 0)) or 0),
                    social_battery=float(result.get("social_battery", result.get("社交电量", 0)) or 0),
                    captured_at=state_mod.now_iso(),
                )
        except Exception:
            pass
    paths = getattr(ctx, "paths", None)
    if paths is not None:
        state_dir = getattr(paths, "state_dir", None) or getattr(paths, "data_dir", None)
        if state_dir:
            path = Path(state_dir) / contracts.FALLBACK_LIFE_STATE_FILE
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    return LifeTrace(
                        routine=str(data.get("routine", "")),
                        mood=str(data.get("mood", "")),
                        energy=float(data.get("energy", 0) or 0),
                        social_battery=float(data.get("social_battery", 0) or 0),
                        captured_at=state_mod.now_iso(),
                    )
                except (OSError, ValueError):
                    pass
    return LifeTrace(captured_at=state_mod.now_iso())


async def can_speak_now(ctx: Any, trace: LifeTrace) -> bool:
    """判断「现在能不能开口」（社交电量过低 / 睡觉时段返回 False）。

    社交电量为 None（未接生活频率 / 接口没返回）时视为「未知」，不拦截；
    只有明确读到低电量才拦。理由见 models.LifeTrace 的注释。
    """
    sense_cfg = _section(ctx, "sense")
    pause_on_sleep = _read_bool(sense_cfg, "pause_on_sleep", True)
    battery_threshold = _read_int(sense_cfg, "social_battery_threshold", 40)
    if pause_on_sleep and trace.routine in {"深夜", "睡觉", "睡眠", "凌晨"}:
        return False
    battery = trace.social_battery
    if battery is not None and 0 <= battery * 100 < battery_threshold:
        return False
    return True


async def fetch_hot_topics(ctx: Any, cursor: Cursor | None = None) -> list[Topic]:
    """拉取热门话题列表（B 类直连；最小闭环阶段走 ctx 注入客户端，否则返回空）。"""
    client = getattr(ctx, "xhh_client", None)
    if client is not None and hasattr(client, "fetch_hot_topics"):
        raw = await client.fetch_hot_topics()
        topics: list[Topic] = []
        for item in raw:
            if isinstance(item, dict):
                topics.append(
                    Topic(
                        id=int(item.get("topic_id", item.get("id", 0)) or 0),
                        name=str(item.get("name", "")),
                        heat=float(item.get("heat", item.get("popularity", 0)) or 0),
                    )
                )
        return topics
    return []


async def fetch_reward_topics(ctx: Any, cursor: Cursor | None = None) -> list[Topic]:
    """拉取有奖 / 悬赏类话题（最小闭环阶段复用 fetch_hot_topics，筛选逻辑待接入）。"""
    topics = await fetch_hot_topics(ctx, cursor)
    return topics


# ===================== 作息 / 发帖时段（纯逻辑，可测） =====================

# 视为「睡觉」的作息词：这些时段一律不发帖、不互动。
SLEEP_ROUTINES = frozenset({"深夜", "睡觉", "睡眠", "凌晨", "小睡", "午睡", "打盹", "刚睡下"})


def parse_time_window(window: str, default: tuple[str, str] = ("09:00", "22:00")) -> tuple[int, int]:
    """把 "09:00-22:00" 解析成分钟数区间；解析失败用默认值。"""
    text = str(window or "").strip().replace("~", "-").replace("—", "-").replace("–", "-")
    start_text, sep, end_text = text.partition("-")
    if not sep:
        start_text, end_text = default

    def _to_minutes(value: str, fallback: str) -> int:
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            parts = fallback.split(":")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            hour, minute = 0, 0
        return max(0, min(hour, 23)) * 60 + max(0, min(minute, 59))

    return _to_minutes(start_text, default[0]), _to_minutes(end_text, default[1])


def in_post_time_window(window: str, now_minutes: int | None = None) -> bool:
    """判断当前是否落在允许发帖的时间段内（纯逻辑，可测；支持跨零点如 22:00-06:00）。"""
    if now_minutes is None:
        import time as _time

        local = _time.localtime()
        now_minutes = local.tm_hour * 60 + local.tm_min
    start, end = parse_time_window(window)
    if start == end:
        return True
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def should_pause_for_routine(trace: LifeTrace, pause_on_sleep: bool = True) -> bool:
    """作息联动：睡觉 / 小睡时段停发（纯逻辑，可测）。"""
    if not pause_on_sleep:
        return False
    routine = str(getattr(trace, "routine", "") or "").strip()
    return routine in SLEEP_ROUTINES


def routine_pause_reason(trace: LifeTrace) -> str:
    """给日志 / 操作记录用的中文停发原因。"""
    routine = str(getattr(trace, "routine", "") or "").strip()
    return f"作息为「{routine or '未知'}」，睡觉时段暂停动作" if routine else "睡觉时段暂停动作"


__all__ = [
    "read_life_trace",
    "can_speak_now",
    "fetch_hot_topics",
    "fetch_reward_topics",
    "SLEEP_ROUTINES",
    "parse_time_window",
    "in_post_time_window",
    "should_pause_for_routine",
    "routine_pause_reason",
]
