# -*- coding: utf-8 -*-
"""
回响插件：状态机与幂等存储（落地版）。

【职责边界】
- 动作状态机：草稿 / 已核验 / 发送结果未知 / 失败 的迁移规则（状态枚举定义在 models.py）。
- 登录态：扫码登录后的会话凭据（只存宿主数据目录，不进 config；委托 auth.py）。
- 游标：热门话题 / 分区目录 / 浏览流的增量拉取位置标记。
- 分区缓存：分区目录清单与其刷新时间（定时重测保持最新）。
- 每日额度计数：按 (运行模式, 日期) 分桶。
- 去重指纹：同一内容不重复发布。
- 幂等：同一动作不重复发送，尤其「发送结果未知」时禁止盲目重试。
- 熔断：验证码熔断时间戳与冷却时长（5 分钟）。

【存储实现】
本文件用 JSON 文件作为持久化后端（原子写、进程内缓存），数据目录经 ctx.paths.* 注入；
演练与真实严格隔离：分桶键 quota_bucket_key(mode, day) 保证两套额度 / 去重 / 动作记录不串。

【演练不占额度开关的关系（务必读清）】
- 分桶键 quota_bucket_key(mode, day) 保证演练 / 真实键不同，是「隔离」的根基。
- 另有护栏开关 cfg.guard.dry_run_no_quota_cost（默认 true）：演练模式且该开关为真时，
  护栏层 check_quota / reserve_quota / check_interval 直接放行、根本不读写额度桶、也不写 post_gap
  （见 layers/guard.py）。
- 二者是两层防线：分桶键「即使触碰也隔离」，开关「演练时干脆不触碰」。实现时都不可漏。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from vendor.security import atomic_write_json

from models import (
    ActionRecord,
    ActionState,
    BrowseItem,
    Cursor,
    Partition,
    QuotaCount,
    RunMode,
)

# ===================== 状态迁移表（纯数据） =====================

# 允许的状态迁移。特别注意：SEND_UNKNOWN 不允许直接回到「重新发送」，
# 必须先经回读核验确认，避免网络超时导致重复发帖（历史踩坑）。
ALLOWED_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.DRAFT: frozenset({ActionState.VERIFIED, ActionState.FAILED}),
    ActionState.VERIFIED: frozenset({ActionState.SEND_UNKNOWN, ActionState.FAILED}),
    ActionState.SEND_UNKNOWN: frozenset({ActionState.VERIFIED, ActionState.FAILED}),
    ActionState.FAILED: frozenset({ActionState.DRAFT}),
}


# ===================== 额度分桶（演练 / 真实隔离） =====================

DRY_RUN_QUOTA_PREFIX = "quota:dry_run"
REAL_QUOTA_PREFIX = "quota:real"


def quota_bucket_key(mode: RunMode, day: str) -> str:
    """生成每日额度分桶键，不同 mode 一定得到不同键（演练 / 真实隔离的根基）。"""
    prefix = DRY_RUN_QUOTA_PREFIX if mode is RunMode.DRY_RUN else REAL_QUOTA_PREFIX
    return f"{prefix}:{day}"


def today_str() -> str:
    """返回今日日期（YYYY-MM-DD），用于额度分桶与去重键。"""
    return date.today().isoformat()


def now_iso() -> str:
    """返回当前时间 ISO 字符串。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ===================== 去重指纹（纯函数） =====================

def make_fingerprint(payload: dict[str, Any]) -> str:
    """根据动作内容生成去重指纹（稳定序列化 + SHA-256）。"""
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


# ===================== JSON 文件后端 =====================

class _JsonKV:
    """JSON 键值文件后端：原子写 + 进程内缓存，供各 Store 复用。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        if self._cache is None:
            if self.path.exists():
                try:
                    self._cache = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    self._cache = {}
            else:
                self._cache = {}
        return dict(self._cache)

    def write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)
        self._cache = dict(data)


def _action_to_dict(record: ActionRecord) -> dict[str, Any]:
    return {
        "action_id": record.action_id,
        "fingerprint": record.fingerprint,
        "state": record.state.value,
        "mode": record.mode.value,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "note": record.note,
    }


# ===================== 状态机接口 =====================

class ActionStateMachine:
    """动作状态机：判定迁移是否合法，并记录状态落点。"""

    def can_transition(self, current: ActionState, target: ActionState) -> bool:
        """判定一次状态迁移是否允许（纯逻辑，基于 ALLOWED_TRANSITIONS）。"""
        return target in ALLOWED_TRANSITIONS.get(current, frozenset())

    def transition(
        self, record: ActionRecord, target: ActionState, store: Any = None
    ) -> ActionRecord:
        """把动作记录推进到目标状态（校验合法后落状态，可选持久化）。

        :param store: 可选的动作记录存储（IdempotencyStore），传入时把新状态持久化。
        """
        if not self.can_transition(record.state, target):
            raise ValueError(f"非法状态迁移：{record.state.value} → {target.value}")
        record.state = target
        record.updated_at = now_iso()
        if store is not None and hasattr(store, "save_action"):
            store.save_action(record)
        return record


# ===================== 游标存储接口 =====================

class CursorStore:
    """游标存储：记住热门话题 / 分区目录增量拉取到哪儿了。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "cursors.json")

    def get(self, name: str) -> Cursor:
        payload = self._kv.read().get(name)
        if isinstance(payload, dict):
            return Cursor(name=name, value=str(payload.get("value", "")), updated_at=str(payload.get("updated_at", "")))
        return Cursor(name=name)

    def set(self, cursor: Cursor) -> None:
        data = self._kv.read()
        data[cursor.name] = {"value": cursor.value, "updated_at": now_iso()}
        self._kv.write(data)


# ===================== 额度存储接口 =====================

class QuotaStore:
    """每日额度存储：按 quota_bucket_key(mode, day) 分桶读写，演练 / 真实互不写串。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "quota.json")

    def get(self, mode: RunMode, day: str) -> QuotaCount:
        key = quota_bucket_key(mode, day)
        used = int(self._kv.read().get(key, 0))
        return QuotaCount(mode=mode, day=day, used=used, limit=0)

    def incr(self, mode: RunMode, day: str, amount: int = 1) -> QuotaCount:
        key = quota_bucket_key(mode, day)
        data = self._kv.read()
        used = int(data.get(key, 0)) + amount
        data[key] = used
        self._kv.write(data)
        return QuotaCount(mode=mode, day=day, used=used, limit=0)

    def is_exhausted(self, mode: RunMode, day: str, limit: int = 0) -> bool:
        """判断某模式某天额度是否已用尽（limit 由护栏层从配置注入）。"""
        return self.get(mode, day).used >= max(0, int(limit))


# ===================== 去重存储接口 =====================

class DedupStore:
    """去重存储：按指纹判断内容是否已发布过；演练 / 真实按 mode 隔离。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "dedup.json")

    @staticmethod
    def _key(fingerprint: str, mode: RunMode) -> str:
        return f"{mode.value}:{fingerprint}"

    def seen(self, fingerprint: str, mode: RunMode) -> bool:
        return self._key(fingerprint, mode) in self._kv.read()

    def mark(self, fingerprint: str, mode: RunMode) -> None:
        data = self._kv.read()
        data[self._key(fingerprint, mode)] = now_iso()
        self._kv.write(data)


# ===================== 幂等存储接口 =====================

class IdempotencyStore:
    """幂等存储：以动作 ID / 指纹幂等化一次发送，SEND_UNKNOWN 禁止盲目重试。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "actions.json")

    @staticmethod
    def _key(record: ActionRecord) -> str:
        return f"{record.mode.value}:{record.fingerprint}"

    def begin(self, record: ActionRecord) -> bool:
        """尝试开始一个动作；若同指纹已有进行中 / 已完成动作则返回 False。"""
        key = self._key(record)
        data = self._kv.read()
        existing = data.get(key)
        if isinstance(existing, dict) and existing.get("state") in {
            ActionState.VERIFIED.value,
            ActionState.SEND_UNKNOWN.value,
        }:
            return False
        record.state = ActionState.DRAFT
        record.created_at = record.created_at or now_iso()
        data[key] = _action_to_dict(record)
        self._kv.write(data)
        return True

    def finish(self, record: ActionRecord, verified: bool) -> None:
        """结束一个动作：verified=True 才标记完成；否则保留 SEND_UNKNOWN 等待回读。"""
        key = self._key(record)
        data = self._kv.read()
        record.state = ActionState.VERIFIED if verified else ActionState.SEND_UNKNOWN
        record.updated_at = now_iso()
        data[key] = _action_to_dict(record)
        self._kv.write(data)

    def save_action(self, record: ActionRecord) -> None:
        key = self._key(record)
        data = self._kv.read()
        record.updated_at = now_iso()
        data[key] = _action_to_dict(record)
        self._kv.write(data)


# ===================== 登录态存储接口 =====================

class LoginStore:
    """登录态存储：委托 auth.py，凭据只落宿主数据目录、绝不进 config。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def get(self) -> Any:
        """读取当前登录态（vendor Credentials；未登录抛 FileNotFoundError）。"""
        import auth

        return auth.load_credentials(self.data_dir)

    def save(self, credentials: Any) -> None:
        """保存登录态（四字段齐全才写盘）。"""
        import auth

        auth.save_credentials(self.data_dir, credentials)

    def clear(self) -> None:
        """清除登录态（登出）。"""
        import auth

        auth.clear_credentials(self.data_dir)


# ===================== 浏览游标存储接口 =====================

class BrowseCursorStore:
    """浏览游标存储：记住主动浏览读到哪一页，避免重复拉取。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "browse_cursors.json")

    def get(self, partition_id: int) -> Cursor:
        key = f"partition:{partition_id}"
        payload = self._kv.read().get(key)
        if isinstance(payload, dict):
            return Cursor(name=key, value=str(payload.get("value", "")), updated_at=str(payload.get("updated_at", "")))
        return Cursor(name=key)

    def set(self, partition_id: int, cursor: Cursor) -> None:
        key = f"partition:{partition_id}"
        data = self._kv.read()
        data[key] = {"value": cursor.value, "updated_at": now_iso()}
        self._kv.write(data)


# ===================== 分区缓存存储接口 =====================

class PartitionCacheStore:
    """分区缓存：缓存分区目录清单与其刷新时间，定时重测保持最新。"""

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "partitions.json")

    def get(self) -> list[Partition]:
        raw = self._kv.read().get("partitions", [])
        if not isinstance(raw, list):
            return []
        result: list[Partition] = []
        for item in raw:
            if isinstance(item, dict):
                result.append(
                    Partition(
                        id=int(item.get("id", 0)),
                        name=str(item.get("name", "")),
                        topic_ids=[int(t) for t in item.get("topic_ids", []) if str(t).isdigit()],
                    )
                )
        return result

    def set(self, partitions: list[Partition]) -> None:
        data = self._kv.read()
        data["partitions"] = [
            {"id": p.id, "name": p.name, "topic_ids": p.topic_ids} for p in partitions
        ]
        data["last_refresh_at"] = now_iso()
        self._kv.write(data)

    def last_refresh_at(self) -> str:
        return str(self._kv.read().get("last_refresh_at", ""))


# ===================== 熔断存储接口 =====================

class CircuitBreakerStore:
    """验证码熔断存储：记录熔断时间戳与冷却时长（5 分钟）。"""

    def __init__(self, data_dir: Path, cooldown_minutes: int = 5) -> None:
        self._kv = _JsonKV(data_dir / "circuit.json")
        self.cooldown_seconds = max(0, int(cooldown_minutes)) * 60

    def is_open(self) -> bool:
        ts = self._kv.read().get("tripped_at", 0)
        if not ts:
            return False
        try:
            return (time.time() - float(ts)) < self.cooldown_seconds
        except (TypeError, ValueError):
            return False

    def trip(self) -> None:
        self._kv.write({"tripped_at": time.time()})

    def reset(self) -> None:
        self._kv.write({})


# ===================== 通知去重存储接口 =====================

class NotificationStore:
    """通知去重存储：记录已处理通知 ID 与最后拉取时间，保证同一通知不重复处理。

    小黑盒通知接口会反复返回同一批通知（按 offset/limit 分页），处理前先查这里，
    处理成功后立即标记，避免「同一通知被反复回复」。为防集合无限增长，仅保留最近
    _MAX_KEPT 条已处理记录。
    """

    _MAX_KEPT = 1000

    def __init__(self, data_dir: Path) -> None:
        self._kv = _JsonKV(data_dir / "notifications.json")

    def seen(self, notification_id: str) -> bool:
        processed = self._kv.read().get("processed", {})
        return isinstance(processed, dict) and notification_id in processed

    def mark(self, notification_id: str) -> None:
        data = self._kv.read()
        processed = data.get("processed", {})
        if not isinstance(processed, dict):
            processed = {}
        processed[notification_id] = now_iso()
        if len(processed) > self._MAX_KEPT:
            ordered = sorted(processed.items(), key=lambda kv: kv[1])
            processed = dict(ordered[-self._MAX_KEPT:])
        data["processed"] = processed
        data["last_pull_at"] = now_iso()
        self._kv.write(data)

    def last_pull_at(self) -> str:
        return str(self._kv.read().get("last_pull_at", ""))


# ===================== 浏览摘要存储接口 =====================

class BrowseDigestStore:
    """浏览摘要存储：记住刷过哪些帖子，供感想总结取素材、以及按帖数 / 时间触发。

    结构：{"items": {post_id: {post_id,title,excerpt,partition_id,partition_name,
                              author_name,seen_at,seen_ts,summarized}},
           "last_summary_at": iso}
    - 按 post_id 去重（同一帖反复刷到只保留一条，刷新 seen_at）；
    - 只保留最近 max_entries 条，防文件无限增长；
    - summarized 标记该帖是否已被某次总结消费过，避免重复总结同一批内容。
    """

    def __init__(self, data_dir: Path, max_entries: int = 200) -> None:
        self._kv = _JsonKV(data_dir / "browse_digest.json")
        self.max_entries = max(1, int(max_entries))

    def _items(self) -> dict[str, dict[str, Any]]:
        raw = self._kv.read().get("items", {})
        return dict(raw) if isinstance(raw, dict) else {}

    def add_many(self, items: list[BrowseItem]) -> int:
        """把一批浏览项写进摘要（按 post_id 去重、刷新时间），返回新增条数。"""
        if not items:
            return 0
        data = self._kv.read()
        stored = data.get("items", {})
        if not isinstance(stored, dict):
            stored = {}
        added = 0
        now_ts = time.time()
        now_text = now_iso()
        for item in items:
            pid = str(getattr(item, "post_id", "") or "")
            if not pid:
                continue
            if pid not in stored:
                added += 1
            stored[pid] = {
                "post_id": pid,
                "title": str(getattr(item, "title", "") or ""),
                "excerpt": str(getattr(item, "excerpt", "") or "")[:200],
                "partition_id": int(getattr(item, "partition_id", 0) or 0),
                "partition_name": str(getattr(item, "partition_name", "") or ""),
                "author_name": str(getattr(item, "author_name", "") or ""),
                "seen_at": now_text,
                "seen_ts": now_ts,
                "summarized": False,
            }
        if len(stored) > self.max_entries:
            ordered = sorted(stored.values(), key=lambda v: float(v.get("seen_ts", 0) or 0))
            stored = {str(v.get("post_id", "")): v for v in ordered[-self.max_entries:]}
        data["items"] = stored
        self._kv.write(data)
        return added

    def pending(self, limit: int = 0) -> list[dict[str, Any]]:
        """取尚未被总结消费过的浏览项（按刷到时间倒序）。"""
        rows = [v for v in self._items().values() if isinstance(v, dict) and not v.get("summarized")]
        rows.sort(key=lambda v: float(v.get("seen_ts", 0) or 0), reverse=True)
        return rows[:limit] if limit > 0 else rows

    def pending_count(self) -> int:
        """未被总结消费的浏览条数（触发判定用）。"""
        return len(self.pending())

    def pending_count_since(self, since_ts: float) -> int:
        """指定时间点之后刷到、且未被消费的条数。"""
        return sum(
            1
            for v in self._items().values()
            if isinstance(v, dict)
            and not v.get("summarized")
            and float(v.get("seen_ts", 0) or 0) >= since_ts
        )

    def mark_summarized(self, post_ids: list[str]) -> None:
        """标记一批浏览项已被总结消费，并记录本次总结时间。"""
        data = self._kv.read()
        stored = data.get("items", {})
        if not isinstance(stored, dict):
            stored = {}
        for pid in post_ids:
            row = stored.get(str(pid))
            if isinstance(row, dict):
                row["summarized"] = True
        data["items"] = stored
        data["last_summary_at"] = now_iso()
        data["last_summary_ts"] = time.time()
        self._kv.write(data)

    def last_summary_at(self) -> str:
        return str(self._kv.read().get("last_summary_at", ""))

    def last_summary_ts(self) -> float:
        value = self._kv.read().get("last_summary_ts", 0)
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0


# ===================== 操作记录存储接口 =====================

class ActionLogStore:
    """操作记录：每轮动作的留痕（时间 / 类型 / 结果 / 演练或真实 / 中文备注）。

    WebUI 的「操作记录」面板据此渲染；只保留最近 max_entries 条，防文件无限增长。
    记录的是「发生了什么」，不参与任何判定，因此读写失败也不应影响主流程。
    """

    def __init__(self, data_dir: Path, max_entries: int = 500) -> None:
        self._kv = _JsonKV(data_dir / "action_log.json")
        self.max_entries = max(1, int(max_entries))

    def add(
        self,
        action: str,
        *,
        ok: bool,
        mode: RunMode = RunMode.DRY_RUN,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条操作记录，返回该条记录。"""
        entry = {
            "at": now_iso(),
            "action": str(action),
            "ok": bool(ok),
            "mode": mode.value,
            "message": str(message),
            "detail": dict(detail or {}),
        }
        data = self._kv.read()
        rows = data.get("entries", [])
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        if len(rows) > self.max_entries:
            rows = rows[-self.max_entries :]
        data["entries"] = rows
        self._kv.write(data)
        return entry

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """取最近若干条记录（新的在前）。"""
        rows = self._kv.read().get("entries", [])
        if not isinstance(rows, list):
            return []
        cleaned = [dict(row) for row in rows if isinstance(row, dict)]
        cleaned.reverse()
        return cleaned[: max(1, int(limit))]

    def count(self) -> int:
        rows = self._kv.read().get("entries", [])
        return len(rows) if isinstance(rows, list) else 0


# ===================== 存储容器（把各 Store 一次装齐） =====================

class StoreContainer:
    """把各 Store 一次装齐，供六块共用（演练 / 真实隔离由各 Store 自己保证）。

    【为什么放在 state.py 而不是 plugin.py】
    业务层（layers/*）与单测都需要它，但不该为此去 import 插件入口——
    插件入口依赖 `maibot_sdk`，本地没装 SDK 时会把纯逻辑测试一起拖崩。
    放在这里，`state.py` 保持「零第三方依赖」，业务层可独立测试。
    """

    def __init__(self, data_dir: Path) -> None:
        self.quota = QuotaStore(data_dir)
        self.dedup = DedupStore(data_dir)
        self.idempotency = IdempotencyStore(data_dir)
        self.cursor = CursorStore(data_dir)
        self.browse_cursor = BrowseCursorStore(data_dir)
        self.partition = PartitionCacheStore(data_dir)
        self.circuit = CircuitBreakerStore(data_dir)
        self.login = LoginStore(data_dir)
        self.notification = NotificationStore(data_dir)
        self.digest = BrowseDigestStore(data_dir)
        self.action_log = ActionLogStore(data_dir)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DRY_RUN_QUOTA_PREFIX",
    "REAL_QUOTA_PREFIX",
    "quota_bucket_key",
    "today_str",
    "now_iso",
    "make_fingerprint",
    "ActionStateMachine",
    "CursorStore",
    "QuotaStore",
    "DedupStore",
    "IdempotencyStore",
    "LoginStore",
    "BrowseCursorStore",
    "PartitionCacheStore",
    "CircuitBreakerStore",
    "NotificationStore",
    "BrowseDigestStore",
    "ActionLogStore",
    "StoreContainer",
]
