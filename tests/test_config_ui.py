# -*- coding: utf-8 -*-
"""配置页 UI 单测：作废字段不该出现在界面上。

【背景】用户 2026-11-08 报障：「你说看下面发帖窗口，我又没看见发帖窗口在哪。」
根因是「什么时候触发」这个作废字段的说明写着「已弃用，现在看下面「发帖窗口」」，
而「发帖窗口」其实在另一个分组（发布设置）里。用户照着提示找，当然找不到。

修法：作废字段统一以「［已作废］」开头，渲染前直接从 schema 里剔除。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _strip(sections):
    """只取出剔除逻辑本身，避免 import plugin（依赖宿主 SDK）。"""
    source = (ROOT / "plugin.py").read_text(encoding="utf-8")
    start = source.index('DEPRECATED_LABEL_PREFIX = "［已作废］"')
    end = source.index("\n\n\n", source.index("def strip_deprecated_fields"))
    ns: dict = {}
    exec("from typing import Any\n" + source[start:end], ns)  # noqa: S102 —— 只为拿纯函数。
    return ns["strip_deprecated_fields"](sections)


class StripDeprecatedTest(unittest.TestCase):
    def test_removes_deprecated(self) -> None:
        sections = {
            "summarize": {
                "fields": {
                    "enabled": {"label": "允许发帖"},
                    "trigger_by": {"label": "［已作废］触发方式"},
                    "count_threshold": {"label": "［已作废］攒够多少条"},
                }
            }
        }
        removed = _strip(sections)
        self.assertEqual(removed, 2)
        self.assertEqual(list(sections["summarize"]["fields"]), ["enabled"])

    def test_keeps_normal_fields(self) -> None:
        sections = {"publish": {"fields": {"post_slots": {"label": "每天发帖时段"}}}}
        self.assertEqual(_strip(sections), 0)
        self.assertIn("post_slots", sections["publish"]["fields"])

    def test_handles_bad_shape(self) -> None:
        self.assertEqual(_strip(None), 0)
        self.assertEqual(_strip({}), 0)
        self.assertEqual(_strip({"x": {"fields": None}}), 0)
        self.assertEqual(_strip({"x": "不是字典"}), 0)


class ConfigLabelsTest(unittest.TestCase):
    """作废字段必须带前缀，否则剔除逻辑认不出来。"""

    def test_deprecated_fields_marked(self) -> None:
        src = (ROOT / "config_model.py").read_text(encoding="utf-8")
        for field in ("trigger_by", "count_threshold", "time_hours"):
            idx = src.index(f"{field}:")
            chunk = src[idx : idx + 260]
            self.assertIn(
                "［已作废］", chunk, f"{field} 应该在标签里标「［已作废］」，否则界面上不会隐藏"
            )

    def test_legacy_ui_changed(self) -> None:
        # 分组名带上序号，用户能看出「发帖」是三页里的第几页。
        src = (ROOT / "config_model.py").read_text(encoding="utf-8")
        self.assertIn("发帖①", src)
        self.assertIn("发帖②", src)
        self.assertIn("发帖③", src)
        self.assertNotIn('__ui_label__ = "发布设置"', src)
        self.assertNotIn('__ui_label__ = "配图设置"', src)

    def test_no_dangling_pointer(self) -> None:
        # 不许再写「看下面『某个字段』」这种跨分组指路的话：
        # 用户照着找，那字段其实在别的分组里。
        src = (ROOT / "config_model.py").read_text(encoding="utf-8")
        self.assertNotIn("看下面「", src)
        self.assertNotIn("如下设置", src)


if __name__ == "__main__":
    unittest.main()
