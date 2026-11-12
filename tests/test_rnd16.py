# -*- coding: utf-8 -*-
"""_rnd v16（AES-GCM）协议测试 + 账号级拒绝话术的熔断覆盖。

协议来源：2026-11-12 线上前端 index-C10NdQWW.js 的 aSe / Tr / PW。
"""
from __future__ import annotations

import base64
import json
import unittest

import comment_signing
from layers import cooldown

FIXED_IV = b"\x00" * 12
NONCE = "A" * 32


def _decode(blob: str) -> bytes:
    return base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))


class TestRnd16(unittest.TestCase):
    def test_prefix_and_base64url(self) -> None:
        q = comment_signing.comment_query(now=1700000000, nonce=NONCE, iv=FIXED_IV)
        self.assertTrue(q["_rnd"].startswith("16:"))
        raw = q["_rnd"][3:]
        self.assertNotIn("=", raw)
        self.assertNotIn("+", raw)
        self.assertNotIn("/", raw)
        self.assertGreaterEqual(len(_decode(raw)), 12 + 16 + 10)

    def test_decrypt_payload_matches_frontend_shape(self) -> None:
        """解密后必须是 ["<time>","<nonce>","<path>/"]，路径带尾斜杠。"""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        path = "/bbs/app/comment/create"
        q = comment_signing.comment_query(
            now=1700000000, nonce=NONCE, path=path, iv=FIXED_IV)
        blob = _decode(q["_rnd"][3:])
        key = base64.b64decode(comment_signing.RND16_KEY_B64)
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], comment_signing.RND16_AAD)
        self.assertEqual(json.loads(plain.decode()),
                         ["1700000000", NONCE, "/bbs/app/comment/create/"])

    def test_path_participates_in_payload(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        q = comment_signing.comment_query(
            now=1700000000, nonce=NONCE, path="/bbs/app/api/link/post", iv=FIXED_IV)
        blob = _decode(q["_rnd"][3:])
        key = base64.b64decode(comment_signing.RND16_KEY_B64)
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], comment_signing.RND16_AAD)
        self.assertEqual(json.loads(plain.decode())[2], "/bbs/app/api/link/post/")

    def test_fixed_iv_is_deterministic_and_random_iv_differs(self) -> None:
        a = comment_signing.comment_query(now=1700000000, nonce=NONCE, iv=FIXED_IV)
        b = comment_signing.comment_query(now=1700000000, nonce=NONCE, iv=FIXED_IV)
        self.assertEqual(a["_rnd"], b["_rnd"])
        c = comment_signing.comment_query(now=1700000000, nonce=NONCE)
        self.assertNotEqual(a["_rnd"], c["_rnd"], "生产环境必须用随机 IV")

    def test_v15_compat(self) -> None:
        q = comment_signing.comment_query(now=1700000000, nonce=NONCE, rnd_version=15)
        self.assertTrue(q["_rnd"].startswith("15:"))
        self.assertEqual(len(q["_rnd"]), 3 + 64)

    def test_invalid_version_and_iv(self) -> None:
        with self.assertRaises(ValueError):
            comment_signing.comment_query(rnd_version=17)
        with self.assertRaises(ValueError):
            comment_signing.comment_query(iv=b"short")

    def test_heybox_id_in_query(self) -> None:
        q = comment_signing.comment_query(heybox_id="34182985")
        self.assertEqual(q["heybox_id"], "34182985")
        self.assertNotIn("heybox_id", comment_signing.comment_query())

    def test_hkey_still_uses_stamp_plus_one(self) -> None:
        from vendor.request_signing import generate_hkey

        q = comment_signing.comment_query(now=1700000000, nonce=NONCE, iv=FIXED_IV)
        self.assertEqual(q["hkey"], generate_hkey(
            comment_signing.COMMENT_PATH, 1700000001, NONCE))


class TestAccountBlockMarker(unittest.TestCase):
    def test_new_platform_message_triggers_block(self) -> None:
        msg = "你的账号无法使用该功能，如有疑问请前往设置-帮助中心&问题反馈进行反馈"
        self.assertEqual(cooldown.classify(msg), "platform")


if __name__ == "__main__":
    unittest.main()
