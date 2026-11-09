# -*- coding: utf-8 -*-
"""
回响插件：入口（模块级工厂 + 主类）。

【项目】中文名「回响」，插件 ID `local.huixiang-operator`。
【架构】六块：接入层 → 感知层 → 创作层 → 护栏层 → 发布层 + 状态/存储（见 layers/__init__.py）。

【两类依赖（口径严格区分）】
- A 类「宿主能力，走 ctx.api.call / ctx.llm」：感知层生活轨迹、外围联动（语音 / QQ 空间 / 日记）、
  创作层大模型生成。名字与降级策略见 contracts.py。
- B 类「本插件自带签名客户端直连小黑盒」：接入层与发布层的读写，不经 127.0.0.1:8757 的 MCP 中转。

【真实 SDK 契约（2026-11-04 在部署环境逐条实测核对，勿凭记忆改）】
- 只允许 `from maibot_sdk import ...`，禁止 `import src.*`。
- `create_plugin()` 必须是**模块级、无参**函数。
- 主类继承 `MaiBotPlugin`，`__init__` 调 `super().__init__()`（无参）；**不能给 self.config 赋值**
  （基类 config 是只读 property）。
- `on_load` / `on_unload` / `on_config_update(scope, config_data, version)` 三个都必须实现，
  且全部是 **async**（基类会抛 NotImplementedError，缺一个插件就加载失败）。
- 组件用装饰器：`@Command(name, description, pattern=...)` / `@Tool(name, brief_description=...)`。
- 配置在 `self.config`；数据目录在 `self.ctx.paths.data_dir`；日志用 `self.ctx.logger`。
- 本文件**不写** `from __future__ import annotations`（pydantic 解析配置会失败）。

【编排方式】
业务层（layers/*）只认一个薄上下文（.config / .paths / .store / .api / .xhh_client / .generate_text），
由 runtime.Runtime 把「插件实例 + PluginContext」包成它。因此本文件只做两件事：
1) 生命周期与组件注册（SDK 契约层）；2) 把 Runtime 交给业务层跑流水线。

【包内导入（真机踩坑）】
MaiBot 的 Runner 用 `importlib` 按**文件路径**加载本文件，模块名是扁平的 `plugin`，
它**不会**把插件目录加进 `sys.path`。所以下面这行「路径自举」必须放在所有包内 import
之前，否则 `import contracts` 会直接 `ModuleNotFoundError`，插件加载失败（实测报错原文：
`加载插件失败 [local.huixiang-operator]: No module named 'contracts'`）。
"""

import sys as _sys
from pathlib import Path as _Path

# 路径自举：把插件根目录与它自己加进 sys.path，让顶层模块名互引成立。
_PLUGIN_ROOT = _Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_ROOT))

import asyncio
import functools
import inspect
import logging
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import API, Command, MaiBotPlugin, Tool

import contracts
import state
from config_model import HuiXiangConfig
from models import RunMode, Topic
from layers import LAYER_ORDER, inbound, sense, create, guard, publish, interact, summarize, links
from layers._cfg import NO_PARTITION_CHOICE, norm_choice
from runtime import Runtime
from worker import OpsWorker

logger = logging.getLogger(__name__)

PLUGIN_ID = "local.huixiang-operator"


def _toml_value(value: Any) -> str:
    """把 Python 值写成合法的 TOML 字面量（后台保存配置用）。"""
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if value is None:
        return '""'
    # 字符串统一用 JSON 转义，ensure_ascii=False 保留中文原样。
    return json.dumps(str(value), ensure_ascii=False)



def auto_reply(func: Any) -> Any:
    """命令结果自动发到会话。

    【真机踩坑（2026-11-05）】
    命令方法 `return (True, "文本", 2)` 只是告诉宿主「我处理了」，
    **宿主不会替插件把这段文本发出去**。日志里能看到
    「命令执行成功: hx_status (拦截等级: 1)」，但聊天里没有任何回复。
    必须插件自己调 `self.ctx.send.text(text, stream_id)`。
    这个装饰器统一补上，免得每个命令各写一遍、又漏掉。

    装饰器顺序：`@Command(...)` 在上、`@auto_reply` 在下，
    这样 Command 注册到的是包装后的协程函数。
    """

    @functools.wraps(func)
    async def wrapper(self: Any, stream_id: str = "", **kwargs: Any) -> Any:
        result = await func(self, stream_id=stream_id, **kwargs)
        # 只处理标准三元组 (ok, text, level)。
        if not isinstance(result, tuple) or len(result) != 3:
            return result
        ok, text, level = result
        if not stream_id or not text:
            return result
        try:
            await self.ctx.send.text(str(text), stream_id)
        except Exception as exc:  # noqa: BLE001 —— 发送失败不影响命令本身的返回值。
            self.ctx.logger.warning("回响：发送回复失败（已忽略）：%s", exc)
        return ok, text, level

    return wrapper


DEPRECATED_LABEL_PREFIX = "［已作废］"


def strip_deprecated_fields(sections: Any) -> int:
    """从 schema 的各个分组里删掉「已作废」字段，返回删掉的数量。

    作废字段留在界面上只有坏处：用户会照着它的说明去找别的分组里才有的东西。
    """
    removed = 0
    if not isinstance(sections, dict):
        return removed
    for section in sections.values():
        fields = section.get("fields") if isinstance(section, dict) else None
        if not isinstance(fields, dict):
            continue
        for key in [
            name
            for name, field in fields.items()
            if isinstance(field, dict)
            and str(field.get("label") or "").startswith(DEPRECATED_LABEL_PREFIX)
        ]:
            fields.pop(key, None)
            removed += 1
    return removed


class HuiXiangPlugin(MaiBotPlugin):
    """回响插件主类：只做生命周期与编排，业务逻辑全部下沉到六块。"""

    # 配置模型（SDK 用它生成默认配置、校验、WebUI 表单）。
    config_model = HuiXiangConfig

    # 注意：**不要**设 config_reload_subscriptions = ("self",)。
    # SDK 的 normalize_config_reload_subscription 只认 bot / model 两种别名，
    # 写 "self" 会抛 ValueError: 不支持的配置热重载订阅: self，插件直接加载失败（真机实测）。
    # 本插件不需要额外订阅 bot/model 变更，保持基类默认（空）即可。

    def __init__(self) -> None:
        """无参构造：配置与上下文由宿主通过 set_plugin_config / _set_context 注入。"""
        super().__init__()
        self._store: state.StoreContainer | None = None
        self._runtime: Runtime | None = None
        self._worker: OpsWorker | None = None
        self._qr_service: Any = None
        self._login_started_at: float = 0.0
        # 后台试写任务：WebUI action 有宿主 11 秒硬超时，试写必须异步化。
        # 存 {status: running/done, at: iso, result: dict|None, error: str|None}
        self._test_job: dict[str, Any] | None = None
        self._test_task: Any = None
        # 后台「预览总结」任务：同样是同步 LLM 生成，同样要异步化，否则撞 11 秒超时。
        self._preview_job: dict[str, Any] | None = None
        self._preview_task: Any = None

    # ===================== 生命周期（必须全部 async） =====================

    async def on_load(self) -> None:
        """加载：初始化存储与运行时；enabled=false 时只记一条日志、不起 worker。"""
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._store = state.StoreContainer(data_dir)
        self._runtime = Runtime(self, self.ctx, data_dir)

        # 加载运行时扩充的表情白名单（小黑盒上新表情后由 worker 采样并入）。
        # 内置 57 个始终兜底，这里只是叠加，读不到不影响任何功能。
        try:
            from layers import emojis as emoji_mod

            extra = emoji_mod.load_extra(data_dir)
            if extra:
                self.ctx.logger.info("回响：表情白名单已加载（内置 57 + 扩充 %d）。", extra)
        except Exception as exc:  # noqa: BLE001 —— 白名单加载失败不该影响插件启动。
            self.ctx.logger.warning("回响：表情白名单加载失败（已忽略）：%s", exc)

        # 恢复「必须单独发布」的分区名单（发帖踩坑时学到的，落在这里免得重启又踩）。
        try:
            from layers import topics as topics_mod

            topics_mod.load_exclusive(self.ctx)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回响：独占分区名单加载失败（已忽略）：%s", exc)

        enabled = self.cfg().basic.enabled
        self.ctx.logger.info(
            "回响：插件已加载（enabled=%s，mode=%s，数据目录=%s）。",
            enabled,
            self.cfg().basic.mode,
            data_dir,
        )
        if not enabled:
            self.ctx.logger.info("回响：enabled=false，仅完成加载，不启动后台 worker。")
            return
        self._start_worker()
        self.ctx.logger.info("回响：六块能力已就绪，顺序 %s。", " → ".join(LAYER_ORDER))

    async def on_unload(self) -> None:
        """卸载：先停 worker 与登录轮询，再关小黑盒客户端连接。"""
        await self._stop_worker()
        await self._cancel_login()
        rt = self._runtime
        client = getattr(rt, "_xhh_client", None) if rt is not None else None
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 —— 关连接失败不影响卸载。
                self.ctx.logger.warning("回响：关闭小黑盒客户端失败（已忽略）：%s", exc)
        self._runtime = None
        self.ctx.logger.info("回响：插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：只处理自身范围；enabled 变化时启停 worker。"""
        if scope != "self":
            return
        self.ctx.logger.info("回响：配置已更新（version=%s）。", version)
        if self._store is None:
            return
        # 配置已由基类写入 self.config；这里只需重建运行时引用并调整 worker。
        data_dir = Path(self.ctx.paths.data_dir)
        self._runtime = Runtime(self, self.ctx, data_dir)
        if self.cfg().basic.enabled:
            self._start_worker()
        else:
            await self._stop_worker()

    # ===================== 配置 / 存储访问 =====================

    def cfg(self) -> HuiXiangConfig:
        """返回强类型配置（基类在注入前会抛 RuntimeError，这里兜一份默认值自检用）。"""
        try:
            return self.config  # type: ignore[return-value]
        except RuntimeError:
            return HuiXiangConfig()

    def store(self) -> state.StoreContainer:
        """返回状态存储容器（on_load 之前访问则就地初始化）。"""
        if self._store is None:
            data_dir = Path(self.ctx.paths.data_dir) if self.ctx else Path(".")
            self._store = state.StoreContainer(data_dir)
        return self._store

    def runtime(self) -> Runtime:
        """返回薄上下文（on_load 之前访问则就地构建）。"""
        if self._runtime is None:
            data_dir = Path(self.ctx.paths.data_dir) if self.ctx else Path(".")
            self._runtime = Runtime(self, self.ctx, data_dir)
        return self._runtime

    # ===================== 命令（@Command 装饰器） =====================

    @Command(
        "hx_login",
        description="生成小黑盒扫码登录二维码（在聊天里直接出图）。",
        pattern=r"^/(回响登录|hx_login)$",
        aliases=["/hx_login"],
        permission="operator",
    )
    @auto_reply
    async def cmd_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """/回响登录：生成二维码并发到当前会话；过期或失败可重复执行。"""
        try:
            service = self._login_service()
            await service.start()
            # 等二维码渲染完（_run 是后台任务，渲染要读网络 + 画图）。
            image_data = ""
            for _ in range(40):  # 最多等约 8 秒
                await asyncio.sleep(0.2)
                snap = service.snapshot()
                if snap.get("qr_image"):
                    image_data = str(snap["qr_image"])
                    break
                if snap.get("login_state") in {"failed", "expired"}:
                    return False, f"登录失败：{snap.get('login_message', '')}", 2
            if not image_data:
                return False, "二维码生成超时，请稍后重试 /回响登录。", 2
            await self._send_image(image_data, stream_id)
            return True, "二维码已发送，请用小黑盒 App 扫码并在手机上确认（约 2 分钟内有效）。", 2
        except Exception as exc:  # noqa: BLE001 —— 登录失败只回报，不抛给宿主。
            self.ctx.logger.warning("回响：生成登录二维码失败：%s", exc)
            return False, f"生成二维码失败：{type(exc).__name__}", 2

    @Command(
        "hx_login_check",
        description="检查小黑盒扫码登录状态。",
        pattern=r"^/(回响登录状态|hx_login_check)$",
        aliases=["/hx_login_check"],
        permission="operator",
    )
    @auto_reply
    async def cmd_login_check(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """/回响登录状态：查扫码进度；登录成功后自动重建小黑盒客户端。"""
        if self._qr_service is None:
            try:
                from auth import load_credentials

                credentials = load_credentials(Path(self.ctx.paths.data_dir))
                nickname = getattr(credentials, "nickname", "") or credentials.uid
                return True, f"已登录：{nickname}（uid {credentials.uid}）", 2
            except Exception:
                return True, "尚未登录。发 /回响登录 生成二维码。", 2
        snap = self._qr_service.snapshot()
        state = str(snap.get("login_state", ""))
        message = str(snap.get("login_message", ""))
        if state == "success":
            return True, "登录成功，凭据已保存。现在可以 /回响浏览 了。", 2
        if state in {"failed", "expired"}:
            return False, f"{message} 请重新发 /回响登录。", 2
        remaining = int(snap.get("remaining_seconds", 0) or 0)
        return True, f"{message}（剩余约 {remaining} 秒）", 2

    @Command(
        "hx_logout",
        description="清除小黑盒登录凭据（登出）。",
        pattern=r"^/(回响登出|hx_logout)$",
        aliases=["/hx_logout"],
        permission="operator",
    )
    @auto_reply
    async def cmd_logout(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """/回响登出：清掉数据目录里的凭据并释放客户端。"""
        try:
            from auth import clear_credentials

            await self._cancel_login()
            clear_credentials(Path(self.ctx.paths.data_dir))
            rt = self._runtime
            if rt is not None:
                client = getattr(rt, "_xhh_client", None)
                if client is not None and hasattr(client, "close"):
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001 —— 关连接失败不影响登出。
                        pass
                rt.reset_xhh_client()
            return True, "已清除登录凭据。", 2
        except Exception as exc:  # noqa: BLE001 —— 登出失败只回报。
            return False, f"登出失败：{type(exc).__name__}", 2

    @Command(
        "hx_status",
        description="查看回响状态：今日额度、熔断、后台 worker 与已开联动。",
        pattern=r"^/回响状态$",
        aliases=["/hx_status"],
    )
    @auto_reply
    async def cmd_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响状态：额度 + 熔断 + worker + 联动概览。"""
        result = await self._status_payload()
        lines = [
            f"回响状态（{result['date']}）",
            f"演练额度已用：{result['dry_run_quota_used']}",
            f"真实额度已用：{result['real_quota_used']}",
            f"熔断：{'开启' if result['circuit_open'] else '正常'}",
            f"后台 worker：{'运行中' if result['worker_running'] else '未运行'}（{result['worker_rounds']} 轮）",
            f"已开联动：{'、'.join(result['enabled_links']) or '无'}",
        ]
        return True, "\n".join(lines), 2

    @Command(
        "hx_try_publish",
        description="演练试发一条帖子，不占用真实额度、不发真实请求。",
        pattern=r"^/回响试发$",
        aliases=["/hx_try"],
    )
    @auto_reply
    async def cmd_try_publish(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响试发：以演练模式跑一遍六块流水线。"""
        result = await self.run_once(self.runtime(), RunMode.DRY_RUN)
        if result.get("ok"):
            return True, f"演练完成：{result.get('note', '已组装，未发送')}", 2
        return False, f"演练未通过：{result.get('message', result.get('code', '未知原因'))}", 2

    @Command(
        "hx_browse",
        description="主动浏览一轮小黑盒动态（演练，只读不互动）。",
        pattern=r"^/回响浏览$",
        aliases=["/hx_browse"],
    )
    @auto_reply
    async def cmd_browse(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响浏览：拉一轮动态并写入浏览摘要。"""
        rt = self.runtime()
        result = await self.run_browse_once(rt, RunMode.DRY_RUN)
        self._log(rt, "browse", bool(result.get("ok")), f"浏览到 {result.get('count', 0)} 条", mode=RunMode.DRY_RUN)
        return True, f"浏览完成：{result.get('count', 0)} 条（分区范围 {result.get('partition_mode')}）", 2

    @Command(
        "hx_interact",
        description="响应通知并对浏览到的帖子互动评论（演练，不真发）。",
        pattern=r"^/回响互动$",
        aliases=["/hx_interact"],
    )
    @auto_reply
    async def cmd_interact(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响互动：通知响应 + 主动浏览评论。"""
        rt = self.runtime()
        result = await self.run_interact_once(rt, RunMode.DRY_RUN)
        self._log(
            rt,
            "interact",
            bool(result.get("ok")),
            f"通知 {result.get('notifications_handled', 0)} 条 / 评论 {result.get('comments_sent', 0)} 条",
            mode=RunMode.DRY_RUN,
        )
        return (
            True,
            f"互动完成：通知 {result.get('notifications_handled', 0)} 条，评论 {result.get('comments_sent', 0)} 条",
            2,
        )

    @Command(
        "hx_summarize",
        description="演练跑一次发帖：看这轮会不会想发、会写成什么样（不真发）。",
        pattern=r"^/回响总结$",
        aliases=["/hx_summary"],
    )
    @auto_reply
    async def cmd_summarize(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响总结：手动触发，跳过触发判定。"""
        rt = self.runtime()
        result = await self.run_summary_once(rt, RunMode.DRY_RUN, force=True)
        self._log(
            rt,
            "summary",
            bool(result.get("ok")),
            str(result.get("message") or result.get("note") or ""),
            {
                "chars": result.get("chars"),
                "paragraphs": result.get("paragraphs"),
                # 重写次数与最终 AI 味分数要留痕：用户报「提示重写却没动静」时，
                # 这两项是判断「到底重写了没有」的唯一依据。
                "rewrites": result.get("rewrites"),
                "ai_flavor_score": result.get("ai_flavor_score"),
            },
            mode=RunMode.DRY_RUN,
        )
        if result.get("ok"):
            rewrites = result.get("rewrites") or 0
            extra = f"，AI 味重写过 {rewrites} 次" if rewrites else ""
            return True, f"总结完成：{result.get('chars', 0)} 字 / {result.get('paragraphs', 0)} 段{extra}", 2
        return False, f"总结未完成：{result.get('message', result.get('code', '未知原因'))}", 2

    @Command(
        "hx_log",
        description="查看最近的操作记录。",
        pattern=r"^/回响记录$",
        aliases=["/hx_log"],
    )
    @auto_reply
    async def cmd_action_log(self, **kwargs: Any) -> tuple[bool, str, int]:
        """/回响记录：最近操作留痕。"""
        payload = await self._action_log_payload(limit=10)
        if not payload["count"]:
            return True, "还没有操作记录。", 2
        lines = ["最近操作记录："]
        for row in payload["entries"]:
            mark = "成功" if row.get("ok") else "失败"
            lines.append(f"· {row.get('at', '')} [{row.get('action', '')}] {mark} {row.get('message', '')}".rstrip())
        return True, "\n".join(lines), 2

    # ===================== 工具（@Tool 装饰器） =====================

    @Tool("huixiang_status", brief_description="读取回响的额度、熔断与后台状态")
    async def tool_status(self, **kwargs: Any) -> dict[str, Any]:
        """给模型调用的状态读取工具。"""
        return {"success": True, **await self._status_payload()}

    @Tool("huixiang_dry_run", brief_description="以演练模式跑一遍发帖流水线（不真发）")
    async def tool_dry_run(self, **kwargs: Any) -> dict[str, Any]:
        """给模型调用的演练工具。"""
        result = await self.run_once(self.runtime(), RunMode.DRY_RUN)
        return {"success": bool(result.get("ok")), **result}

    # ===================== 登录辅助 =====================

    def _login_service(self) -> Any:
        """懒建扫码登录服务（复用同一个实例，便于轮询进度）。"""
        if self._qr_service is None:
            from auth import QrLoginService

            self._qr_service = QrLoginService(
                directory=Path(self.ctx.paths.data_dir),
                on_login=self._on_login_success,
                profile_id=str(self.cfg().basic.profile_id or "default"),
            )
        return self._qr_service

    async def _on_login_success(self, credentials: Any) -> None:
        """登录成功回调：重建小黑盒客户端，让后续动作立刻可用。"""
        rt = self._runtime
        if rt is not None:
            rt.reset_xhh_client()
        self.ctx.logger.info(
            "回响：扫码登录成功（uid=%s），小黑盒客户端已重置。", getattr(credentials, "uid", "")
        )

    async def _cancel_login(self) -> None:
        """取消进行中的登录轮询。"""
        service = self._qr_service
        if service is None:
            return
        try:
            await service.cancel()
        except Exception:  # noqa: BLE001 —— 取消失败不影响卸载。
            pass

    async def _send_image(self, image_data: str, stream_id: str) -> None:
        """把 data URI 图片发到当前会话（失败只记日志，不影响命令返回）。"""
        if not stream_id:
            self.ctx.logger.warning("回响：命令未被传入 stream_id，二维码无法直接发送。")
            return
        base64_part = image_data.split(",", 1)[-1] if image_data.startswith("data:") else image_data
        send = getattr(self.ctx, "send", None)
        if send is None or not hasattr(send, "image"):
            self.ctx.logger.warning("回响：宿主未提供 ctx.send.image，二维码无法发送。")
            return
        try:
            await send.image(base64_part, stream_id)
        except Exception as exc:  # noqa: BLE001 —— 发图失败不影响登录流程本身。
            self.ctx.logger.warning("回响：发送二维码图片失败：%s", exc)

    # ===================== 后台 worker =====================

    def _start_worker(self) -> None:
        """启动后台 worker：按配置间隔跑「浏览 → 互动 → 总结」。

        【真机踩坑 2026-11-06】
        原来「worker 已在运行就直接 return」，导致在后台把模式从「演练」改成「自动」后，
        旧 worker 仍持有启动时的 DRY_RUN，操作记录一直写「演练：已组装发帖」——
        用户切了模式却完全不生效。这里改成：运行模式或间隔变了就重启 worker。

        【二次踩坑】重启走的是 `asyncio.create_task`，在没有事件循环的上下文里会抛
        RuntimeError；异常一抛，旧 worker 既没停、新 worker 也没起，模式就永久卡住。
        现在先尝试拿事件循环，没有就同步停掉旧 worker 再新建（新建本身不需要循环）。
        """
        mode = self._runtime_mode()
        interval = self.cfg().browse.browse_interval_seconds
        old = self._worker
        if old is not None and old.running:
            if old.mode == mode and old.interval_seconds == interval:
                return
            self.ctx.logger.info(
                "回响：配置变了（%s → %s），重启后台 worker。", old.mode.value, mode.value
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._restart_worker(mode, interval))
                return
            # 没有运行中的事件循环：旧 worker 的协程已无人驱动，直接丢弃引用即可，
            # 用新模式新建一个（它的 start() 依赖 create_task，下面会再判断一次）。
            self.ctx.logger.warning("回响：当前没有事件循环，无法异步重启 worker，改为直接替换。")
            self._worker = None
            old = None
        self._worker = OpsWorker(self, self.runtime(), mode=mode, interval_seconds=interval)
        try:
            self._worker.start()
        except RuntimeError as exc:
            self.ctx.logger.warning("回响：worker 启动失败（可能不在事件循环内）：%s", exc)
            self._worker = None
            return
        self.ctx.logger.info("回响：后台 worker 已启动（mode=%s，间隔 %s 秒）。", mode.value, interval)

    async def _restart_worker(self, mode: RunMode, interval: int) -> None:
        """停掉旧 worker，用新模式重启（异步，避免阻塞配置更新回调）。"""
        await self._stop_worker()
        self._worker = OpsWorker(self, self.runtime(), mode=mode, interval_seconds=interval)
        self._worker.start()
        self.ctx.logger.info("回响：后台 worker 已按新模式重启（mode=%s）。", mode.value)

    async def _stop_worker(self) -> None:
        """停止后台 worker（等当前轮退出）。"""
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        await worker.stop()

    def _runtime_mode(self) -> RunMode:
        """运行模式：只有 enabled=true 且模式为「自动」才允许真实，其余一律演练。

        配置界面的下拉值是中文（演练 / 自动），老配置写 auto 也认，
        统一经 norm_choice 归一化，避免「界面显示中文、判断认英文」的错位。
        """
        cfg = self.cfg().basic
        if not cfg.enabled:
            return RunMode.DRY_RUN
        mode = norm_choice("mode", cfg.mode, "dry_run")
        return RunMode.REAL if mode == "auto" else RunMode.DRY_RUN

    # ===================== 六块编排 =====================

    async def run_once(self, ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
        """跑一遍六块流水线（接入 → 感知 → 创作 → 护栏 → 发布 → 回读）。

        演练：走完整流程但不真正发帖、不占额度、不写 post_gap。
        真实：过护栏后真正发帖，回读核验通过才占用额度并标记去重。
        """
        self._ensure_store(ctx)
        trace = await sense.read_life_trace(ctx)
        if not await sense.can_speak_now(ctx, trace):
            self.ctx.logger.info("回响：当前不宜开口（睡眠或社交电量不足），本轮跳过。")
            return {"ok": False, "code": "can_speak_now", "message": "当前不宜开口"}
        topics = await sense.fetch_hot_topics(ctx)
        topic = topics[0] if topics else Topic(id=0, name="日常")
        partition = await self._pick_partition(ctx)
        draft = await create.generate_post(ctx, topic, trace)
        draft.partition = partition
        if self.cfg().image.with_image:
            draft.images = await create.generate_images(ctx, draft, self.cfg().image.image_count)
            if mode is not RunMode.DRY_RUN:
                for asset in draft.images:
                    try:
                        await publish.upload_image(ctx, asset)
                    except Exception as exc:  # noqa: BLE001 —— 配图失败降级纯文字。
                        self.ctx.logger.warning("回响：配图上传失败，降级纯文字：%s", exc)
                        draft = await create.fallback_text_only(draft)
                        break
        verdict = await guard.is_publishable(ctx, draft, mode)
        if not verdict.ok:
            self.ctx.logger.info("回响：护栏拦截（%s）：%s", verdict.code, verdict.message)
            return {"ok": False, "code": verdict.code, "message": verdict.message}
        result = await publish.publish_post(ctx, draft, dry_run=(mode is RunMode.DRY_RUN))
        if mode is RunMode.DRY_RUN:
            self._log(ctx, "post", True, result.note or "演练发帖完成", {"title": draft.title})
            return {"ok": True, "dry_run": True, "note": result.note}
        if not result.post_id:
            self._log(ctx, "post", False, "发帖未返回帖子 ID")
            return {"ok": False, "code": "no_post_id", "message": "发帖未返回帖子 ID"}
        verify = await publish.verify_post_published(ctx, result.post_id)
        if verify.ok:
            await guard.reserve_quota(ctx, mode, "post")
            ctx.store.dedup.mark(verdict.detail.get("fingerprint", ""), mode)
            await self._announce(ctx, draft.title, result.post_id)
            self._log(ctx, "post", True, "发帖并回读核验通过", {"title": draft.title, "post_id": result.post_id})
            return {"ok": True, "post_id": result.post_id, "verified": True}
        self._log(ctx, "post", False, verify.message, {"post_id": result.post_id})
        return {
            "ok": False,
            "code": verify.code,
            "post_id": result.post_id,
            "verified": False,
            "message": verify.message,
        }

    async def run_browse_once(self, ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
        """主动浏览一轮：刷新分区目录 → 按 [browse] 分区范围拉动态 → 写入浏览摘要。"""
        self._ensure_store(ctx)
        # 分区目录拉取失败不该让整轮浏览挂掉（上游偶发拒绝 / 超时）。
        try:
            partitions = await inbound.refresh_partitions(ctx, ctx.store.partition)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回响：分区目录刷新失败，改用缓存：%s", exc)
            try:
                partitions = ctx.store.partition.get() or []
            except Exception:
                partitions = []
        browse = self.cfg().browse
        items = await inbound.browse_feeds(
            ctx,
            partitions,
            partition_mode=browse.partition_mode,
            selected_partitions=list(browse.selected_partitions),
            posts_per_round=browse.posts_per_round,
        )
        if items:
            ctx.store.digest.add_many(items)
        return {
            "ok": True,
            "partition_mode": browse.partition_mode,
            "count": len(items),
            "items": [{"post_id": i.post_id, "title": i.title, "author": i.author_name} for i in items],
        }

    async def run_interact_once(self, ctx: Any, mode: RunMode = RunMode.DRY_RUN) -> dict:
        """互动一轮：响应通知 + 主动浏览互动评论。"""
        self._ensure_store(ctx)
        return await interact.respond_and_interact(ctx, mode)

    async def run_summary_once(self, ctx: Any, mode: RunMode = RunMode.DRY_RUN, force: bool = False) -> dict:
        """发一条帖子：判定想不想发 → 生成 → 校验形状 → 护栏 → 发帖 → 回读。

        :param force: 手动命令触发时跳过「时段判定」，但仍会问一句「想发吗」
            （除非用户明确说要发，见 run_post_now）。
        """
        self._ensure_store(ctx)
        store = ctx.store.digest
        if not force:
            # 【2026-11-08】触发从「刷够 N 条」改成「时段窗口 + 有没有值得发的」：
            # 早/中/晚各一条的节奏，窗口里有东西就发，没东西不硬凑。
            triggered, reason = await summarize.post_ready(ctx, store)
            if not triggered:
                return {"ok": False, "code": "not_triggered", "message": reason}
        partition = await self._pick_partition(ctx)
        # 自动跑的时候会先问「想发吗」；手动「发一条」直接把 check_want 关掉。
        result = await summarize.run_summary_once(
            ctx,
            partition=partition,
            mode=mode,
            allow_life_only=True,
            check_want=not force,
        )
        if not force:
            result["trigger"] = reason
        if result.get("ok") and result.get("post_id"):
            await self._announce(ctx, "发了条帖子", str(result.get("post_id", "")))
        return result

    # ===================== 动态配置表单（发帖分区下拉） =====================

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict:
        """生成配置页 Schema，并把「真实分区目录」注入成下拉选项。

        【为什么要覆盖（用户反馈 2026-11-06：发到分区应该是下拉框、能检测到分区）】
        宿主的 `build_config_schema()` 只从类型注解生成静态 schema：
        `choices` 只能来自 `Literal`，而分区是运行时从接口拉的，写死 Literal 就过期了。
        基类这个方法是允许覆盖的返回点，所以在这里：
          1. 先拿基类生成的完整 schema（含所有中文 label）；
          2. 从分区缓存读真实分区名（worker 每轮 / 手动浏览都会刷新）；
          3. 把 `publish.default_partition` 从文本框改成 `select` + `choices`；
          4. 同样处理 `browse.selected_partitions`（改成多选下拉，可选）。

        前端渲染条件（已核对 dist/assets/plugin-config-*.js）：
        `if (n.type === "select" && n.choices) { ... <Select> choices.map(...) }`
        所以必须同时给 `type` 和 `choices`，缺一个都不出下拉框。

        分区列表为空（还没登录 / 还没刷过）时保留原文本框，不让用户卡在空下拉。
        """
        schema = super().get_webui_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        if not isinstance(schema, dict):
            return schema

        names = self._cached_partition_names()
        if not names:
            return schema

        sections = schema.get("sections")
        if not isinstance(sections, dict):
            return schema

        # 1) 发帖默认分区：单选下拉。第一项「不指定分区」用空串语义的可读中文。
        publish = sections.get("publish")
        if isinstance(publish, dict):
            fields = publish.get("fields")
            if isinstance(fields, dict) and "default_partition" in fields:
                field = fields["default_partition"]
                if isinstance(field, dict):
                    field["type"] = "select"
                    field["ui_type"] = "select"
                    # 前端 Select 不接受空字符串作为选项值，用可读的中文哨兵；
                    # 发布层通过 layers._cfg.norm_partition 把它还原成「不挂分区」。
                    field["choices"] = [NO_PARTITION_CHOICE, *names]
                    field["label"] = "默认发到哪个分区"
                    field["hint"] = (
                        f"从接口动态拉取的 {len(names)} 个分区里选；"
                        f"选「{NO_PARTITION_CHOICE}」就发到动态、不挂分区。"
                    )
                    field["description"] = field["hint"]
                    field["placeholder"] = "请选择分区"

        # 2) 浏览指定分区：多选下拉（类型 list[str] → multiple）。
        browse = sections.get("browse")
        if isinstance(browse, dict):
            fields = browse.get("fields")
            if isinstance(fields, dict) and "selected_partitions" in fields:
                field = fields["selected_partitions"]
                if isinstance(field, dict):
                    field["type"] = "select"
                    field["ui_type"] = "select"
                    field["multiple"] = True
                    field["choices"] = names
                    field["label"] = "指定分区"
                    field["hint"] = (
                        f"从接口动态拉取的 {len(names)} 个分区里勾选；"
                        "只有上面「浏览范围」选了「仅指定分区」才生效。"
                    )
                    field["description"] = field["hint"]

        # 3) 已经作废的字段不渲染：留着只会让用户在配置页上找不存在的东西。
        #    【2026-11-08 用户报障「你说看下面发帖窗口，我又没看见发帖窗口在哪」】
        #    根因是「什么时候触发」写着「已弃用，看下面的发帖窗口」，而那个窗口
        #    在另一个分组里。现在作废字段直接从 UI 剔除，分组名也标了序号。
        strip_deprecated_fields(sections)

        return schema

    def _cached_partition_names(self) -> list[str]:
        """从分区缓存读去重后的分区名（只读本地缓存，不发网络请求）。

        配置页可能随时被打开，不能在这里等一个网络请求，所以只读缓存；
        缓存由 worker 每轮浏览 / 后台「立刻浏览一轮」自动刷新。
        """
        try:
            cache = self.store().partition
            parts = cache.get() if hasattr(cache, "get") else []
            names: list[str] = []
            for p in parts or []:
                name = str(getattr(p, "name", "") or "").strip()
                if name and name not in names:
                    names.append(name)
            return names[:120]
        except Exception:  # noqa: BLE001 —— 读缓存失败就退回静态文本框。
            return []

    # ===================== WebUI 后台接口（@API 组件） =====================

    @API(name="hx_overview", description="读取回响工作台总览：运行状态、模式、额度与熔断", version="1")
    async def api_overview(self, **kwargs: Any) -> dict:
        """总览数据：控制台首屏用。

        【2026-11-07 声明式页面改造】除原始值外，额外提供一批「已渲染好的中文文案」
        （*_text）。声明式页面（WebUI schema v2 的 stat/text）只能直接显示字段值，
        没有格式化能力，所以把「已用 / 上限」「运行中 / 已停用」这类判断放在这里做，
        页面侧只做取值绑定。这样页面不需要任何脚本，也不会出现两套格式化逻辑。
        """
        payload = await self._status_payload()
        cfg = self.cfg()
        real_used = int(payload["real_quota_used"] or 0)
        real_limit = int(payload.get("real_quota_limit", 0) or 0)
        left = int(payload.get("real_quota_left", -1) or -1)
        if real_limit > 0:
            quota_text = f"{real_used} / {real_limit}"
            quota_left_text = "今天的发帖额度已用完" if left <= 0 else f"今天还能发 {left} 篇"
        else:
            quota_text = str(real_used)
            quota_left_text = "未设置上限"
        test_job = self._test_job or {}
        preview_job = self._preview_job or {}
        return {
            "ok": True,
            "enabled": bool(cfg.basic.enabled),
            "mode": str(cfg.basic.mode),
            "date": payload["date"],
            "worker_running": payload["worker_running"],
            "worker_mode": payload.get("worker_mode", ""),
            "worker_rounds": payload["worker_rounds"],
            "circuit_open": payload["circuit_open"],
            "dry_run_quota_used": payload["dry_run_quota_used"],
            "real_quota_used": real_used,
            "real_quota_limit": real_limit,
            "real_quota_left": left,
            "text_model": payload.get("text_model", ""),
            "enabled_links": payload["enabled_links"],
            "logged_in": self._is_logged_in(),
            "pending_material": self.store().digest.pending_count(),
            # 后台试写结果（异步化后经 overview 透出，前端每次刷新时拿到）。
            "test_model": self._test_job,
            # 后台预览总结结果（同样异步化透出）。
            "preview_summary": self._preview_job,
            # ---- 给声明式页面用的「已格式化」文案 ----
            "enabled_text": "已开启" if cfg.basic.enabled else "已关闭",
            "worker_text": "正在跑" if payload["worker_running"] else "没在跑",
            "circuit_text": "已熔断" if payload["circuit_open"] else "正常",
            "login_text": "已登录" if self._is_logged_in() else "未登录",
            "quota_text": quota_text,
            "quota_left_text": quota_left_text,
            "dry_run_text": f"{payload['dry_run_quota_used']} 次",
            "material_text": f"{self.store().digest.pending_count()} 条",
            "model_text": payload.get("text_model", "") or "跟随宿主默认",
            "test_model_text": self._describe_test_job(test_job),
            "preview_summary_text": self._describe_preview_job(preview_job),
        }

    @staticmethod
    def _describe_test_job(job: dict) -> str:
        """把试写任务状态翻成一句人话（声明式页面只能显示文本）。"""
        if not job:
            return "还没试写过。点上面的「试写一段」跑一次。"
        status = str(job.get("status", "") or "")
        if status == "running":
            return "正在后台生成（推理模型较慢，约几十秒），稍后刷新就能看到结果。"
        result = job.get("result") or {}
        if result:
            lines = [
                f"配置指定：{result.get('configured', '')}",
                f"实际回报：{result.get('actual_model', '')}",
            ]
            if result.get("fallback"):
                lines.append(
                    f"注意：配置的模型「{result.get('configured', '')}」已不可用，"
                    f"自动改用「{result.get('requested_model', '')}」。"
                )
            sample = str(result.get("sample") or "").strip()
            if sample:
                lines.append("")
                lines.append("生成样稿：")
                lines.append(sample)
            return "\n".join(lines)
        if job.get("error"):
            return f"试写失败：{job['error']}"
        return "还没试写过。"

    @staticmethod
    def _describe_preview_job(job: dict) -> str:
        """把预览总结任务状态翻成一段可读文本（含正文）。"""
        if not job:
            return "还没预览过。点上面的「预览一版总结」跑一次（演练，不发帖）。"
        status = str(job.get("status", "") or "")
        if status == "running":
            return "总结正在后台生成（可能要一两分钟），稍后刷新就能看到。"
        result = job.get("result") or {}
        if result:
            bits: list[str] = []
            if result.get("chars"):
                bits.append(f"{result['chars']} 字")
            if result.get("paragraphs"):
                bits.append(f"{result['paragraphs']} 段")
            if result.get("rewrites"):
                bits.append(f"重写 {result['rewrites']} 次")
            score = result.get("ai_flavor_score")
            if score is not None:
                bits.append(f"AI 味 {round(float(score))}")
            head = str(result.get("message") or "").strip()
            if bits:
                head = (head + "（" + " · ".join(bits) + "）") if head else "（" + " · ".join(bits) + "）"
            title = str(result.get("title") or "").strip()
            body = str(result.get("text") or "").strip()
            parts = [head] if head else []
            if title:
                parts.append(f"标题：{title}")
            if body:
                parts.append("")
                parts.append(body)
            return "\n".join(parts) if parts else "这轮没有产出内容。"
        if job.get("error"):
            return f"预览失败：{job['error']}"
        return "还没预览过。"

    @API(name="hx_action_log", description="读取最近的操作记录，返回中文条目", version="1")
    async def api_action_log(self, limit: int = 20, **kwargs: Any) -> dict:
        payload = await self._action_log_payload(limit=max(1, min(int(limit or 20), 200)))
        labels = {"browse": "浏览", "interact": "互动", "summary": "发帖", "post": "发帖"}
        for row in payload["entries"]:
            row["action_label"] = labels.get(row.get("action", ""), row.get("action", ""))
            row["ok_text"] = "成功" if row.get("ok") else "失败"
        return payload

    @API(name="hx_browse_now", description="手动触发一轮浏览（演练，只读）", version="1")
    async def api_browse_now(self, **kwargs: Any) -> dict:
        result = await self.run_browse_once(self.runtime(), RunMode.DRY_RUN)
        # 后台要能看到本轮全部动态，不能只给前 5 条（用户反馈 2026-11-06：
        # 提示「10 条动态」但下面只列了 5 条，对不上）。
        items = result.get("items") or []
        samples = [str(i.get("title") or "")[:80] for i in items if (i.get("title") or "").strip()]
        count = result.get("count", len(items))
        self._log(self.runtime(), "browse", bool(result.get("ok")), f"后台手动浏览 {count} 条", mode=RunMode.DRY_RUN)
        return {
            "ok": bool(result.get("ok")),
            "count": count,
            "message": (
                f"本轮浏览到 {count} 条动态（演练，未产生真实动作），"
                f"其中 {len(samples)} 条有标题，列在下面"
                if len(samples) < count else
                f"本轮浏览到 {count} 条动态（演练，未产生真实动作）"
            ),
            "samples": samples,
        }

    @API(name="hx_preview_summary", description="预览一版帖子（演练，不占额度不发帖）", version="1")
    async def api_preview_summary(self, **kwargs: Any) -> dict:
        """演练跑一次发帖，把正文和 AI 味分数给出来。

        【为什么需要】用户 2026-11-06 报障「总结太像机器、建议重写却没动静」，
        而当时真实额度已用完，任何真实总结都会先被额度闸门拦住，看不到重写效果。
        这里固定走 DRY_RUN：不占额度、不发帖、不消费素材，随时能验证
        「重写到底有没有在跑」。

        【异步化 2026-11-06】生成总结要真调 LLM 甚至多轮重写，远超宿主
        WebUI action 的 11 秒硬超时，同步 await 必然 504。改成提交后台任务
        立即返回，结果写 self._preview_job，由 hx_overview 透出、前端自动刷新。
        """
        if self._preview_job and self._preview_job.get("status") == "running":
            return {
                "ok": True,
                "status": "running",
                "message": "上一次预览总结还在生成中，稍等自动刷新查看结果",
            }
        self._preview_job = {"status": "running", "at": state.now_iso(), "result": None, "error": None}
        self._preview_task = asyncio.create_task(self._do_preview_summary())
        return {
            "ok": True,
            "status": "running",
            "message": "预览总结已提交，正在后台生成（可能要一两分钟），结果会自动刷新显示",
        }

    async def _do_preview_summary(self) -> None:
        """后台执行预览总结，把结果写回 self._preview_job。"""
        rt = self.runtime()
        try:
            result = await self.run_summary_once(rt, RunMode.DRY_RUN, force=True)
        except Exception as exc:  # noqa: BLE001
            self._preview_job = {
                "status": "done",
                "at": state.now_iso(),
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return
        self._log(
            rt,
            "summary",
            bool(result.get("ok")),
            "预览一版总结（演练，未发送）" + (
                f"：{result.get('message', '')}" if not result.get("ok") else ""
            ),
            {
                "chars": result.get("chars"),
                "paragraphs": result.get("paragraphs"),
                "rewrites": result.get("rewrites"),
                "ai_flavor_score": result.get("ai_flavor_score"),
                "preview": True,
            },
            mode=RunMode.DRY_RUN,
        )
        self._preview_job = {
            "status": "done",
            "at": state.now_iso(),
            "result": {
                "ok": bool(result.get("ok")),
                "message": str(
                    result.get("message")
                    or result.get("note")
                    or (f"预览成功：{result.get('chars')} 字 / {result.get('paragraphs')} 段" if result.get("ok") else "预览未成功")
                ),
                "title": result.get("title"),
                "text": result.get("text"),
                "chars": result.get("chars"),
                "paragraphs": result.get("paragraphs"),
                "rewrites": result.get("rewrites"),
                "ai_flavor_score": result.get("ai_flavor_score"),
            },
            "error": None,
        }

    @API(name="hx_test_model", description="试写一小段，确认当前用的是哪个模型（不发布）", version="1")
    async def api_test_model(self, **kwargs: Any) -> dict:
        """诊断用：真调一次宿主大模型，回报「实际用了哪个模型」和一小段样本。

        用户 2026-11-06 报障「配了 GM 却用 DeepSeek」，光看配置无法确认，
        必须真跑一次并把宿主回传的 model 字段摊开，才能证明改对了。
        只生成文本，不碰小黑盒、不占额度、不写任何状态。

        【异步化 2026-11-06】宿主 WebUI action 有 11 秒硬超时
        （webui_extensions.py 里 asyncio.wait_for(..., timeout=11)），而
        feiyu-gemini-flash 是推理模型、试写动辄几十秒，同步 await 必然撞墙。
        改成：提交后台任务立即返回，结果写进 self._test_job，由 hx_overview
        透出，前端每 10 秒自动刷新时自然拿到结果。
        """
        if self._test_job and self._test_job.get("status") == "running":
            return {
                "ok": True,
                "status": "running",
                "message": "上一次试写还在进行中，稍等自动刷新查看结果",
            }
        self._test_job = {"status": "running", "at": state.now_iso(), "result": None, "error": None}
        self._test_task = asyncio.create_task(self._do_test_model())
        return {
            "ok": True,
            "status": "running",
            "message": "试写已提交，正在后台生成（推理模型较慢，约几十秒），结果会自动刷新显示",
        }

    async def _do_test_model(self) -> None:
        """后台执行试写：真调一次宿主大模型，把结果写回 self._test_job。"""
        rt = self.runtime()
        model_cfg = getattr(self.cfg(), "model", None)
        wanted = str(getattr(model_cfg, "text_model", "") or "")
        try:
            llm = getattr(rt.host, "llm", None)
            if llm is None or not hasattr(llm, "generate"):
                self._test_job = {"status": "done", "at": state.now_iso(), "result": None, "error": "宿主未提供大模型能力"}
                return
            wanted_model, temperature, max_tokens = rt._text_model_options()
            # 配置名可能已下线/改名，先解析成当前真实可用的模型（不把名字写死）。
            model_name = await rt._resolve_model_name(wanted_model) if wanted_model else ""
            call_kwargs: dict[str, Any] = {"task_name": "replyer"}
            from runtime import DEFAULT_TIMEOUT_MS

            call_kwargs["timeout_ms"] = DEFAULT_TIMEOUT_MS
            if model_name:
                call_kwargs["model_name"] = model_name
            if temperature is not None:
                call_kwargs["temperature"] = temperature
            if max_tokens:
                call_kwargs["max_tokens"] = max_tokens
            raw = await llm.generate(
                "用一句话说说你今天刷到了什么，20 字以内，别客套。只输出中文这一句，不要思考过程、不要英文。",
                **call_kwargs,
            )
            text = raw.get("text") or raw.get("content") or raw.get("response") or "" if isinstance(raw, dict) else str(raw)
            # 飞鱼 Gemini 是推理模型，思考内联在 content 里且是英文、无 <think> 标签，
            # 试写会带出一大段英文思考（用户 2026-11-06 报「试写出来是英文」）。
            # 复用 create 的清洗：先剥开头英文思考段，再强清洗（转简/剥英文/去重）。
            text = create.sanitize_comment(create.strip_reasoning_prefix(str(text)))
            actual = ""
            if isinstance(raw, dict):
                actual = str(raw.get("model") or raw.get("model_name") or "")
            result = {
                "ok": bool(str(text).strip()),
                "configured": wanted or "（跟随宿主默认）",
                "requested_model": model_name or "（不指定，用任务第一个）",
                "actual_model": actual or "（宿主未回报）",
                "fallback": bool(wanted_model and model_name and model_name != wanted_model),
                "available_models": (await rt._available_models())[:12],
                "sample": str(text).strip()[:200],
                "message": (
                    f"配置用 {wanted or '宿主默认'}，实际回报模型 {actual or '未知'}，试写成功"
                    if str(text).strip() else "宿主没返回内容，模型可能没配好"
                ),
            }
            self._test_job = {"status": "done", "at": state.now_iso(), "result": result, "error": None}
        except Exception as exc:  # noqa: BLE001 —— 试写失败如实记下，由前端展示。
            self._test_job = {
                "status": "done",
                "at": state.now_iso(),
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    @API(name="hx_reset_circuit", description="手动解除验证码熔断", version="1")
    async def api_reset_circuit(self, **kwargs: Any) -> dict:
        circuit = self.store().circuit
        if hasattr(circuit, "reset"):
            circuit.reset()
        elif hasattr(circuit, "clear"):
            circuit.clear()
        self._log(self.runtime(), "circuit", True, "后台手动解除验证码熔断", mode=RunMode.DRY_RUN)
        return {"ok": True, "message": "熔断已解除，可以继续执行动作了"}

    @API(name="hx_reset_quota", description="把今天某模式的额度清零（测试占用后手动复位）", version="1")
    async def api_reset_quota(self, mode: str = "real", **kwargs: Any) -> dict:
        """重置额度：默认清「真实」今天全部动作的额度（发帖 / 评论 / 点赞 / 回复）。

        背景（用户 2026-11-06）：切自动模式试发帖时会把 daily_quota_post 占掉，
        之后想继续发帖被额度闸门拦住，需要一个人工复位入口。

        【2026-11-07 修正】额度桶已改为「模式 + 日期 + 动作」三键隔离，
        评论不再挤占发帖额度；这里的复位按钮仍然是「今天全部动作一起清零」，
        否则用户点一次只清一个桶、还得点四次。
        """
        m = RunMode.DRY_RUN if str(mode or "real").lower() in {"dry_run", "dry", "演练"} else RunMode.REAL
        day = state.today_str()
        before = self.store().quota.reset_all(m, day)
        label = "演练" if m is RunMode.DRY_RUN else "真实"
        self._log(self.runtime(), "quota", True, f"后台重置{label}额度（复位前已用 {before}）", mode=m)
        return {
            "ok": True,
            "mode": m.value,
            "date": day,
            "before": before,
            "message": f"已清零今天{label}额度（复位前已用 {before} 次），可以继续发帖了",
        }

    @API(name="hx_delete_post", description="删除自己发的一条帖子（清理测试内容）", version="1")
    async def api_delete_post(self, post_id: str = "", **kwargs: Any) -> dict:
        """删除指定帖子。

        【为什么需要】验证发帖链路时难免真的发出一条测试帖；
        没有删除入口就只能让用户去 App 里翻。
        实测 `/bbs/app/link/delete` 可用，重复删返回「帖子已经删除」按成功处理。
        """
        pid = str(post_id or "").strip()
        if not pid:
            pid = str(kwargs.get("id") or kwargs.get("link_id") or "").strip()
        if not pid:
            return {"ok": False, "message": "请填写要删除的帖子 ID"}
        rt = self.runtime()
        self._ensure_store(rt)
        ok, message = await publish.delete_post(rt, pid)
        self._log(rt, "delete", ok, message, {"post_id": pid})
        return {"ok": ok, "post_id": pid, "message": message}

    @API(name="hx_get_config", description="读取当前插件配置，供后台管理页面编辑", version="1")
    async def api_get_config(self, **kwargs: Any) -> dict:
        """从 config.toml 读当前值（不返回密钥原文，密钥只回占位）。"""
        try:
            import tomllib

            path = Path(__file__).resolve().parent / "config.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取配置失败：{type(exc).__name__}", "sections": {}}
        # 密钥字段不回传原文
        image = data.get("image")
        if isinstance(image, dict) and image.get("image_api_key") is not None:
            image["image_api_key"] = "" if not image.get("image_api_key") else "******"
        return {
            "ok": True,
            # 「插件信息」段只有 config_version，由插件维护且页面不可改（disabled），
            # 放到后台里纯占地方（用户反馈 2026-11-06）。后台只返回可编辑的业务段。
            "sections": {
                k: v for k, v in data.items()
                if isinstance(v, dict) and k in {
                    "basic", "login", "browse", "interact", "publish", "summarize",
                    "image", "create", "sense", "guard", "links",
                }
            },
        }

    @API(name="hx_save_config", description="保存后台管理页面修改后的配置并立即生效", version="1")
    async def api_save_config(self, sections: Any = None, **kwargs: Any) -> dict:
        """写回 config.toml，并调用 set_plugin_config 让当前实例立即生效。

        【真机踩坑 2026-11-06】
        参数原来声明成 `type: object`，宿主 `Parameter.accepts()` 对 object 要求
        「value 的键必须全部在 properties 里」，而配置段的键是动态的，
        声明不了 → 页面保存时直接报 `ApiError: 参数不符合声明: sections`。
        改成传 **JSON 字符串**（type: string, max_length 60000），后端再解析。
        """
        if isinstance(sections, str):
            try:
                import json

                sections = json.loads(sections)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"配置数据格式错误：{exc}"}
        if not isinstance(sections, dict) or not sections:
            return {"ok": False, "message": "没有要保存的配置"}
        allowed_sections = {
            "basic", "model", "login", "browse", "interact", "publish", "summarize",
            "image", "create", "sense", "guard", "links",
        }
        patch = {k: v for k, v in sections.items() if k in allowed_sections and isinstance(v, dict)}
        if not patch:
            return {"ok": False, "message": "没有可识别的配置段"}

        # 写文件前先按配置模型校验，不合法就整单拒绝。
        # 真机踩坑（2026-11-06）：多选字段曾写成字符串 'AI'，文件保存成功但
        # HuiXiangConfig 校验失败 → 宿主检测文件变动自动重载插件 →
        # 用户开着的页面持有旧注册句柄，报「插件 WebUI 已下线」。
        # 校验前置能把这条连锁反应从源头掐掉。
        try:
            import tomllib

            cfg_path = Path(__file__).resolve().parent / "config.toml"
            current = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            merged = {**current}
            for name, values in patch.items():
                merged[name] = {**(current.get(name) or {}), **values}
            HuiXiangConfig.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回响：配置校验未通过，未写入：%s", exc)
            return {"ok": False, "message": f"配置不合法，未保存：{exc}"}

        try:
            import shutil

            path = Path(__file__).resolve().parent / "config.toml"
            original = path.read_text(encoding="utf-8")
            # 不用 tomlkit：第三方库可能在别的环境缺失。用标准库按行改写，
            # 只替换已存在的 `key = value`，注释、段顺序、[plugin] 位置全部原样保留。
            lines = original.splitlines(keepends=True)
            current_section = ""
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    current_section = stripped[1:-1].strip()
                    continue
                if current_section not in patch or not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key not in patch[current_section]:
                    continue
                value = patch[current_section][key]
                if current_section == "image" and key == "image_api_key" and value == "******":
                    continue
                lines[idx] = f"{key} = {_toml_value(value)}\n"
            backup = path.with_suffix(".toml.bak_webui")
            shutil.copy2(path, backup)
            path.write_text("".join(lines), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回响：后台保存配置失败：%s", exc)
            return {"ok": False, "message": f"保存配置失败：{type(exc).__name__}: {exc}"}

        # 立即热更新：把完整配置重新注入当前实例，不用重启就生效。
        try:
            import tomllib

            merged = tomllib.loads(path.read_text(encoding="utf-8"))
            self.set_plugin_config(merged)
            # set_plugin_config 是我们自己调的，不会触发宿主 on_config_update，
            # 所以要主动按新模式重建 worker（否则切「自动」后仍跑演练）。
            if self._store is not None:
                self._runtime = Runtime(self, self.ctx, Path(self.ctx.paths.data_dir))
                if self.cfg().basic.enabled:
                    self._start_worker()
                else:
                    asyncio.create_task(self._stop_worker())
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回响：配置热更新失败（文件已保存，重启后生效）：%s", exc)
            self._log(self.runtime(), "config", True, "后台保存配置（需重启生效）", mode=self._runtime_mode())
            return {"ok": True, "message": "配置已保存，但热更新失败，重启插件后生效"}

        self._log(self.runtime(), "config", True, "后台保存配置并热更新", mode=self._runtime_mode())
        return {"ok": True, "message": "配置已保存并立即生效"}

    @API(name="hx_config_schema", description="读取配置表单定义（中文标签与下拉选项）", version="1")
    async def api_config_schema(self, **kwargs: Any) -> dict:
        """把宿主生成的配置 Schema 交给后台页面渲染，避免两套表单不一致。"""
        try:
            schema = self.get_webui_config_schema(
                plugin_id=PLUGIN_ID, plugin_name="回响", plugin_version="0.7.0"
            )
            sections = schema.get("sections", {}) if isinstance(schema, dict) else {}
            # 过滤掉只读的「插件信息」段（config_version，页面改不了）。
            editable = {
                "basic", "login", "browse", "interact", "publish", "summarize",
                "image", "create", "sense", "guard", "links",
            }
            return {
                "ok": True,
                "sections": {
                    name: {
                        "title": sec.get("title", name),
                        "description": sec.get("description", ""),
                        "order": sec.get("order", 99),
                        "fields": sec.get("fields", {}),
                    }
                    for name, sec in sections.items()
                    if isinstance(sec, dict) and name in editable
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"读取表单定义失败：{type(exc).__name__}", "sections": {}}

    @API(name="hx_partitions", description="读取当前缓存的分区目录", version="1")
    async def api_partitions(self, **kwargs: Any) -> dict:
        cache = self.store().partition
        parts = cache.get() if hasattr(cache, "get") else []
        rows = [{"id": getattr(p, "id", 0), "name": getattr(p, "name", "")} for p in parts]
        return {"ok": True, "count": len(rows), "partitions": rows[:60]}

    @API(name="hx_login_status", description="读取小黑盒登录状态与当前二维码", version="1")
    async def api_login_status(self, **kwargs: Any) -> dict:
        """后台轮询用：未登录时带二维码，已登录时带账号信息。凭据绝不返回。

        【2026-11-07 声明式页面改造】额外给 `qr_hint`：声明式页面只能显示文本或
        `image` 节点，没法写「有码 / 没码」的条件逻辑，所以把提示语在这里拼好。
        """
        info = self._login_identity()
        if info.get("logged_in"):
            return {
                "logged_in": True,
                "nickname": info.get("nickname", ""),
                "uid_tail": info.get("uid_tail", ""),
                "message": f"已登录：{info.get('nickname') or '小黑盒账号'}",
                "qr_image": "",
                "remaining_seconds": 0,
                "state": "success",
                "qr_hint": "已登录，不需要扫码。",
            }
        service = self._qr_service
        snap = service.snapshot() if service is not None else {}
        state = str(snap.get("login_state") or "idle")
        qr_image = str(snap.get("qr_image") or "")
        remaining = int(snap.get("remaining_seconds") or 0)
        if qr_image and remaining > 0:
            qr_hint = f"二维码已生成，剩余约 {remaining} 秒。"
        elif qr_image:
            qr_hint = "二维码已过期，请在聊天里发 /回响登录 重新生成。"
        else:
            qr_hint = "还没有二维码。请在聊天里发 /回响登录，机器人会把二维码发给你。"
        return {
            "logged_in": False,
            "nickname": "",
            "uid_tail": "",
            "message": str(snap.get("login_message") or "尚未登录，请生成二维码后扫码"),
            "qr_image": qr_image,
            "remaining_seconds": remaining,
            "state": state,
            "qr_hint": qr_hint,
        }

    @API(name="hx_request_qr", description="生成新的小黑盒扫码登录二维码", version="1")
    async def api_request_qr(self, **kwargs: Any) -> dict:
        """生成/重新生成二维码；返回 data URI，前端直接 <img src>。"""
        try:
            service = self._login_service()
            await service.start()
            # _run 是后台任务，等二维码渲染出来（最多约 8 秒）。
            for _ in range(40):
                await asyncio.sleep(0.2)
                snap = service.snapshot()
                if snap.get("qr_image"):
                    return {
                        "ok": True,
                        "qr_image": str(snap["qr_image"]),
                        "message": "请用小黑盒 App 扫码并在手机上确认",
                        "remaining_seconds": int(snap.get("remaining_seconds") or 0),
                        "state": str(snap.get("login_state") or ""),
                    }
                if snap.get("login_state") in {"failed", "expired"}:
                    return {"ok": False, "message": str(snap.get("login_message") or "二维码生成失败"), "qr_image": ""}
            return {"ok": False, "message": "二维码生成超时，请点「换一张二维码」重试", "qr_image": ""}
        except Exception as exc:  # noqa: BLE001 —— 生成失败给可读中文原因。
            self.ctx.logger.warning("回响：后台生成二维码失败：%s", exc)
            return {"ok": False, "message": f"生成二维码失败：{type(exc).__name__}", "qr_image": ""}

    @API(name="hx_logout", description="清除小黑盒登录凭据（登出）", version="1")
    async def api_logout(self, **kwargs: Any) -> dict:
        try:
            from auth import clear_credentials

            await self._cancel_login()
            clear_credentials(Path(self.ctx.paths.data_dir))
            rt = self._runtime
            if rt is not None:
                client = getattr(rt, "_xhh_client", None)
                if client is not None and hasattr(client, "close"):
                    try:
                        await client.close()
                    except Exception:
                        pass
                rt.reset_xhh_client()
            self._log(self.runtime(), "login", True, "后台手动登出", mode=RunMode.DRY_RUN)
            return {"ok": True, "message": "已退出登录，凭据已清除"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"登出失败：{type(exc).__name__}"}

    def _login_identity(self) -> dict:
        """读取凭据里的展示信息（只取昵称和 uid 尾号，绝不返回完整凭据）。"""
        try:
            from auth import load_credentials

            credentials = load_credentials(Path(self.ctx.paths.data_dir))
            uid = str(getattr(credentials, "uid", "") or "")
            return {
                "logged_in": True,
                "nickname": str(getattr(credentials, "nickname", "") or ""),
                "uid_tail": uid[-4:] if len(uid) >= 4 else "",
            }
        except Exception:
            return {"logged_in": False, "nickname": "", "uid_tail": ""}

    def _is_logged_in(self) -> bool:
        """检查数据目录里是否已有合法凭据（不打印任何凭据内容）。"""
        return bool(self._login_identity().get("logged_in"))

    # ===================== WebUI 查询 / 动作 =====================

    async def _status_payload(self) -> dict:
        """状态总览（命令、工具、WebUI 共用一份数据）。"""
        store = self.store()
        day = state.today_str()
        guard_cfg = self.cfg().guard
        model_cfg = getattr(self.cfg(), "model", None)
        real_used = store.quota.get(RunMode.REAL, day, "post").used
        post_limit = int(getattr(guard_cfg, "daily_quota_post", 3) or 0)
        comment_used = store.quota.get(RunMode.REAL, day, "comment").used
        comment_limit = int(getattr(guard_cfg, "daily_quota_comment", 20) or 0)
        return {
            "date": day,
            "dry_run_quota_used": store.quota.get(RunMode.DRY_RUN, day, "post").used,
            "real_quota_used": real_used,
            # 额度上限要一起给：只报「用了 4 次」看不出「3 条已经用完了」。
            "real_quota_limit": post_limit,
            "real_quota_left": max(0, post_limit - real_used) if post_limit > 0 else -1,
            "real_comment_used": comment_used,
            "real_comment_limit": comment_limit,
            "text_model": str(getattr(model_cfg, "text_model", "") or "跟随宿主默认"),
            "circuit_open": store.circuit.is_open(),
            "worker_running": bool(self._worker and self._worker.running),
            "worker_mode": (self._worker.mode.value if self._worker else ""),
            "worker_rounds": int(getattr(self._worker, "round_count", 0) or 0),
            "enabled_links": links.enabled_links(self.runtime()),
        }

    async def _action_log_payload(self, limit: int = 50) -> dict:
        """操作记录（命令与 WebUI 共用）。"""
        rows = self.store().action_log.recent(limit)
        return {"ok": True, "count": len(rows), "entries": rows}

    # ===================== 内部工具 =====================

    async def _announce(self, ctx: Any, title: str, post_id: str = "") -> None:
        """发帖成功后的外围联动播报（未开启或失败都不影响主闭环）。"""
        try:
            await links.announce_published(ctx, title, post_id)
        except Exception as exc:  # noqa: BLE001 —— 联动异常只记日志。
            self.ctx.logger.warning("回响：外围联动播报异常（已忽略）：%s", exc)

    def _log(
        self,
        ctx: Any,
        action: str,
        ok: bool,
        message: str = "",
        detail: dict | None = None,
        mode: RunMode | None = None,
    ) -> None:
        """写一条操作记录（失败不影响主流程）。"""
        store = getattr(getattr(ctx, "store", None), "action_log", None)
        if store is None:
            return
        try:
            store.add(
                action,
                ok=ok,
                mode=mode or self._runtime_mode(),
                message=message,
                detail=detail or {},
            )
        except Exception:  # noqa: BLE001 —— 记录失败只丢日志。
            self.ctx.logger.warning("回响：操作记录写入失败（已忽略）。")

    def _ensure_store(self, ctx: Any) -> None:
        """确保 ctx.store 已注入（幂等）。"""
        if getattr(ctx, "store", None) is None:
            ctx.store = self.store()

    async def _pick_partition(self, ctx: Any) -> Any:
        """按 [publish].default_partition 选分区；找不到就退第一个，拉不到就返回 None。

        【真机踩坑 2026-11-06】
        `inbound.fetch_partitions` 走真实 TOPIC_INDEX 接口，上游偶尔拒绝
        （抛 CommentRejectedError）。分区只是发帖的可选装饰，
        拉不到不该让整轮「感想总结」失败——日志里出现过
        `[summary] ok=False 执行异常：CommentRejectedError`。
        这里兜住异常，退回缓存；缓存也没有就返回 None（发动态、不挂分区）。
        """
        try:
            partitions = await inbound.fetch_partitions(ctx)
        except Exception as exc:  # noqa: BLE001 —— 上游拒绝/超时都不该拖垮总结。
            self.ctx.logger.warning("回响：分区目录拉取失败，改用缓存：%s", exc)
            try:
                cache = self.store().partition
                partitions = cache.get() if hasattr(cache, "get") else []
            except Exception:
                partitions = []
        default = str(self.cfg().publish.default_partition or "")
        if default == NO_PARTITION_CHOICE:
            default = ""
        for p in partitions:
            if default and (p.name == default or str(p.id) == default):
                return p
        return partitions[0] if partitions else None


def create_plugin() -> HuiXiangPlugin:
    """模块级工厂：宿主按此入口实例化插件（**必须无参**）。"""
    return HuiXiangPlugin()


__all__ = ["create_plugin", "HuiXiangPlugin", "PLUGIN_ID"]
