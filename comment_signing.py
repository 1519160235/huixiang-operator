# -*- coding: utf-8 -*-
"""小黑盒网页端写接口签名参数（对齐 2026-11-12 线上前端真实协议）。

来源：线上前端 `https://static.max-c.com/static/heybox/webapp/@heybox-spa/website/app/index-C10NdQWW.js`
里的 `Tr`（hkey）、`PW`（写请求参数）、`aSe`（_rnd v16）、`Lke`/`Nke`（拦截器），
以及 `Zot(){_n.defaults.rnd="16"}` —— 前端所有写请求默认已切到 **_rnd 版本 16**。

【签名分流（对齐 docs/接口契约.md 第三节）】
- 读请求（GET）：走 `generate_hkey(path, stamp, nonce)`（见 vendor/request_signing.py）。
- 写请求（POST）：走本模块 `comment_query`，hkey 用 `stamp + 1` 计算。
两套口径不同，不可混用。

【_rnd 版本】
- v15：HMAC-SHA256，盐值 `WORKSHOP_KEY`（旧实现，保留仅为兼容/回滚）。
- v16：AES-256-GCM，密钥 `RND16_KEY_B64`，AAD `heybox-rnd:v16`，
      明文 `["<time>","<nonce>","<path>/"]`，输出 `base64url(iv || ciphertext+tag)`。
      服务端验签失败会回「评论功能暂不可用」；v16 验签通过才会进入后续校验。

服务器环境若缺 AES-GCM 实现（cryptography / pycryptodome），自动回退 v15，
避免插件因依赖问题崩溃；manifest 已声明 cryptography 依赖。
"""

from __future__ import annotations

from typing import Dict, Optional

import base64
import hashlib
import hmac
import json
import secrets
import time

from vendor.request_signing import generate_hkey

COMMENT_PATH = "/bbs/app/comment/create"

# 旧版 v15 公开协议常量，不是账号凭证。
WORKSHOP_KEY = b"Z7mFG4tQp9Ws2LxB8H"  # gitleaks:allow
# 线上前端公开的 v16 协议常量（aSe 里的 oSe），不是账号凭证。
RND16_KEY_B64 = "HFFSTsqmvG3uGxPNXDQW+1VARfqgG4nT6LS1PX7F8NQ="  # gitleaks:allow
RND16_AAD = b"heybox-rnd:v16"
RND_VERSION = 16


def _aes_gcm_encrypt(key: bytes, iv: bytes, plain: bytes, aad: bytes) -> bytes:
    """返回 ciphertext + 16 字节 GCM tag。优先 cryptography，其次 pycryptodome。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).encrypt(iv, plain, aad)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
        cipher.update(aad)
        ciphertext, tag = cipher.encrypt_and_digest(plain)
        return ciphertext + tag
    except ImportError as exc:
        raise RuntimeError("缺少 AES-GCM 依赖（cryptography 或 pycryptodome）") from exc


def rnd16(path: str, stamp: int, nonce: str, iv: Optional[bytes] = None) -> str:
    """复现前端 aSe()：base64url(iv + AES-GCM(JSON([time, nonce, path/])))，去掉 padding。

    :param iv: 12 字节随机向量；测试时可传固定值以获得确定性结果。
    """
    normalized = path if path.endswith("/") else path + "/"
    plain = json.dumps([str(stamp), nonce, normalized], separators=(",", ":")).encode("utf-8")
    key = base64.b64decode(RND16_KEY_B64)
    vector = iv if iv is not None else secrets.token_bytes(12)
    if len(vector) != 12:
        raise ValueError("RND16 IV 必须是 12 字节")
    blob = vector + _aes_gcm_encrypt(key, vector, plain, RND16_AAD)
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def rnd15(nonce: str, stamp: int) -> str:
    """旧版 v15：HMAC-SHA256(salt, salt + nonce + time + ':' + nonce)。"""
    message = WORKSHOP_KEY + f"{nonce}{stamp}:{nonce}".encode()
    return hmac.new(WORKSHOP_KEY, message, hashlib.sha256).hexdigest()


def comment_query(
    now: Optional[int] = None,
    nonce: Optional[str] = None,
    *,
    path: str = COMMENT_PATH,
    rnd_version: int = RND_VERSION,
    heybox_id: str = "",
    iv: Optional[bytes] = None,
) -> Dict[str, str]:
    """生成写请求（POST 表单）的签名参数。

    :param now: 时间戳；缺省取当前时间。测试时传入固定值以保证确定性。
    :param nonce: 32 位大写 hex 随机串；缺省自动生成。
    :param path: 写接口路径。v16 的 _rnd 会用带尾部斜杠的规范化路径。
    :param rnd_version: 16（默认，线上现行）或 15（旧协议回滚用）。
    :param heybox_id: 账号 id；线上前端会把 heybox_id 带在 query 上。
    :param iv: 仅供测试，固定 12 字节 IV。
    :return: 含 _time / nonce / hkey / _rnd 的表单参数。
    :契约: hkey 用 `stamp + 1` 计算（写请求口径），而 `_time` 与 `_rnd` 保留原始时间。
    """
    stamp = int(time.time()) if now is None else now
    request_nonce = secrets.token_hex(16).upper() if nonce is None else nonce

    params: Dict[str, str] = {
        "app": "heybox",
        "os_type": "web",
        "x_app": "heybox_website",
        "x_client_type": "web",
        "x_os_type": "Windows",
        "x_client_version": "",
        "client_type": "web",
        "web_version": "3.0",
        "version": "999.0.4",
        "_time": str(stamp),
        "nonce": request_nonce,
        # 网页算法使用 time+1 生成 hkey，而 _time 与 _rnd 保留原始时间。
        "hkey": generate_hkey(path, stamp + 1, request_nonce),
    }
    if heybox_id:
        params["heybox_id"] = str(heybox_id)

    if rnd_version == 16:
        try:
            params["_rnd"] = "16:" + rnd16(path, stamp, request_nonce, iv=iv)
        except RuntimeError:
            # 缺 AES-GCM 依赖时退回旧协议，宁可被平台明确拒绝，也不让插件崩掉。
            params["_rnd"] = "15:" + rnd15(request_nonce, stamp)
    elif rnd_version == 15:
        params["_rnd"] = "15:" + rnd15(request_nonce, stamp)
    else:
        raise ValueError(f"不支持的 _rnd 版本: {rnd_version}")
    return params
