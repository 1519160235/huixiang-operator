# -*- coding: utf-8 -*-
"""网页评论写接口签名参数。

来源：小黑盒适配器 `comment_signing.py`（直接搬，仅改导入路径，算法原样）。
协议参考 xiaoheihe-cli/xhh_operator/signing.py。

【签名分流（对齐 docs/接口契约.md 第三节）】
- 读请求（GET）：走 `generate_hkey(path, stamp, nonce)`（见 vendor/request_signing.py）。
- 写请求（POST）：走本模块 `comment_query`，hkey 用 `stamp + 1` 计算。
两套口径不同，不可混用。
"""

from __future__ import annotations

from typing import Dict, Optional

import hashlib
import hmac
import secrets
import time

from vendor.request_signing import generate_hkey

COMMENT_PATH = "/bbs/app/comment/create"
# 网页客户端公开的版本 15 协议常量，不是账号凭证。
WORKSHOP_KEY = b"Z7mFG4tQp9Ws2LxB8H"  # gitleaks:allow


def comment_query(
    now: Optional[int] = None, nonce: Optional[str] = None, *, path: str = COMMENT_PATH,
) -> Dict[str, str]:
    """生成写请求（POST 表单）的签名参数。

    :param now: 时间戳；缺省取当前时间。测试时传入固定值以保证确定性。
    :param nonce: 随机串；缺省自动生成。测试时传入固定值以保证确定性。
    :param path: 写接口路径。
    :return: 含 _time / nonce / hkey / _rnd 的表单参数。
    :契约: hkey 用 `stamp + 1` 计算（写请求口径），而 `_time` 与 `_rnd` 保留原始时间。
    """
    stamp = int(time.time()) if now is None else now
    request_nonce = secrets.token_hex(16).upper() if nonce is None else nonce
    message = WORKSHOP_KEY + f"{request_nonce}{stamp}:{request_nonce}".encode()
    digest = hmac.new(WORKSHOP_KEY, message, hashlib.sha256).hexdigest()
    return {
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
        "_rnd": "15:" + digest,
    }
