# -*- coding: utf-8 -*-
"""App 端真实写协议：签名保真、表单转换、熔断话术。"""
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_protocol  # noqa: E402
from layers import cooldown  # noqa: E402

# 线上环境可能真的放着 app_pairs.json（农场注入），测试必须与宿主状态隔离，
# 否则「内置签名」相关断言会被外部文件污染。
_ORIG_ENV = None


def setUpModule():
    global _ORIG_ENV
    import os as _os
    import tempfile as _tempfile
    _ORIG_ENV = _os.environ.get("HXH_APP_PAIRS")
    _os.environ["HXH_APP_PAIRS"] = _os.path.join(_tempfile.gettempdir(),
                                                 "hx_no_such_pairs.json")
    app_protocol._EFFECTIVE.clear()
    app_protocol._PAIR_COOKIES.clear()
    app_protocol._LOADED_SIGNATURE = (0.0, 0)


def tearDownModule():
    import os as _os
    if _ORIG_ENV is None:
        _os.environ.pop("HXH_APP_PAIRS", None)
    else:
        _os.environ["HXH_APP_PAIRS"] = _ORIG_ENV
    app_protocol._EFFECTIVE.clear()
    app_protocol._PAIR_COOKIES.clear()
    app_protocol._LOADED_SIGNATURE = (0.0, 0)


class TestAppSignature(unittest.TestCase):
    def test_only_write_paths_are_app(self):
        self.assertTrue(app_protocol.is_app_write("/bbs/app/comment/create"))
        self.assertTrue(app_protocol.is_app_write("/bbs/app/api/link/post"))
        self.assertFalse(app_protocol.is_app_write("/bbs/app/link/delete"))
        self.assertFalse(app_protocol.is_app_write("/bbs/app/api/qcloud/cos/upload/"))

    def test_url_keeps_captured_signature(self):
        url = app_protocol.app_url(app_protocol.COMMENT_PATH, nonce="FIXEDNONCE")
        p = urlsplit(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        self.assertEqual(p.scheme + "://" + p.netloc, app_protocol.APP_ORIGIN)
        self.assertEqual(p.path, app_protocol.COMMENT_PATH)
        self.assertEqual(q["hkey"], "E2AF3D9")
        self.assertEqual(q["_time"], "1794476477")
        self.assertEqual(q["heybox_id"], "34182985")
        self.assertEqual(q["x_app"], "heybox")
        self.assertEqual(q["version"], "1.3.401")
        self.assertEqual(q["nonce"], "FIXEDNONCE")
        self.assertEqual(q["time_zone"], "Asia/Shanghai")

    def test_post_signature_differs_from_comment(self):
        q = dict(parse_qsl(urlsplit(app_protocol.app_url(app_protocol.POST_PATH,
                                                        nonce="N")).query,
                           keep_blank_values=True))
        self.assertEqual(q["hkey"], "1AA09A3D")
        self.assertEqual(q["_time"], "1794476918")

    def test_nonce_is_random_mixed_alnum(self):
        a = app_protocol.app_url(app_protocol.COMMENT_PATH)
        b = app_protocol.app_url(app_protocol.COMMENT_PATH)
        self.assertNotEqual(a, b, "nonce 必须每次随机，避免被当成重放")
        q = dict(parse_qsl(urlsplit(a).query))
        self.assertEqual(len(q["nonce"]), 32)
        self.assertTrue(all(c.isalnum() and c.isascii() for c in q["nonce"]))

    def test_signature_meta(self):
        meta = app_protocol.signature_meta(app_protocol.COMMENT_PATH)
        self.assertEqual(meta["hkey"], "E2AF3D9")
        self.assertEqual(meta["imei"], "a5b8bce0553f20c8")

    def test_unknown_path_raises(self):
        with self.assertRaises(ValueError):
            app_protocol.app_url("/bbs/app/link/delete")


class TestAppForm(unittest.TestCase):
    def test_comment_form_drops_web_only_fields(self):
        form = {"is_cy": "0", "link_id": "123", "reply_id": "-1",
                "root_id": "-1", "text": "你好", "__extra": "x"}
        out = app_protocol.convert_form(app_protocol.COMMENT_PATH, form)
        self.assertEqual(out, {"link_id": "123", "text": "你好",
                               "imgs": "", "is_cy": "0"})

    def test_reply_form_keeps_ids(self):
        form = {"link_id": "123", "text": "回复", "reply_id": "998",
                "root_id": "997", "is_cy": "0"}
        out = app_protocol.convert_form(app_protocol.COMMENT_PATH, form)
        self.assertEqual(out["reply_id"], "998")
        self.assertEqual(out["root_id"], "997")

    def test_post_form_is_app_shape(self):
        form = {
            "link_tag": "27", "text": '[{"type":"text","text":"正文"}]',
            "title": "标题", "desc": "摘要", "words_count": "2",
            "topic_ids": "58144", "hashtags": "[]", "schedule_ts": "0",
            "view_limit": "1", "post_type": "1", "original": "1",
            "declaration": "0",
        }
        out = app_protocol.convert_form(app_protocol.POST_PATH, form)
        self.assertEqual(set(out), {"draft", "topic_ids", "link_tag",
                                    "text", "title", "desc"})
        self.assertEqual(out["draft"], "0")
        self.assertEqual(out["topic_ids"], "58144")
        self.assertEqual(out["link_tag"], "27")
        self.assertEqual(out["text"], form["text"])

    def test_headers(self):
        h = app_protocol.app_headers()
        self.assertIn("ApiMaxJia/1.0", h["User-Agent"])
        self.assertEqual(h["Referer"], "http://api.maxjia.com/")
        self.assertEqual(h["Content-Type"], "application/x-www-form-urlencoded")


class TestAppCooldown(unittest.TestCase):
    def test_signature_failure_messages_are_blocked(self):
        for msg in ("验证参数错误", "非法请求", "请升级至最新版本"):
            self.assertEqual(cooldown.classify(msg), "platform", msg)

    def test_expired_signature_enters_platform_cooldown(self):
        for msg in ("请求失败了", "评论被平台限制，App 兜底签名已过期（900 秒前抓的包）"):
            self.assertEqual(cooldown.classify(msg), "platform", msg)

    def test_rate_limit_still_short_cooldown(self):
        self.assertEqual(cooldown.classify("发帖频率过快，请稍后再试:10006#02"),
                         "rate_limit")


class TestSignatureFreshness(unittest.TestCase):
    """签名对只有 5 分钟寿命，守卫必须按实测窗口判断。"""

    T0 = 1794476477  # COMMENT_PATH 的 _time

    def test_pair_age(self):
        self.assertEqual(app_protocol.pair_age(app_protocol.COMMENT_PATH,
                                               now=self.T0 + 88), 88)
        self.assertIsNone(app_protocol.pair_age("/bbs/app/link/delete"))

    def test_fresh_within_window(self):
        for age in (0, 88, 148, app_protocol.PAIR_SAFE_AGE_SECONDS):
            self.assertTrue(
                app_protocol.is_fresh(app_protocol.COMMENT_PATH, now=self.T0 + age),
                "age=%s 应仍在窗口内" % age)

    def test_stale_after_ttl(self):
        # 实测 329 秒稳定失败；安全线 240 秒之后不再使用
        self.assertFalse(app_protocol.is_fresh(app_protocol.COMMENT_PATH,
                                               now=self.T0 + 329))
        self.assertTrue(app_protocol.PAIR_TTL_SECONDS < 360)

    def test_block_markers(self):
        for msg in ("你的账号无法使用该功能", "发布功能暂不可用", "禁止评论"):
            self.assertTrue(app_protocol.looks_blocked(msg), msg)
        for msg in ("仅用标点符号无法发布哦，请记得搭配文字内容",
                    "发帖频率过快，请稍后再试:10006#02", "error link"):
            self.assertFalse(app_protocol.looks_blocked(msg), msg)


class TestExternalPairs(unittest.TestCase):
    """签名农场 / 手工注入：外部文件里的新鲜签名对必须能覆盖内置抓包值。"""

    def setUp(self):
        import json as _json
        import os as _os
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._path = _os.path.join(self._tmp, "app_pairs.json")
        self._env = _os.environ.get("HXH_APP_PAIRS")
        _os.environ["HXH_APP_PAIRS"] = self._path
        with open(self._path, "w", encoding="utf-8") as fh:
            _json.dump({"comment": {"hkey": "D4C29517", "_time": 1794478228},
                        "post": {"hkey": "1AA09A3D", "_time": 1794476918}}, fh)
        self._json = _json
        self._os = _os
        app_protocol.refresh_pairs(force=True)

    def tearDown(self):
        import shutil
        if self._env is None:
            self._os.environ.pop("HXH_APP_PAIRS", None)
        else:
            self._os.environ["HXH_APP_PAIRS"] = self._env
        app_protocol._EFFECTIVE.clear()
        app_protocol._PAIR_COOKIES.clear()
        app_protocol._LOADED_SIGNATURE = (0.0, 0)
        app_protocol.refresh_pairs(force=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_injected_pair_overrides_builtin(self):
        q = dict(parse_qsl(urlsplit(app_protocol.app_url(
            app_protocol.COMMENT_PATH, nonce="N")).query, keep_blank_values=True))
        self.assertEqual(q["hkey"], "D4C29517")
        self.assertEqual(q["_time"], "1794478228")
        # 其余参数必须原样保留
        self.assertEqual(q["heybox_id"], "34182985")
        self.assertEqual(q["version"], "1.3.401")
        self.assertEqual(app_protocol.pair_age(app_protocol.COMMENT_PATH,
                                               now=1794478235), 7)

    def test_invalid_pair_ignored(self):
        with open(self._path, "w", encoding="utf-8") as fh:
            self._json.dump({"comment": {"hkey": "bad-key!", "_time": "abc"}}, fh)
        app_protocol.refresh_pairs(force=True)
        q = dict(parse_qsl(urlsplit(app_protocol.app_url(
            app_protocol.COMMENT_PATH, nonce="N")).query, keep_blank_values=True))
        self.assertEqual(q["hkey"], "D4C29517", "非法值不应覆盖已生效的签名")

    def test_missing_file_keeps_builtin(self):
        self._os.environ["HXH_APP_PAIRS"] = self._path + ".none"
        app_protocol.refresh_pairs(force=True)
        q = dict(parse_qsl(urlsplit(app_protocol.app_url(
            app_protocol.COMMENT_PATH, nonce="N")).query, keep_blank_values=True))
        self.assertEqual(q["hkey"], "D4C29517")


    def test_cookie_from_pair_is_sent(self):
        """App 写接口必须带 Cookie，否则回「请重新登录」。"""
        import json as _json
        payload = {"comment": {"query": "h_src=X&heybox_id=34182985&nonce=N"
                                        "&hkey=550D84B&_time=1794485519",
                               "cookie": "pkey=AAA;x_xhh_tokenid=BBB"}}
        with open(self._path, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh)
        app_protocol.refresh_pairs(force=True)
        h = app_protocol.app_headers(app_protocol.COMMENT_PATH)
        self.assertEqual(h["Cookie"], "pkey=AAA;x_xhh_tokenid=BBB")
        self.assertNotIn("Cookie", app_protocol.app_headers())

    def test_full_query_pair_is_used_verbatim(self):
        """农场给的是整条 query：h_src / index 必须原样带过去。"""
        import json as _json
        payload = {"comment": {"query": "h_src=FARMED&index=2&heybox_id=34182985"
                                          "&nonce=ABC&hkey=550D84B&_time=1794485519"
                                          "&version=1.3.401&build=1151"},
                   "post": {"query": "h_src=&heybox_id=34182985&nonce=X"
                                     "&hkey=1AA09A3D&_time=1794476918"}}
        with open(self._path, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh)
        app_protocol.refresh_pairs(force=True)
        q = dict(parse_qsl(urlsplit(app_protocol.app_url(
            app_protocol.COMMENT_PATH, nonce="N")).query, keep_blank_values=True))
        self.assertEqual(q["h_src"], "FARMED")
        self.assertEqual(q["index"], "2")
        self.assertEqual(q["hkey"], "550D84B")
        self.assertEqual(q["_time"], "1794485519")


if __name__ == "__main__":
    unittest.main()
