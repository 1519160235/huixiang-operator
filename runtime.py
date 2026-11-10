# -*- coding: utf-8 -*-
"""回响插件：宿主运行时适配层。

【为什么需要这一层】
六块业务代码（layers/*）只认一个「薄上下文」：`.config`（配置对象）、`.paths.data_dir`、
`.store`（状态存储）、`.api.call`（A 类宿主能力）、`.xhh_client`（B 类直连客户端）、
`.generate_text`（大模型）。真实 MaiBot 的 `PluginContext` 只有 `api / llm / send / paths`，
且配置是插件基类的 `self.config` 属性、不是 ctx 上的东西。

本模块把两者对接起来：把 `MaiBotPlugin` 实例 + `PluginContext` 包成一个薄上下文，
业务层完全不用知道宿主长什么样。这样单测里用 `SimpleNamespace` 也能跑同一套代码。

【真实 SDK 契约（2026-11-04 在部署环境实测核对）】
- `maibot_sdk.__version__ == 2.21.0`
- `PluginContext.paths` 是 `PluginPaths(data_dir: Path, runtime_dir: Path)`
- `PluginContext.api.call(api_name, *, version="", **kwargs)` —— A 类能力
- `PluginContext.llm.generate(prompt, model="", temperature=None, max_tokens=None, ...)` -> dict
- 配置在插件基类：`self.config`（property，返回 PluginConfigBase 子类实例）
- 日志用 `self.ctx.logger`
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 宿主模型任务名。
# 【真机踩坑（2026-11-06）】`ctx.llm.generate(task_name=...)` 的 task_name **不是自由标签**，
# 必须是 model_config.toml 里已声明的 `[model_task_config.XXX]` 段名，否则直接报
# `未找到名为 XXX 的模型配置`，整条内容生成链路全废。
# 服务器上现有的合法名字：web_search / replyer / planner / memory / utils / learner /
# fast_model / emoji / vlm / voice / embedding / image_embedding。
# 写内容属于「回复生成」这一类，用 replyer 最贴切。
DEFAULT_MODEL_TASK = "replyer"

# 兜底用的任务名：replyer 不可用时依次尝试。
FALLBACK_MODEL_TASKS: tuple[str, ...] = ("replyer", "utils", "fast_model", "planner")

# 默认写作用的具体模型名（空字符串=不指定，沿用任务里的优先级顺序）。
# 【真机踩坑 2026-11-06】宿主 model_config.toml 里 `replyer` 的 model_list 是
# ["ds-official", "feiyu-gemini-flash"]，DeepSeek 排第一，所以只传 task_name 时
# 内容一定是 DeepSeek 生成的。用户明确要求「不要 DeepSeek，用 GM」，
# 必须用 model_name 直选具体模型（SDK 契约：传 model_name 一律直选，绕过任务优先级）。
#
# 【真机踩坑 2026-11-07 用户报障「中转站一个密钥可能有几个模型，不能把模型名写死」】
# 中转站的模型会下线、改名（实际发生过：`feiyu-gemini-flash` 后来变成 `gemini-3.8-flash`，
# 旧名直接 404 model_not_found）。所以这个名字只当「**期望的首选**」，不再当硬编码：
# 运行时先问宿主要可用模型列表，配置里的名字不在列表里就按规则降级/模糊匹配（见
# `_resolve_model_name`）。列表拿不到时（老宿主/接口异常）才退回这个常量。
DEFAULT_MODEL_NAME = "feiyu-gemini-flash"

# 降级时按顺序尝试的「同厂/同类」候选前缀。
# 用户偏好是 Gemini（GM），所以先找 gemini 系；找不到再退到 glm；最后交给任务默认。
# 【真机缺陷 2026-11-10】宿主 `llm.get_available_models()` 返回的其实是**任务名**
# （embedding / emoji / fast_model …），不是模型名。拿它去比对配置里的模型名，
# 必然得出「模型不可用」，然后白白降级。这里把纯任务名的列表识别出来，
# 当成「问了但没得到模型清单」处理。
_TASK_NAME_ONLY = {
    "embedding", "emoji", "fast_model", "image_embedding", "learner", "memory",
    "replyer", "planner", "utils", "vlm", "voice", "tts", "asr", "web_search",
    "tool", "image", "reasoning", "router", "judge",
}


MODEL_PREFERENCE_PREFIXES: tuple[str, ...] = ("gemini", "glm", "claude", "gpt")

# 单次生成给宿主留多久（毫秒）。
# 【真机踩坑 2026-11-06】宿主 cap.call 的默认超时只有 30000ms，写 200~400 字的
# 帖子经常跑不完，日志里直接是 `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`，
# 上层只看到「生成失败」，用户看到的就是「提示重写之后没动静」。
# SDK 的 llm.generate 支持 `timeout_ms=`，它会被当成这次 RPC 的等待预算
# （life-frequency 插件里同款用法），这里给足 180 秒。
DEFAULT_TIMEOUT_MS = 180_000


class _Paths:
    """路径代理：同时提供 data_dir 与 state_dir（业务层两种叫法都认）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(data_dir)


class _ApiProxy:
    """A 类宿主能力代理：把 ctx.api.call(name, **kwargs) 原样透传。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def call(self, api_name: str, **kwargs: Any) -> Any:
        api = getattr(self._ctx, "api", None)
        if api is None or not hasattr(api, "call"):
            raise RuntimeError("宿主未提供 ctx.api.call 能力")
        # 真实签名是 call(api_name, *, version="", **kwargs)，直接透传即可。
        return await api.call(api_name, **kwargs)


class Runtime:
    """薄上下文：业务层唯一依赖的运行时对象。

    :param plugin: 插件实例（提供 config / store / 日志）。
    :param host_ctx: 真实 `PluginContext`；单测可传 SimpleNamespace。
    :param data_dir: 数据目录（已解析好的绝对路径）。
    """

    def __init__(self, plugin: Any, host_ctx: Any, data_dir: Path) -> None:
        self.plugin = plugin
        self.host = host_ctx
        self.config = plugin.cfg()
        self.paths = _Paths(data_dir)
        self.store = plugin.store()
        self.logger = getattr(host_ctx, "logger", logger)
        self.api = _ApiProxy(host_ctx)
        self._xhh_client: Any = None
        self._xhh_error: str = ""
        # 宿主可用模型列表缓存（None = 还没问过；[] = 问过但没有/不支持）。
        self._models_cache: list[str] | None = None
        # 宿主黑话表缓存（None = 还没读过；[] = 读过但没有黑话）。
        self._jargon_cache: list[tuple[str, str, int, bool]] | None = None

    # ===================== A 类：大模型 =====================

    async def generate_text(self, prompt: str) -> str:
        """经宿主大模型生成文本（真实 SDK：ctx.llm.generate）。

        task_name 必须是宿主 model_config.toml 里已声明的任务名（见模块顶部注释）。

        【真机踩坑 2026-11-06】只传 task_name 时，宿主会按该任务 model_list 的
        顺序取模型；本机 `replyer` 是 ["ds-official", "feiyu-gemini-flash"]，
        DeepSeek 永远排第一 —— 用户改了「用 GM」也照样生成 DeepSeek 内容。
        解法：用 `model_name=` 直选具体模型（SDK 契约里 model_name 一律直选，
        即使与任务同名也不当任务解析），从而绕过任务里的优先级顺序。
        """
        llm = getattr(self.host, "llm", None)
        if llm is None or not hasattr(llm, "generate"):
            raise NotImplementedError("宿主未提供 ctx.llm.generate 能力")

        wanted, temperature, max_tokens = self._text_model_options()
        # 配置名可能已下线/改名（中转站常态），先向宿主要真实可用列表做解析。
        model_name = await self._resolve_model_name(wanted) if wanted else ""

        last_error: Exception | None = None
        empty_tasks: list[str] = []
        for task in FALLBACK_MODEL_TASKS:
            kwargs: dict[str, Any] = {"task_name": task, "timeout_ms": DEFAULT_TIMEOUT_MS}
            if model_name:
                kwargs["model_name"] = model_name
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            try:
                result = await llm.generate(prompt, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 换下一个任务名再试。
                last_error = exc
                self.logger.debug("回响：模型任务 %s 调用失败，换下一个：%s", task, exc)
                continue
            text = _extract_text(result)
            if text.strip():
                return text
            empty_tasks.append(task)
        if last_error is not None:
            raise last_error
        # 宿主在某些失败场景下不抛异常、只回一个空结果（真机实测：任务名不存在时就是这样）。
        # 这里必须显式报错，否则上层只会看到「生成 0 字」，误判成模型话少。
        raise RuntimeError(
            f"宿主大模型未返回内容（已尝试任务：{'、'.join(empty_tasks) or '无'}，"
            f"模型：{model_name or '跟随任务默认'}）。"
            "请检查 model_config.toml 里这些任务的 model_list 是否配了可用模型。"
        )

    def _text_model_options(self) -> tuple[str, float | None, int | None]:
        """读「模型设置」段：返回 (模型名, 温度, max_tokens)。

        配置缺失 / 读取失败时退回 DEFAULT_MODEL_NAME，绝不因为一个配置项读不到
        就把整条生成链路搞崩。

        注意：这里只读「用户想要哪个」，**是否真的可用由 `_resolve_model_name`
        在调用时确认**（中转站模型会下线改名，写死必然失效）。
        """
        try:
            model_cfg = getattr(self.config, "model", None)
            if model_cfg is None:
                return DEFAULT_MODEL_NAME, None, None
            raw = str(getattr(model_cfg, "text_model", "") or "").strip()
            # 「跟随宿主默认」是界面上的选项，实际含义是「不指定模型」。
            name = "" if raw in ("", "跟随宿主默认") else raw
            temp = getattr(model_cfg, "temperature", None)
            tokens = getattr(model_cfg, "max_tokens", None)
            return (
                name,
                float(temp) if isinstance(temp, (int, float)) else None,
                int(tokens) if isinstance(tokens, (int, float)) and int(tokens) > 0 else None,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("回响：读取模型设置失败，退回默认模型 %s：%s", DEFAULT_MODEL_NAME, exc)
            return DEFAULT_MODEL_NAME, None, None

    async def _available_models(self) -> list[str]:
        """问宿主要「当前真实可用」的模型名列表。

        拿不到就返回空列表——调用方必须把空列表当成「不知道」，而不是「没有模型」。
        """
        cached = getattr(self, "_models_cache", None)
        if cached is not None:
            return cached
        llm = getattr(self.host, "llm", None)
        if llm is None or not hasattr(llm, "get_available_models"):
            self._models_cache = []
            return []
        try:
            models = await llm.get_available_models()
            names = [str(m) for m in (models or []) if str(m).strip()]
        except Exception as exc:  # noqa: BLE001 —— 探测失败不冒泡，退回不指定模型。
            self.logger.debug("回响：获取可用模型列表失败（不影响主流程）：%s", exc)
            names = []
        self._models_cache = names
        return names

    async def _resolve_model_name(self, wanted: str) -> str:
        """把「配置里期望的模型名」解析成「当前真能用的模型名」。

        【2026-11-07 用户要求「不能把模型名写死」】
        中转站一个密钥常挂好几个模型，模型会下线、改名，写死名字早晚会失效。
        用户明确要求：模型名要动态解析。

        【重要澄清 2026-11-07 —— 我自己一度误判了这个名字】
        我看到 `config.toml` 里 `text_model = "feiyu-gemini-flash"`，直接拿它去打
        中转站 `/models` 和 `/chat/completions`，都报 404 `model not supported`，
        于是错误地判定「这个名字失效了、线上评论一直在失败」。

        真相在 `model_config.toml` 的 `[[models]]` 段：
            model_identifier = "gemini-3.8-flash"   # 服务商那边的真实模型
            name = "feiyu-gemini-flash"             # 宿主内部起的别名
        也就是说 `feiyu-gemini-flash` 是**宿主别名**，宿主会把它翻译成
        `gemini-3.8-flash` 再请求上游。**它一直是可用的**，日志里出现这个名字是正常的，
        我拿裸名去直连上游才会 404。

        所以本函数的安全设计是：**拿不到宿主可用列表时，原样返回配置名**
        （见下面 `if not models: return name`）。绝不能因为探测不到就擅自改名，
        那会把本来好好的别名换掉，反而搞坏。

        解析顺序（任一命中就返回）：
          1. 配置名在可用列表里 → 原样用；
          2. 去掉厂商前缀后再匹配（`feiyu-gemini-flash` 的 `gemini-flash` 片段）；
          3. 名字里有相同「家族 + 版本」特征（都含 gemini 且都含 flash）；
          4. 按 MODEL_PREFERENCE_PREFIXES 找同厂可用模型（gemini 优先）；
          5. 全都不行 → 返回空串，表示「不指定模型，交给任务自己的 model_list」。

        返回空串是安全的：宿主会按任务配置的顺序挑，不会全军覆没。
        """
        name = str(wanted or "").strip()
        models = await self._available_models()

        # 拿不到列表：说明不了问题，维持原样（老宿主可能不支持这个能力）。
        if not models:
            return name

        # 宿主给的是任务名清单（不是模型名）：同样说明不了问题，别瞎降级。
        if all(m.lower() in _TASK_NAME_ONLY for m in models):
            return name

        if name and name in models:
            return name

        # 精确不可用，开始按特征找替代。
        if name:
            # 2) 去掉前面的厂商前缀再比：feiyu-gemini-flash → gemini-flash
            tail = name.split("-", 1)[1] if "-" in name else name
            for m in models:
                if m == tail or m.endswith("-" + tail) or tail.endswith("-" + m):
                    self.logger.info("回响：配置的模型 %s 已不可用，改用 %s", name, m)
                    return m

            # 3) 家族 + 版本特征匹配（gemini + flash / glm + flash）
            tokens = [t for t in re.split(r"[-_]", name.lower()) if t]
            family = next((t for t in tokens if t in ("gemini", "glm", "claude", "gpt", "qwen", "deepseek")), "")
            speed = next((t for t in tokens if t in ("flash", "turbo", "mini", "lite", "air")), "")
            if family:
                cands = [m for m in models if family in m.lower()]
                if speed:
                    cands = [m for m in cands if speed in m.lower()] or cands
                if cands:
                    # 版本号大的优先（gemini-3.8-flash > gemini-2.5-flash）
                    best = max(cands, key=self._version_key)
                    self.logger.info("回响：配置的模型 %s 已不可用，按家族匹配到 %s", name, best)
                    return best

        # 4) 按用户偏好顺序找同厂模型。
        for prefix in MODEL_PREFERENCE_PREFIXES:
            cands = [m for m in models if prefix in m.lower()]
            if cands:
                best = max(cands, key=self._version_key)
                self.logger.info(
                    "回响：配置的模型 %s 不可用，按偏好降级到 %s（可用：%s）",
                    name or "（未指定）", best, "、".join(models[:6]),
                )
                return best

        # 5) 实在没有同类模型，交给任务默认顺序。
        self.logger.warning(
            "回响：配置的模型 %s 不可用，且没有可匹配的替代模型（可用：%s），改用任务默认",
            name or "（未指定）", "、".join(models[:6]) or "无",
        )
        return ""

    @staticmethod
    def _version_key(model_name: str) -> tuple[int, ...]:
        """从模型名里抽出版本号，用于「版本大的优先」排序。"""
        return tuple(int(x) for x in re.findall(r"\d+", model_name)) or (0,)

    # ===================== A 类：视觉理解 =====================

    async def describe_image(self, image_url: str, hint: str = "") -> str:
        """让视觉模型看一眼图片，返回中文描述；失败返回空串（绝不抛异常）。

        【为什么需要】小黑盒大量帖子是「图 + 一句话」，正文极短。
        只把标题正文喂给模型，评论只能瞎猜 —— 用户反馈「回复跟帖子没关系」。
        必须先把图里的内容转成文字，再据此写评论。

        【实现约束】
        - 走宿主 `vlm` 任务（model_config.toml 里已声明，本机配的是 glm-4v-flash）；
        - prompt 用消息列表（SDK 契约：`prompt: str | List[Dict]`），
          图片按 OpenAI 兼容的 `image_url` 结构塞进去；
        - 任何失败都吞掉返回空串：识图是增强，不该拖垮评论主流程。
        """
        url = str(image_url or "").strip()
        if not url:
            return ""
        llm = getattr(self.host, "llm", None)
        if llm is None or not hasattr(llm, "generate"):
            return ""
        # 【真机踩坑（2026-11-06）】宿主 LLM 服务只接受 Data URL 形式的图片输入，
        # 直接把 https://... 传进去会报「仅支持 Data URL 形式的图片输入」。
        # 必须先下载成字节、转 base64 data URL 再喂给 vlm。
        data_url = await _image_to_data_url(url)
        if not data_url:
            self.logger.info("回响：图片下载失败（已跳过识图）：%s", url[:80])
            return ""
        ask = (
            "看这张图，用一两句中文说清楚图里有什么。"
            "如果是截图，把关键文字和界面内容也说出来。只描述，不评价。"
        )
        if hint:
            ask += f"\n补充背景：{hint[:100]}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ask},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        try:
            result = await llm.generate(
                messages, task_name="vlm", timeout_ms=DEFAULT_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 —— 识图失败只降级，不冒泡。
            self.logger.info("回响：图片识别失败（已跳过）：%s", exc)
            return ""
        text = _extract_text(result).strip()
        return text[:400]

    # ===================== A 类：黑话（复用宿主自带黑话系统） =====================

    async def look_up_jargon(self, texts: list[str], limit: int = 8) -> str:
        """从**宿主自带的黑话表**里捞与当前帖子相关的黑话，返回「词：含义」提示。

        【为什么不自己建知识库】（用户 2026-11-07 明确指出）
        宿主框架（MaiBot）自带完整黑话系统：`MaiBot.db` 的 `jargons` 表、自动学习
        （`jargon_learner` / `jargon_miner`）、上下文匹配（`jargon_context_matcher`）、
        WebUI 管理页、全局/会话作用域（`is_global`）。再自建一套是重复造轮子，
        且用户在宿主后台增删的黑话我们读不到。

        所以这里只做「读」，不做「写」：
        - 直接查宿主的 `jargons` 表（SQLite 只读打开，不动它的数据）；
        - 优先取 `is_global=1` 的全局黑话（跨会话可见）；
        - 按「词是否出现在帖子文本里」做机械匹配，取出现次数高的前 N 条；
        - 任何异常都返回空串——黑话是增强，不能拖垮评论。

        用户在宿主 WebUI「黑话管理」里加的词，下一轮就能被这里读到。
        """
        blob = "\n".join(str(t) for t in texts if t)
        if not blob.strip():
            return ""
        rows = self._read_jargon_rows()
        if not rows:
            return ""
        # 机械匹配：词出现在帖子文本里才算命中（宿主 matcher 也是同一思路）。
        norm_blob = _normalize_for_match(blob)
        hits: list[tuple[int, str, str]] = []
        for content, meaning, count, is_global in rows:
            key = _normalize_for_match(content)
            if not key or key not in norm_blob:
                continue
            hits.append((int(count or 0) + (1000 if is_global else 0), content, meaning))
        if not hits:
            return ""
        hits.sort(key=lambda x: x[0], reverse=True)
        picked = hits[: max(1, limit)]
        self.logger.info(
            "回响：命中宿主黑话 %d 条（%s）", len(picked), "、".join(h[1] for h in picked)
        )
        return "\n".join(f"{c}：{m}" for _, c, m in picked if m)

    def _read_jargon_rows(self) -> list[tuple[str, str, int, bool]]:
        """只读宿主 `jargons` 表，返回 [(内容, 含义, 次数, 是否全局)]。

        【非侵入】用 SQLite 只读 URI 打开，绝不写宿主库；没有 `is_jargon` 标记的行
        （宿主学出来但判定「不是黑话」的）跳过。

        找不到库 / 表不存在 / 读写冲突，一律返回空列表，由调用方按「没有黑话」降级。
        """
        cached = getattr(self, "_jargon_cache", None)
        if cached is not None:
            return cached
        rows: list[tuple[str, str, int, bool]] = []
        try:
            import sqlite3

            db_path = self._host_db_path()
            if not db_path:
                self._jargon_cache = []
                return []
            # mode=ro：只读；uri=True 才认这个参数。
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT content, meaning, count, is_global FROM jargons "
                    "WHERE meaning IS NOT NULL AND TRIM(meaning) != '' "
                    "AND (is_jargon IS NULL OR is_jargon = 1) "
                    "ORDER BY is_global DESC, count DESC LIMIT 200"
                )
                for content, meaning, count, is_global in cur.fetchall():
                    c = str(content or "").strip()
                    m = str(meaning or "").strip()
                    if c and m:
                        rows.append((c, m, int(count or 0), bool(is_global)))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 —— 黑话读不到不影响主流程。
            self.logger.debug("回响：读取宿主黑话表失败（跳过黑话）：%s", exc)
            rows = []
        self._jargon_cache = rows
        return rows

    def _host_db_path(self) -> str:
        """定位宿主 MaiBot.db。找不到返回空串。"""
        # 数据目录形态：<root>/data/plugins/<plugin>，宿主库在 <root>/data/MaiBot.db
        try:
            data_dir = Path(self.paths.data_dir)
            for parent in (data_dir, data_dir.parent, data_dir.parent.parent, data_dir.parent.parent.parent):
                cand = parent / "MaiBot.db"
                if cand.is_file():
                    return str(cand)
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ===================== B 类：小黑盒直连客户端 =====================

    async def fetch_existing_comments(self, post_id: str, limit: int = 8) -> list[str]:
        """拉这条帖子下别人已有的评论（供评论生成参考「大家在聊什么」）。

        【用户要求 2026-11-07】「你再看一下其他人评论的啥，你再结合一下」——
        不看评论区就永远在自说自话。真机实测评论区多是
        `来了来了[cube_喜欢]`、`看看可以`、`没搭子` 这种短句 + 表情。
        """
        from layers import inbound

        return await inbound.fetch_existing_comments(self, post_id, limit=limit)

    @property
    def xhh_client(self) -> Any:
        """懒加载小黑盒直连客户端（CommunityClient）。

        未登录 / 缺依赖时返回 None，由各层按「没有客户端」降级处理，
        不让「没登录」把整个插件加载搞崩。
        """
        if self._xhh_client is not None or self._xhh_error:
            return self._xhh_client
        try:
            from auth import load_credentials
            from community_client import CommunityClient

            credentials = load_credentials(self.paths.data_dir)
            self._xhh_client = CommunityClient(
                profile_id=str(self.config.basic.profile_id or "default"),
                credentials=credentials,
            )
        except Exception as exc:  # noqa: BLE001 —— 未登录 / 缺 httpx 都只是「暂无客户端」。
            self._xhh_error = f"{type(exc).__name__}: {exc}"
            self._xhh_client = None
            self.logger.info("回响：小黑盒客户端未就绪（%s），需要先扫码登录。", self._xhh_error)
        return self._xhh_client

    def reset_xhh_client(self) -> None:
        """登录成功后清掉缓存，下次访问重建客户端。"""
        self._xhh_client = None
        self._xhh_error = ""


async def _image_to_data_url(url: str) -> str:
    """把图片 URL 下载并转成 base64 Data URL，失败返回空串。

    【真机踩坑（2026-11-06）】宿主 LLM 只接受 Data URL 形式的图片，
    直接传 https 地址会报「仅支持 Data URL 形式的图片输入」。
    这里负责「下载 + 猜 mime + base64」三步，任何一步失败都返回空串，
    让上层按「没图」降级，绝不拖垮评论主流程。
    """
    if url.startswith("data:"):
        return url
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        import base64

        import httpx  # 延迟导入：识图才需要，纯逻辑测试不依赖它。
    except Exception:  # noqa: BLE001 —— 缺 httpx 就放弃识图。
        return ""

    mime = "image/jpeg"
    low = url.split("?", 1)[0].lower()
    for ext, m in (
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".jpeg", "image/jpeg"),
        (".jpg", "image/jpeg"),
    ):
        if low.endswith(ext):
            mime = m
            break
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            data = resp.content
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    except Exception:  # noqa: BLE001 —— 下载失败按没图处理。
        return ""
    if not data or len(data) > 8 * 1024 * 1024:  # 空图 / 超过 8MB 都不要。
        return ""
    if content_type.startswith("image/"):
        mime = content_type
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _normalize_for_match(text: str) -> str:
    """黑话机械匹配用的归一化：转小写、去空白和常见标点。

    与宿主 `jargon_context_matcher._normalize_match_text` 同思路：
    让「博启！」和「博启」都能匹配上。
    """
    t = str(text or "").lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[，。！？、；：\"'“”‘’（）()\[\]【】《》<>~!?,.;:|\-_/\\]+", "", t)
    return t


def _extract_text(result: Any) -> str:
    """从宿主 llm.generate 的返回里取正文。

    真实 SDK 返回 dict，常见字段：text / content / response / message / answer。
    兼容 dict 里再套一层 data / result。
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result or "")
    for key in ("text", "content", "response", "answer", "message", "output"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("data", "result"):
        nested = result.get(key)
        if isinstance(nested, (dict, str)):
            text = _extract_text(nested)
            if text.strip():
                return text
    return ""


__all__ = ["Runtime", "_Paths", "_ApiProxy"]
