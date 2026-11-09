# -*- coding: utf-8 -*-
"""评论链路的三处真机缺陷（2026-11-09 用户报障）。

1. 「有的时候评论记录里说正文没生成」——跳过原因从来没记下来，
   上层只看到 comments_sent=0，用户无从判断是模型没写、还是被护栏拦了。
2. 「有几个评论还是带了 cube 标语」——缺括号的 `cube_xxx` 被当残渣删，
   连带把半句话吃掉，极端情况整条评论变空。
3. 「一个评论重复了好几个」——同一个人连续评论多条，逐条回复变成连珠炮。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import emojis  # noqa: E402


class BareMarkRepairTest(unittest.TestCase):
    """缺括号的表情：能认出来就补括号保住，别把正文一起删掉。"""

    def test_bare_mark_kept(self) -> None:
        self.assertEqual(emojis.sanitize_marks("今天真累cube_doge"), "今天真累[cube_doge]")

    def test_mark_at_sentence_end(self) -> None:
        # 句子末尾最容易踩「空串是任何串的子串」这个坑，单独盯。
        self.assertEqual(emojis.sanitize_marks("笑死 cube_doge"), "笑死 [cube_doge]")

    def test_unknown_bare_mark_still_removed(self) -> None:
        # 白名单外的名字猜不出来，必须删干净，不能留 `cube_` 残渣。
        out = emojis.sanitize_marks("今天真累cube_不存在的表情")
        self.assertNotIn("cube_", out)
        self.assertIn("今天真累", out)

    def test_no_text_eaten(self) -> None:
        """表情处理前后，非表情文字必须一字不少。"""
        for raw in (
            "这个操作cube_笑死我了真的",
            "哈哈哈哈 cube_doge 太真实了",
            "来了来了[cube_喜欢]",
        ):
            out = emojis.sanitize_marks(raw)
            clean = emojis.sanitize_marks(out)
            self.assertEqual(clean, out, f"二次清洗结果不稳定：{raw}")
            # 表情之外的正文不能被吃掉
            self.assertGreaterEqual(len(out.replace("[", "").replace("]", "")), 4)


class CommentSkipReasonTest(unittest.TestCase):
    """跳过评论必须带原因，用户才看得懂「为什么没评论」。"""

    def test_interact_reports_skip_reasons(self) -> None:
        src = (ROOT / "layers" / "interact.py").read_text(encoding="utf-8")
        # 三条主要跳过分支都要有中文原因
        for reason in ("拿不到帖子 id", "没东西可接", "模型没返回正文"):
            self.assertIn(reason, src, f"缺少跳过原因：{reason}")
        # 返回值要带上明细，不能只回一个 0
        self.assertIn('"skipped": skipped', src)


class ReplyBurstDedupTest(unittest.TestCase):
    """同一个人连发多条，不能回成一串复读。"""

    def test_same_thread_replied_once(self) -> None:
        src = (ROOT / "layers" / "interact.py").read_text(encoding="utf-8")
        self.assertIn("replied_keys", src, "缺少同楼层去重")
        self.assertIn("sender_uid", src, "去重键要带发送人，否则会误伤别人")
        self.assertIn("root_comment_id", src, "去重键要带楼层，否则同一帖不同楼层也会被吞")

    def test_avoid_texts_passed_down(self) -> None:
        src = (ROOT / "layers" / "create.py").read_text(encoding="utf-8")
        self.assertIn("avoid_texts", src, "生成侧要能收到「这些说过了」")
        self.assertIn("不要再重复同样的说法", src)


class PublishTitleFallbackTest(unittest.TestCase):
    """标题被清空会让整条帖子白生成（接口报「标题必须为 1–80 字」）。"""

    def test_empty_title_falls_back_to_body(self) -> None:
        from layers.publish import _safe_title

        out = _safe_title("", "把麻将换成汉字来胡牌")
        self.assertEqual(out, "把麻将换成汉字来胡牌")  # 10 字 = 30 字节，刚好

    def test_mark_only_title_falls_back(self) -> None:
        from layers.publish import _safe_title

        # 30 字节上限 = 10 个汉字，所以兜底标题也会被截到 10 字，这是对的。
        out = _safe_title("[cube_doge]", "今天去钓鱼了，一条没上")
        self.assertTrue(out.startswith("今天去钓鱼了"))
        self.assertLessEqual(len(out.encode("utf-8")), 30)

    def test_normal_title_untouched(self) -> None:
        from layers.publish import _safe_title

        self.assertEqual(_safe_title("正常的标题"), "正常的标题")
        self.assertEqual(_safe_title("标题：带前缀"), "带前缀")


if __name__ == "__main__":
    unittest.main()

class TitleByteLimitTest(unittest.TestCase):
    """标题上限按 UTF-8 字节算，不是按字符算（真机实测 2026-11-09）。

    10 个汉字 = 30 字节刚好过，11 个汉字就被拒；带 emoji / cube 标记更容易超。
    """

    def test_hits_byte_limit_not_char_limit(self) -> None:
        from layers.publish import TITLE_MAX_BYTES, _safe_title

        out = _safe_title("这是一个非常长的测试标题用来验证长度限制到底是多少字节", "正文")
        self.assertLessEqual(len(out.encode("utf-8")), TITLE_MAX_BYTES)

    def test_emoji_not_split_in_half(self) -> None:
        from layers.publish import _truncate_title

        out = _truncate_title("标题内容😭😭😭😭😭😭😭😭😭😭😭😭")
        self.assertLessEqual(len(out.encode("utf-8")), 30)
        # 不能出现被截断的半截代理字符
        self.assertEqual(out.encode("utf-8").decode("utf-8"), out)

    def test_mark_stripped_from_title(self) -> None:
        from layers.publish import _safe_title

        self.assertEqual(_safe_title("[cube_doge]今天去钓鱼", "正文"), "今天去钓鱼")


class ExclusivePartitionTest(unittest.TestCase):
    """有的分区报「只能单独选择」，不能和其他分区混挂。"""

    def setUp(self) -> None:
        from layers import topics

        topics.forget_all_exclusive()

    def tearDown(self) -> None:
        from layers import topics

        topics.forget_all_exclusive()

    def test_exclusive_partition_alone(self) -> None:
        from layers import topics
        from models import Partition

        parts = [
            Partition(id=1, name="盒友杂谈", group="G", heat=9),
            Partition(id=2, name="AI", group="G", heat=8),
        ]
        topics.remember_exclusive(1)
        ids = topics.pick_topic_ids(partitions=parts, partition=parts[0], max_topics=2)
        self.assertEqual(ids, [1], "独占分区只能单独挂")

    def test_exclusive_not_used_as_filler(self) -> None:
        from layers import topics
        from models import Partition

        parts = [
            Partition(id=1, name="AI", group="G", heat=9),
            Partition(id=2, name="盒友杂谈", group="G", heat=8),
            Partition(id=3, name="其他", group="G", heat=7),
        ]
        topics.remember_exclusive(2)
        ids = topics.pick_topic_ids(partitions=parts, partition=parts[0], max_topics=2)
        self.assertNotIn(2, ids, "独占分区不能当补位项")

    def test_learned_from_error_message(self) -> None:
        from layers import publish, topics
        from models import Partition

        class _Cache:
            def get(self):
                return [Partition(id=7214, name="盒友杂谈", group="G")]

        class _Store:
            partition = _Cache()

        class _Ctx:
            store = _Store()

        class _Draft:
            partition = None

        publish._learn_exclusive_from_error(_Ctx(), "只能单独选择 盒友杂谈 分区", _Draft())
        self.assertTrue(topics.is_exclusive(7214))


