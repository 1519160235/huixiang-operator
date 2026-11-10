# -*- coding: utf-8 -*-
"""系列常识：认游戏、注入常识、拦住瞎认亲戚。

来源是 2026-11-10 的真机事故：在《丝之歌》实体版手册那条帖子下，我们连发两条
「先把本体端上来再说吧」「等本体都等成化石了」，被评论区集体判定为 AI
（「你是 ai 吗」「现在 AI 模仿傻子这么像了吗」「本体又来本传的」）。

错因不是语气，是模型把「本体」当成了《空洞骑士》初代，而帖子里指的是
《丝之歌》自己。所以这里既要有知识，也要有「不知道就闭嘴」的闸门。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import series  # noqa: E402


class _Paths:
    data_dir = str(ROOT)


class _Ctx:
    paths = _Paths()


CTX = _Ctx()


class LookupTest(unittest.TestCase):
    def test_kb_loads(self) -> None:
        kb = series.load(CTX)
        self.assertGreaterEqual(len(kb), 20, "知识库条目太少，说明文件没读到")
        names = {s.name for s in kb}
        self.assertIn("空洞骑士", names)

    def test_finds_silksong_by_alias(self) -> None:
        found = series.lookup(CTX, "《丝之歌》实体版手册曝光：多个免费DLC正在路上")
        self.assertIsNotNone(found)
        _, work = found
        self.assertIn("丝之歌", work.name)

    def test_longer_name_wins(self) -> None:
        """"空洞骑士：丝之歌" 不能被 "空洞骑士" 截胡。"""
        found = series.lookup(CTX, "空洞骑士：丝之歌 免费DLC")
        self.assertIsNotNone(found)
        _, work = found
        self.assertEqual(work.relation, "续作（独立游戏，不是 DLC）")

    def test_unknown_game_returns_none(self) -> None:
        self.assertIsNone(series.lookup(CTX, "今天天气不错，出去吃了个饭"))


class HintTest(unittest.TestCase):
    def test_hint_explains_base_game(self) -> None:
        hint = series.build_hint(CTX, "《丝之歌》实体版手册曝光")
        self.assertIn("空洞骑士", hint)
        # 必须点明「本体指它自己」，这正是这次翻车的知识点。
        self.assertIn("本体", hint)
        self.assertIn("2025", hint)

    def test_hint_empty_for_unknown(self) -> None:
        self.assertEqual(series.build_hint(CTX, "随便一条和游戏无关的帖子"), "")


class GuardTest(unittest.TestCase):
    def test_blocks_risky_terms_when_unknown(self) -> None:
        """这次那两句，必须整条作废。"""
        for bad in (
            "不急，先把本体端上来再说吧",
            "等本体都等成化石了，现在居然连都有饼了",
            "前作我玩过，续作还没买",
        ):
            self.assertEqual(series.guard_relations(bad, known=False), "", bad)

    def test_passes_normal_comment(self) -> None:
        text = "这波更新挺良心，等一个实机"
        self.assertEqual(series.guard_relations(text, known=False), text)

    def test_passes_when_series_known(self) -> None:
        text = "丝之歌本体确实做完了，DLC 慢慢等"
        self.assertEqual(series.guard_relations(text, known=True), text)

    def test_empty_stays_empty(self) -> None:
        self.assertEqual(series.guard_relations("", known=False), "")


class IntegrationTest(unittest.TestCase):
    def test_comment_prompt_injects_series(self) -> None:
        src = (ROOT / "layers" / "create.py").read_text(encoding="utf-8")
        self.assertIn("series.build_hint", src, "评论提示词没接系列常识")
        self.assertIn("series.guard_relations", src, "生成后没兜底")
        # 认不到时必须明确禁止相对说法
        self.assertIn("绝对不要用「本体」", src)


if __name__ == "__main__":
    unittest.main()