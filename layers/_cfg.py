# -*- coding: utf-8 -*-
"""回响插件：配置读取小工具（统一口径，别再各层各写一份）。

【为什么单独抽出来】
配置在真机是 pydantic 对象（`HuiXiangConfig.basic.enabled`），在单测里常写成 dict
（`{"basic": {"enabled": True}}`）。之前每层各写一遍 `isinstance(cfg, dict)` 分支，
既啰嗦又容易漏，出过一个 `since_last_hours` 变量名笔误。统一收在这里：
- `section(ctx, name)` 取某一段（对象属性或 dict 键）；
- `read(sec, key, default)` 取某段里的字段（对象属性或 dict 键）。

刻意保持零第三方依赖：本地没装 maibot_sdk / pydantic 也能用。
"""

from __future__ import annotations

from typing import Any


def section(ctx: Any, name: str) -> Any:
    """取配置里的某一段（如 "browse"）；取不到返回 None。"""
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        return cfg.get(name)
    return getattr(cfg, name, None)


def read(sec: Any, key: str, default: Any = None) -> Any:
    """从一段配置里读字段；段是 None 时返回默认值。"""
    if sec is None:
        return default
    if isinstance(sec, dict):
        value = sec.get(key, default)
    else:
        value = getattr(sec, key, default)
    return default if value is None else value


def read_str(sec: Any, key: str, default: str = "") -> str:
    """读字符串字段。"""
    return str(read(sec, key, default))


def read_int(sec: Any, key: str, default: int = 0) -> int:
    """读整数字段（解析失败退回默认值，不抛异常）。"""
    try:
        return int(read(sec, key, default))
    except (TypeError, ValueError):
        return default


def read_bool(sec: Any, key: str, default: bool = False) -> bool:
    """读布尔字段。"""
    return bool(read(sec, key, default))


def read_list(sec: Any, key: str, default: list | None = None) -> list:
    """读列表字段（返回副本，避免调用方改到配置本身）。"""
    value = read(sec, key, default if default is not None else [])
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


__all__ = ["section", "read", "read_str", "read_int", "read_bool", "read_list", "norm_choice", "norm_partition"]


# 「不挂分区」在配置下拉框里的显示值（前端 Select 不接受空字符串做选项）。
NO_PARTITION_CHOICE = "不指定分区"


def norm_partition(value: Any) -> str:
    """把「默认分区」配置值归一化：中文哨兵 / 空值 → 空串（不挂分区）。"""
    raw = str(value or "").strip()
    return "" if raw in {"", NO_PARTITION_CHOICE, "无", "none", "null"} else raw


# ===================== 中文选项值 → 内部值 =====================
# 2026-11-06：配置界面的下拉候选只能来自 Literal，没有「值 → 中文」映射机制，
# 所以把选项值本身改成了中文（演练 / 自动 / 全部分区 / 仅指定分区 / 按帖数 …），
# 界面上才看得懂。业务层仍按内部英文值判断，这里做一层统一映射，
# 同时兼容老配置里写的 dry_run / auto / all / selected / count / time / both。

_CHOICE_ALIASES: dict[str, dict[str, str]] = {
    "mode": {
        "演练": "dry_run",
        "dry_run": "dry_run",
        "自动": "auto",
        "auto": "auto",
    },
    "partition_mode": {
        "全部分区": "all",
        "all": "all",
        "仅指定分区": "selected",
        "selected": "selected",
    },
    "trigger_by": {
        "按帖数": "count",
        "count": "count",
        "按时间": "time",
        "time": "time",
        "两者都看": "both",
        "both": "both",
    },
}


def norm_choice(field: str, value: Any, default: str = "") -> str:
    """把配置里的选项值规范成内部英文值（中文界面值、老英文值都认）。

    :param field: 字段名（mode / partition_mode / trigger_by）。
    :param value: 配置里读到的原始值。
    :param default: 认不出来时返回的兜底值。
    """
    raw = str(value or "").strip()
    if not raw:
        return default
    table = _CHOICE_ALIASES.get(field, {})
    return table.get(raw, table.get(raw.lower(), default))
