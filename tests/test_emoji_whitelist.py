# -*- coding: utf-8 -*-
"""回响插件：小黑盒表情白名单单测（unittest，纯本地、不联网）。

【覆盖点】
1. 白名单内的表情原样保留（含 `cube_上学-乐` 这种带短横线的官方活动表情）；
2. 白名单外的表情（模型编造的名字）一律删除 —— 客户端查不到就不渲染，留着只会露白；
3. 一条评论最多保留 max_marks 个表情；
4. 全角括号 / 圆括号写法归一化；
5. 缺括号的半残标记清理干净，不留 `[cube_` 残渣；
6. `sanitize_comment` 端到端链路里表情不被英文清洗破坏。

【背景】真机 2026-11-08 用户报障「刷小黑盒回复有时候表情加载不出来」：
小黑盒评论接口不返回 emojis 字段，服务端把 `[cube_xxx]` 当纯文本存，
渲染完全靠客户端内置表情表按名字查；名字编错就画不出来。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import create as create_mod  # noqa: E402
from layers import emojis as emoji_mod  # noqa: E402


class EmojiWhitelistTest(unittest.TestCase):
    def test_whitelist_all_prefixed_and_nonempty(self) -> None:
        self.assertGreater(len(emoji_mod.ALLOWED), 30)
        for name in emoji_mod.ALLOWED:
            self.assertTrue(name.startswith("cube_"), name)
            self.assertNotIn(" ", name)

    def test_allowed_mark_kept(self) -> None:
        self.assertEqual(emoji_mod.sanitize_marks("可以的[cube_喜欢]"), "可以的[cube_喜欢]")
        self.assertEqual(emoji_mod.sanitize_marks("[cube_捂脸哭]"), "[cube_捂脸哭]")

    def test_hyphen_mark_kept(self) -> None:
        self.assertEqual(emoji_mod.sanitize_marks("上学[cube_上学-乐]"), "上学[cube_上学-乐]")

    def test_invented_mark_removed(self) -> None:
        for raw in ("笑死[cube_无语]", "离谱[cube_吃瓜看戏]", "是啊[cube_狗头]"):
            self.assertNotIn("cube_", emoji_mod.sanitize_marks(raw))

    def test_max_marks(self) -> None:
        out = emoji_mod.sanitize_marks("[cube_惊讶][cube_滑稽][cube_喜欢]")
        self.assertEqual(out, "[cube_惊讶]")

    def test_fullwidth_brackets_normalized(self) -> None:
        self.assertEqual(emoji_mod.sanitize_marks("离谱【cube_捂脸哭】"), "离谱[cube_捂脸哭]")
        self.assertEqual(emoji_mod.sanitize_marks("离谱（cube_滑稽）"), "离谱[cube_滑稽]")

    def test_broken_mark_removed(self) -> None:
        # 白名单外 / 只有半个括号：当残渣删干净，不留 `[cube_` 之类。
        self.assertEqual(emoji_mod.sanitize_marks("残渣[cube_喜"), "残渣")
        self.assertEqual(emoji_mod.sanitize_marks("裸奔 cube_不存在的 结束").strip(), "裸奔结束")

    def test_bare_mark_repaired(self) -> None:
        """缺括号但名字在白名单里：补括号保住表情，别把半句话一起吃掉。

        【2026-11-09 用户报障「有几个评论还是带了 cube 标语」】
        原来这里一律按残渣删，模型写 `累cube_无语` 时评论只剩半句；
        极端情况整条变空，用户看到的就是「正文没生成」。
        """
        self.assertEqual(
            emoji_mod.sanitize_marks("裸奔 cube_doge 结束").strip(), "裸奔 [cube_doge] 结束"
        )
        # 句尾是最容易踩「空串是任何串子串」的位置，单独盯一下。
        self.assertEqual(emoji_mod.sanitize_marks("今天真累cube_doge"), "今天真累[cube_doge]")

    def test_plain_text_untouched(self) -> None:
        text = "这波操作真可以，我也想要一个"
        self.assertEqual(emoji_mod.sanitize_marks(text), text)

    def test_strip_english_noise_keeps_hyphen_mark(self) -> None:
        """英文清洗不能把 `cube_` 当成英文单词删掉（2026-11-07 踩过的坑）。"""
        out = create_mod._strip_english_noise("上学[cube_上学-乐]")
        self.assertIn("[cube_上学-乐]", out)

    def test_sanitize_comment_end_to_end(self) -> None:
        raw = "The post is funny. 这波确实好笑[cube_无语]"
        out = create_mod.sanitize_comment(raw)
        self.assertNotIn("cube_", out)
        self.assertIn("好笑", out)

    def test_sanitize_comment_keeps_valid_mark(self) -> None:
        out = create_mod.sanitize_comment("这波确实好笑[cube_笑cry]")
        self.assertIn("[cube_笑cry]", out)

    def test_extract_marks(self) -> None:
        self.assertEqual(
            emoji_mod.extract_marks("a[cube_喜欢]b[cube_无语]"),
            ["cube_喜欢", "cube_无语"],
        )


if __name__ == "__main__":
    unittest.main()
