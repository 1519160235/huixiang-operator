# -*- coding: utf-8 -*-
"""发帖勾选社区（= 话题 id）。

【真机缺陷 2026-11-08 用户报障「不是会自动发 5 个话题吗？怎么没了」】
根因：话题从来没挂上过，不是「丢了」。
- `config_model` 里 `linked_topics_enabled` / `max_topics` / `custom_topics` 三个配置项
  一直在界面上，但**代码里没有任何地方读它们**；
- `fetch_partitions` 只把 `topic_id` 存进 `Partition.id`，`Partition.topic_ids` 从来没填过；
- 发帖时 `summary_to_draft(summary, partition=partition)` 没传 `topic_ids`，
  `summary.topic_ids` 也是空的，最后 `_topic_ids_str` 拼出来是空串 → 一个都没挂。

小黑盒发帖接口原话：「可点击下方『选择社区』勾选对应社区，正确勾选可增加曝光展示机会。
如没有勾选社区将作为动态发出，仅粉丝可见。」——所以这不是装饰，勾不满就是少曝光。

【真机缺陷 2026-11-09 用户报障「发帖一直失败」】
小黑盒发帖接口原话：「请选择2个以内的分区」。之前把上限设成 5，
只要正文里提到的分区 + 主分区 + 同组补齐超过 2 个，整条帖子直接被拒，
表现是「一直在跑但一条也发不出去」。所以上限压到 2（平台硬限制，不是我们的口味）。

现在按这个顺序凑够 `max_topics` 个：
1. 用户手填的 `custom_topics`（点名要的，优先）；
2. 本次选中的分区（主社区，必挂）；
3. 标题/正文里出现的社区名（提到「原神」就挂原神）；
4. 主分区同组的其他社区，按热度从高到低补；
5. 还不够就跨组按热度补。
全部去重，按 `max_topics` 截断。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

# 小黑盒发帖接口的硬限制：分区最多 2 个，多一个就整条被拒。
DEFAULT_MAX_TOPICS = 2
MAX_TOPICS_HARD_LIMIT = 2

# 【真机缺陷 2026-11-09】有些分区（如「盒友杂谈」）一旦被拒会报
# 「只能单独选择 XX 分区」—— 说明它不能和任何其他分区一起勾。
# 这类分区只能靠真机错误学出来：发帖失败时由 publish 调 `remember_exclusive` 记下，
# 之后挑分区时把它排到第一位、且不再补第二个。
_EXCLUSIVE_IDS: set[int] = set()


# 落盘文件（数据目录下），重启不用重新踩坑学一遍。
_EXCLUSIVE_FILE = "exclusive_partitions.json"


def _exclusive_path(ctx: Any = None) -> Path | None:
    paths = getattr(ctx, "paths", None) if ctx is not None else None
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    if not data_dir:
        return None
    return Path(data_dir) / _EXCLUSIVE_FILE


def load_exclusive(ctx: Any) -> None:
    """从数据目录恢复独占分区名单（插件启动时调）。"""
    path = _exclusive_path(ctx)
    if path is None or not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    ids = raw.get("ids") if isinstance(raw, dict) else raw
    for pid in ids or []:
        try:
            _EXCLUSIVE_IDS.add(int(pid))
        except (TypeError, ValueError):
            continue


def _save_exclusive(ctx: Any = None) -> None:
    path = _exclusive_path(ctx)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ids": sorted(_EXCLUSIVE_IDS)}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def remember_exclusive(partition_id: int, ctx: Any = None) -> None:
    """记下「必须单独发布」的分区 id（由发帖失败的报错触发），并落盘。"""
    try:
        pid = int(partition_id or 0)
    except (TypeError, ValueError):
        return
    if pid and pid not in _EXCLUSIVE_IDS:
        _EXCLUSIVE_IDS.add(pid)
        _save_exclusive(ctx)


def is_exclusive(partition_id: int) -> bool:
    try:
        return int(partition_id or 0) in _EXCLUSIVE_IDS
    except (TypeError, ValueError):
        return False


def forget_all_exclusive() -> None:
    """清空记忆（测试用 / 平台规则变了以后手工复位）。"""
    _EXCLUSIVE_IDS.clear()


def _table(partitions: Iterable[Any]) -> dict[str, Any]:
    """社区名 → Partition（名字去掉空格，便于匹配）。"""
    table: dict[str, Any] = {}
    for p in partitions or []:
        name = str(getattr(p, "name", "") or "").strip()
        if name and int(getattr(p, "id", 0) or 0):
            table[name.replace(" ", "")] = p
    return table


def pick_topic_ids(
    *,
    partitions: Iterable[Any],
    partition: Any = None,
    text: str = "",
    custom: Iterable[str] | None = None,
    max_topics: int = DEFAULT_MAX_TOPICS,
    fill_same_group: bool = True,
) -> list[int]:
    """挑出这次发帖要勾选的社区 id（去重、按 max_topics 截断）。

    对应手机端的行为：**选完分区后，App 自动推几个相关话题**。
    这里照这个逻辑走，相关性只认同组和正文命中，绝不按热度乱凑。

    :param partitions: 分区目录（`Partition.id` 就是发帖用的 topic_id）。
    :param partition: 本次发帖的主分区。
    :param text: 标题 + 正文，用来匹配正文里提到的社区名。
    :param custom: 用户手填的社区名（`community_allowlist`）。
    :param max_topics: 最多勾几个。
    :param fill_same_group: 不够时是否用**同一分组**的社区补（默认补，这才是「相关」）。
        小黑盒原话「正确勾选可增加曝光展示机会，错误勾选将会被处理」，
        所以只补同组，不跨组按热度乱勾。想固定多勾就在 `community_allowlist` 里写。
    """
    # 配置里写多大都无所谓，最终不许超过平台硬限制。
    limit = min(max(1, int(max_topics or DEFAULT_MAX_TOPICS)), MAX_TOPICS_HARD_LIMIT)
    all_parts = [p for p in (partitions or []) if int(getattr(p, "id", 0) or 0)]
    table = _table(all_parts)
    picked: list[int] = []

    def _add(pid: int) -> None:
        if pid and pid not in picked and len(picked) < limit:
            picked.append(pid)

    # 1) 自定义社区优先（用户点名要的）。
    for name in custom or []:
        key = str(name or "").strip().replace(" ", "")
        if not key:
            continue
        if key in table:
            _add(int(getattr(table[key], "id", 0) or 0))
            continue
        # 退一步做包含匹配：用户可能只写简称/全称的一部分。
        for pname, part in table.items():
            if key in pname or pname in key:
                _add(int(getattr(part, "id", 0) or 0))
                break

    # 2) 主分区必挂。
    main_id = int(getattr(partition, "id", 0) or 0) if partition is not None else 0
    _add(main_id)
    # 主分区是「必须单独发布」的那种：一个都不能多挂，否则整条被拒。
    if main_id and is_exclusive(main_id):
        return [main_id]

    # 3) 正文里提到的社区名，按名字长度从长到短（长名更具体，避免「游戏」误命中）。
    body = str(text or "")
    if body:
        for pname in sorted(table, key=len, reverse=True):
            if len(picked) >= limit:
                break
            if len(pname) >= 2 and pname in body:
                _add(int(getattr(table[pname], "id", 0) or 0))

    if not fill_same_group or len(picked) >= limit:
        return picked

    # 4) 同组社区按热度补 —— 等价于手机端「选完分区自动推的相关话题」。
    main_group = str(getattr(partition, "group", "") or "") if partition is not None else ""
    if main_group and main_group != "推荐":
        same_group = sorted(
            [p for p in all_parts if str(getattr(p, "group", "") or "") == main_group],
            key=lambda p: float(getattr(p, "heat", 0) or 0),
            reverse=True,
        )
        for p in same_group:
            if len(picked) >= limit:
                break
            pid = int(getattr(p, "id", 0) or 0)
            # 独占分区只能自己单独出现，不能当补位项。
            if is_exclusive(pid) and pid != main_id:
                continue
            _add(pid)

    return picked


__all__ = [
    "DEFAULT_MAX_TOPICS",
    "load_exclusive",
    "MAX_TOPICS_HARD_LIMIT",
    "pick_topic_ids",
    "remember_exclusive",
    "is_exclusive",
    "forget_all_exclusive",
]
