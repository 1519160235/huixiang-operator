# -*- coding: utf-8 -*-
"""小黑盒 App 端真实写协议（2026-11-12 用雷电模拟器 + mitmproxy 抓包取证）。

## 为什么需要它

web 通道的发帖 / 评论在 2026-11-11 起被平台定向限制：
`_rnd` v16 签名正确、cookie / 风控 token / IP 全换过，仍稳定回
「你的账号无法使用该功能」；而 App 端手动评论完全正常。

真机抓包（App v1.3.401 / build 1151，Android 9）显示 App 是另一套协议：
- 参数组：`os_type=Android` / `x_client_type=mobile` / `x_app=heybox` /
  `imei` / `device_info` / `build` / `dw` / `channel` / `time_zone` 等；
- `hkey` 是 8 位十六进制整数（web 是字符集映射串），`_rnd=14:XXXXXXXX`；
- **不带任何 cookie**，靠 query 里的 `heybox_id` + 签名鉴权；
- UA `ApiMaxJia/1.0`，Referer `http://api.maxjia.com/`。

## 为什么可以固化签名对

真机消融（全部用不存在的 link_id + 空文本，评论不会真发）：
- 改 `nonce` / `index` / 删 `h_src` / 改或删 `_rnd` → 仍通过验签；
- 改 `_time` 或 `hkey` → 「非法请求」/「验证参数错误」；
- 改 `heybox_id` / 换 web 参数组 → 「非法请求」；
- 固定抓包时的 `_time` + `hkey`，间隔数分钟、换服务器出口 IP 重放 →
  仍进入业务层校验（「仅用标点符号无法发布哦」/「error link」）。

结论：`hkey` 绑定 path + `heybox_id` + `_time` + 设备/版本参数，不绑定 body。

**但签名对只有 5 分钟寿命**（2026-11-12 18:22 起实测：88/148/209/269 秒仍通过，
329 秒起稳定回「请求失败了」）。因此固化签名只能在抓包后 4 分钟内用，
长时间无人值守必须解决实时签名，不能靠这一份常量硬扛。
`pair_age()` / `is_fresh()` 就是给调用方做这个守卫的，绝不要绕过。

## 维护

签名与账号、App 版本、设备指纹绑定。若平台更换校验规则或换账号，
需重新抓一次真实请求，更新下面 `_RAW_QUERIES` 里的
`hkey` / `_time` / `_rnd` / `heybox_id` / 设备参数。
熔断会拦住「验证参数错误」这类失效信号，不会继续烧生图钱。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

COMMENT_PATH = "/bbs/app/comment/create"
POST_PATH = "/bbs/app/api/link/post"
APP_ORIGIN = "https://api.xiaoheihe.cn"

# 抓包得到的完整 query（保真保存，只把 nonce 换成随机值）。
# 注意：time_zone 里的 "/"、h_src 里的 base64 都保持原始编码，避免重新
# urlencode 改变字节导致验签失败。
_RAW_QUERIES: Dict[str, str] = {
    COMMENT_PATH: (
        "h_src=bmV3c19mZWVkc18tMV9fbGlua19pZF9fMTk2NzEyOTU5X19yZXF1ZXN0X2lkX19"
        "WN3pGOEhlOWZHTjhIek1uOTllMXpGVVZQbDlmSW1QSV9fYWxfX1JFQzAxMA%3D%3D"
        "&index=1&heybox_id=34182985&imei=a5b8bce0553f20c8&device_info=25098PN5AC"
        "&nonce={nonce}&hkey=E2AF3D9&_rnd=14:68F9D728"
        "&os_type=Android&x_os_type=Android&x_client_type=mobile&os_version=9"
        "&version=1.3.401&build=1151&_time=1794476477&dw=1097&channel=heybox"
        "&x_app=heybox&time_zone=Asia/Shanghai"
    ),
    POST_PATH: (
        "h_src=&heybox_id=34182985&imei=a5b8bce0553f20c8&device_info=25098PN5AC"
        "&nonce={nonce}&hkey=1AA09A3D&_rnd=14:BD524F90"
        "&os_type=Android&x_os_type=Android&x_client_type=mobile&os_version=9"
        "&version=1.3.401&build=1151&_time=1794476918&dw=1097&channel=heybox"
        "&x_app=heybox&time_zone=Asia/Shanghai"
    ),
}

APP_HEADERS: Dict[str, str] = {
    "Referer": "http://api.maxjia.com/",
    "User-Agent": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/41.0.2272.118 Safari/537.36 ApiMaxJia/1.0"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
}

_ALPHABET = string.ascii_letters + string.digits

#: 实测签名有效期上限（服务端 `_time` 新鲜度窗口，秒）。
PAIR_TTL_SECONDS = 300
#: 留给网络往返 / 重试的安全余量：超过这个年龄就不再用。
PAIR_SAFE_AGE_SECONDS = 240

#: 平台「账号级写限制」话术。web 通道命中这些时，才值得切 App 通道。
BLOCK_MARKERS: tuple[str, ...] = (
    "你的账号无法使用该功能",
    "无法使用该功能",
    "账号无法使用",
    "发布功能暂不可用",
    "评论功能暂不可用",
    "回复功能暂不可用",
    "功能暂不可用",
    "禁止发布",
    "禁止评论",
)


def looks_blocked(message: str) -> bool:
    """该错误消息是否属于「账号级写限制」（而不是内容校验 / 频率限制）。"""
    text = str(message or "")
    return any(marker in text for marker in BLOCK_MARKERS)


# ===================== 外部签名对（签名农场 / 手工注入） =====================

#: 签名对文件默认路径（服务器侧固定放这里，本地抓包后可覆盖）。
PAIRS_FILE_DEFAULT = "E:/mai/data/plugins/local.huixiang-operator/app_pairs.json"

_KEY_TO_PATH = {"comment": COMMENT_PATH, "post": POST_PATH}

_EFFECTIVE: Dict[str, str] = {}
_PAIR_COOKIES: Dict[str, str] = {}
_LOADED_SIGNATURE: tuple = (0.0, 0)


def pairs_file() -> str:
    """签名对文件路径，可用环境变量 HXH_APP_PAIRS 覆盖。"""
    return os.environ.get("HXH_APP_PAIRS") or PAIRS_FILE_DEFAULT


def _apply_pair(template: str, hkey: str, stamp: Any) -> str:
    """把模板里的 hkey / _time 换成新抓到的值，其余参数原样保留。"""
    out = re.sub(r"(?<![A-Za-z0-9_])hkey=[^&]*", "hkey=%s" % hkey, template, count=1)
    return re.sub(r"(?<![A-Za-z0-9_])_time=[^&]*", "_time=%s" % stamp, out, count=1)


def refresh_pairs(force: bool = False) -> bool:
    """从外部文件读入更新的签名对（按 mtime/size 判断是否需要重读）。

    文件格式：{"comment": {"hkey": "D4C29517", "_time": 1794478228},
              "post":    {"hkey": "1AA09A3D", "_time": 1794476918}}
    只接受 4–12 位大小写字母数字的 hkey 与纯数字 _time，其余忽略。
    返回是否有生效的更新。
    """
    global _LOADED_SIGNATURE
    path = pairs_file()
    try:
        st = os.stat(path)
    except OSError:
        return False
    sig = (st.st_mtime, st.st_size)
    if sig == _LOADED_SIGNATURE and not force:
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    for key, api_path in _KEY_TO_PATH.items():
        pair = data.get(key)
        if not isinstance(pair, dict):
            continue
        query = str(pair.get("query") or "").strip().lstrip("?")
        if query:
            # 完整 query：h_src / index 也必须是这次抓包的，否则服务端回「请重新登录」
            q = dict(parse_qsl(query, keep_blank_values=True))
            if (not re.fullmatch(r"[A-Za-z0-9]{4,12}", str(q.get("hkey") or ""))
                    or not str(q.get("_time") or "").isdigit()):
                continue
            candidate = query
        else:
            # 兼容只给 hkey/_time 的旧格式（h_src 沿用内置模板，可能被判 relogin）
            hkey = str(pair.get("hkey") or "")
            stamp = str(pair.get("_time") or "")
            if not re.fullmatch(r"[A-Za-z0-9]{4,12}", hkey) or not stamp.isdigit():
                continue
            candidate = _apply_pair(_RAW_QUERIES[api_path], hkey, stamp)
        if _EFFECTIVE.get(api_path) != candidate:
            _EFFECTIVE[api_path] = candidate
            changed = True
        cookie = str(pair.get("cookie") or "").strip()
        if cookie and _PAIR_COOKIES.get(api_path) != cookie:
            _PAIR_COOKIES[api_path] = cookie
            changed = True
    _LOADED_SIGNATURE = sig
    return changed


def _template(path: str) -> Optional[str]:
    """当前生效的签名模板：外部注入的优先，其次用内置抓包值。"""
    refresh_pairs()
    return _EFFECTIVE.get(path) or _RAW_QUERIES.get(path)


def random_nonce(length: int = 32) -> str:
    """App 端 nonce 是混合大小写字母数字（web 端是大写 hex）。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def is_app_write(path: str) -> bool:
    """该写路径是否走 App 通道。"""
    return path in _RAW_QUERIES


def app_url(path: str, nonce: Optional[str] = None) -> str:
    """返回带真实签名的完整 URL，只替换不参与验签的 nonce。"""
    template = _template(path)
    if template is None:
        raise ValueError("没有该路径的 App 签名：%s" % path)
    return APP_ORIGIN + path + "?" + template.replace("{nonce}", nonce or random_nonce())


def app_headers(path: Optional[str] = None) -> Dict[str, str]:
    """App 请求头。给了 path 就带上该路径抓到的 Cookie。

    2026-11-12 20:17 实测：App 写接口**必须带** `pkey` + `x_xhh_tokenid`
    Cookie，缺了会回「请重新登录」（不是签名问题）。早期文档说「App 不带
    cookie」是错的——当时抓包打印把 Cookie 过滤掉了。
    """
    headers = dict(APP_HEADERS)
    if path:
        refresh_pairs()
        cookie = _PAIR_COOKIES.get(path)
        if cookie:
            headers["Cookie"] = cookie
    return headers


def convert_form(path: str, form: Dict[str, Any]) -> Dict[str, str]:
    """把插件内部的 web 表单口径转成 App 抓包口径。

    评论（App）：link_id / text / imgs / is_cy，回复时带 reply_id、root_id。
    发帖（App）：draft / topic_ids / link_tag / text / title / desc。
    其余 web 专属字段（words_count / hashtags / schedule_ts / view_limit /
    post_type / original / declaration）App 不带。
    """
    if path == COMMENT_PATH:
        out: Dict[str, str] = {
            "link_id": str(form.get("link_id", "")),
            "text": str(form.get("text", "")),
            "imgs": str(form.get("imgs", "")),
            "is_cy": str(form.get("is_cy", "0")),
        }
        for key in ("reply_id", "root_id"):
            value = str(form.get(key, ""))
            if value and value != "-1":
                out[key] = value
        return out
    if path == POST_PATH:
        return {
            "draft": "0",
            "topic_ids": str(form.get("topic_ids", "")),
            "link_tag": str(form.get("link_tag", "28")),
            "text": str(form.get("text", "")),
            "title": str(form.get("title", "")),
            "desc": str(form.get("desc", "")),
        }
    raise ValueError("没有该路径的 App 表单口径：%s" % path)


def signature_meta(path: str) -> Dict[str, str]:
    """供日志 / 自检使用的签名摘要（不含 nonce）。"""
    from urllib.parse import parse_qsl

    q = _template(path)
    if q is None:
        return {}
    pairs = dict(parse_qsl(q.replace("{nonce}", ""), keep_blank_values=True))
    return {k: pairs.get(k, "") for k in
            ("heybox_id", "hkey", "_rnd", "_time", "version", "build", "imei")}


def pair_age(path: str, now: Optional[float] = None) -> Optional[int]:
    """抓包签名对距今多少秒；该路径没有签名时返回 None。"""
    meta = signature_meta(path)
    raw = str(meta.get("_time") or "")
    if not raw.isdigit():
        return None
    return int((now if now is not None else time.time()) - int(raw))


def is_fresh(path: str, max_age: int = PAIR_SAFE_AGE_SECONDS,
             now: Optional[float] = None) -> bool:
    """签名对是否还在安全使用窗口内（默认 4 分钟）。"""
    age = pair_age(path, now=now)
    return age is not None and -30 <= age <= max_age


__all__ = [
    "COMMENT_PATH", "POST_PATH", "APP_ORIGIN", "APP_HEADERS",
    "PAIR_TTL_SECONDS", "PAIR_SAFE_AGE_SECONDS", "BLOCK_MARKERS",
    "PAIRS_FILE_DEFAULT",
    "is_app_write", "app_url", "app_headers", "convert_form",
    "random_nonce", "signature_meta", "pair_age", "is_fresh", "looks_blocked",
    "pairs_file", "refresh_pairs",
]
