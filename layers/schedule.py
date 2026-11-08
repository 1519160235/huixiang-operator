# -*- coding: utf-8 -*-
"""发帖时机：时段窗口 + 间隔 + 软目标。

【真机缺陷 2026-11-08 用户反馈】
「这个额度我也没搞懂……我之前 MCP 时代是早中晚各一个，但是现在我有点拿不准了，
我的想法是它想什么时候发什么时候发，它不是一个什么情绪插件吗？咱们能配合用啊。」

原来的模型是「每天最多 3 条」的硬额度 + 「刷够 50 条才触发」，两个都不像人：
- 硬额度：到量就彻底不发了，哪怕刚刷到特别好玩的；
- 刷够 50 条：一天可能都凑不满，于是要么不发、要么一发就是一大篇汇总。

现在改成「时段窗口」：
- 一天给几个窗口（默认早/中/晚），每个窗口最多发 1 条；
- 窗口内只要生活状态 OK、有值得发的东西，就发；没有就不发，不硬凑；
- 硬额度只作为防失控的兜底（默认 5 条），不是主要节奏。

生活状态来自 life-frequency（作息 / 情绪 / 体力 / 社交电量），
睡觉、刚醒、电量见底时不发 —— 这就是「配合情绪插件」的部分。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Sequence

# 默认：白天到深夜一整段可发（「想什么时候发就什么时候发」），
# 频率交给最小间隔 + 硬上限，而不是把一天切碎成几个窗口。
# 想回到「早中晚各一条」的节奏，把它改成 08:00-11:00,12:00-14:00,19:00-23:00 即可。
DEFAULT_SLOTS = "09:00-23:30"

# 只配一个窗口时，建议的最小间隔（分钟）：避免中午发完下午又发。
DEFAULT_MIN_GAP_MINUTES = 120

# 同一天最多记多少个已发窗口（滚动保留，防文件膨胀）。
MAX_POSTED_SLOTS = 24


def parse_slots(raw: str) -> list[tuple[int, int]]:
    """把 "08:00-11:00,12:00-14:00" 解析成 [(480, 660), ...]（分钟数）。

    非法片段直接跳过；起止相等或倒挂的也跳过，绝不让配置错误卡死发帖。
    """
    slots: list[tuple[int, int]] = []
    for chunk in str(raw or "").replace("；", ",").replace(";", ",").split(","):
        piece = chunk.strip()
        if not piece or "-" not in piece:
            continue
        left, _, right = piece.partition("-")
        try:
            start = _to_minutes(left)
            end = _to_minutes(right)
        except ValueError:
            continue
        if start == end or start < 0 or end < 0:
            continue
        # 起止倒挂：只有「结束落在凌晨 6 点前」才当跨零点窗口（22:00-02:00）；
        # 其余（14:00-13:00 这种）是写反了，直接跳过，别让笔误变成 23 小时窗口。
        if end < start and end > 6 * 60:
            continue
        slots.append((start, end))
    return slots or parse_slots(DEFAULT_SLOTS)


def _to_minutes(text: str) -> int:
    hh, _, mm = str(text).strip().partition(":")
    hour = int(hh)
    minute = int(mm) if mm else 0
    if not (0 <= hour <= 24) or not (0 <= minute < 60):
        raise ValueError("时间越界")
    return hour * 60 + minute


def _in_window(now_minute: int, start: int, end: int) -> bool:
    """判断当前分钟是否落在窗口内；跨零点的窗口（22:00-02:00）也支持。"""
    if start < end:
        return start <= now_minute < end
    return now_minute >= start or now_minute < end


def in_any_slot(slots_raw: str, now_minutes: int | None = None) -> bool:
    """当前时间是否落在「发帖时段」的任意一段里（供护栏做闸门用）。

    和 `should_post_now` 分开：那个还要看今天发没发、间隔够不够；
    这里只回答「现在这个点，属于不属于允许发帖的时段」。

    :param now_minutes: 当天已过的分钟数（测试注入用）；留空取当前时间。
    """
    if now_minutes is None:
        moment = datetime.now()
        now_minutes = moment.hour * 60 + moment.minute
    return any(_in_window(int(now_minutes), s, e) for s, e in parse_slots(slots_raw))


def current_slot(now: datetime | None, raw_slots: str) -> str:
    """返回当前所处窗口的可读文本（如 "12:00-14:00"）；不在任何窗口返回空串。"""
    moment = now or datetime.now()
    minute = moment.hour * 60 + moment.minute
    for start, end in parse_slots(raw_slots):
        if _in_window(minute, start, end):
            return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
    return ""


def slot_key(now: datetime | None, raw_slots: str) -> str:
    """当前窗口的唯一键（日期 + 窗口），用来记「这个窗口已经发过了」。"""
    moment = now or datetime.now()
    slot = current_slot(moment, raw_slots)
    return f"{moment.strftime('%Y-%m-%d')}|{slot}" if slot else ""


def can_post_in_slot(posted: Sequence[str], key: str) -> bool:
    """这个窗口是不是还没发过。"""
    return bool(key) and key not in set(posted or [])


def trim_posted(posted: Sequence[str]) -> list[str]:
    """只保留最近 24 个窗口记录。"""
    return list(posted or [])[-MAX_POSTED_SLOTS:]


def _life_blocked(trace: Any) -> str:
    """生活状态是否不适合发帖。返回原因，空串表示可以发。

    - 睡觉 / 深夜：不发（凌晨发帖本身就假）；
    - 社交电量明确低于 20%：不发；
    - 体力明确低于 15%：不发（累到不想说话）。
    `None` 表示未接生活插件，一律放行 —— 不能因为读不到状态就把人锁死。
    """
    if trace is None:
        return ""
    routine = str(getattr(trace, "routine", "") or "")
    if routine in {"深夜", "睡觉", "睡眠", "凌晨"}:
        return f"现在是{routine}，不适合发帖"
    battery = getattr(trace, "social_battery", None)
    if isinstance(battery, (int, float)) and 0 <= float(battery) < 0.2:
        return "社交电量太低，先不发"
    energy = getattr(trace, "energy", None)
    if isinstance(energy, (int, float)) and 0 <= float(energy) < 0.15:
        return "体力见底，先不发"
    return ""


def should_post_now(
    *,
    now: datetime | None = None,
    slots_raw: str = "",
    posted_slots: Sequence[str] = (),
    last_post_ts: float = 0.0,
    today_count: int = 0,
    hard_limit: int = 0,
    min_gap_minutes: int = 0,
    trace: Any = None,
) -> tuple[bool, str]:
    """综合判断「现在能不能发」。返回 (能否, 中文原因)。

    【2026-11-08 用户要求「既然都说了他是自己想发，那就不要设置什么时间段了」】
    时段（`post_slots`）、每段一条、最小间隔全部取消 —— 剩下的只有两道闸：
    当天硬上限（防失控）和生活状态（睡觉/累垮不发）。
    什么时候发、发不发，交给「想不想发」那一步，不再按时间表卡。

    `slots_raw` / `posted_slots` / `min_gap_minutes` 仅为兼容旧调用保留，已不参与判断。
    """
    if hard_limit > 0 and today_count >= hard_limit:
        return False, f"今天已经发了 {today_count} 条，到硬上限 {hard_limit} 了"

    blocked = _life_blocked(trace)
    if blocked:
        return False, blocked

    return True, "可以发"


__all__ = [
    "DEFAULT_SLOTS",
    "DEFAULT_MIN_GAP_MINUTES",
    "MAX_POSTED_SLOTS",
    "parse_slots",
    "current_slot",
    "in_any_slot",
    "slot_key",
    "can_post_in_slot",
    "trim_posted",
    "should_post_now",
]
