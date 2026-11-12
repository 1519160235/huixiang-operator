# -*- coding: utf-8 -*-
"""
回响插件：发布层（六块第五块，落地版）。

【职责边界】
只负责把「护栏层已放行」的草稿送出去并回读核验，不做业务判断、不生成内容：
发图文帖 / 发文章 / 评论 / 回复楼中楼 / 上传图片 / 发帖后回读核验。

【依赖口径：本插件自带签名客户端直连小黑盒，不经 MCP 8757 中转】
- 通过 ctx.xhh_client（CommunityClient，来自适配器 vendor/ 直连客户端）发帖 / 评论 / 图床；
- 读请求（回读）走 generate_hkey，写请求（发帖 / 评论 / 图床）走 comment_query（hkey 用 stamp+1）；
- 历史契约（link_tag 数值、topic_ids 逗号串、img 段内嵌、回读用 get_post/fetch_thread_context、
  楼中楼带 rootId）详见模块内各函数与 contracts.py。

【演练】
publish_post / publish_article 带 dry_run 参数：演练只组装、不真正发送、不触碰账号。
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

import media_upload
from layers import cooldown
from layers._cfg import read as _read, section as _section
from models import (
    ActionState,
    CommentTarget,
    ImageAsset,
    ImageStatus,
    LinkTag,
    PostDraft,
    PublishResult,
    VerifyResult,
)

logger = logging.getLogger(__name__)
from vendor.models import RoutingTarget


def _endpoint(name: str) -> Any:
    """按名字取 vendor 端点枚举值（避免在模块顶部硬 import，缺依赖时不炸）。"""
    from vendor.endpoints import EndpointName

    return getattr(EndpointName, name)


def _client(ctx: Any) -> Any:
    client = getattr(ctx, "xhh_client", None)
    if client is None:
        raise RuntimeError("发布层需要 ctx.xhh_client（已登录的 CommunityClient），请先扫码登录")
    return client


def _data_dir(ctx: Any) -> Path:
    paths = getattr(ctx, "paths", None)
    if paths is not None:
        d = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
        if d:
            return Path(d)
    return Path(".")


def _resolve_local(ctx: Any, local_ref: str) -> Path:
    """把相对名解析为本地路径（经 ctx.paths.* 注入，禁止绝对路径）。"""
    if Path(local_ref).is_absolute():
        raise ValueError("配图引用禁止绝对路径，请使用相对名（经 ctx.paths.* 注入解析）")
    return _data_dir(ctx) / local_ref


def _topic_ids_str(ctx: Any, draft: PostDraft) -> str:
    """把内部 list[int] 拼成逗号分隔数字字符串，最多 max_topics 个（默认 5）。

    契约：topicIds 是逗号分隔数字字符串（如 "12,34,56"），不是 int 数组。
    """
    max_topics = 5
    publish_cfg = _section(ctx, "publish")
    if publish_cfg is not None:
        value = _read(publish_cfg, "max_topics", 5)
        try:
            max_topics = int(value)
        except (TypeError, ValueError):
            max_topics = 5
    ids: list[int] = [int(t) for t in draft.topic_ids if t]
    if draft.partition is not None and draft.partition.id:
        if draft.partition.id not in ids:
            ids.insert(0, draft.partition.id)
    return ",".join(str(t) for t in ids[: max(1, max_topics)])


def _build_segments(draft: PostDraft) -> list[dict[str, Any]]:
    """把纯文本 + 配图组装成 JSON 段数组（文字段 + img 段内嵌）。"""
    segments: list[dict[str, Any]] = []
    if draft.text.strip():
        segments.append({"type": "text", "text": draft.text})
    for asset in draft.images:
        if asset.remote_ref:
            segments.append(
                {"type": "img", "url": asset.remote_ref, "width": asset.width, "height": asset.height}
            )
    return segments


def _effective_link_tag(draft: PostDraft) -> LinkTag:
    """决定这次发帖用哪个 link_tag。

    【重要修正 2026-11-07】原实现「无图就降级成 28（纯动态）」，但真机实测
    **`link_tag=28` 被小黑盒一律拒绝**（任何分区都返回 `failed: 错误的动态分区`），
    而同样的分区用 `link_tag=27`（图文）就能过。用户也确认「明明是可以发的」。

    所以无图也照发 27：图文帖本来就允许只写文字。宁可发成「纯文字图文帖」，
    也不要发一个必然被拒的 28。
    """
    return LinkTag.IMAGE_TEXT


async def upload_image(ctx: Any, asset: ImageAsset) -> ImageAsset:
    """上传一张配图，拿到远端句柄（图床三步 info→token→PUT→callback）。"""
    client = _client(ctx)
    path = _resolve_local(ctx, asset.local_ref)
    binary = path.read_bytes()
    image = media_upload.parse_image(
        {"binary_data_base64": base64.b64encode(binary).decode("ascii")}
    )
    url = await media_upload.upload_image(client, image)
    asset.remote_ref = url
    asset.width = image.width
    asset.height = image.height
    asset.status = ImageStatus.READY
    return asset


async def check_post_permission(ctx: Any) -> tuple[bool, str]:
    """探测账号能不能发帖。

    【重要修正 2026-11-07，用户当场纠正了误判】
    我上一版拿 `CURRENT_USER.bbs_basic_permission == 0` 判定「没有发帖权限」，
    **这是错的**：真机实测该账号 `link_tag=27`（图文）能正常通过分区+类型校验，
    用户也确认「明明是可以发的」。`bbs_basic_permission` 是**站务/版主类权限**，
    跟「普通用户能不能发帖」没有关系，拿它当发帖闸门会误杀正常账号。

    真正发不出去的是 **`link_tag=28`（纯动态）**：实测无论传哪个分区都返回
    `failed: 错误的动态分区`；而同样的分区换成 `link_tag=27` 就能过。
    也就是说：**小黑盒这条链路现在只接受图文帖（27），不接受纯动态（28）**。

    所以这里不再判断「权限」，只判断「这个发布类型能不能用」，
    并且永远返回「能发」——真正的失败由 `_explain_publish_error` 翻译成人话。
    保留这个函数是给调用方一个统一入口，将来真要加预检（比如风控状态）也在这里加。

    【2026-11-12 真机事故】预检现在真的有了：平台回「发布功能暂不可用」时进入冷却，
    冷却期内在这里直接拦下整轮。必须在生成文本和生图**之前**拦，否则图已经花钱画完了。
    """
    blocked, why = cooldown.is_blocked(ctx)
    if blocked:
        return False, why
    return True, ""


def _safe_title(title: str, body: str = "") -> str:
    """把标题裁到小黑盒允许的长度。

    【真机实测 2026-11-07】`link_tag=27`（图文帖）标题上限 **30 字**，
    超一个字就整条失败（`failed: 标题最多输入30字`）。
    生成侧已按 30 字裁过，这里再兜一道：任何来源的标题（手改、旧数据、
    模型输出带额外前缀）超长时都裁掉，避免因为一个标题白生成一整篇。
    """
    t = str(title or "").strip().replace("\n", " ")
    t = re.sub(r"^\s*(标题|题目|title)\s*[:：]\s*", "", t, flags=re.I).strip(" \"'“”「」『』《》")
    # 表情标记不能进标题：`[cube_doge]` 是评论里的表情语法，标题栏不认，
    # 还会被算进长度（真机报「标题必须为 1–80 字」就是这么来的）。
    t = re.sub(r"\[[^\[\]]{1,24}\]", "", t).strip()

    # 【真机缺陷 2026-11-09】模型把标题写成纯表情、或第一行只有 `[cube_xxx]`，
    # 清洗后变空串，接口直接报「帖子标题必须为 1–80 字」，整条帖子白生成。
    # 空标题就从正文首句兜一个，绝不留空、也绝不因为标题把整篇丢掉。
    if not t.strip(" 　.,，。:：;；!?！？~～-—"):
        body_head = str(body or "").strip().splitlines()[0] if str(body or "").strip() else ""
        body_head = re.sub(r"\[[^\[\]]{1,24}\]", "", body_head).strip(" 　.,，。:：;；!！？?")
        t = body_head or t
    return _truncate_title(t)


# 真机实测（2026-11-09）：接口说「标题最多输入30字」，但实际按 **UTF-8 字节** 算——
# 10 个汉字（30 字节）刚好通过，11 个汉字就被拒。所以这里按字节截，不是按字符截。
TITLE_MAX_BYTES = 30


def _truncate_title(text: str, max_bytes: int = TITLE_MAX_BYTES) -> str:
    """按 UTF-8 字节数截断标题，且不把字词截成半个emoji。"""
    t = str(text or "").strip()
    if len(t.encode("utf-8")) <= max_bytes:
        return t
    out: list[str] = []
    used = 0
    for ch in t:
        n = len(ch.encode("utf-8"))
        if used + n > max_bytes:
            break
        out.append(ch)
        used += n
    return "".join(out).rstrip(" 　.,，。:：;；!?！？~～-—")


async def delete_post(ctx: Any, post_id: str) -> tuple[bool, str]:
    """删除自己发的帖子（清理测试内容用）。

    【为什么需要】真机验证发帖链路时难免发出测试帖；没有删除手段就只能
    让用户在 App 里手动删。实测 `/bbs/app/link/delete` 可用，
    重复删会返回「帖子已经删除」，按已删成功处理。

    返回 (是否已删, 说明)。只用于删**自己**的帖子，别人的删不掉是正常权限限制。
    """
    pid = str(post_id or "").strip()
    if not pid:
        return False, "缺少帖子 ID"
    client = _client(ctx)
    if client is None:
        return False, "小黑盒未登录"
    try:
        await client._write_form("/bbs/app/link/delete", {"link_id": pid}, "删除帖子")
    except Exception as exc:  # noqa: BLE001
        if "已经删除" in str(exc):
            return True, "帖子已经是删除状态"
        return False, f"删除失败：{str(exc)[:160]}"
    return True, "帖子已删除"


async def publish_post(ctx: Any, draft: PostDraft, dry_run: bool = False) -> PublishResult:
    """发布一条图文帖（link_tag 图文=27 / 动态=28，topic_ids 逗号串，img 段内嵌）。

    【真机缺陷 2026-11-07 用户报障「感想总结 失败 执行异常：CommentRejectedError」】
    结论修正过一次（用户当场纠正）：
      - ❌ 不是「账号没发帖权限」——`bbs_basic_permission=0` 是站务类权限，
        跟普通发帖无关，拿它当闸门会误杀正常账号；
      - ✅ 真因是 **`link_tag=28`（纯动态）被小黑盒拒**：同样的分区换成 27 就能过，
        说明这条链路现在只接受图文帖（27）。

    所以这里做两件事：
      1. 无图时**不再降级成 28**，仍用 27 发（正文讲清楚，发布类型由服务端认）；
      2. 把上游那些看不懂的报错翻成能行动的中文提示。
    """
    segments = _build_segments(draft)
    topic_ids_str = _topic_ids_str(ctx, draft)
    link_tag = _effective_link_tag(draft)
    if dry_run:
        tags = "、".join(draft.hashtags or [])
        return PublishResult(
            state=ActionState.VERIFIED,
            note=(
                f"演练：已组装发帖（link_tag={link_tag.value}，topic_ids={topic_ids_str}"
                + (f"，hashtags={tags}" if tags else "")
                + "），未真正发送"
            ),
        )
    client = _client(ctx)
    tags = list(draft.hashtags or [])
    try:
        post_id = await client.publish_post(
            title=_safe_title(draft.title, draft.text),
            segments=segments,
            link_tag=link_tag.value,
            topic_ids_str=topic_ids_str,
            hashtags=tags,
        )
    except Exception as exc:  # noqa: BLE001 —— 把上游黑话翻成人话，再原样抛给上层。
        # 【真机缺陷 2026-11-09】有的分区报「只能单独选择 XX 分区」——
        # 这类分区不能和别的混挂。这里把分区名翻成 id 记下来，下次挑分区自动规避。
        _learn_exclusive_from_error(ctx, str(exc), draft)

        # 带话题失败不一定是话题的问题（也可能是分区/限流），
        # 但去掉话题重试成本很低，值得再试一次。
        if tags:
            logger.warning("回响：带话题发帖失败，去掉话题重试：%s", str(exc)[:120])
            try:
                post_id = await client.publish_post(
                    title=_safe_title(draft.title, draft.text),
                    segments=segments,
                    link_tag=link_tag.value,
                    topic_ids_str=topic_ids_str,
                    hashtags=[],
                )
                cooldown.clear(ctx)
                return PublishResult(
                    state=ActionState.SEND_UNKNOWN,
                    post_id=post_id,
                    note="已发出（话题未挂上），待回读核验",
                )
            except Exception as retry_exc:  # noqa: BLE001
                # 只在「最终要抛给调用方」时记一次，避免带话题重试把次数算成两次。
                cooldown.record_rejection(ctx, str(retry_exc))
                raise _explain_publish_error(retry_exc, topic_ids_str) from retry_exc
        cooldown.record_rejection(ctx, str(exc))
        raise _explain_publish_error(exc, topic_ids_str) from exc
    cooldown.clear(ctx)
    return PublishResult(
        state=ActionState.SEND_UNKNOWN,
        post_id=post_id,
        note="已发出，待回读核验",
    )


# 服务端报错 → 能行动的中文提示。
# 【为什么需要】上游用「错误的动态分区」搪塞无发帖权限的账号（真机实测），
# 照着这句话去改分区只会越改越错。这里按已知语义翻译。
def _learn_exclusive_from_error(ctx: Any, message: str, draft: Any) -> None:
    """从「只能单独选择 X 分区」的报错里学出独占分区，下次自动规避。

    平台只给名字不给 id，所以拿本地分区缓存把名字对回 id。
    学到的结果会落盘，重启后不用再踩一遍。
    """
    if "只能单独选择" not in message:
        return
    try:
        from layers import topics as _topics
    except Exception:  # noqa: BLE001
        return
    cache = getattr(getattr(ctx, "store", None), "partition", None)
    parts = cache.get() if cache is not None else []
    for p in parts or []:
        name = str(getattr(p, "name", "") or "").strip()
        if name and name in message:  # 报错形如「只能单独选择 盒友杂谈 分区」
            _topics.remember_exclusive(getattr(p, "id", 0), ctx)
            logger.info("回响：记下独占分区「%s」，以后不跟其他分区混挂", name)
            return
    part = getattr(draft, "partition", None)
    if part is not None:
        _topics.remember_exclusive(getattr(part, "id", 0), ctx)




_PUBLISH_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("错误的动态分区",
     "发帖被拒：小黑盒报「错误的动态分区」。真机实测这条链路只接受图文帖"
     "（link_tag=27），纯动态（28）无论传哪个分区都会被这么拒。"
     "本插件已改为统一用 27 发布，若仍报此错，请检查该分区是否允许发帖。"),
    ("请添加分区", "发帖被拒：没有选分区。请在「发帖」设置里指定默认分区。"),
    ("只能单独选择",
     "发帖被拒：该分区要求单独发布，不能和其他分区混选。请只保留一个分区。"),
    ("标题最多输入", "发帖被拒：标题超长（图文帖标题上限 30 字），请缩短标题。"),
    ("频率过快", "发帖被拒：小黑盒限流了（发帖太频繁），等几分钟再试。"),
    ("expired", "登录态已过期（接口返回 expired），请在后台重新扫码登录。"),
)


def _explain_publish_error(exc: Exception, topic_ids_str: str) -> Exception:
    """把发帖异常换成带上「人话解释」的同类异常，保留原始异常作 cause。"""
    raw = str(exc)
    for key, hint in _PUBLISH_ERROR_HINTS:
        if key in raw:
            try:
                new_exc = type(exc)(f"{hint}（原始报错：{raw[:120]}）")
                return new_exc
            except Exception:  # noqa: BLE001 —— 构造失败就直接返回原异常。
                return exc
    if topic_ids_str:
        return exc
    return exc


async def publish_article(ctx: Any, draft: PostDraft, dry_run: bool = False) -> PublishResult:
    """发布一篇文章（link_tag=11）。"""
    topic_ids_str = _topic_ids_str(ctx, draft)
    if dry_run:
        return PublishResult(
            state=ActionState.VERIFIED,
            note=f"演练：已组装文章（link_tag=11，topic_ids={topic_ids_str}），未真正发送",
        )
    client = _client(ctx)
    post_id = await client.publish_article(
        title=_safe_title(draft.title, draft.text),
        html=draft.text,
        topic_ids_str=topic_ids_str,
    )
    return PublishResult(state=ActionState.SEND_UNKNOWN, post_id=post_id, note="文章已发出，待回读核验")


async def publish_comment(ctx: Any, target: CommentTarget, text: str, dry_run: bool = False) -> PublishResult:
    """对帖子发表一条顶层评论（帖子 ID 取自推荐流 postId 字段；顶层评论 rootId 用 -1）。"""
    if dry_run:
        return PublishResult(state=ActionState.VERIFIED, note="演练：已组装顶层评论，未真正发送")
    client = _client(ctx)
    route = RoutingTarget(profile_id="default", post_id=target.post_id)
    result = await client.send_comment(route, text)
    return PublishResult(state=ActionState.VERIFIED, note=f"评论已发 comment_id={result.external_comment_id}")


async def reply_floor(ctx: Any, target: CommentTarget, text: str, dry_run: bool = False) -> PublishResult:
    """回复楼中楼：必须带 rootId（root_id），否则发错层级；parent 默认同 root。

    契约（避坑清单第 7 条）：回楼中楼取通知里的 rootCommentId 作 rootId，顶层评论才用 -1。
    """
    if not target.root_id:
        return PublishResult(state=ActionState.FAILED, note="楼中楼回复必须带 rootId（root_id）")
    if dry_run:
        return PublishResult(state=ActionState.VERIFIED, note="演练：已组装楼中楼回复（带 rootId），未真正发送")
    client = _client(ctx)
    route = RoutingTarget(
        profile_id="default",
        post_id=target.post_id,
        root_comment_id=target.root_id,
        parent_comment_id=target.parent_id or target.root_id,
    )
    result = await client.send_comment(route, text)
    return PublishResult(state=ActionState.VERIFIED, note=f"楼中楼已发 comment_id={result.external_comment_id}")


async def reply_notification(ctx: Any, notif: Any, text: str, dry_run: bool = False) -> PublishResult:
    """按通知回复：回复 / 楼中楼带 rootId 走 reply_floor；@ 帖子（无评论目标）走顶层评论。

    :param notif: vendor.models.Notification（含 post_id / root_comment_id / parent_comment_id）。
    """
    post_id = str(getattr(notif, "post_id", "") or "")
    root_id = str(getattr(notif, "root_comment_id", "") or "")
    parent_id = str(getattr(notif, "parent_comment_id", "") or "")
    if root_id:
        target = CommentTarget(post_id=post_id, root_id=root_id, parent_id=parent_id or root_id)
        return await reply_floor(ctx, target, text, dry_run=dry_run)
    target = CommentTarget(post_id=post_id)
    return await publish_comment(ctx, target, text, dry_run=dry_run)


async def like_post(ctx: Any, post_id: str) -> VerifyResult:
    """给帖子点赞（阶段 4 接入，本阶段占位返回未实现结果）。"""
    return VerifyResult(ok=False, code="not_implemented", message="点赞待阶段 4 接入", post_id=post_id)


async def verify_post_published(ctx: Any, post_id: str) -> VerifyResult:
    """发帖后回读核验：用 fetch_thread_context 按 postId 回读，禁用 search_posts。"""
    client = _client(ctx)
    try:
        context = await client.fetch_thread_context(post_id)
        ok = bool(context is not None and context.post_id)
        if ok:
            return VerifyResult(ok=True, code="verified", post_id=post_id, message="回读核验通过：帖子存在")
        return VerifyResult(ok=False, code="verify_not_found", post_id=post_id, message="回读未找到帖子")
    except Exception as exc:  # noqa: BLE001 —— 回读异常统一返回未确认。
        return VerifyResult(ok=False, code="verify_timeout", post_id=post_id, message=f"回读异常：{type(exc).__name__}")


__all__ = [
    "upload_image",
    "delete_post",
    "publish_post",
    "publish_article",
    "publish_comment",
    "reply_floor",
    "reply_notification",
    "like_post",
    "verify_post_published",
]
