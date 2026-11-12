# -*- coding: utf-8 -*-
"""
回响插件：总结层（阶段 5 落地，「感想总结发帖」子流水线）。

【职责边界】
只做编排，不新增业务能力：把「刷过什么」变成「发一篇总结帖」，串起
接入层（浏览素材）→ 创作层（生成 200–400 字 / 2–4 段）→ 护栏层（正文 60 阈值总闸）
→ 发布层（发帖 + 回读核验）。

【触发判定（纯逻辑，可测）】
should_summarize：
- count：未被总结消费的浏览条数 ≥ count_threshold；
- time：距上次总结 ≥ time_hours 小时，且至少有一条待总结素材；
- both：上面任一成立。

【素材来源】
state.BrowseDigestStore 记录刷过的帖子（post_id / 标题 / 摘要 / 分区）。
总结成功后把这些素材标记为已消费，避免同一批内容被反复总结。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from models import LinkTag, PostDraft, RunMode, SummaryDraft

from . import cooldown, create, guard, hashtags, publish, schedule, sense, topics, voices
from ._cfg import norm_choice as _norm_choice, read as _read_section_value, section as _section

logger = logging.getLogger(__name__)


def _warn(ctx: Any, message: str) -> None:
    """优先写宿主日志；单测里的 ctx 可能没有 logger，退回模块 logger。"""
    log = getattr(ctx, "logger", None) or logger
    try:
        log.warning(message)
    except Exception:  # noqa: BLE001 —— 日志失败绝不能影响主流程。
        pass


# 护栏判「太像机器」/ 篇幅不合规时，最多重写几轮。
# 每轮都是一次完整的大模型调用，别设太大（烧额度也慢）；3 轮不达标就如实失败。
REWRITE_ROUNDS = 2


def _summarize_cfg(ctx: Any) -> dict[str, Any]:
    """读取 [summarize] 段配置（缺省值与 config_model 一致）。"""
    defaults: dict[str, Any] = {
        "enabled": True,
        "trigger_by": "count",
        "count_threshold": 10,
        "time_hours": 24,
        "min_chars": 200,
        "max_chars": 400,
        "min_paragraphs": 2,
        "max_paragraphs": 4,
    }
    section = _section(ctx, "summarize")
    for key in defaults:
        defaults[key] = _read_section_value(section, key, defaults[key])
    return defaults


def should_summarize(
    *,
    trigger_by: str = "count",
    count_threshold: int = 10,
    time_hours: int = 24,
    pending_count: int = 0,
    since_last_hours: float = 0.0,
    last_summary_ts: float = 0.0,
) -> tuple[bool, str]:
    """判断是否该发一篇感想总结（纯逻辑，可测）。

    :return: (是否触发, 中文原因)
    """
    if pending_count <= 0:
        return False, "没有待总结的浏览素材"
    by_count = pending_count >= max(1, int(count_threshold))
    if last_summary_ts <= 0:
        by_time = pending_count > 0
    else:
        by_time = since_last_hours >= max(1, int(time_hours))
    mode = str(trigger_by or "count")
    if mode == "count":
        return (True, f"已刷 {pending_count} 条，达到帖数阈值 {count_threshold}") if by_count else (
            False,
            f"已刷 {pending_count} 条，未到帖数阈值 {count_threshold}",
        )
    if mode == "time":
        if last_summary_ts <= 0:
            return (True, f"还没总结过，已攒 {pending_count} 条素材") if by_time else (
                False,
                "没有待总结的浏览素材",
            )
        return (True, f"距上次总结已 {since_last_hours:.1f} 小时") if by_time else (
            False,
            f"距上次总结 {since_last_hours:.1f} 小时，未到时间窗口 {time_hours} 小时",
        )
    if by_count:
        return True, f"已刷 {pending_count} 条，达到帖数阈值 {count_threshold}"
    if by_time:
        return True, f"距上次总结已 {since_last_hours:.1f} 小时"
    return False, "帖数与时间两个条件都未满足"


def summary_ready(ctx: Any, store: Any = None, now: float | None = None) -> tuple[bool, str]:
    """从配置与存储读实时状态，判断是否该发总结（不含生成）。"""
    cfg = _summarize_cfg(ctx)
    if not bool(cfg.get("enabled", True)):
        return False, "感想总结开关未开启"
    if store is None:
        return False, "浏览摘要存储未初始化"
    now_ts = now if now is not None else time.time()
    last_ts = store.last_summary_ts()
    since_hours = ((now_ts - last_ts) / 3600.0) if last_ts > 0 else 0.0
    return should_summarize(
        trigger_by=_norm_choice("trigger_by", cfg.get("trigger_by"), "count"),
        count_threshold=int(cfg.get("count_threshold", 10)),
        time_hours=int(cfg.get("time_hours", 24)),
        pending_count=store.pending_count(),
        since_last_hours=since_hours,
        last_summary_ts=last_ts,
    )


def _posted_slots(cursor: Any) -> list[str]:
    """读「已发过的时段」记录（存的是 JSON 字符串）。"""
    if cursor is None:
        return []
    try:
        raw = cursor.get("posted_slots").value
        rows = json.loads(raw) if raw else []
        return [str(x) for x in rows] if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def _mark_slot_posted(cursor: Any, key: str) -> None:
    """发完一条后记下这个时段，避免同一窗口连发。"""
    if cursor is None or not key:
        return
    try:
        from models import Cursor

        rows = _posted_slots(cursor) + [key]
        cursor.set(
            Cursor(
                name="posted_slots",
                value=json.dumps(schedule.trim_posted(rows), ensure_ascii=False),
            )
        )
    except Exception:  # noqa: BLE001 —— 记录失败最多多发一条，不影响发布。
        pass


async def post_ready(ctx: Any, store: Any = None, now: Any = None) -> tuple[bool, str]:
    """判断「现在能不能发一条」。

    【2026-11-08 用户要求「既然都说了他是自己想发，那就不要设置什么时间段了」】
    时段和间隔限制全部取消：只要没到当天硬上限、生活状态允许（睡觉/累垮不发），
    剩下发不发交给「想不想发」那一步判断，不再按时间表卡。

    素材判断也放宽了：有 1 条待总结的帖子就够（单条驱动的动机只聊一条）；
    完全没有素材时，允许发「自己的状态」，但最近 3 篇里已经发过就不再发。
    """
    cfg = _summarize_cfg(ctx)
    if not bool(cfg.get("enabled", True)):
        return False, "发帖开关未开启"
    if store is None:
        return False, "浏览摘要存储未初始化"

    items = store.pending()
    post_history = getattr(getattr(ctx, "store", None), "post_history", None)
    recent_formats = post_history.recent_formats(3) if post_history is not None else []
    life_recent = False
    for fid in recent_formats:
        try:
            if str(voices.get_format(fid).get("material")) == "life":
                life_recent = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not items and life_recent:
        return False, "没有新素材，最近也发过日常了，先不发"

    cursor = getattr(getattr(ctx, "store", None), "cursor", None)
    quota = getattr(getattr(ctx, "store", None), "quota", None)
    hard_limit = int(_read_section_value(_section(ctx, "guard"), "daily_quota_post", 5) or 0)

    today_count = 0
    last_ts = 0.0
    if quota is not None:
        try:
            from state import today_str

            today_count = int(quota.get(RunMode.REAL, today_str(), "post").used)
        except Exception:  # noqa: BLE001
            today_count = 0
    if cursor is not None:
        try:
            raw_last = cursor.get("last_post_at").value
            last_ts = float(raw_last) if raw_last else 0.0
        except Exception:  # noqa: BLE001
            last_ts = 0.0

    trace = await sense.read_life_trace(ctx)
    return schedule.should_post_now(
        now=now,
        last_post_ts=last_ts,
        today_count=today_count,
        hard_limit=hard_limit,
        min_gap_minutes=0,  # 不再限制间隔，想发就发
        trace=trace,
    )


def _pick_topic_ids(ctx: Any, partition: Any, title: str, body: str) -> list[int]:
    """挑这次发帖要挂的**分区**（小黑盒字段叫 topic_ids，但它装的是分区/社区 id）。

    【2026-11-08 用户纠正「分区是分区，话题是话题」】
    真机读回一条真实帖子：
        topics   = [{"topic_id": 806933, "name": "AI"}, ...]   ← 分区
        hashtags = [{"name": "deepseek", "hashtag_id": 689666}] ← 话题
    所以 topic_ids 这套只是分区多选（真实帖子会同时挂 AI + 沙雕日常），
    真正的话题是 hashtags，见 `_pick_hashtags`。
    """
    pub = _section(ctx, "publish")
    enabled = bool(_read_section_value(pub, "linked_topics_enabled", True))
    if not enabled:
        return []
    try:
        max_topics = int(_read_section_value(pub, "max_topics", topics.DEFAULT_MAX_TOPICS))
    except (TypeError, ValueError):
        max_topics = topics.DEFAULT_MAX_TOPICS
    custom = _read_section_value(pub, "community_allowlist", []) or []
    if isinstance(custom, str):
        custom = [x for x in custom.replace("，", ",").split(",") if x.strip()]
    legacy = _read_section_value(pub, "custom_topics", []) or []
    if isinstance(legacy, str):
        legacy = [x for x in legacy.replace("，", ",").split(",") if x.strip()]
    # 两个配置项都认（老配置叫 custom_topics）。
    custom = list(custom) + [x for x in legacy if x not in custom]

    cache = getattr(getattr(ctx, "store", None), "partition", None)
    partitions = cache.get() if cache is not None else []
    return topics.pick_topic_ids(
        partitions=partitions,
        partition=partition,
        text=f"{title} {body}",
        custom=custom,
        max_topics=max_topics,
    )


def _pick_hashtags(ctx: Any, title: str, body: str) -> list[str]:
    """挑这次发帖要带的**话题**（hashtag 名数组）。

    话题池来自真实帖子采样（`hashtags.sample_from_feed`），这里按正文匹配挑。
    匹配不上就不带 —— 硬塞不相关的话题和小黑盒「错误勾选将会被处理」是一个性质。
    """
    pub = _section(ctx, "publish")
    if not bool(_read_section_value(pub, "hashtags_enabled", True)):
        return []
    try:
        limit = int(_read_section_value(pub, "max_hashtags", 5))
    except (TypeError, ValueError):
        limit = 5
    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    pool = hashtags.HashtagPool(data_dir).load()
    if not pool:
        return []
    custom = _read_section_value(pub, "custom_hashtags", []) or []
    if isinstance(custom, str):
        custom = [x for x in custom.replace("，", ",").split(",") if x.strip()]
    picked = hashtags.pick_hashtags(pool, f"{title} {body}", limit=limit)
    for name in custom:
        clean = str(name or "").strip()
        if clean and clean not in picked and len(picked) < limit:
            picked.insert(0, clean)
    return picked[:limit]


def _image_cfg(ctx: Any) -> tuple[bool, int]:
    """读 [image] 段的 with_image / image_count（缺省开启、1 张）。"""
    section = _section(ctx, "image")
    enabled = bool(_read_section_value(section, "with_image", True))
    try:
        count = int(_read_section_value(section, "image_count", 1))
    except (TypeError, ValueError):
        count = 1
    return enabled, max(1, count)


def summary_to_draft(
    summary: SummaryDraft,
    partition: Any = None,
    topic_ids: list[int] | None = None,
    hashtags: list[str] | None = None,
) -> PostDraft:
    """把 SummaryDraft 转成可走发布层的 PostDraft。

    :param topic_ids: 分区/社区 id（小黑盒字段叫 topic_ids）。
    :param hashtags: 话题名数组（和分区是两回事）。
    """
    return PostDraft(
        title=summary.title,
        text=summary.text,
        link_tag=LinkTag.DYNAMIC,
        partition=partition,
        topic_ids=list(topic_ids or summary.topic_ids or []),
        hashtags=list(hashtags or []),
        images=[],
        polished=True,
        ai_flavor_score=summary.ai_flavor_score,
    )


def check_summary_shape(summary: SummaryDraft, cfg: dict[str, Any]) -> tuple[bool, str]:
    """校验总结字数与段数是否落在约束内（纯逻辑，可测）。"""
    min_chars = int(cfg.get("min_chars", 200))
    max_chars = int(cfg.get("max_chars", 400))
    min_par = int(cfg.get("min_paragraphs", 2))
    max_par = int(cfg.get("max_paragraphs", 4))
    chars = summary.chars or create.count_chars(summary.text)
    paragraphs = summary.paragraphs or create.count_paragraphs(summary.text)
    if chars < min_chars:
        return False, f"总结字数不足（{chars} < {min_chars}）"
    if chars > max_chars:
        return False, f"总结字数超限（{chars} > {max_chars}）"
    if paragraphs < min_par:
        return False, f"总结段数不足（{paragraphs} < {min_par}）"
    if paragraphs > max_par:
        return False, f"总结段数超限（{paragraphs} > {max_par}）"
    return True, f"总结字数段数合规（{chars} 字 / {paragraphs} 段）"


async def run_summary_once(
    ctx: Any,
    partition: Any = None,
    mode: RunMode = RunMode.DRY_RUN,
    *,
    allow_life_only: bool = False,
    format_id: str = "",
    check_want: bool = True,
) -> dict:
    """跑一遍发帖流水线：选动机 → 想不想发 → 取素材 → 生成 → 校验形状 → 护栏 → 发布 → 回读。

    :param allow_life_only: 没有帖子素材时是否允许发「自己的状态」（日记）。
        默认 False：底层保持「没素材就不发」，由上层（post_ready）决定要不要开这个口子。
    :param format_id: 指定动机（见 layers/voices.py）；留空则按「最久未用」自动挑。
    :param check_want: 生成前先问一句「想发吗」，不想发就直接返回。
        手动「发一条」时传 False（用户明确要发，不用再问）。

    【真机踩坑 2026-11-06 用户报障】
    「写得跟机器一样，提示建议重写，然后就没动静了；过了会儿又变成演练」——
    根因有两个：
    1. 护栏判 `ai_flavor_block` 时只**返回**一句「建议重写」，**没有任何代码真的去重写**，
       于是这一轮直接以失败结束，用户看到的就是「提示完就没下文」。
    2. 那道失败记录写的是 worker 自己的 mode；而 worker 的 mode 又是在启动时固化的，
       切「自动」后旧 worker 还在跑演练 —— 日志里就出现「前面自动、后面又演练」。
    这里补上真正的重写闭环：AI 味超标 → 带反馈重新生成，最多 REWRITE_ROUNDS 轮。
    """
    store = getattr(getattr(ctx, "store", None), "digest", None)
    cfg = _summarize_cfg(ctx)
    if not bool(cfg.get("enabled", True)):
        return {"ok": False, "code": "summary_disabled", "message": "感想总结开关未开启"}
    if store is None:
        return {"ok": False, "code": "no_digest_store", "message": "浏览摘要存储未初始化"}
    items = store.pending()

    # 【真机缺陷 2026-11-07】真实模式下先查发帖权限：账号 bbs_basic_permission=0 时
    # 发帖必被拒，而服务端只说「错误的动态分区」，白白生成一整篇再失败。
    # 提前拦下来，直接把真因告诉用户。演练模式不发帖，不用查。
    if mode is not RunMode.DRY_RUN:
        can_post, why = await publish.check_post_permission(ctx)
        if not can_post:
            return {"ok": False, "code": "no_post_permission", "message": why}

    trace = await sense.read_life_trace(ctx)
    # 动机轮换 + 历史标题避让：连着几篇不能一个模子，也不能每篇都是「感想」。
    # 【2026-11-08 用户反馈「为什么是发感想呢？他觉得好玩就发一个，或者发个日记」】
    post_history = getattr(getattr(ctx, "store", None), "post_history", None)
    recent_titles = post_history.recent_titles(6) if post_history is not None else []
    recent_formats = post_history.recent_formats(5) if post_history is not None else []
    fmt = voices.get_format(format_id) if format_id else voices.pick_format(
        recent_formats, has_material=bool(items)
    )
    material_mode = str(fmt.get("material") or "multi")
    if not items and (material_mode != "life" or not allow_life_only):
        return {"ok": False, "code": "no_material", "message": "没有待总结的浏览素材"}

    # 「想发吗？」——不想发就不发，不当作业交。
    # 【2026-11-08 用户要求】「他觉得好玩他就发帖，他觉得不好玩就不发帖」
    # 「就跟你发抖音、发朋友圈、发 QQ 空间，有分享就发呗，乐意发发呗，爱发发呗，不发不发呗」
    if check_want:
        decision = await create.decide_want_to_post(ctx, items=items, fmt=fmt)
        if not decision.get("want"):
            reason = str(decision.get("reason") or "").strip()
            return {
                "ok": False,
                "code": "not_in_the_mood",
                "message": f"这轮没想发的（{reason}）" if reason else "这轮没想发的",
                "reason": reason,
            }

    summary = await create.generate_summary(
        ctx,
        items,
        trace,
        format_id=fmt["id"],
        recent_titles=recent_titles,
        material_mode=material_mode,
        life_trace=trace,
    )

    # 形状不合规 / AI 味超标都可能靠重写解决，统一放一个循环里。
    # 护栏的其他闸门（额度、间隔、去重、时段）重写也没用，直接失败返回。
    rewrite_hint = ""
    previous_text = ""
    attempts: list[dict] = []
    for round_no in range(REWRITE_ROUNDS + 1):
        if rewrite_hint:
            summary = await create.generate_summary(
                ctx,
                items,
                trace,
                rewrite_hint=rewrite_hint,
                previous_text=previous_text,
                format_id=fmt["id"],
                recent_titles=recent_titles,
                material_mode=material_mode,
                life_trace=trace,
            )

        # 【2026-11-08 用户要求「字数多少也就不要设置了」】
        # 不再校验字数段数：写成一句话是它的事，写成十段也是它的事。
        # 这里只留一个空壳调用（check_summary_shape 在无上下限时恒通过），
        # 方便以后想加「内容质量」类校验时有落脚点。
        ok, message = True, "篇幅不限"
        if not ok:
            attempts.append({"round": round_no, "code": "summary_shape", "message": message})
            if round_no >= REWRITE_ROUNDS:
                return {
                    "ok": False,
                    "code": "summary_shape",
                    "message": message,
                    "title": summary.title,
                    "text": summary.text,
                    "chars": summary.chars,
                    "paragraphs": summary.paragraphs,
                    "rewrites": round_no,
                }
            # 字数/段数不够 → 用「写长一点」的提示重写，而不是换话题。
            rewrite_hint = f"篇幅不合规（{message}）"
            previous_text = summary.text
            continue

        draft = summary_to_draft(
            summary,
            partition=partition,
            topic_ids=_pick_topic_ids(ctx, partition, summary.title, summary.text),
            hashtags=_pick_hashtags(ctx, summary.title, summary.text),
        )
        verdict = await guard.is_publishable(ctx, draft, mode)
        if verdict.ok:
            break

        attempts.append({"round": round_no, "code": verdict.code, "message": verdict.message})
        # 只有「文字太像机器」值得重写。额度/间隔/去重/时段重写一百遍也一样。
        if verdict.code != "ai_flavor_block":
            return {"ok": False, "code": verdict.code, "message": verdict.message}
        if round_no >= REWRITE_ROUNDS:
            # 把最终分数和字数一起带出来：用户看到「重写 N 次仍不达标」时，
            # 需要知道是「差一点」还是「模型根本写不像人」，否则只能干瞪眼。
            return {
                "ok": False,
                "code": verdict.code,
                "message": f"{verdict.message}（已重写 {round_no} 次仍不达标）",
                "title": summary.title,
                "text": summary.text,
                "chars": summary.chars,
                "paragraphs": summary.paragraphs,
                "ai_flavor_score": summary.ai_flavor_score,
                "rewrites": round_no,
            }
        rewrite_hint = "文字太像机器、AI 味太重"
        previous_text = summary.text
    else:  # pragma: no cover —— 循环必然 break 或 return，这里只是兜底。
        return {"ok": False, "code": "summary_loop_exhausted", "message": "重写循环异常结束"}

    # 【2026-11-07 用户报障「发帖子要配图，图没了」】
    # 原来 summary_to_draft 写死 images=[]，总结帖永远纯文字。
    # 现在按 [image].with_image 生图：真实模式上传图床，演练只生成不上传。
    # 生图/上传失败一律降级纯文字，绝不阻断发帖。
    image_on, image_count = _image_cfg(ctx)
    # 【2026-11-12】冷却刚到期时先用纯文字试探，别急着花钱生图：
    # 平台要是还没恢复，这一轮图又白发一张。发成功了 clear() 会自动恢复生图。
    if image_on and cooldown.should_skip_image(ctx):
        _warn(ctx, "回响：发布冷却刚结束，本轮用纯文字试探，暂不生图")
        image_on = False
    if image_on:
        try:
            draft.images = await create.generate_images(ctx, draft, image_count)
        except Exception as exc:  # noqa: BLE001 —— 配图不是发帖的必要条件。
            _warn(ctx, f"回响：总结配图生成失败，降级纯文字：{type(exc).__name__}")
            draft.images = []
        if draft.images and mode is not RunMode.DRY_RUN:
            for asset in draft.images:
                try:
                    await publish.upload_image(ctx, asset)
                except Exception as exc:  # noqa: BLE001
                    _warn(ctx, f"回响：总结配图上传失败，降级纯文字：{type(exc).__name__}")
                    draft = await create.fallback_text_only(draft)
                    break

    # 发帖异常不要冒泡成「执行异常：CommentRejectedError」——
    # 上层日志和后台面板只能显示这个类名，用户根本不知道发生了什么。
    # 这里转成结构化失败结果，message 是能行动的中文提示。
    try:
        result = await publish.publish_post(ctx, draft, dry_run=(mode is RunMode.DRY_RUN))
    except Exception as exc:  # noqa: BLE001 —— 发布失败要有可读原因，而不是裸异常。
        return {
            "ok": False,
            "code": "publish_rejected",
            "message": str(exc) or f"{type(exc).__name__}",
            "title": summary.title,
            "chars": summary.chars,
            "paragraphs": summary.paragraphs,
        }
    if mode is RunMode.DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "note": result.note,
            "title": summary.title,
            "text": summary.text,
            "chars": summary.chars,
            "paragraphs": summary.paragraphs,
            "rewrites": len(attempts),
        }
    if not result.post_id:
        return {"ok": False, "code": "no_post_id", "message": "总结帖未返回帖子 ID"}
    verify = await publish.verify_post_published(ctx, result.post_id)
    if verify.ok:
        await guard.reserve_quota(ctx, mode, "post")
        # 记下这个时段已经发过，同一窗口不再连发（早/中/晚各一条的节奏）。
        _mark_slot_posted(
            getattr(getattr(ctx, "store", None), "cursor", None),
            schedule.slot_key(
                None,
                str(_read_section_value(_section(ctx, "publish"), "post_slots", schedule.DEFAULT_SLOTS)),
            ),
        )
        # 只消费这一篇真正用到的素材：单条驱动只标记那一条，其余留给下次。
        used_ids = [pid for pid in (summary.source_post_ids or []) if pid]
        store.mark_summarized(used_ids)
        if post_history is not None:
            post_history.add(
                post_id=result.post_id,
                title=summary.title,
                format_id=fmt["id"],
                chars=summary.chars,
            )
        return {
            "ok": True,
            "post_id": result.post_id,
            "verified": True,
            "title": summary.title,
            "format": fmt["id"],
            "text": summary.text,
            "chars": summary.chars,
            "paragraphs": summary.paragraphs,
            "rewrites": len(attempts),
        }
    return {
        "ok": False,
        "code": verify.code,
        "post_id": result.post_id,
        "verified": False,
        "message": verify.message,
    }


__all__ = [
    "should_summarize",
    "summary_ready",
    "summary_to_draft",
    "check_summary_shape",
    "run_summary_once",
]
