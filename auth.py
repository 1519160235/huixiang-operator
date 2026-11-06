# -*- coding: utf-8 -*-
"""
回响插件：扫码登录与凭据校验（第二阶段交付）。

【职责】
- 扫码登录：生成二维码 → 轮询扫码状态 → 拿齐四字段凭据 → 落数据目录。
- 二维码过期（约 60~120 秒）后能重新生成，不写死等待。
- 凭据四字段校验：`user_pkey` / `user_heybox_id` / `heybox_id` / `x_xhh_tokenid` 缺一不可。
- 凭据只落数据目录（`credentials.json`），不写日志、不进 config。

【依赖】
- 直连客户端：`vendor.api_client.XiaoheiheApiClient`（来源：小黑盒适配器 vendor，直接搬）。
- 签名：读 GET 走 `vendor.request_signing.generate_hkey(path, stamp, nonce)`；
        写 POST 走 `comment_signing.comment_query`（hkey 用 stamp+1）。
- 二维码渲染：`qrcode`（`_manifest` 依赖 qrcode>=8）。
- 脱敏：`vendor.security.redact_log_text`。

【四字段契约（对齐 docs/接口契约.md 第七节）】
vendor 登录成功后 cookies 的归一化键为 `pkey` / `heybox_id` / `x_xhh_tokenid`，uid 另存。
本模块做「契约四字段 ← vendor 字段」的映射，避免字段名漂移：
  user_pkey      ← cookies 的 pkey（别名 user_pkey / key）
  user_heybox_id ← cookies 的 heybox_id（别名 user_heybox_id / heyboxid / user_id / userid）
  heybox_id      ← credentials.uid（回退 cookies 的 heybox_id）
  x_xhh_tokenid  ← cookies 的 x_xhh_tokenid
四个都非空才「齐全」，缺一不继续。

【安全】
- 凭据写盘用原子写（vendor.security.atomic_write_json），目录权限 best-effort 700。
- 日志与异常一律经 redact_log_text 脱敏，绝不出现 Cookie / token / pkey 明文。

【可测性】
qrcode 与 httpx（vendor.api_client 依赖）延迟到真正登录的函数内部 import，
模块级只依赖标准库 + vendor.models + vendor.security，故四字段校验与凭据读写
无需安装第三方依赖即可本地自检（见 tests/test_stage2_signing.py）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from vendor.models import Credentials, LoginState
from vendor.security import atomic_write_json, redact_log_text, secure_unlink

# 契约表四字段（docs/接口契约.md 第七节）：缺一不可。
REQUIRED_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "user_pkey",
    "user_heybox_id",
    "heybox_id",
    "x_xhh_tokenid",
)

# 凭据文件名（相对名，经数据目录注入解析，禁止绝对路径）。
CREDENTIAL_FILE_NAME = "credentials.json"

# 二维码轮询间隔（秒）。
POLL_INTERVAL_SECONDS = 3


# ===================== 四字段校验（纯逻辑，无第三方依赖） =====================

def normalize_credential_fields(credentials: Credentials) -> dict[str, str]:
    """把 vendor 的 Credentials 归一化成契约四字段，缺失字段为空串。"""
    cookies = credentials.cookies or {}
    uid = (credentials.uid or "").strip()

    def pick(*keys: str) -> str:
        for key in keys:
            value = cookies.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    user_pkey = pick("user_pkey", "pkey", "key")
    user_heybox_id = pick("user_heybox_id", "heybox_id", "heyboxid", "user_id", "userid") or uid
    heybox_id = uid or pick("heybox_id", "heyboxid", "user_heybox_id")
    token_id = pick("x_xhh_tokenid", "xhh_tokenid")

    return {
        "user_pkey": user_pkey,
        "user_heybox_id": user_heybox_id,
        "heybox_id": heybox_id,
        "x_xhh_tokenid": token_id,
    }


def missing_credential_fields(credentials: Credentials) -> list[str]:
    """返回缺失的契约四字段名列表；为空表示齐全。"""
    normalized = normalize_credential_fields(credentials)
    return [name for name in REQUIRED_CREDENTIAL_FIELDS if not normalized.get(name)]


def has_complete_credentials(credentials: Credentials) -> bool:
    """四字段是否齐全。"""
    return not missing_credential_fields(credentials)


# ===================== 凭据落盘 / 读取 / 清除 =====================

def save_credentials(directory: Path, credentials: Credentials) -> None:
    """把登录凭据原子写进数据目录；写前校验四字段齐全。"""
    missing = missing_credential_fields(credentials)
    if missing:
        raise ValueError(f"登录凭据不完整，缺少字段：{'、'.join(missing)}")
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(directory / CREDENTIAL_FILE_NAME, asdict(credentials))


def load_credentials(directory: Path) -> Credentials:
    """读取数据目录里的登录凭据；读后校验四字段齐全。"""
    path = directory / CREDENTIAL_FILE_NAME
    if not path.exists():
        raise FileNotFoundError(f"尚未登录：{CREDENTIAL_FILE_NAME} 不存在，请先扫码登录")
    payload = json.loads(path.read_text(encoding="utf-8"))
    credentials = Credentials(**payload)
    missing = missing_credential_fields(credentials)
    if missing:
        raise ValueError(f"已存凭据不完整，缺少字段：{'、'.join(missing)}，请重新扫码")
    return credentials


def clear_credentials(directory: Path) -> None:
    """清除登录凭据（登出）。"""
    secure_unlink(directory / CREDENTIAL_FILE_NAME)


# ===================== 二维码渲染 =====================

def qr_image(content: str) -> str:
    """把二维码内容渲染成 data URI（base64 PNG），供 WebUI 直接显示。"""
    import qrcode  # 延迟导入：qrcode 仅登录渲染时用到。

    output = io.BytesIO()
    qrcode.make(content).save(output, format="PNG")
    image = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    if len(image) > 24000:
        raise ValueError("登录二维码超过页面图片大小限制，请重新生成")
    return image


# ===================== 扫码登录服务 =====================

class QrLoginService:
    """扫码登录服务：生成二维码 → 轮询 → 四字段齐全才落盘并回调。

    二维码过期后可再次 start() 重新生成，不写死等待。登录轮询在独立 asyncio 任务中执行，
    snapshot() 只返回内存快照（状态 / 二维码 / 剩余秒数），便于 WebUI 轮询展示。
    """

    def __init__(
        self,
        directory: Path,
        on_login: Optional[Callable[[Credentials], Awaitable[None]]] = None,
        *,
        profile_id: str = "default",
    ) -> None:
        self.directory = directory
        self.on_login = on_login
        self.profile_id = profile_id
        self._task: Optional[asyncio.Task] = None
        self._state = "idle"
        self._message = "点击生成二维码，使用小黑盒 App 扫码并确认。"
        self._image = ""
        self._expires_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        """返回登录状态快照，供 WebUI 轮询展示（不含任何凭据明文）。"""
        remaining = max(0, int(self._expires_at - time.time()))
        active = self._state in {"waiting_scan", "scanned_waiting_confirm"} and remaining > 0
        return {
            "login_state": self._state,
            "login_message": self._message,
            "qr_image": self._image if active else "",
            "remaining_seconds": remaining if active else 0,
            "login_active": self._state == "requesting_qr" or active,
        }

    async def start(self) -> None:
        """发起一次扫码登录（先取消上一次，再生成新二维码）。"""
        await self.cancel()
        self._state = "requesting_qr"
        self._message = "正在生成登录二维码…"
        self._task = asyncio.create_task(self._run(), name="huixiang-qr-login")

    async def cancel(self) -> None:
        """取消当前登录，回到 idle。"""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._image = ""
        self._expires_at = 0.0
        self._state = "idle"
        self._message = "点击生成二维码，使用小黑盒 App 扫码并确认。"

    async def _run(self) -> None:
        from vendor.api_client import XiaoheiheApiClient  # 延迟导入：登录时才需要 httpx。

        client = XiaoheiheApiClient(self.profile_id)
        try:
            session = await client.request_qr()
            self._expires_at = session.expires_at
            self._image = await asyncio.to_thread(qr_image, session.qr_content)
            self._state = "waiting_scan"
            self._message = "请使用小黑盒 App 扫码。"
            while time.time() < session.expires_at:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                state, _, credentials = await client.check_qr(session)
                self._state = state.value
                if state is LoginState.SUCCESS:
                    if credentials is None:
                        raise RuntimeError("登录成功但没有收到账号凭证")
                    missing = missing_credential_fields(credentials)
                    if missing:
                        raise RuntimeError(
                            f"登录凭据不完整，缺少字段：{'、'.join(missing)}，请重新扫码"
                        )
                    await client.check_credentials()
                    await asyncio.to_thread(save_credentials, self.directory, credentials)
                    self._image = ""
                    self._expires_at = 0.0
                    if self.on_login is not None:
                        await self.on_login(credentials)
                    self._message = "登录成功，正在连接小黑盒社区。"
                    return
                if state is LoginState.SCANNED_WAITING_CONFIRM:
                    self._message = "已扫码，请在小黑盒 App 中确认登录。"
                elif state is LoginState.WAITING_SCAN:
                    self._message = "等待扫码，请使用小黑盒 App 扫码。"
                elif state in {LoginState.EXPIRED, LoginState.FAILED}:
                    break
            self._state = "expired"
            self._message = "二维码已过期或登录失败，请重新生成。"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 任何异常都不泄露凭据明文。
            self._state = "failed"
            self._message = f"登录失败：{redact_log_text(str(exc))}"
        finally:
            self._image = ""
            await client.close()


__all__ = [
    "REQUIRED_CREDENTIAL_FIELDS",
    "normalize_credential_fields",
    "missing_credential_fields",
    "has_complete_credentials",
    "save_credentials",
    "load_credentials",
    "clear_credentials",
    "qr_image",
    "QrLoginService",
]
