# -*- coding: utf-8 -*-
"""发帖熔断：平台明确说「发不了」时，别再一轮轮重新生成 + 生图白烧钱。

【真机事故 2026-11-12】
小黑盒从当天 07:21 起对每一次发帖都回 `failed: 发布功能暂不可用`，
一天 156 次 `publish_rejected`、0 成功。但发帖流水线是
「生成文本 → 生图 → 上传图床 → 发帖」，图是在**发帖之前**生成的，
发布失败后整轮草稿丢弃，下一轮又从头生一遍：当天数据目录里堆了
328 张 `hx_img_*.png`，一张都没发出去，用户 API 后台白扣了 5 块钱。

【设计】
1. 平台级拒绝（不是分区/标题这类本插件能自己修的错误）→ 记一次并进入冷却；
   冷却期内在 `check_post_permission` 就拦下整轮，连文本都不生成。
2. 连续被拒则指数退避，上限 6 小时，避免每 2 分钟白试一次。
3. 冷却到期后不立刻恢复生图：先用**纯文字**试探一轮
   （`should_skip_image` 返回 True）。发出去了才 `clear()` 恢复正常；
   又被拒则继续退避。这样试探阶段也不花生图的钱。
4. 演练模式不发帖、也不登记冷却。

状态落盘在 data_dir/publish_cooldown.json，重启后仍然有效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# 这些是「平台/账号层面发不了」的回绝。重试没有意义，必须冷却。
# 注意别把「错误的动态分区 / 标题超长 / 独占分区」这类能自动修的错误算进来。
PLATFORM_BLOCK_MARKERS: tuple[str, ...] = (
    "发布功能暂不可用",
    "评论功能暂不可用",
    "回复功能暂不可用",
    "功能暂不可用",          # 通用兜底：平台换措辞也能认出来
    "发布功能暂未开放",
    "暂不可发帖",
    "暂不可评论",
    "禁止发布",
    "禁止评论",
    "账号被限制",
    "已被禁言",
    "没有发帖权限",
    # 2026-11-12 v16 协议下平台的新话术：验签通过后，账号级写功能被限制。
    "你的账号无法使用该功能",
    "无法使用该功能",
    "账号无法使用",
)

# 限流单独走短冷却：等一会儿就好，不该按「平台坏了」退避几小时。
RATE_LIMIT_MARKERS: tuple[str, ...] = ("频率过快", "429", "请求频率受限", "操作太频繁")

BASE_COOLDOWN_SEC = 30 * 60          # 首次 30 分钟
RATE_LIMIT_COOLDOWN_SEC = 10 * 60    # 限流 10 分钟
MAX_COOLDOWN_SEC = 6 * 60 * 60       # 上限 6 小时

STATE_FILE = "publish_cooldown.json"


def _state_path(ctx: Any) -> Path:
    paths = getattr(ctx, "paths", None)
    d = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None) or "."
    return Path(d) / STATE_FILE


def _read(ctx: Any) -> dict[str, Any]:
    path = _state_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(ctx: Any, data: dict[str, Any]) -> None:
    path = _state_path(ctx)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 存不下就当作没熔断，不让状态文件把发帖链路拖垮。
        pass


def is_blocked(ctx: Any, now: float | None = None) -> tuple[bool, str]:
    """冷却中返回 (True, 说明)；否则 (False, "")。"""
    data = _read(ctx)
    until = float(data.get("until") or 0)
    now = time.time() if now is None else now
    if until <= now:
        return False, ""
    reason = str(data.get("reason") or "平台拒绝发布")
    left = int(until - now)
    return True, (
        f"发布冷却中（{reason}），剩余约 {left // 60} 分钟。"
        "本轮跳过生成与生图，避免重复白扣 API 费用。"
        f"已连续被拒 {int(data.get('consecutive') or 0)} 次，若长时间不恢复请人工检查账号状态。"
    )


def should_skip_image(ctx: Any, now: float | None = None) -> bool:
    """刚从冷却里出来、还没发成功过：本轮只发纯文字试探，不生图。

    连续失败次数 > 0 且冷却已到期 == 探测轮。成功后 `clear()` 会把它归零。
    """
    data = _read(ctx)
    n = int(data.get("consecutive") or 0)
    if n <= 0:
        return False
    now = time.time() if now is None else now
    return float(data.get("until") or 0) <= now


def classify(message: str) -> str:
    """判断一次发布失败属于哪类；返回 'platform' / 'rate_limit' / ''。"""
    raw = str(message or "")
    if any(k in raw for k in PLATFORM_BLOCK_MARKERS):
        return "platform"
    if any(k in raw for k in RATE_LIMIT_MARKERS):
        return "rate_limit"
    return ""


def record_rejection(ctx: Any, message: str, now: float | None = None) -> dict[str, Any] | None:
    """登记一次发布失败。非平台级/限流类错误直接忽略，返回 None。"""
    kind = classify(message)
    if not kind:
        return None
    now = time.time() if now is None else now
    data = _read(ctx)
    n = int(data.get("consecutive") or 0) + 1
    if kind == "rate_limit":
        seconds = RATE_LIMIT_COOLDOWN_SEC
        reason = "小黑盒限流"
    else:
        seconds = min(BASE_COOLDOWN_SEC * (2 ** (n - 1)), MAX_COOLDOWN_SEC)
        # 保留平台原始话术：v16 协议下平台会换成「你的账号无法使用该功能」，
        # 写死旧话术会让状态文件误导排查。
        reason = "小黑盒：" + str(message or "")[:120]
    state = {
        "until": now + seconds,
        "consecutive": n,
        "reason": reason,
        "last_error": str(message or "")[:200],
        "last_at": now,
        "cooldown_sec": seconds,
    }
    _write(ctx, state)
    return state


def clear(ctx: Any) -> None:
    """发布成功后重置：恢复正常发帖与生图。"""
    path = _state_path(ctx)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _write(ctx, {})


__all__ = [
    "classify",
    "clear",
    "is_blocked",
    "record_rejection",
    "should_skip_image",
]
