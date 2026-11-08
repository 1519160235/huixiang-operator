# -*- coding: utf-8 -*-
"""回响插件：话题（hashtag）单测。

【背景】用户 2026-11-08 纠正：「分区是分区，话题是话题，你是不是理解错了？」

真机读回一条真实帖子：
    topics   = [{"topic_id": 806933, "name": "AI"}]          ← 分区
    hashtags = [{"name": "deepseek", "hashtag_id": 689666}]  ← 话题

之前我把 `topic_index` 返回的分区当成了话题，全错。
现在话题从真实帖子里采样（不猜、不编），发帖时按正文匹配挑。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import hashtags  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_strip_hash_and_space(self) -> None:
        self.assertEqual(hashtags.normalize(" #盒友 日常 "), "盒友日常")

    def test_lowercase(self) -> None:
        self.assertEqual(hashtags.normalize("DeepSeek"), "deepseek")


class CollectTest(unittest.TestCase):
    def test_collect_from_post(self) -> None:
        post = {"hashtags": [{"name": "deepseek", "hashtag_id": 689666}, {"name": "", "hashtag_id": 1}]}
        self.assertEqual(hashtags.collect_from_post(post), [{"name": "deepseek", "id": 689666}])

    def test_missing_hashtags(self) -> None:
        self.assertEqual(hashtags.collect_from_post({}), [])
        self.assertEqual(hashtags.collect_from_post(None), [])


class MergeTest(unittest.TestCase):
    def test_dedupe_by_id(self) -> None:
        pool = [{"name": "旧名", "id": 1}]
        merged = hashtags.merge(pool, [{"name": "新名", "id": 1}, {"name": "另一个", "id": 2}])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["name"], "新名", "同 id 用最新名字")

    def test_keep_existing(self) -> None:
        merged = hashtags.merge([{"name": "a", "id": 1}], [])
        self.assertEqual(merged, [{"name": "a", "id": 1}])


class PickTest(unittest.TestCase):
    POOL = [
        {"name": "deepseek", "id": 689666},
        {"name": "盒友日常", "id": 8401},
        {"name": "家常", "id": 986745},
        {"name": "沙雕", "id": 2871},
        {"name": "ai人工智能", "id": 135213},
    ]

    def test_match_english_case_insensitive(self) -> None:
        picked = hashtags.pick_hashtags(self.POOL, "刷到 DeepSeek 的新模型")
        self.assertEqual(picked, ["deepseek"])

    def test_match_chinese(self) -> None:
        picked = hashtags.pick_hashtags(self.POOL, "今天这条太沙雕了")
        self.assertIn("沙雕", picked)

    def test_no_match_returns_empty(self) -> None:
        # 匹配不上就不带，不硬塞不相关的话题。
        self.assertEqual(hashtags.pick_hashtags(self.POOL, "今天天气不错出去走了走"), [])

    def test_respects_limit(self) -> None:
        picked = hashtags.pick_hashtags(self.POOL, "deepseek 沙雕 家常 盒友日常", limit=2)
        self.assertEqual(len(picked), 2)

    def test_no_duplicates(self) -> None:
        pool = [{"name": "沙雕", "id": 1}, {"name": "#沙雕", "id": 2}]
        picked = hashtags.pick_hashtags(pool, "太沙雕了")
        self.assertEqual(len(picked), 1)

    def test_empty_text(self) -> None:
        self.assertEqual(hashtags.pick_hashtags(self.POOL, ""), [])

    def test_short_name_skipped(self) -> None:
        # 单字话题太容易误命中，不参与匹配。
        pool = [{"name": "图", "id": 9}]
        self.assertEqual(hashtags.pick_hashtags(pool, "这张图不错"), [])


class PoolStoreTest(unittest.TestCase):
    def test_add_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = hashtags.HashtagPool(tmp)
            added = pool.add_many([{"name": "deepseek", "id": 689666}])
            self.assertEqual(added, 1)
            self.assertEqual(pool.load(), [{"name": "deepseek", "id": 689666}])

    def test_add_same_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = hashtags.HashtagPool(tmp)
            pool.add_many([{"name": "a", "id": 1}])
            self.assertEqual(pool.add_many([{"name": "a", "id": 1}]), 0)

    def test_missing_dir_is_safe(self) -> None:
        pool = hashtags.HashtagPool(None)
        self.assertEqual(pool.load(), [])
        self.assertEqual(pool.add_many([{"name": "a", "id": 1}]), 1)


if __name__ == "__main__":
    unittest.main()
