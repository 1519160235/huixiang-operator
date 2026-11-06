# -*- coding: utf-8 -*-
"""
回响插件：第七阶段「外围联动」自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. 开关：默认全关；开启哪个才调哪个。
2. 短路：关掉的出口连 api.call 都不发起。
3. 降级：api.call 抛异常 / 宿主无调用通道时，只返回失败结果，绝不上抛。
4. 解耦：卸掉任一联动，发帖闭环仍然通（用假发布链路验证）。
5. 文案：build_broadcast_text 全中文、带帖子号。

【验收对齐（docs/开发路线.md 阶段 7）】
- 逐个卸掉联动，发帖闭环仍然通；
- 文案全中文；
- 页面分区下拉联动话题仍正常（WebUI 静态定义已含 options_source）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from layers import links  # noqa: E402


class _FakeApi:
    def __init__(self, raise_on: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.raise_on = raise_on

    async def call(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if self.raise_on and self.raise_on in name:
            raise RuntimeError("模拟联动失败")
        return {"ok": True}


def _ctx(links_cfg: dict | None = None, api: object | None = None) -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.config = {"links": links_cfg or {}}
    if api is not None:
        ctx.api = api
    return ctx


# ===================== 开关 =====================

class TestLinkSwitches(unittest.TestCase):
    def test_default_all_off(self) -> None:
        ctx = _ctx()
        self.assertEqual(links.enabled_links(ctx), [])
        for name in ("voice_broadcast", "qzone_sync", "diary_link"):
            self.assertFalse(links.link_enabled(ctx, name))

    def test_partial_on(self) -> None:
        ctx = _ctx({"voice_broadcast": True, "qzone_sync": False, "diary_link": True})
        self.assertEqual(links.enabled_links(ctx), ["voice_broadcast", "diary_link"])

    def test_no_config(self) -> None:
        ctx = SimpleNamespace()
        self.assertEqual(links.enabled_links(ctx), [])


# ===================== 短路 / 降级 =====================

class TestLinkDegrade(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_link_does_not_call_api(self) -> None:
        api = _FakeApi()
        ctx = _ctx({}, api=api)
        result = await links.broadcast_voice(ctx, "测试")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "disabled")
        self.assertEqual(api.calls, [])

    async def test_enabled_link_calls_api(self) -> None:
        api = _FakeApi()
        ctx = _ctx({"voice_broadcast": True}, api=api)
        result = await links.broadcast_voice(ctx, "测试")
        self.assertTrue(result["ok"])
        self.assertEqual(len(api.calls), 1)

    async def test_api_exception_is_swallowed(self) -> None:
        api = _FakeApi(raise_on="voice")
        ctx = _ctx({"voice_broadcast": True}, api=api)
        result = await links.broadcast_voice(ctx, "测试")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "link_failed")

    async def test_no_api_channel(self) -> None:
        ctx = _ctx({"voice_broadcast": True})
        result = await links.broadcast_voice(ctx, "测试")
        self.assertFalse(result["ok"])
        self.assertIn(result["code"], {"no_api_channel", "link_failed"})

    async def test_announce_all_enabled(self) -> None:
        api = _FakeApi()
        ctx = _ctx({"voice_broadcast": True, "qzone_sync": True, "diary_link": True}, api=api)
        result = await links.announce_published(ctx, "标题", "123")
        self.assertEqual(result["enabled"], ["voice_broadcast", "qzone_sync", "diary_link"])
        self.assertEqual(len(api.calls), 3)

    async def test_announce_none_enabled(self) -> None:
        api = _FakeApi()
        ctx = _ctx({}, api=api)
        result = await links.announce_published(ctx, "标题", "123")
        self.assertEqual(result["enabled"], [])
        self.assertEqual(api.calls, [])

    async def test_one_link_failure_does_not_block_others(self) -> None:
        api = _FakeApi(raise_on="voice")
        ctx = _ctx({"voice_broadcast": True, "qzone_sync": True}, api=api)
        result = await links.announce_published(ctx, "标题", "123")
        self.assertFalse(result["results"]["voice_broadcast"]["ok"])
        self.assertTrue(result["results"]["qzone_sync"]["ok"])


# ===================== 文案 =====================

class TestBroadcastText(unittest.TestCase):
    def test_text_contains_title_and_id(self) -> None:
        text = links.build_broadcast_text("今天随便聊聊", "999")
        self.assertIn("今天随便聊聊", text)
        self.assertIn("999", text)

    def test_text_without_id(self) -> None:
        text = links.build_broadcast_text("只有标题")
        self.assertEqual(text, "刚在小黑盒发了一条：只有标题")

    def test_text_with_extra(self) -> None:
        text = links.build_broadcast_text("标题", "1", "两段三段")
        self.assertIn("两段三段", text)


# ===================== 解耦：卸掉联动闭环仍通 =====================

class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, title: str) -> str:
        self.published.append(title)
        return "post-1"


class TestDecoupling(unittest.IsolatedAsyncioTestCase):
    async def test_publish_loop_survives_all_links_off(self) -> None:
        publisher = _FakePublisher()
        ctx = _ctx({})
        post_id = await publisher.publish("标题")
        announce = await links.announce_published(ctx, "标题", post_id)
        self.assertEqual(post_id, "post-1")
        self.assertEqual(announce["enabled"], [])
        self.assertEqual(publisher.published, ["标题"])

    async def test_publish_loop_survives_each_link_removed(self) -> None:
        combos = [
            {"voice_broadcast": True, "qzone_sync": True, "diary_link": True},
            {"qzone_sync": True, "diary_link": True},
            {"voice_broadcast": True, "diary_link": True},
            {"voice_broadcast": True, "qzone_sync": True},
            {},
        ]
        for cfg in combos:
            with self.subTest(cfg=cfg):
                publisher = _FakePublisher()
                api = _FakeApi(raise_on="diary")  # 故意让某个出口失败
                ctx = _ctx(cfg, api=api)
                post_id = await publisher.publish("标题")
                await links.announce_published(ctx, "标题", post_id)
                self.assertEqual(post_id, "post-1")


# ===================== WebUI 声明合法性 =====================

class TestWebuiDeclaration(unittest.TestCase):
    """webui.json 必须符合 MaiBot 的真实 WebUIExtension schema（schema_version=2）。

    历史教训（2026-11-04）：旧版格式（webui_version/title/sections）直接部署，
    导致插件注册失败、整个 WebUI 崩掉。现在用真实 schema 校验，不再靠猜。
    """

    def test_webui_declaration_valid(self) -> None:
        import json

        path = ROOT / "webui.json"
        if not path.is_file():
            # 合法状态：宿主 load_webui_extension() 在文件不存在时 return None，
            # 插件照常加载、只是不注册 WebUI 页面。2026-11-05 就是这么恢复服务的。
            self.skipTest("本版本未提供 webui.json（宿主允许无 WebUI 扩展）")

        data = json.loads(path.read_text(encoding="utf-8"))

        # 1) 顶层必须是 v2 结构，不能有旧字段
        self.assertEqual(data.get("schema_version"), 2, "webui.json 必须是 schema_version=2")
        legacy = {"webui_version", "title", "page", "sections", "skeleton_samples"} & set(data)
        self.assertEqual(legacy, set(), f"出现旧版字段：{sorted(legacy)}")

        # 2) pages 必填、非空
        pages = data.get("pages")
        self.assertIsInstance(pages, list)
        self.assertGreaterEqual(len(pages), 1)

        # 3) 有 workspace 页必须声明 workspace_title
        if any(p.get("placement") == "workspace" for p in pages):
            self.assertTrue(data.get("workspace_title"), "有 workspace 页面时必须写 workspace_title")

        # 4) custom 页必须用 entry 指向插件内 .html，不能内联 document
        for page in pages:
            if page.get("mode") != "custom":
                continue
            self.assertNotIn("document", page, "custom 页不能内联 document，要用 entry")
            entry = page.get("entry", "")
            self.assertTrue(entry.endswith(".html"), "entry 必须是 .html 相对路径")
            self.assertTrue((ROOT / entry).is_file(), f"entry 文件不存在：{entry}")
        # custom 页必须在 capabilities 里声明
        if any(p.get("mode") == "custom" for p in pages):
            self.assertIn("custom_pages", data.get("required_capabilities", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
