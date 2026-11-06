# -*- coding: utf-8 -*-
"""
回响插件：创作层（六块第三块，落地版）。

【职责边界】
据感知层给的状态与话题产出「草稿」，但不决定能不能发（那是护栏层的事）：
- 生成帖子 / 评论 / 感想总结（A 类宿主能力，走 ctx 注入的生成器）；
- 中文口语清洗（去楼层词、破折号转逗号、去句号、段间不空行）；
- AI 味打分（0–100，直译 MCP aiflavor.ts）；
- 配图生成（1~N 张），以及生图失败时降级为纯文字帖。

【复用约定】
生成与生图通过 `ctx` 注入的生成器复用（优先 `ctx.generate_text` / `ctx.generate_image`，
否则 `ctx.api.call(...)`）；口语清洗与 AI 味打分是纯本地文本处理，不联网。

【AI 味打分口径（直译 MCP aiflavor.ts scoreText，阈值对齐 contracts.py）】
评论 ≥45 判机器、正文 ≥60 判机器。返回 0–100 的 float。
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Awaitable, Callable, Union

from models import (
    CommentTarget,
    ImageAsset,
    ImageStatus,
    LifeTrace,
    LinkTag,
    PostDraft,
    SummaryDraft,
    Topic,
)
from layers._cfg import read as _read, read_str as _read_str, section as _section

# ===================== AI 味词表（直译 aiflavor.ts） =====================

SPEC_WORDS = (
    "必须", "不许", "白名单", "校验", "剔除", "控制在", "阈值", "维度", "机制",
    "配置", "参数", "规范", "层面", "范畴", "换言之", "本质上", "关键在于",
)
BOOK_WORDS = (
    "受着", "就算", "算不上", "究竟", "毕竟", "某种程度", "之类", "而已", "罢了",
    "颇", "所谓", "固然", "无疑", "必然", "终究", "维持", "或许", "恐怕",
)
REASON_WORDS = ("其实", "说白了", "说到底", "总的来说", "总之", "因此", "从而")
TIC_WORDS = ("我认", "认了", "行吧", "行啊", "反正", "算了", "那就不", "别急", "倒也", "就这")
POLITE_WORDS = (
    "感谢分享", "谢谢分享", "感谢你的", "谢谢你的", "很高兴", "希望对你有帮助",
    "希望这些", "一起加油", "共勉", "祝你好运", "加油哦", "期待你的",
    "如果有任何", "随时告诉我", "随时问我", "欢迎交流",
)
EMPTY_WORDS = (
    "值得思考", "很有意义", "非常有价值", "发人深省", "引人深思",
    "见仁见智", "各有各的", "因人而异", "看个人", "视情况而定",
    "总的来说", "总体而言", "各方面", "多角度",
)
SELF_DX = (
    "工具的描述", "工具描述", "人设卡", "上下文窗口", "上下文长度", "系统提示",
    "token数", "token 数", "我的参数", "我的配置", "模型参数", "推理速度", "显存",
    "上下文挤", "窗口大", "prompt", "Prompt",
)
FLOOR_WORDS = ("几楼", "楼说", "楼主", "楼上", "楼下", "沙发", "前排", "板凳", "顶帖", "楼层")

# ===================== 中文口语清洗 =====================

# 行首楼层词（1楼 / 二楼 / #3 / 楼主 / 沙发 …）。
_FLOOR_LINE_RE = re.compile(
    r"(?m)^\s*(?:第?[0-9一二三四五六七八九十百]+\s*(?:楼|层|層)|#[0-9]+|"
    r"楼主|楼上|楼下|沙发|前排|板凳|顶帖|楼层)\s*[:：、．.]?\s*"
)
# 各种破折号（含连续多个）统一转一个中文逗号。
_DASH_RE = re.compile(r"[—–―]+")
# 句末句号。
_PERIOD_RE = re.compile(r"。")
# 段间连续空行压成单换行（段间不留空行）。
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def polish_chinese(text: str) -> str:
    """中文口语清洗（纯本地，不联网）。

    规则：去楼层词 / 破折号转逗号 / 去句号 / 段间不空行。
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _FLOOR_LINE_RE.sub("", value)
    value = _DASH_RE.sub("，", value)
    value = _PERIOD_RE.sub("", value)
    value = _BLANK_LINE_RE.sub("\n", value)
    return value.strip()


# ===================== AI 味打分（直译 aiflavor.ts） =====================

def ai_flavor_score(text: str) -> float:
    """AI 味打分（0–100，越高越像机器）。纯本地，直译 MCP aiflavor.ts scoreText。"""
    t = str(text or "")
    n = len(t)
    score = 0.0

    def _hits(words: tuple[str, ...]) -> list[str]:
        return [w for w in words if w in t]

    spec = _hits(SPEC_WORDS)
    if spec:
        score += min(30, 12 * len(spec))
    book = _hits(BOOK_WORDS)
    if book:
        score += min(24, 10 * len(book))
    reason = _hits(REASON_WORDS)
    if reason:
        score += min(20, 10 * len(reason))
    if _hits(SELF_DX):
        score += 45
    floor = _hits(FLOOR_WORDS)
    if floor:
        score += min(60, 45 * len(floor))
    tic = _hits(TIC_WORDS)
    if tic:
        score += min(24, 10 * len(tic))
    polite = _hits(POLITE_WORDS)
    if polite:
        score += min(40, 20 * len(polite))
    empty = _hits(EMPTY_WORDS)
    if empty:
        score += min(30, 15 * len(empty))
    if "——" in t:
        score += 12
    if t.count("，") >= 4:
        score += 10
    if n > 20 and not re.search(r"[，。？！、；]", t):
        score += 25
    if re.search(r"\n\s*\n", t):
        score += 20
    return min(100.0, score)


# ===================== 降级 =====================

async def fallback_text_only(draft: PostDraft) -> PostDraft:
    """生图失败降级：丢弃所有配图，转为纯文字帖（图文 27 → 动态 28）。"""
    draft.images = []
    if draft.link_tag is LinkTag.IMAGE_TEXT:
        draft.link_tag = LinkTag.DYNAMIC
    return draft


# ===================== 生成能力（A 类，走 ctx 注入） =====================

def _maybe_await(value: Union[str, Awaitable[str]]) -> "Awaitable[str]":
    import asyncio

    if inspect.isawaitable(value):
        async def _run() -> str:
            return str(await value)

        return _run()

    async def _identity() -> str:
        return str(value)

    return _identity()


async def _generate_text(ctx: Any, prompt: str) -> str:
    """经宿主 LLM 生成文本：优先 ctx.generate_text，否则 ctx.api.call。"""
    generator: Any = getattr(ctx, "generate_text", None)
    if callable(generator):
        return await _maybe_await(generator(prompt))
    api: Any = getattr(ctx, "api", None)
    if api is not None and hasattr(api, "call"):
        result = await api.call("maisaka.reply.generate", prompt=prompt)
        if isinstance(result, dict):
            result = result.get("text", result.get("content", result))
        return str(result)
    raise NotImplementedError(
        "创作层需要宿主大模型能力：请提供 ctx.generate_text(prompt) 或 ctx.api.call 能力"
    )


async def _generate_images(ctx: Any, prompt: str, count: int) -> list[str]:
    """经宿主生图能力生成 count 张配图，返回本地路径列表。"""
    generator: Any = getattr(ctx, "generate_image", None)
    if callable(generator):
        paths: list[str] = []
        for _ in range(max(1, count)):
            paths.append(str(await _maybe_await(generator(prompt))))
        return paths
    api: Any = getattr(ctx, "api", None)
    if api is not None and hasattr(api, "call"):
        result = await api.call("host.image.generate", prompt=prompt, count=count)
        if isinstance(result, dict):
            result = result.get("paths", result.get("images", result))
        if isinstance(result, list):
            return [str(p) for p in result]
    return []


def _split_title_body(raw: str) -> tuple[str, str]:
    """从生成文本拆标题与正文：首行标题，其余为正文。"""
    lines = [line.strip() for line in str(raw or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""
    if len(lines) == 1:
        return lines[0][:30], lines[0]
    title = lines[0][:30]
    body = "\n".join(lines[1:])
    return title, body


# ===================== 帖子 / 评论 / 总结生成 =====================

def _post_prompt(topic: Topic, trace: LifeTrace, tone: str) -> str:
    topic_name = topic.name or "日常"
    return (
        f"你是小黑盒社区的一个普通用户，用「{tone}」的语气，围绕「{topic_name}」随手写一条帖子。\n"
        "要求：说人话、口语化，别讲道理，别解释自己怎么想的，别客套，别用破折号，别空行分段，"
        "别用「首先/其次/总之」这类套话。\n"
        "输出格式：第一行是标题（30 字以内），从第二行开始是正文，正文 2 段以上。"
    )


async def generate_post(ctx: Any, topic: Topic, trace: LifeTrace) -> PostDraft:
    """生成一条帖子草稿（标题 + 口语化正文 + AI 味打分）。"""
    tone = _read_str(_section(ctx, "create"), "tone", "平和")
    raw = await _generate_text(ctx, _post_prompt(topic, trace, tone))
    title, body = _split_title_body(raw)
    body = polish_chinese(body)
    draft = PostDraft(
        title=title,
        text=body,
        link_tag=LinkTag.IMAGE_TEXT,
        topic_ids=[topic.id] if topic.id else [],
        polished=True,
    )
    draft.ai_flavor_score = ai_flavor_score(body)
    return draft


async def generate_comment(ctx: Any, target: CommentTarget, trace: LifeTrace) -> str:
    """生成一条评论文本（口语化短评）。"""
    prompt = (
        "你在小黑盒上看到一条帖子，随手回一句短评。要短、要口语、像真人聊天，"
        "别讲道理、别客套、别用破折号，30 字以内。"
    )
    raw = await _generate_text(ctx, prompt)
    return polish_chinese(str(raw))


async def generate_images(ctx: Any, draft: PostDraft, count: int = 1) -> list[ImageAsset]:
    """为帖子草稿生成配图（1~N 张），返回 ImageAsset 列表。"""
    prompt = f"为这条帖子配图：{draft.title} {draft.text[:120]}"
    paths = await _generate_images(ctx, prompt, count)
    assets: list[ImageAsset] = []
    for path in paths:
        if path:
            assets.append(ImageAsset(local_ref=path, status=ImageStatus.PENDING))
    return assets


# ===================== 感想总结：字数 / 段数收敛（纯逻辑，可测） =====================

# 中文句读：截断与拆段都优先落在这些字符后面，避免句子断一半。
_SENTENCE_BREAKS = "，、；：,;:！？!?"


def count_chars(text: str) -> int:
    """统计中文字数（按非空白字符计，标点也算，符合「200–400 字」的口语口径）。"""
    return sum(1 for ch in str(text or "") if not ch.isspace())


def count_paragraphs(text: str) -> int:
    """统计段数（按换行切分，只数非空段）。"""
    return len([p for p in re.split(r"\n+", str(text or "")) if p.strip()])


def clamp_chars(text: str, max_chars: int) -> str:
    """把正文压到 max_chars 以内；尽量切在句读处，不把句子砍半。"""
    if max_chars <= 0:
        return ""
    value = str(text or "")
    if count_chars(value) <= max_chars:
        return value
    out: list[str] = []
    counted = 0
    for ch in value:
        if not ch.isspace():
            counted += 1
        if counted > max_chars:
            break
        out.append(ch)
    truncated = "".join(out)
    # 从尾部往前找最近的句读，最多回退 40 字，避免整段只剩标点。
    floor = max(0, len(truncated) - 40)
    for idx in range(len(truncated) - 1, floor, -1):
        if truncated[idx] in _SENTENCE_BREAKS:
            return truncated[: idx + 1].strip()
    return truncated.strip()


def _split_once(paragraph: str) -> tuple[str, str] | None:
    """把一段按中间位置的句读拆成两段；整段没有标点时按中点硬拆。"""
    text = paragraph.strip()
    if len(text) < 20:
        return None
    mid = len(text) // 2
    # 先找中点右侧最近的句读，再找左侧。
    for idx in range(mid, len(text) - 1):
        if text[idx] in _SENTENCE_BREAKS and idx + 1 < len(text):
            return text[: idx + 1].strip(), text[idx + 1 :].strip()
    for idx in range(mid - 1, 0, -1):
        if text[idx] in _SENTENCE_BREAKS:
            return text[: idx + 1].strip(), text[idx + 1 :].strip()
    # 整段没标点（或只有开头/结尾有）：按中点硬拆，保证段数下限。
    return text[:mid].strip(), text[mid:].strip()


def fit_summary_shape(
    text: str,
    min_chars: int = 200,
    max_chars: int = 400,
    min_paragraphs: int = 2,
    max_paragraphs: int = 4,
) -> str:
    """把总结正文收敛到「字数 / 段数」约束内（纯逻辑，可测）。

    规则：
    - 超长按句读截到 max_chars 以内；
    - 段数多于 max_paragraphs：保留前 max-1 段，其余并入最后一段；
    - 段数少于 min_paragraphs：反复把最长段按句读拆开，拆不动就停。
    字数下限无法靠裁剪补齐，由调用方重试生成（本函数只管「不超上限、段数落区间」）。
    """
    paragraphs = [p.strip() for p in re.split(r"\n+", str(text or "")) if p.strip()]
    if not paragraphs:
        return ""
    paragraphs = [clamp_chars(p, max_chars) for p in paragraphs]

    # 段数过多：多出来的并进最后一段。
    if max_paragraphs > 0 and len(paragraphs) > max_paragraphs:
        head = paragraphs[: max_paragraphs - 1]
        tail = "".join(paragraphs[max_paragraphs - 1 :])
        paragraphs = head + [tail]

    # 段数过少：拆最长的一段。
    guard = 0
    while min_paragraphs > 0 and len(paragraphs) < min_paragraphs and guard < 12:
        guard += 1
        longest_idx = max(range(len(paragraphs)), key=lambda i: len(paragraphs[i]))
        split = _split_once(paragraphs[longest_idx])
        if split is None:
            break
        first, second = split
        paragraphs = paragraphs[:longest_idx] + [first, second] + paragraphs[longest_idx + 1 :]

    body = "\n".join(p for p in paragraphs if p)
    body = clamp_chars(body, max_chars)

    # 整体裁剪可能把末尾段落砍掉，导致段数不足；再拆一次（拆不增加字数）。
    guard = 0
    while min_paragraphs > 0 and count_paragraphs(body) < min_paragraphs and guard < 6:
        guard += 1
        rows = [p for p in re.split(r"\n+", body) if p.strip()]
        if not rows:
            break
        longest_idx = max(range(len(rows)), key=lambda i: len(rows[i]))
        split = _split_once(rows[longest_idx])
        if split is None:
            break
        rows = rows[:longest_idx] + [split[0], split[1]] + rows[longest_idx + 1 :]
        body = "\n".join(rows)
    return body


def _summary_cfg(ctx: Any) -> dict[str, int]:
    """读取 [summarize] 的字数 / 段数约束（缺省 200–400 字、2–4 段）。"""
    defaults = {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4}
    section = _section(ctx, "summarize")
    for key in defaults:
        try:
            defaults[key] = int(_read(section, key, defaults[key]))
        except (TypeError, ValueError):
            pass
    return defaults


def build_summary_material(items: list[Any], limit: int = 12) -> str:
    """把浏览摘要整理成提示词素材（纯逻辑，可测）。

    :param items: 浏览摘要行（dict，含 title / excerpt / partition_name），或纯 post_id 字符串。
    """
    lines: list[str] = []
    for item in list(items)[: max(1, limit)]:
        if isinstance(item, dict):
            title = str(item.get("title", "") or "").strip()
            excerpt = str(item.get("excerpt", "") or "").strip()
            partition = str(item.get("partition_name", "") or "").strip()
            if not title and not excerpt:
                continue
            prefix = f"【{partition}】" if partition else ""
            lines.append(f"{prefix}{title}" + (f"：{excerpt[:60]}" if excerpt else ""))
        else:
            text = str(item or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def pad_summary_to_min(
    text: str,
    items: list[Any],
    min_chars: int = 200,
    max_chars: int = 400,
) -> str:
    """字数不足时，用真实素材补一段「具体看到了什么」（纯逻辑，可测）。

    【为什么需要这一步（真机踩坑 2026-11-06）】
    模型经常只写 100~130 字就收尾，重试也压不出更多内容，而配置要求 200 字下限，
    结果整条总结链路直接判失败。单纯加次数不管用——模型就是「话少」。
    这里不编造，只做一件诚实的事：把已经刷到的真实标题铺开，
    用口语说「还刷到些别的」，补到下限为止（超过上限就停）。

    :return: 补足后的正文（未补足也原样返回，调用方再判）。
    """
    body = str(text or "").strip()
    if not body or count_chars(body) >= min_chars:
        return body

    # 抽真实标题（去掉【分区】前缀，避免每句都带括号）。
    titles: list[str] = []
    for item in list(items)[:12]:
        if isinstance(item, dict):
            title = str(item.get("title", "") or "").strip()
        else:
            title = str(item or "").strip()
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return body

    # 候选句按「信息量递减」排：先列标题（只列一次），后续只补感受，绝不重复同一批标题。
    listed = "、".join(f"「{t}」" for t in titles)
    candidates = [
        f"另外还刷到不少，{listed}，乱七八糟什么都有。",
        "看下来感觉大家最近都在聊 AI，要么就是发梗图图一乐，气氛倒是挺松的。",
        "反正刷完这一圈，脑子里的信息还挺杂的，得消化一会儿。",
        "有些标题看着就想点进去，有些扫一眼划过去了，也没什么特别的理由。",
    ]
    filler = ""
    for piece in candidates:
        candidate = body + "\n" + filler + piece
        if count_chars(candidate) <= max_chars:
            filler += piece
            if count_chars(body + "\n" + filler) >= min_chars:
                break
        else:
            break

    if not filler:
        return body
    return body + "\n" + filler


async def generate_summary(
    ctx: Any,
    items: list[Any],
    trace: LifeTrace,
    max_attempts: int = 3,
    rewrite_hint: str = "",
    previous_text: str = "",
) -> SummaryDraft:
    """生成感想总结草稿（对刷过的内容做总结，200–400 字、2–4 段）。

    流程：整理素材 → 生成 → 口语清洗 → 字数段数收敛 → 不足下限则重试；
    无论如何都返回草稿，是否可发由护栏层与调用方按字数段数判定。

    :param rewrite_hint: 重写要求（如「太像机器」）。非空时提示词会强调避开 AI 腔，
        并把上一版正文贴进去让模型改写，而不是从头重写。
    :param previous_text: 上一版正文；配合 rewrite_hint 做「定向重写」。

    【真机踩坑 2026-11-06】护栏判「文字太像机器（AI 味 X ≥ 60）」时只回一句
    「建议重写」，但**没有任何代码去重写** —— 用户看到的就是「提示重写，然后没动静」。
    重写必须在这里做：把护栏的反馈变成提示词，重新生成一版。
    """
    shape = _summary_cfg(ctx)
    material = build_summary_material(items)
    tone = _read_str(_section(ctx, "create"), "tone", "平和")
    source_ids = [str(i.get("post_id", "")) for i in items if isinstance(i, dict) and i.get("post_id")]

    best_text = ""
    best_title = ""
    last_chars = 0
    for attempt in range(max(1, max_attempts)):
        # 重试时把「上一版多少字、差多少」明确写进提示词。
        # 真机踩坑（2026-11-06）：原来的重试提示词与第一次完全相同，
        # 模型每次都给 120 字左右，重试 2 次照样不够，最后直接判失败。
        shortfall = ""
        if attempt > 0 and last_chars and last_chars < shape["min_chars"]:
            shortfall = (
                f"\n注意：上一版只有 {last_chars} 字，没达到下限。"
                f"这次必须写到 {shape['min_chars']} 字以上（目标 {shape['min_chars']}–{shape['max_chars']} 字），"
                "多说点具体细节和自己的感受，别只写概括。\n"
            )
        # 定向重写：护栏说「太像机器」时，把上一版原文和去 AI 味的具体要求一起给模型。
        rewrite_block = ""
        if rewrite_hint:
            rewrite_block = (
                f"\n【必须重写】上一版被判定为「{rewrite_hint}」，这次要改写，不能原样复述。\n"
                + (f"上一版正文：\n{previous_text}\n" if previous_text else "")
                + "改写要求：\n"
                "- 把「总的来说 / 不得不说 / 让人不禁 / 在这个快节奏的时代」这类套话全部删掉；\n"
                "- 少用四字成语堆砌，改成具体的小事、具体的人名地名、具体的时间；\n"
                "- 句子长短交错，允许口语、允许不完整的句子，别每句都一样长；\n"
                "- 少用「不仅…而且」「既…又」这种对仗结构；\n"
                "- 保留原来的意思和素材，只换说法。\n"
            )
        prompt = (
            f"你刚在小黑盒刷了下面这些内容，用「{tone}」的语气写一篇自己的感想发出去。\n"
            f"素材：\n{material or '（这轮没刷到什么，就写点最近的状态）'}\n"
            f"要求：正文 {shape['min_chars']}–{shape['max_chars']} 字，分 "
            f"{shape['min_paragraphs']}–{shape['max_paragraphs']} 段；说人话、口语化，"
            "别讲道理、别客套、别用破折号、别空行分段、别用「首先/其次/总之」这类套话。"
            f"{shortfall}{rewrite_block}\n"
            "输出格式：第一行标题（30 字以内），从第二行开始是正文。"
        )
        raw = await _generate_text(ctx, prompt)
        title, body = _split_title_body(raw)
        body = polish_chinese(body)
        body = fit_summary_shape(
            body,
            min_chars=shape["min_chars"],
            max_chars=shape["max_chars"],
            min_paragraphs=shape["min_paragraphs"],
            max_paragraphs=shape["max_paragraphs"],
        )
        if not body:
            continue
        last_chars = count_chars(body)
        if last_chars > count_chars(best_text):
            best_text, best_title = body, title
        if last_chars >= shape["min_chars"]:
            break

    # 兜底：模型话太少时，用真实素材补到下限（不编造，只铺开已刷到的标题）。
    if best_text and count_chars(best_text) < shape["min_chars"]:
        best_text = pad_summary_to_min(
            best_text,
            items,
            min_chars=shape["min_chars"],
            max_chars=shape["max_chars"],
        )
        best_text = fit_summary_shape(
            best_text,
            min_chars=shape["min_chars"],
            max_chars=shape["max_chars"],
            min_paragraphs=shape["min_paragraphs"],
            max_paragraphs=shape["max_paragraphs"],
        )

    return SummaryDraft(
        title=best_title,
        text=best_text,
        source_post_ids=source_ids,
        topic_ids=[],
        paragraphs=count_paragraphs(best_text),
        chars=count_chars(best_text),
        ai_flavor_score=ai_flavor_score(best_text),
    )


__all__ = [
    "polish_chinese",
    "ai_flavor_score",
    "fallback_text_only",
    "generate_post",
    "generate_comment",
    "generate_summary",
    "generate_images",
    "count_chars",
    "count_paragraphs",
    "clamp_chars",
    "fit_summary_shape",
    "build_summary_material",
    "pad_summary_to_min",
]
