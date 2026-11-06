# -*- coding: utf-8 -*-
"""
回响插件：第二阶段签名自查单测（unittest，纯本地、不联网、不连真实账号）。

【覆盖点】
1. vendor 直连客户端各模块可 import；
2. 读签名：RequestSigner.sign 的 hkey 等于 generate_hkey(path, stamp, nonce)；
3. 写签名：comment_query 的 hkey 用 stamp+1 计算，而 _time 保留原始时间；
4. generate_hkey 确定性（相同输入相同输出）与输出格式；
5. 四字段凭据校验：齐全通过、缺一报缺失；
6. 凭据落盘 / 读取 round-trip，缺失字段拒绝写盘；
7. 日志脱敏：redact_log_text / redact_data 不泄露 Cookie / token / pkey 明文。

【验收对齐（docs/开发路线.md 阶段 2）】
- 用合成凭据跑签名自查，不连真实账号（本测试不发起任何网络请求）。
- 四字段齐全才继续（missing_credential_fields 为空）。
- 读走 generate_hkey(path, stamp, nonce)、写走 comment_query 且 hkey 用 stamp+1。
- 日志里没有凭据明文（redact 覆盖）。

【运行】
    python tests/test_stage2_signing.py
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 插件目录名含连字符，不作为包导入；把目录加入 sys.path，各模块以顶层模块名互引。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_credentials(**overrides: object) -> object:
    """构造一个四字段齐全的合成凭据（vendor.models.Credentials）。"""
    from vendor.models import Credentials

    defaults: dict[str, object] = {
        "profile_id": "default",
        "uid": "123456",
        "nickname": "合成用户",
        "cookies": {
            "pkey": "synthetic-pkey",
            "heybox_id": "123456",
            "x_xhh_tokenid": "synthetic-tokenid",
        },
    }
    defaults.update(overrides)
    return Credentials(**defaults)


class TestVendorImport(unittest.TestCase):
    """vendor 直连客户端各模块可 import。"""

    def test_vendor_modules_importable(self) -> None:
        import importlib

        # 纯标准库模块必测；api_client 依赖 httpx（运行时依赖），单独测试并允许跳过。
        for name in (
            "vendor.request_signing",
            "vendor.security",
            "vendor.endpoints",
            "vendor.parsers",
            "vendor.rate_limiter",
            "vendor.models",
        ):
            importlib.import_module(name)

    def test_vendor_api_client_importable(self) -> None:
        import importlib

        try:
            importlib.import_module("vendor.api_client")
        except ImportError as exc:
            self.skipTest(f"httpx 未安装，跳过 api_client 导入：{exc}")


class TestReadSigning(unittest.TestCase):
    """读请求签名：generate_hkey(path, stamp, nonce)。"""

    def test_generate_hkey_deterministic(self) -> None:
        from vendor.request_signing import generate_hkey

        a = generate_hkey("/bbs/app/feeds", 1700000000, "NONCE123")
        b = generate_hkey("/bbs/app/feeds", 1700000000, "NONCE123")
        self.assertEqual(a, b, "相同输入必须得到相同 hkey")

    def test_generate_hkey_format(self) -> None:
        from vendor.request_signing import generate_hkey

        value = generate_hkey("/bbs/app/feeds", 1700000000, "NONCE123")
        self.assertIsInstance(value, str)
        self.assertEqual(len(value), 7, "hkey 应为 5 位前缀 + 2 位后缀，共 7 字符")
        self.assertTrue(value.isalnum(), "hkey 应仅含字母与数字")

    def test_request_signer_hkey_uses_generate_hkey(self) -> None:
        from vendor.request_signing import RequestSigner, generate_hkey

        signer = RequestSigner()
        signed = signer.sign(
            "GET",
            "/bbs/app/feeds",
            params={"app": "web"},
            json_body=None,
            signing_key="",
            device_id="dev123",
            now=1700000000,
            nonce="FIXEDNONCE",
        )
        expected = generate_hkey("/bbs/app/feeds", 1700000000, "FIXEDNONCE")
        self.assertEqual(signed.params["hkey"], expected, "读签名 hkey 必须等于 generate_hkey(path, stamp, nonce)")
        self.assertEqual(signed.params["_time"], "1700000000")
        self.assertEqual(signed.params["nonce"], "FIXEDNONCE")
        self.assertEqual(signed.params["device_id"], "dev123")

    def test_hkey_differs_by_path(self) -> None:
        from vendor.request_signing import generate_hkey

        # 用前缀差异足够大的路径，验证 path 确实参与 hkey 计算。
        # 说明：hkey 算法对 path 有截断（交错后只取前 20 字符），共享前 7 字符前缀的路径
        # 会得到相同 hkey，故此处用 "/bbs/app/..." 与 "/account/..." 保证前缀不同。
        a = generate_hkey("/bbs/app/feeds", 1700000000, "NONCE123")
        b = generate_hkey("/account/qr_state/", 1700000000, "NONCE123")
        self.assertNotEqual(a, b, "path 应参与 hkey 计算")


class TestWriteSigning(unittest.TestCase):
    """写请求签名：comment_query，hkey 用 stamp+1。"""

    def test_comment_query_hkey_uses_stamp_plus_one(self) -> None:
        import comment_signing
        from vendor.request_signing import generate_hkey

        result = comment_signing.comment_query(now=1700000000, nonce="FIXEDNONCE")
        expected = generate_hkey("/bbs/app/comment/create", 1700000001, "FIXEDNONCE")
        self.assertEqual(result["hkey"], expected, "写签名 hkey 必须用 stamp+1 计算")
        self.assertEqual(result["_time"], "1700000000", "_time 必须保留原始时间，不能用 stamp+1")
        self.assertIn("_rnd", result)
        self.assertTrue(result["_rnd"].startswith("15:"))

    def test_comment_query_deterministic(self) -> None:
        import comment_signing

        a = comment_signing.comment_query(now=1700000000, nonce="FIXEDNONCE")
        b = comment_signing.comment_query(now=1700000000, nonce="FIXEDNONCE")
        self.assertEqual(a, b, "相同 now/nonce 必须得到相同写签名")

    def test_read_write_signing_differ(self) -> None:
        import comment_signing
        from vendor.request_signing import generate_hkey

        # 用会进位的 timestamp：stamp=1699999999 → stamp+1=1700000000，前 7 位变化，
        # 使读（stamp）/ 写（stamp+1）两套 hkey 确实不同，验证「写用 stamp+1 而非 stamp」。
        # 说明：普通 timestamp 的 +1 只改末位，末位被 hkey 截断，读 / 写结果会相同，
        # 故用进位 timestamp 让差异显性化。
        read_hkey = generate_hkey("/bbs/app/comment/create", 1699999999, "FIXEDNONCE")
        write = comment_signing.comment_query(now=1699999999, nonce="FIXEDNONCE")
        self.assertNotEqual(write["hkey"], read_hkey, "写签名必须用 stamp+1（进位时与读签名不同）")
        self.assertEqual(write["_time"], "1699999999", "_time 保留原始 stamp，不用 stamp+1")


class TestCredentialFields(unittest.TestCase):
    """四字段凭据校验：齐全通过、缺一报缺失。"""

    def test_complete_credentials_pass(self) -> None:
        import auth

        credentials = _make_credentials()
        self.assertTrue(auth.has_complete_credentials(credentials))
        self.assertEqual(auth.missing_credential_fields(credentials), [])

    def test_normalize_maps_vendor_fields(self) -> None:
        import auth

        credentials = _make_credentials()
        fields = auth.normalize_credential_fields(credentials)
        self.assertEqual(fields["user_pkey"], "synthetic-pkey")
        self.assertEqual(fields["user_heybox_id"], "123456")
        self.assertEqual(fields["heybox_id"], "123456")
        self.assertEqual(fields["x_xhh_tokenid"], "synthetic-tokenid")

    def test_missing_pkey_reported(self) -> None:
        import auth

        credentials = _make_credentials(cookies={"heybox_id": "123456", "x_xhh_tokenid": "t"})
        missing = auth.missing_credential_fields(credentials)
        self.assertEqual(missing, ["user_pkey"])
        self.assertFalse(auth.has_complete_credentials(credentials))

    def test_each_required_field_is_checked(self) -> None:
        import auth

        # 四字段里 user_heybox_id 与 heybox_id 同源（都是 heybox_id 数值），
        # 缺失时成对报出；user_pkey 与 x_xhh_tokenid 各自独立缺失。
        cases = (
            ("user_pkey", {"heybox_id": "1", "x_xhh_tokenid": "t"}, "123456"),
            ("x_xhh_tokenid", {"pkey": "p", "heybox_id": "1"}, "123456"),
            ("heybox_id", {"pkey": "p", "x_xhh_tokenid": "t"}, ""),
        )
        for expected_missing, cookies, uid in cases:
            credentials = _make_credentials(cookies=cookies, uid=uid)
            missing = auth.missing_credential_fields(credentials)
            self.assertIn(expected_missing, missing, f"应报告缺失字段 {expected_missing}，实际 {missing}")


class TestCredentialStore(unittest.TestCase):
    """凭据落盘 / 读取 round-trip，缺失字段拒绝写盘。"""

    def test_save_load_roundtrip(self) -> None:
        import auth

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            credentials = _make_credentials()
            auth.save_credentials(directory, credentials)
            loaded = auth.load_credentials(directory)
            self.assertEqual(loaded.uid, "123456")
            self.assertEqual(loaded.cookies["pkey"], "synthetic-pkey")
            self.assertTrue(auth.has_complete_credentials(loaded))

    def test_save_rejects_incomplete(self) -> None:
        import auth

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            incomplete = _make_credentials(cookies={"heybox_id": "1"})
            with self.assertRaises(ValueError):
                auth.save_credentials(directory, incomplete)
            self.assertFalse((directory / auth.CREDENTIAL_FILE_NAME).exists(), "缺失字段不得写盘")

    def test_clear_removes_file(self) -> None:
        import auth

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            auth.save_credentials(directory, _make_credentials())
            auth.clear_credentials(directory)
            self.assertFalse((directory / auth.CREDENTIAL_FILE_NAME).exists())


class TestRedaction(unittest.TestCase):
    """日志脱敏：不泄露 Cookie / token / pkey 明文。"""

    def test_redact_log_text_hides_secrets(self) -> None:
        from vendor.security import redact_log_text

        raw = "登录失败 token=secret-token-value cookie=x_xhh_tokenid=real-token 请重试"
        cleaned = redact_log_text(raw)
        self.assertNotIn("secret-token-value", cleaned)
        self.assertNotIn("real-token", cleaned)

    def test_redact_data_hides_sensitive_keys(self) -> None:
        from vendor.security import redact_data

        payload = {
            "cookie": "a=b; c=d",
            "x_xhh_tokenid": "real-token",
            "user_pkey": "real-pkey",
            "title": "普通标题",
        }
        cleaned = redact_data(payload)
        self.assertEqual(cleaned["cookie"], "[REDACTED]")
        self.assertEqual(cleaned["x_xhh_tokenid"], "[REDACTED]")
        self.assertEqual(cleaned["user_pkey"], "[REDACTED]")
        self.assertEqual(cleaned["title"], "普通标题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
