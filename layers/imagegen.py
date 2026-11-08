# -*- coding: utf-8 -*-
"""配图生图：直连 OpenAI 兼容生图接口，或走麦麦绘图插件 API。

【来源】源③ 二创小黑盒的 `image_generation.py` + `drawpic_gateway.py` 直接搬入，
只改导入路径与参数解析（复用本插件 `[image]` 配置）。

【两条路径，按顺序尝试】
1. 直连生图（优先）：OpenAI 兼容 `POST {base_url}/images/generations`。
   参数来自 `[image] image_api_base_url / image_api_key / image_model`；
   三者留空则回退读 MaiBot 主配置 `model_config.toml` 里 `api_provider == "画图"` 的 provider。
2. 麦麦绘图插件（回退）：走 `ctx.api.call("maimai-drawpic-plugin.*")`，
   submit → wait → read_image。

【降级】任一路径失败都抛 / 返回空，由上层降级为纯文字发帖，绝不阻塞发帖闭环。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from layers._cfg import read as _read, section as _section

logger = logging.getLogger(__name__)


class ImageGenerationError(RuntimeError):
    """生图失败，调用方应降级为纯文字发帖，而不是抛给上层。"""


# ===================== 直连生图（OpenAI 兼容 /images/generations） =====================

class DirectImageGenerator:
    """OpenAI 兼容生图，封装成异步接口（urllib 实现，零额外依赖）。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, size: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.size = (size or "").strip()

    async def generate(self, prompt: str, *, size: str = "") -> bytes:
        """生成一张图，返回图片字节。失败抛 ImageGenerationError。

        :param size: 覆盖尺寸（如 `3840x2160`）。留空用构造时配置的 size。
            不支持尺寸的模型会报错，调用方负责降级重试（见 generate_image_bytes）。
        """
        if not self.base_url or not self.api_key or not self.model:
            raise ImageGenerationError("生图配置不完整：需要 base_url / api_key / model")

        url = self.base_url + "/images/generations"
        wanted = (size or self.size or "").strip()
        payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "n": 1}
        if wanted:
            payload["size"] = wanted

        def _sync_call() -> bytes:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise ImageGenerationError(f"生图接口返回 {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError) as exc:
                raise ImageGenerationError(f"生图请求失败: {type(exc).__name__}") from exc

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ImageGenerationError("生图接口返回非 JSON") from exc

            items = data.get("data") or []
            if not items:
                raise ImageGenerationError(f"生图接口没有返回图片数据: {raw[:300]}")
            item = items[0]
            if item.get("b64_json"):
                try:
                    return base64.b64decode(item["b64_json"])
                except Exception as exc:
                    raise ImageGenerationError("图片 base64 解码失败") from exc
            if item.get("url"):
                try:
                    with urllib.request.urlopen(item["url"], timeout=180) as img_resp:
                        return img_resp.read()
                except (urllib.error.URLError, OSError) as exc:
                    raise ImageGenerationError(f"下载生成图片失败: {type(exc).__name__}") from exc
            raise ImageGenerationError("生图返回里既没有 b64_json 也没有 url")

        return await asyncio.get_running_loop().run_in_executor(None, _sync_call)


def build_generator(
    base_url: str, api_key: str, model: str, size: str = ""
) -> DirectImageGenerator | None:
    """三个参数都填了才返回生成器，否则返回 None。"""
    if base_url and api_key and model:
        return DirectImageGenerator(base_url=base_url, api_key=api_key, model=model, size=size)
    return None


# ===================== 麦麦绘图插件 API（回退路径） =====================

class DrawingGateway:
    """只通过 maimai-drawpic-plugin 的正式 API 调用生图。"""

    def __init__(self, api: Any) -> None:
        self.api = api

    async def submit(
        self,
        prompt: str,
        *,
        request_id: str,
        model: str = "",
        source_images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        encoded = [base64.b64encode(item).decode("ascii") for item in (source_images or [])]
        result = await self.api.call(
            "maimai-drawpic-plugin.drawpic_submit_v2",
            version="2",
            prompt=prompt,
            request_id=request_id,
            model=model,
            task_type="edit_image" if encoded else "draw",
            source_images=encoded,
            stream_id="",
        )
        if isinstance(result, dict):
            payload = result.get("result") if isinstance(result.get("result"), dict) else result
            if result.get("success") is True or result.get("accepted") is True:
                if _find_task_id(payload):
                    return payload
        if not isinstance(result, dict) or result.get("success") is not True:
            recovered = await self.api.call(
                "maimai-drawpic-plugin.drawpic_request_v2",
                version="2",
                request_id=request_id,
            )
            if isinstance(recovered, dict):
                payload = recovered.get("result") if isinstance(recovered.get("result"), dict) else recovered
                if _find_task_id(payload):
                    return payload
            raise RuntimeError("绘图任务提交失败且找回任务失败")
        return result.get("result") if isinstance(result.get("result"), dict) else result

    async def wait(self, task_id: str, *, request_id: str, timeout_seconds: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            response = await self.api.call(
                "maimai-drawpic-plugin.drawpic_task_v2",
                version="2",
                task_id=task_id,
            )
            payload = _unwrap_payload(response)
            status = str(payload.get("status", "")).casefold()
            if status == "completed" and not payload.get("images"):
                await asyncio.sleep(2)
                continue
            if status in {"completed", "failed", "rejected", "cancelled", "unconfirmed"}:
                return payload
            await asyncio.sleep(2)
        return {"status": "pending", "task_id": task_id, "request_id": request_id}

    async def read_image(self, task_id: str, *, index: int = 0) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        deadline = time.monotonic() + 120
        while True:
            if time.monotonic() >= deadline or len(chunks) >= 256:
                raise RuntimeError("绘图图片分块读取超时")
            response = await self.api.call(
                "maimai-drawpic-plugin.drawpic_image_v2",
                version="2",
                task_id=task_id,
                index=index,
                kind="original",
                offset=offset,
            )
            payload = _unwrap_payload(response)
            if not isinstance(payload, dict):
                raise RuntimeError("绘图图片读取响应无效")
            error = payload.get("error")
            if error:
                code = error.get("code") if isinstance(error, dict) else str(error)
                raise RuntimeError(f"绘图图片读取失败:{code}")
            encoded = str(payload.get("data", ""))
            if not encoded:
                raise RuntimeError("绘图图片读取返回空分块")
            data = base64.b64decode(encoded, validate=True)
            chunks.append(data)
            if bool(payload.get("done")):
                return b"".join(chunks)
            next_offset = int(payload.get("next_offset", offset + len(data)))
            if next_offset <= offset:
                raise RuntimeError("绘图图片读取偏移未前进")
            offset = next_offset


def _find_task_id(value: object) -> str:
    if isinstance(value, dict):
        for key in ("task_id", "taskId", "id"):
            item = value.get(key)
            if item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _find_task_id(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_task_id(item)
            if found:
                return found
    return ""


def _unwrap_payload(value: object) -> object:
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return current
        if any(key in current for key in ("status", "task_id", "images", "data", "error")):
            return current
        next_value = None
        for key in ("result", "data", "payload", "task"):
            candidate = current.get(key)
            if isinstance(candidate, dict):
                next_value = candidate
                break
        if next_value is None:
            return current
        current = next_value
    return current


# ===================== 参数解析与主流程 =====================

def _image_cfg(ctx: Any) -> tuple[str, str, str, str]:
    """从插件 [image] 段读 base_url / api_key / model / size（三种对象形态都认）。"""
    sec = _section(ctx, "image")
    base = str(_read(sec, "image_api_base_url", "") or "").strip()
    key = str(_read(sec, "image_api_key", "") or "").strip()
    model = str(_read(sec, "image_model", "") or "").strip()
    size = str(_read(sec, "image_size", "") or "").strip()
    return base, key, model, size


def _main_config_candidates(ctx: Any) -> list[Path]:
    """MaiBot 主配置 model_config.toml 的候选路径。"""
    cands: list[Path] = [Path(r"E:\mai\config\model_config.toml")]
    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    if data_dir:
        d = Path(data_dir)
        for parent in (d, d.parent, d.parent.parent, d.parent.parent.parent):
            cands.append(parent / "config" / "model_config.toml")
    return cands


def _generator_from_main_config(ctx: Any) -> DirectImageGenerator | None:
    """回退：从 MaiBot 主配置里找 api_provider == "画图" 的中转站。"""
    try:
        import tomllib
    except ImportError:  # pragma: no cover —— Python < 3.11 无内置 tomllib。
        return None
    for config_path in _main_config_candidates(ctx):
        try:
            if not config_path.exists():
                continue
            cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        provs = {str(x.get("name")): x for x in (cfg.get("api_providers") or [])}
        for m in (cfg.get("models") or []):
            if str(m.get("api_provider")) == "画图":
                prov = provs.get("画图") or {}
                base = str(prov.get("base_url") or "").rstrip("/")
                key = str(prov.get("api_key") or "")
                model = str(m.get("model_identifier") or m.get("name") or "")
                gen = build_generator(base, key, model)
                if gen is not None:
                    return gen
    return None


def resolve_generator(ctx: Any) -> DirectImageGenerator | None:
    """解析生图生成器：优先插件配置，其次 MaiBot 主配置「画图」provider。"""
    base, key, model, size = _image_cfg(ctx)
    gen = build_generator(base, key, model, size)
    if gen is not None:
        return gen
    return _generator_from_main_config(ctx)


# 兜底：模型没给出画面描述时，只把帖子本身交给生图模型，让它自己看着画。
# 不预设场景、不预设道具、不预设风格 —— 用户要的是「按人设自己来」，
# 兜底也不该偷偷换成我写的模板。
def _fallback_scene(title: str, body: str) -> str:
    """模型没给出画面描述时的兜底：直接给帖子内容，让生图模型自己判断画面。"""
    head = str(title or "").strip()
    text = str(body or "").strip()
    if head and text:
        return f"这条帖子的标题是「{head}」，内容大概是：{text[:300]}。给它配一张图。"
    if head:
        return f"这条帖子的标题是「{head}」。给它配一张图。"
    if text:
        return f"这条帖子的内容是：{text[:300]}。给它配一张图。"
    return "配一张日常生活的照片。"


def build_image_prompt(title: str, body: str = "", *, scene: str = "") -> str:
    """组装配图提示词：把模型写的画面描述原样交给生图模型，**不追加任何限制**。

    【规则由用户定，代码不加任何创作限制】
    用户 2026-11-08 原话：「规则是我定的，我定的规则就是让他按照他自己的人设来生成照片
    就行了，想怎么发怎么发，只要不凭空捏造就行了。」

    所以我之前自作主张加的这些东西全部删除，一条不留：
    「严禁出现任何文字」「不要插画感 / 不要 3D 渲染」「必须模糊到看不清」
    「自然光 / 轻微噪点」「不要画成帖子截图」……

    注意「不要画成截图」也不是用户定的 —— 那是我把「不凭空捏造」自己翻译成了
    具体的画面禁令，等于又替用户定了一条规矩。用户说的「不凭空捏造」是
    **内容层面**（别写没刷到的帖子、别编造事件），由正文的接地检查
    `summary_grounding_issues` 负责，跟配图画成什么样无关。

    这里只做一件事：把描述拼成提示词，原样送出去。

    :param scene: 模型按人设与正文写的画面描述；为空时用关键词兜底。
    """
    return str(scene or "").strip() or _fallback_scene(title, body)


# ===================== 让模型按内容 / 人设自己想画面 =====================

_SCENE_PROMPT = (
    "{persona}"
    "你在小黑盒刚发了这条帖子：\n"
    "标题：{title}\n"
    "正文：{body}\n"
    "\n现在给自己这条帖子配一张图。**你想拍什么就拍什么，想怎么写就怎么写**，"
    "按你自己的性子来，没有固定套路，也不用管别人怎么配。\n"
    "把画面描述清楚，让生图模型照着就能画出来。想写多长写多长，"
    "两百字可以，两千字也行，越具体越好。\n"
    "只写画面本身，不要解释你为什么这么配，不要写「配图：」。"
)


def _bot_config_candidates(ctx: Any) -> list[Path]:
    """宿主 bot_config.toml 的候选路径（人设 / 昵称都在这里）。

    【真机缺陷 2026-11-08】原来复用 `_main_config_candidates`，那是给生图 provider
    用的、只含 model_config.toml，于是 `[personality]` 永远读不到 ——
    用户明明配了「阿水」的人设，配图提示词里却只剩一个昵称。
    """
    cands: list[Path] = [Path(r"E:\mai\config\bot_config.toml")]
    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    if data_dir:
        d = Path(data_dir)
        for parent in (d, d.parent, d.parent.parent, d.parent.parent.parent):
            cands.append(parent / "config" / "bot_config.toml")
    return cands


def _persona_hint(ctx: Any) -> str:
    """从宿主 bot_config.toml 读昵称与人格设定（读不到就留空，不编造）。"""
    try:
        import tomllib
    except ImportError:  # pragma: no cover —— Python < 3.11 无内置 tomllib。
        return ""
    nickname = ""
    personality = ""
    reply_style = ""
    for path in _bot_config_candidates(ctx):
        try:
            if not path.exists():
                continue
            cfg = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        nickname = str(cfg.get("nickname") or "").strip()
        section = cfg.get("personality") or {}
        if isinstance(section, dict):
            personality = str(section.get("personality") or "").strip()
            reply_style = str(section.get("reply_style") or "").strip()
            if not nickname:
                nickname = str(section.get("nickname") or "").strip()
        if nickname or personality:
            break
    # 宿主没填人格时，退回小黑盒账号昵称 + 插件里的语气配置（不臆造人格）。
    if not nickname:
        nickname = _xhh_nickname(ctx)
    if not (nickname or personality):
        return ""
    bits = []
    if nickname:
        bits.append(f"你在这个号上叫「{nickname}」。")
    if personality:
        bits.append(f"你的人格设定：{personality[:600]}")
    if reply_style:
        bits.append(f"你平时说话的样子：{reply_style[:200]}")
    tone = _plugin_tone(ctx)
    if tone:
        bits.append(f"整体调子偏「{tone}」，日常生活感，不摆拍。")
    return "".join(bits) + "\n"


def _plugin_tone(ctx: Any) -> str:
    """插件 [create].tone（如「平和」），读不到返回空串。"""
    try:
        return str(_read(_section(ctx, "create"), "tone", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _xhh_nickname(ctx: Any) -> str:
    """从小黑盒凭据里读账号昵称（只取 nickname，不碰任何 token）。"""
    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    if not data_dir:
        return ""
    try:
        payload = json.loads((Path(data_dir) / "credentials.json").read_text(encoding="utf-8"))
        return str(payload.get("nickname") or "").strip()
    except Exception:  # noqa: BLE001 —— 没登录 / 文件损坏都只是没有人设，不影响生图。
        return ""


# 保留这个名字只是为了兼容旧引用；不再用它截断任何内容。
# 用户 2026-11-08：「他描述 2000 个字也行啊……你怎么老想着做限制呢？」
DEFAULT_SCENE_CHARS = 0


async def describe_scene(ctx: Any, *, title: str, body: str) -> str:
    """让模型按人设写画面描述；失败返回空串走兜底。

    【2026-11-08 用户两次纠正「别老想着做限制」】
    第一版限制 80 字一句话 → 用户要详细；我改成 120–220 字并给了一份
    「必须写清镜头/主体/环境/光线」的清单 → 用户：「描述一定要加限制吗？
    他描述 2000 个字也行啊……你怎么老想着做限制呢？」
    所以现在：不设字数上限，不给清单，模型愿意写多长写多长。
    这里只做最必要的清洗（去掉「配图：」前缀、去掉它在解释自己的部分）。
    """
    text = f"{title} {body}".strip()
    if not text:
        return ""
    generate = getattr(ctx, "generate_text", None)
    if not callable(generate):
        return ""
    prompt = _SCENE_PROMPT.format(
        title=str(title)[:120],
        body=str(body)[:800],
        persona=_persona_hint(ctx),
    )
    try:
        raw = generate(prompt)
        if hasattr(raw, "__await__"):
            raw = await raw
    except Exception:  # noqa: BLE001 —— 生图描述失败不该阻塞发帖。
        return ""
    out = str(raw or "").replace("\r\n", "\n").strip()
    # 模型偶尔会写「配图：xxx」或加引号，剥掉外壳。
    out = re.sub(r"^(配图|画面|图片|场景)\s*[:：]\s*", "", out)
    out = out.strip(" \"'“”「」").strip()
    # 出现这些说明它在拒绝或解释，不是画面描述。
    if not out or any(w in out for w in ("抱歉", "无法", "不能提供", "作为AI")):
        return ""
    return out


async def generate_image_bytes(
    ctx: Any,
    *,
    title: str,
    body: str,
    request_id: str = "",
    model: str = "",
) -> bytes | None:
    """为帖子生成一张配图，返回图片字节；失败返回 None（调用方降级纯文字）。

    优先直连生图，其次麦麦绘图插件。
    """
    # 先让模型看着这条帖子自己想画面（按内容 + 人设），再套技术约束。
    # 这样每条帖子的配图都不一样，而不是三张同款的「书桌 + 手机」。
    scene = await describe_scene(ctx, title=title, body=body)
    prompt = build_image_prompt(title, body, scene=scene)

    # 1) 直连生图。目标尺寸失败（模型不支持该分辨率）就退到 2K 再试一次，
    #    再不行才落到麦麦绘图 —— 不能因为「4K 不被支持」就一张图都出不来。
    gen = resolve_generator(ctx)
    if gen is not None:
        tried = [gen.size] if gen.size else []
        for size in tried + ["", FALLBACK_SIZE]:
            if size and size in tried[1:]:
                continue
            try:
                data = await gen.generate(prompt, size=size)
                if data:
                    return _fit_upload(data)
            except ImageGenerationError as exc:
                logger.warning("回响：生图失败（size=%s）：%s", size or "默认", exc)
                continue

    # 2) 麦麦绘图插件。
    api = getattr(ctx, "api", None)
    if api is None or not hasattr(api, "call"):
        return None
    rid = request_id or f"hx_{int(time.time() * 1000)}"
    try:
        gateway = DrawingGateway(api)
        task = await gateway.submit(prompt, request_id=rid, model=model)
        task_id = _find_task_id(task)
        if not task_id:
            return None
        finished = await gateway.wait(task_id, request_id=rid)
        if str(finished.get("status", "")).casefold() != "completed":
            return None
        return _fit_upload(await gateway.read_image(task_id))
    except Exception:
        return None


# 小黑盒图床上限 5 MB，4K 生图（实测 2880×2880 PNG 约 6.4 MB）必须压一下。
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
FALLBACK_SIZE = "2048x2048"
# 压缩时的质量阶梯：先保住画质，实在压不下来再降。
_JPEG_QUALITIES = (92, 86, 80, 72)


def _fit_upload(data: bytes) -> bytes:
    """把图片压到图床能收的大小（≤5 MB）；压不动就原样返回，交给上层降级。

    【真机缺陷 2026-11-08 用户换了 4K 生图模型】
    4K 输出的 PNG 有 6 MB 多，而小黑盒图床单张上限 5 MB —— 不压就直接上传失败，
    表现是「明明生成了图，帖子里却没图」。转 JPEG（丢掉 alpha）质量 92 通常
    落到 1 MB 左右，肉眼看不出差别。
    """
    if not data or len(data) <= MAX_UPLOAD_BYTES:
        return data
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            for quality in _JPEG_QUALITIES:
                buf = BytesIO()
                rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
                out = buf.getvalue()
                if len(out) <= MAX_UPLOAD_BYTES:
                    logger.info(
                        "回响：配图过大（%.1f MB），已压到 %.1f MB（JPEG q%d）。",
                        len(data) / 1048576,
                        len(out) / 1048576,
                        quality,
                    )
                    return out
    except Exception as exc:  # noqa: BLE001 —— 压缩失败不该让整张图作废。
        logger.warning("回响：配图压缩失败（已忽略）：%s", exc)
    return data


__all__ = [
    "ImageGenerationError",
    "DirectImageGenerator",
    "DrawingGateway",
    "build_generator",
    "build_image_prompt",
    "describe_scene",
    "resolve_generator",
    "generate_image_bytes",
]
