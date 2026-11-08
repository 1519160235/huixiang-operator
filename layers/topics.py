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

现在按这个顺序凑够 `max_topics` 个：
1. 用户手填的 `custom_topics`（点名要的，优先）；
2. 本次选中的分区（主社区，必挂）；
3. 标题/正文里出现的社区名（提到「原神」就挂原神）；
4. 主分区同组的其他社区，按热度从高到低补；
5. 还不够就跨组按热度补。
全部去重，按 `max_topics` 截断。
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_MAX_TOPICS = 5


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
    limit = max(1, int(max_topics or DEFAULT_MAX_TOPICS))
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
            _add(int(getattr(p, "id", 0) or 0))

    return picked


__all__ = ["DEFAULT_MAX_TOPICS", "pick_topic_ids"]
