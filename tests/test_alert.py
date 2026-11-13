# -*- coding: utf-8 -*-
"""告警层：去重、落盘、推送与失败重试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layers import alert, cooldown  # noqa: E402


class FakeApi:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def call(self, name, **kwargs):
        if self.fail:
            raise RuntimeError("宿主接口炸了")
        self.calls.append((name, kwargs))
        return {"ok": True}


def make_ctx(tmp: str, api_name: str = "", api=None):
    cfg = {"links": {"alert_api": api_name}}
    return SimpleNamespace(paths=SimpleNamespace(data_dir=tmp), config=cfg,
                           api=api if api is not None else FakeApi())


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx = make_ctx(self.tmp)

    def test_dedupe_within_window(self):
        first = alert.record(self.ctx, "platform", "你的账号无法使用该功能", now=1000)
        second = alert.record(self.ctx, "platform", "你的账号无法使用该功能", now=1100)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "30 分钟内同类事件应去重")

    def test_different_kinds_both_recorded(self):
        self.assertIsNotNone(alert.record(self.ctx, "platform", "A", now=1000))
        self.assertIsNotNone(alert.record(self.ctx, "relogin", "B", now=1000))

    def test_jsonl_and_pending(self):
        alert.record(self.ctx, "signature", "App 兜底签名已过期", now=1000)
        lines = Path(self.tmp, alert.LOG_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("签名", lines[0])
        state = json.loads(Path(self.tmp, alert.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(state["pending"]), 1)

    def test_dedupe_expires(self):
        alert.record(self.ctx, "platform", "A", now=1000)
        later = alert.record(self.ctx, "platform", "A", now=1000 + alert.DEDUPE_SEC + 1)
        self.assertIsNotNone(later, "超过去重窗口应再次告警")


class TestFlush(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_no_host_channel_keeps_pending(self):
        ctx = make_ctx(self.tmp, api_name="", api=None)  # 宿主没有 ctx.api
        ctx.api = None
        alert.record(ctx, "platform", "A", now=1000)
        self.assertEqual(await alert.flush(ctx), 0)
        self.assertEqual(alert.pending_count(ctx), 1, "没有通道时保留队列，等宿主恢复")
        self.assertTrue(Path(self.tmp, alert.LOG_FILE).exists())

    async def test_push_success(self):
        api = FakeApi()
        ctx = make_ctx(self.tmp, api_name="notify.send", api=api)
        alert.record(ctx, "relogin", "小黑盒登录态失效", now=1000)
        self.assertEqual(await alert.flush(ctx), 1)
        self.assertEqual(api.calls[0][0], "notify.send")
        payload = api.calls[0][1]
        text = payload.get("text") or payload["params"]["message"][0]["data"]["text"]
        self.assertIn("重新扫码", text)
        self.assertEqual(alert.pending_count(ctx), 0)

    async def test_push_uses_owner_qq_and_text_shape(self):
        api = FakeApi()
        ctx = make_ctx(self.tmp, api_name="adapter.napcat.message.send_private_msg", api=api)
        ctx.config["links"]["alert_target"] = "1519160235"
        alert.record(ctx, "platform", "账号被限制", now=1000)
        await alert.flush(ctx)
        params = api.calls[0][1]["params"]
        self.assertEqual(params["user_id"], 1519160235)
        self.assertEqual(params["message"][0]["type"], "text")

    async def test_push_failure_retries_then_drops(self):
        api = FakeApi(fail=True)
        ctx = make_ctx(self.tmp, api_name="notify.send", api=api)
        alert.record(ctx, "platform", "A", now=1000)
        for _ in range(alert.MAX_RETRY):
            await alert.flush(ctx)
        self.assertEqual(alert.pending_count(ctx), 0, "超过重试次数应丢弃，别无限堆")

    async def test_push_failure_keeps_one_retry(self):
        api = FakeApi(fail=True)
        ctx = make_ctx(self.tmp, api_name="notify.send", api=api)
        alert.record(ctx, "platform", "A", now=1000)
        self.assertEqual(await alert.flush(ctx), 0)
        self.assertEqual(alert.pending_count(ctx), 1)


class TestAlertTarget(unittest.TestCase):
    def test_owner_qq_from_permission_list(self):
        import os
        import tempfile
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "bot_config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('# 示例 qq:123456 不该被当成主人\n[permission]\npermission = ["qq:1519160235"]\n')
        old = alert.BOT_CONFIG
        alert.BOT_CONFIG = path
        alert._OWNER_CACHE.clear()
        try:
            ctx = make_ctx(tmp)
            self.assertEqual(alert._alert_target(ctx), "1519160235")
        finally:
            alert.BOT_CONFIG = old
            alert._OWNER_CACHE.clear()

    def test_configured_target_wins(self):
        ctx = make_ctx(tempfile.mkdtemp())
        ctx.config["links"]["alert_target"] = "10001"
        self.assertEqual(alert._alert_target(ctx), "10001")


class TestServerChan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posted = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_post(self, code=0):
        def fake(url, data, timeout=15.0):
            self.posted.append((url, data))
            return {"code": code, "message": "ok" if code == 0 else "bad"}
        alert._post_form = fake

    async def test_pushes_via_serverchan(self):
        ctx = make_ctx(self.tmp)
        ctx.config["links"]["serverchan_key"] = "SCTTESTKEY"
        self._patch_post(0)
        alert.record(ctx, "platform", "账号被限制", now=1000)
        self.assertEqual(await alert.flush(ctx), 1)
        url, data = self.posted[0]
        self.assertIn("SCTTESTKEY.send", url)
        self.assertIn("账号被限制", data["desp"])
        self.assertEqual(alert.pending_count(ctx), 0)

    async def test_failure_keeps_pending(self):
        ctx = make_ctx(self.tmp)
        ctx.config["links"]["serverchan_key"] = "SCTTESTKEY"
        self._patch_post(-1)
        alert.record(ctx, "relogin", "要扫码", now=1000)
        self.assertEqual(await alert.flush(ctx), 0)
        self.assertEqual(alert.pending_count(ctx), 1)

    async def test_key_file_in_data_dir(self):
        import os
        ctx = make_ctx(self.tmp)
        with open(os.path.join(self.tmp, alert.KEY_FILE), "w", encoding="utf-8") as fh:
            fh.write("SCTFILEKEY\n")
        self._patch_post(0)
        alert.record(ctx, "platform", "限制", now=1000)
        await alert.flush(ctx)
        self.assertIn("SCTFILEKEY.send", self.posted[0][0])

    async def test_env_key_wins(self):
        import os
        ctx = make_ctx(self.tmp)
        os.environ[alert.SC_KEY_ENV] = "SCTENV"
        try:
            self._patch_post(0)
            alert.record(ctx, "signature", "签名过期", now=1000)
            await alert.flush(ctx)
            self.assertIn("SCTENV.send", self.posted[0][0])
        finally:
            os.environ.pop(alert.SC_KEY_ENV, None)


class TestCooldownHook(unittest.TestCase):
    def test_platform_rejection_records_alert(self):
        tmp = tempfile.mkdtemp()
        ctx = make_ctx(tmp)
        cooldown.record_rejection(ctx, "failed: 你的账号无法使用该功能", now=1000)
        alerts = Path(tmp, alert.LOG_FILE).read_text(encoding="utf-8")
        self.assertIn("账号", alerts)

    def test_signature_message_marked_as_signature(self):
        tmp = tempfile.mkdtemp()
        ctx = make_ctx(tmp)
        cooldown.record_rejection(
            ctx, "评论被平台限制，且没有可用的 App 兜底签名（现有签名 900 秒前抓的）", now=1000)
        state = json.loads(Path(tmp, alert.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["pending"][0]["kind"], "signature")


if __name__ == "__main__":
    unittest.main()
