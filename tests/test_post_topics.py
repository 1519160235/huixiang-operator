# -*- coding: utf-8 -*-
"""回响插件：发帖挂话题单测。

【背景】真机 2026-11-08 用户报障「不是会自动发 5 个话题吗？怎么没了」。
根因不是「丢了」，是从来没挂上：`linked_topics_enabled` / `max_topics` /
`custom_topics` 三个配置项一直没人读，`summary_to_draft` 也没传 topic_ids。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import topics  # noqa: E402
from models import Partition  # noqa: E402

PARTS = [
    Partition(id=806933, name="AI"),
    Partition(id=5483, name="原神"),
    Partition(id=7214, name="盒友杂谈"),
    Partition(id=18745, name="数码硬件"),
    Partition(id=425422, name="Steam"),
]


class PickTopicIdsTest(unittest.TestCase):
    def test_partition_always_first(self) -> None:
        ids = topics.pick_topic_ids(partitions=PARTS, partition=PARTS[1], text="")
        self.assertEqual(ids[0], 5483)

    def test_match_names_in_text(self) -> None:
        # 正文提到的分区优先于「同组补齐」，但总数不超过平台硬限制 2 个。
        # 名额只有 2 个：主分区先占一位，剩下一位给「名字最长的那次命中」
        # （数码硬件 4 字 > 原神 2 字，长名更具体，能少误命中）。
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=PARTS[0], text="今天原神抽卡歪了，顺便聊聊数码硬件"
        )
        self.assertEqual(ids[0], 806933, "主分区必须挂在第一位")
        self.assertEqual(len(ids), topics.MAX_TOPICS_HARD_LIMIT)
        self.assertIn(18745, ids, "正文里提到的分区应该占剩下那一位")

    def test_body_match_beats_group_fill(self) -> None:
        # 正文明确提到的分区，优先级高于同组按热度补齐。
        parts = [
            Partition(id=1, name="AI", group="G", heat=90),
            Partition(id=2, name="原神", group="别的组", heat=1),
            Partition(id=3, name="同组热门", group="G", heat=999),
        ]
        ids = topics.pick_topic_ids(partitions=parts, partition=parts[0], text="聊两句原神")
        self.assertEqual(ids, [1, 2], "正文命中应该挤掉同组补齐")

    def test_custom_topics_first(self) -> None:
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=PARTS[0], text="", custom=["原神"]
        )
        self.assertEqual(ids[0], 5483, "自定义话题应排在分区前面")
        self.assertIn(806933, ids)

    def test_custom_unknown_ignored(self) -> None:
        # 认不出的名字跳过，但主分区仍要挂（不足会用同组/热门补齐）。
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=PARTS[0], text="", custom=["不存在的话题"], fill_same_group=False
        )
        self.assertEqual(ids, [806933])

    def test_partial_name_match(self) -> None:
        # 用户可能只写简称/全称的一部分。
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=None, text="", custom=["盒友"]
        )
        self.assertEqual(ids[0], 7214)

    def test_respects_max_topics(self) -> None:
        ids = topics.pick_topic_ids(
            partitions=PARTS,
            partition=PARTS[0],
            text="原神 数码硬件 盒友杂谈 Steam",
            max_topics=2,
        )
        self.assertEqual(len(ids), 2)

    def test_no_duplicates(self) -> None:
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=PARTS[0], text="AI AI AI 原神", custom=["AI"]
        )
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_partitions(self) -> None:
        self.assertEqual(topics.pick_topic_ids(partitions=[], partition=None, text="随便"), [])

    def test_none_partition_ok(self) -> None:
        ids = topics.pick_topic_ids(partitions=PARTS, partition=None, text="原神", fill_same_group=False)
        self.assertEqual(ids, [5483])

    def test_single_char_name_not_matched(self) -> None:
        # 单字分区名容易误命中，不该参与正文匹配。
        parts = [Partition(id=1, name="游"), Partition(id=2, name="Steam")]
        ids = topics.pick_topic_ids(
            partitions=parts, partition=None, text="游戏真好玩，Steam 打折", fill_same_group=False
        )
        self.assertNotIn(1, ids)
        self.assertIn(2, ids)


if __name__ == "__main__":
    unittest.main()


class SameGroupFillTest(unittest.TestCase):
    """默认不按热度硬凑。

    小黑盒原话：「正确勾选可增加曝光展示机会，错误勾选将会被处理。」
    给一条聊 AI 的帖子勾上「英雄联盟」属于乱勾，宁缺勿滥。
    """

    def test_no_fill_without_group(self) -> None:
        # 分区没有分组信息时，只勾主分区，绝不按热度乱凑。
        ids = topics.pick_topic_ids(partitions=PARTS, partition=PARTS[0], text="", max_topics=5)
        self.assertEqual(ids, [806933])

    def test_fills_from_same_group_only(self) -> None:
        # 对应手机端：选完分区，App 自动推同类的相关话题。
        parts = [
            Partition(id=1, name="AI", group="盒友之家", heat=10),
            Partition(id=2, name="盒友杂谈", group="盒友之家", heat=5),
            Partition(id=3, name="校园生活", group="盒友之家", heat=3),
            Partition(id=4, name="英雄联盟", group="网游竞技", heat=999),
        ]
        # 同组补齐也不许超过 2 个（小黑盒「请选择2个以内的分区」）。
        ids = topics.pick_topic_ids(partitions=parts, partition=parts[0], text="", max_topics=3)
        self.assertEqual(ids, [1, 2], "只补同组，且不跨组拿高热度")
        self.assertNotIn(4, ids)

    def test_never_exceeds_platform_limit(self) -> None:
        """配置写成 5 也只能挂 2 个。

        【2026-11-09 用户报障「发帖一直失败」】真机错误是
        「请选择2个以内的分区」—— 多挂一个整条帖子就被拒，
        所以配置只是上限声明，真正的天花板是平台。
        """
        parts = [Partition(id=i, name=f"社区{i}", group="G", heat=10 - i) for i in range(1, 9)]
        for want in (3, 5, 99):
            ids = topics.pick_topic_ids(partitions=parts, partition=parts[0], text="", max_topics=want)
            self.assertEqual(len(ids), topics.MAX_TOPICS_HARD_LIMIT, f"max_topics={want} 时超限")

    def test_can_disable_group_fill(self) -> None:
        parts = [
            Partition(id=1, name="AI", group="G", heat=10),
            Partition(id=2, name="别的", group="G", heat=5),
        ]
        ids = topics.pick_topic_ids(
            partitions=parts, partition=parts[0], text="", max_topics=5, fill_same_group=False
        )
        self.assertEqual(ids, [1])

    def test_relevant_topics_capped(self) -> None:
        # 相关内容多于上限时按顺序截断。
        ids = topics.pick_topic_ids(
            partitions=PARTS, partition=PARTS[0], text="原神 数码硬件 盒友杂谈 Steam", max_topics=2
        )
        self.assertEqual(len(ids), 2)
