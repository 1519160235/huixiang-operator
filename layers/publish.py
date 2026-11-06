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
from pathlib import Path
from typing import Any

import media_upload
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
from vendor.models import RoutingTarget


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
    """无图时把图文(27)降为动态(28)；有图保持 draft.link_tag。"""
    if not draft.images:
        return LinkTag.DYNAMIC
    return draft.link_tag


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


async def publish_post(ctx: Any, draft: PostDraft, dry_run: bool = False) -> PublishResult:
    """发布一条图文 / 动态帖（link_tag 图文=27 / 动态=28，topic_ids 逗号串，img 段内嵌）。"""
    segments = _build_segments(draft)
    topic_ids_str = _topic_ids_str(ctx, draft)
    link_tag = _effective_link_tag(draft)
    if dry_run:
        return PublishResult(
            state=ActionState.VERIFIED,
            note=f"演练：已组装发帖（link_tag={link_tag.value}，topic_ids={topic_ids_str}），未真正发送",
        )
    client = _client(ctx)
    post_id = await client.publish_post(
        title=draft.title,
        segments=segments,
        link_tag=link_tag.value,
        topic_ids_str=topic_ids_str,
    )
    return PublishResult(
        state=ActionState.SEND_UNKNOWN,
        post_id=post_id,
        note="已发出，待回读核验",
    )


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
        title=draft.title,
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
    "publish_post",
    "publish_article",
    "publish_comment",
    "reply_floor",
    "reply_notification",
    "like_post",
    "verify_post_published",
]
