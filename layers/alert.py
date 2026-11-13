# -*- coding: utf-8 -*-
"""出事了主动通知：平台写限制 / 登录失效 / App 签名过期。

【为什么需要】
平台的账号级写限制、登录态失效、App 兜底签名过期，这三类都不是本插件自己能修的。
以前只写 `action_log.json`，得人去后台翻日志才发现；真机上就出现过
「一天 156 次 publish_rejected、0 成功，晚上才发现」。所以这里把「需要人动手」
的事件单独拎出来，落一份人可读的告警文件，并尽量经宿主接口推给用户。

【设计】
1. `record()` 同步、零依赖：同一类事件 30 分钟内只记一次（防每轮刷屏），
   追加到 `data_dir/owner_alerts.jsonl`（只留最近 300 行），同时进待推送队列。
2. `flush()` 在 worker 每轮开头调用：有队列就逐个尝试经 `ctx.api.call(<配置的接口>)`
   推出去；成功清空，失败保留队列下轮再试（最多 3 次后丢弃，避免无限堆积）。
3. 没配置推送接口时**只落文件**，不报错、不影响主流程 —— 用户仍能在后台看到告警。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from layers._cfg import read_str, section

logger = logging.getLogger(__name__)

#: Server 酱（sct.ftqq.com）推送地址模板；Server 酱³ 可在配置里换成自己的域名。
SC_URL_DEFAULT = "https://sctapi.ftqq.com/{key}.send"
#: 兼容 Server 酱³ 的自定义地址：https://<uid>.push.ft07.com/send/<key>.send
SC_KEY_ENV = "HXH_SERVERCHAN_KEY"

#: 没配 Server 酱时的回退：宿主（SnowLuma/NapCat 适配器）发私聊消息的现成接口。
DEFAULT_API = "adapter.napcat.message.send_private_msg"
#: 找不到配置时，从 MaiBot 主配置里读主人 QQ（`permission = ["qq:123"]`）。
BOT_CONFIG = os.environ.get("HXH_BOT_CONFIG") or "E:/mai/config/bot_config.toml"

KEY_FILE = "serverchan_key.txt"
STATE_FILE = "owner_alerts.json"
LOG_FILE = "owner_alerts.jsonl"
DEDUPE_SEC = 1800
MAX_PENDING = 20
MAX_RETRY = 3
MAX_LOG_LINES = 300

#: 事件类型 → 人话标题。新增类型记得在这里补一行。
KIND_TITLES = {
    "platform": "小黑盒限制了这个账号的写功能",
    "relogin": "小黑盒登录态失效，需要重新扫码",
    "signature": "App 兜底签名过期，需要现场续签",
}


def _data_dir(ctx: Any) -> Path:
    paths = getattr(ctx, "paths", None)
    d = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None) or "."
    return Path(d)


def _sc_key(ctx: Any) -> str:
    """Server 酱 SendKey：环境变量 > 配置 > 数据目录里的 serverchan_key.txt。

    支持文件是为了「不改配置、不重启」就能补 Key：直接把 Key 写进
    `<data_dir>/serverchan_key.txt` 即可，下一轮告警就生效。
    """
    key = (os.environ.get(SC_KEY_ENV) or
           read_str(section(ctx, "links"), "serverchan_key", "")).strip()
    if key:
        return key
    try:
        return (_data_dir(ctx) / KEY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sc_url(ctx: Any) -> str:
    """推送地址模板，默认 Server 酱 Turbo。"""
    return (read_str(section(ctx, "links"), "serverchan_url", "") or SC_URL_DEFAULT).strip()


def _post_form(url: str, data: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    """POST 表单并返回 JSON。单独抽出来便于测试替换。"""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, data=data)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            return {"code": -1, "message": resp.text[:200]}
        return payload if isinstance(payload, dict) else {"code": -1}


async def _push_serverchan(ctx: Any, key: str, title: str, desp: str) -> None:
    """发一条 Server 酱消息；code != 0 视为失败（会被重试）。"""
    url = _sc_url(ctx).format(key=key)
    payload = await asyncio.to_thread(_post_form, url, {"title": title, "desp": desp})
    if int(payload.get("code", -1)) != 0:
        raise RuntimeError("Server酱返回 %s：%s" % (payload.get("code"), str(payload.get("message"))[:80]))


def _alert_api(ctx: Any) -> str:
    """宿主通知接口名；配置留空则用适配器现成的私聊接口。"""
    return read_str(section(ctx, "links"), "alert_api", "").strip() or DEFAULT_API


_OWNER_CACHE: list[str] = []


def _alert_target(ctx: Any) -> str:
    """推给谁：优先配置的 alert_target，否则从 MaiBot 主配置的权限列表里读。"""
    configured = read_str(section(ctx, "links"), "alert_target", "").strip()
    if configured:
        return configured
    if _OWNER_CACHE:
        return _OWNER_CACHE[0]
    try:
        text = open(BOT_CONFIG, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""
    # 只认权限白名单里的 QQ，避免把注释/示例里的号码（如 123456）当成主人。
    for pattern in (r"permission\s*=\s*\[[^\]]*?qq:(\d{6,12})",
                    r"admin_ids\s*=\s*\[[^\]]*?(\d{6,12})",
                    r"qq:(\d{7,12})"):
        match = re.search(pattern, text)
        if match:
            _OWNER_CACHE.append(match.group(1))
            return match.group(1)
    return ""


def _read_state(ctx: Any) -> dict[str, Any]:
    try:
        data = json.loads((_data_dir(ctx) / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(ctx: Any, data: dict[str, Any]) -> None:
    try:
        path = _data_dir(ctx) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _append_log(ctx: Any, item: dict[str, Any]) -> None:
    try:
        path = _data_dir(ctx) / LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_LOG_LINES:]
        lines.append(json.dumps(item, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def record(ctx: Any, kind: str, message: str, now: float | None = None) -> dict[str, Any] | None:
    """登记一条告警。同类事件 30 分钟内只记一次；返回本次记录（被去重时返回 None）。"""
    kind = str(kind or "").strip() or "other"
    now = time.time() if now is None else now
    state = _read_state(ctx)
    last = state.get("last_at") or {}
    last = last if isinstance(last, dict) else {}
    prev = last.get(kind)
    if prev is not None and now - float(prev) < DEDUPE_SEC:
        return None

    item = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "kind": kind,
        "title": KIND_TITLES.get(kind, "回响需要你处理一件事"),
        "message": str(message or "")[:300],
    }
    last = dict(last) if isinstance(last, dict) else {}
    last[kind] = now
    pending = state.get("pending")
    pending = pending if isinstance(pending, list) else []
    pending.append({**item, "retry": 0})
    state = {"last_at": last, "pending": pending[-MAX_PENDING:]}
    _write_state(ctx, state)
    _append_log(ctx, item)

    title = item["title"]
    logger.warning("回响告警：%s —— %s", title, item["message"][:160])
    return item


def pending_count(ctx: Any) -> int:
    pending = _read_state(ctx).get("pending")
    return len(pending) if isinstance(pending, list) else 0


async def flush(ctx: Any) -> int:
    """把待推送告警尽量推给用户，返回成功推送条数。没配接口就原地不动。"""
    api_name = _alert_api(ctx)
    state = _read_state(ctx)
    pending = state.get("pending")
    if not isinstance(pending, list) or not pending:
        return 0

    sc_key = _sc_key(ctx)
    if sc_key:
        return await _flush_serverchan(ctx, state, pending, sc_key)

    if not api_name:
        # 没配推送接口：只保留文件，清空队列，避免每轮白转。
        _write_state(ctx, {**state, "pending": []})
        return 0

    api = getattr(ctx, "api", None)
    call = getattr(api, "call", None) if api is not None else None
    if call is None:
        return 0

    target = _alert_target(ctx)
    sent = 0
    keep: list[dict[str, Any]] = []
    for item in pending:
        text = "【回响】%s：%s" % (item.get("title", "需要处理"), item.get("message", ""))
        try:
            await _push(call, api_name, target, text)
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 推送失败绝不能影响主流程
            retry = int(item.get("retry") or 0) + 1
            logger.debug("回响：告警推送失败（第 %s 次）：%s", retry, exc)
            if retry < MAX_RETRY:
                keep.append({**item, "retry": retry})
    _write_state(ctx, {**state, "pending": keep})
    return sent


async def _flush_serverchan(ctx: Any, state: dict[str, Any], pending: list,
                            key: str) -> int:
    """走 Server 酱推送队列，返回成功条数。"""
    sent = 0
    keep: list[dict[str, Any]] = []
    for item in pending:
        title = "【回响】%s" % item.get("title", "需要处理")
        desp = "%s\n\n时间：%s" % (item.get("message", ""), item.get("at", ""))
        try:
            await _push_serverchan(ctx, key, title[:32], desp)
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 推送失败不影响主流程
            retry = int(item.get("retry") or 0) + 1
            logger.debug("回响：Server酱推送失败（第 %s 次）：%s", retry, exc)
            if retry < MAX_RETRY:
                keep.append({**item, "retry": retry})
    _write_state(ctx, {**state, "pending": keep})
    return sent


async def _push(call: Any, api_name: str, target: str, text: str) -> None:
    """按宿主接口的几种常见入参形态各试一次，全失败才抛。"""
    attempts: list[dict[str, Any]] = []
    if target:
        attempts.append({"params": {"user_id": int(target),
                                    "message": [{"type": "text", "data": {"text": text}}]}})
        attempts.append({"user_id": int(target), "message": text})
    attempts.append({"text": text})
    last: Exception | None = None
    for kwargs in attempts:
        try:
            await call(api_name, **kwargs)
            return
        except Exception as exc:  # noqa: BLE001 —— 换一种形态继续试
            last = exc
    raise last if last else RuntimeError("没有可用的推送形态")


__all__ = ["KIND_TITLES", "DEFAULT_API", "SC_URL_DEFAULT", "SC_KEY_ENV",
           "record", "flush", "pending_count"]
