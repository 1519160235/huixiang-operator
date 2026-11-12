# -*- coding: utf-8 -*-
"""社区客户端：发帖 / 评论 / 图床 / 回读的直连封装。

来源：小黑盒适配器 `community_client.py`（搬入并改造发帖口径）。
- 继承 vendor.api_client.XiaoheiheApiClient，读写域名分流不变。
- 写请求（发帖 / 评论 / 图床）走 `comment_query(path=path)` 签名（hkey 用 stamp+1），
  每次按路径重算 hkey；读请求（回读 / 通知）复用父类 generate_hkey 签名。
- 发帖口径按 docs/接口契约.md 与 MCP publish.ts 对齐：
  `link_tag` 用数值（图文=27 / 动态=28 / 文章=11），`topic_ids` 用逗号分隔数字字符串，
  正文 `text` 是 JSON 段数组（文字段 + img 段内嵌），不再写死 58144 之类硬编码。

【可测性】
本模块顶层 import vendor.api_client（依赖 httpx，运行时依赖）；纯逻辑测试不 import 本模块，
真实发帖 / 图床 / 回读的验证需登录态 + 网络，留待服务器阶段人工盯发。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional

import httpx

from media_upload import UPLOAD_PATH, public_media_url
from comment_signing import COMMENT_PATH, comment_query
from vendor.api_client import (
    WEB_CLIENT_PARAMS,
    ApiResponse,
    CredentialInvalidError,
    RateLimitedError,
    SendUncertainError,
    XiaoheiheApiClient,
    XiaoheiheApiError,
    _is_relogin_response,
    _merge_post_and_thread_context,
    _upstream_error,
)
from vendor.endpoints import EndpointName
from vendor.models import RoutingTarget, SendResult, ThreadContext
from vendor.parsers import ResponseShapeError, parse_send_result, parse_thread_context


def contains_video(post: Mapping[str, Any]) -> bool:
    """判断帖子是否含视频（用于回读上下文标注）。"""
    if str(post.get("has_video", "")).lower() in {"1", "true"} or post.get("video_url"):
        return True
    if post.get("video") or post.get("videos"):
        return True
    content = post.get("text", post.get("content", []))
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return "<video" in content.lower()
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and (block.get("type") == "video" or contains_video(block))
            for block in content
        )
    return False


@dataclass(slots=True)
class CommunityContext(ThreadContext):
    has_video: bool = False
    comment_count: Optional[int] = None


def parse_community_context(payload: Mapping[str, Any], post_id: str) -> CommunityContext:
    """解析帖子详情为 CommunityContext（含评论分组归并）。"""
    context = parse_thread_context(payload, post_id)
    body = payload.get("result", payload.get("data", payload))
    post = body.get("post", body.get("link", body))
    if not any(key in body for key in ("comments", "comment_list", "children")):
        raise ResponseShapeError("帖子详情缺少评论列表字段")
    raw_comments = body.get("comments", body.get("comment_list", body.get("children")))
    if not isinstance(raw_comments, list) or not all(isinstance(group, dict) for group in raw_comments):
        raise ResponseShapeError("评论列表包含非对象节点")
    comments = []
    for group in context.comments:
        nodes = group.get("comment", group)
        if isinstance(nodes, list):
            if not all(isinstance(node, dict) for node in nodes):
                raise ResponseShapeError("评论分组包含非对象节点")
            # 一个分组的第一条是根楼层，后续条目是该楼的回复。
            if nodes:
                root = dict(nodes[0])
                root["children"] = list(root.get("children", [])) + nodes[1:]
                comments.append(root)
        elif isinstance(nodes, dict):
            comments.append(dict(nodes))
        else:
            raise ResponseShapeError("评论分组不是数组或对象")
    count = post.get("comment_num")
    values = asdict(context)
    values["comments"] = comments
    return CommunityContext(
        **values, has_video=contains_video(post), comment_count=int(count) if count is not None else None
    )


class CommentRejectedError(XiaoheiheApiError):
    """服务端已明确拒绝评论 / 发帖，区别于连接中断后的未知结果。"""


class CommunityClient(XiaoheiheApiClient):
    """在小黑盒直连客户端上叠加发帖 / 图床 / 回读 / 带图评论能力。"""

    async def _request(
        self,
        name: EndpointName,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        form_body: Optional[Dict[str, Any]] = None,
    ) -> ApiResponse:
        if name is not EndpointName.CREATE_COMMENT:
            return await super()._request(name, params=params, json_body=json_body, form_body=form_body)
        if form_body is None or params is not None or json_body is not None:
            raise ValueError("评论创建只接受标准表单")
        return await self._write_form(COMMENT_PATH, form_body, "评论")

    # ===================== 发帖 =====================

    async def publish_post(
        self,
        *,
        title: str,
        segments: List[Dict[str, Any]],
        link_tag: int,
        topic_ids_str: str,
        hashtags: Optional[List[str]] = None,
    ) -> str:
        """发布一篇图文 / 动态帖。

        :param title: 标题（1–80 字）。
        :param segments: 正文 JSON 段数组（`{"type":"text","text":...}` 或 `{"type":"img",...}`）。
        :param link_tag: 发布类型数值（图文=27 / 动态无图=28）。
        :param topic_ids_str: 逗号分隔的数字话题字符串（如 "12,34,56"），非 int 数组。
        :param hashtags: 可选标签列表。
        :return: 新帖 link_id（字符串）。
        """
        if not title.strip() or len(title) > 80:
            raise ValueError("帖子标题必须为 1–80 字")
        if link_tag not in (27, 28):
            raise ValueError("图文 / 动态帖的 link_tag 只能是 27（图文）或 28（动态）")
        if not _valid_topic_ids_str(topic_ids_str):
            raise ValueError("发帖必须指定有效分区 / 话题（逗号分隔数字字符串）")
        if not segments:
            raise ValueError("帖子正文与图片不能同时为空")
        plain = "".join(
            str(seg.get("text", "")) for seg in segments if seg.get("type") in {"text", "html"}
        )
        if not plain and not any(seg.get("type") == "img" for seg in segments):
            raise ValueError("帖子正文与图片不能同时为空")
        if len(plain) > 10000:
            raise ValueError("帖子正文超过 10000 字")
        form = self._post_form(title=title, text=json.dumps(segments, ensure_ascii=False),
                               desc=plain[:100], words_count=len(plain), link_tag=link_tag,
                               topic_ids_str=topic_ids_str, hashtags=hashtags)
        response = await self._write_form("/bbs/app/api/link/post", form, "发帖")
        return self._extract_link_id(response, "发帖")

    async def publish_article(
        self,
        *,
        title: str,
        html: str,
        topic_ids_str: str,
        hashtags: Optional[List[str]] = None,
    ) -> str:
        """发布一篇文章（link_tag=11，正文为 HTML）。"""
        if not title.strip() or len(title) > 80:
            raise ValueError("文章标题必须为 1–80 字")
        if not _valid_topic_ids_str(topic_ids_str):
            raise ValueError("发文章必须指定有效分区 / 话题（逗号分隔数字字符串）")
        if not html.strip():
            raise ValueError("文章正文不能为空")
        form = self._post_form(
            title=title,
            text=json.dumps([{"type": "html", "text": html}], ensure_ascii=False),
            desc=html[:100],
            words_count=len(html),
            link_tag=11,
            topic_ids_str=topic_ids_str,
            hashtags=hashtags,
        )
        response = await self._write_form("/bbs/app/api/link/post", form, "发文章")
        return self._extract_link_id(response, "发文章")

    @staticmethod
    def _post_form(
        *,
        title: str,
        text: str,
        desc: str,
        words_count: int,
        link_tag: int,
        topic_ids_str: str,
        hashtags: Optional[List[str]],
    ) -> Dict[str, str]:
        """组装发帖 / 发文章的公共表单（字段口径对齐 MCP publish.ts）。"""
        return {
            "link_tag": str(link_tag),
            "text": text,
            "title": title,
            "desc": desc,
            "words_count": str(words_count),
            "topic_ids": topic_ids_str,
            "hashtags": json.dumps(hashtags or [], ensure_ascii=False),
            "schedule_ts": "0",
            "view_limit": "1",
            "post_type": "1",
            "original": "1",
            "declaration": "0",
        }

    @staticmethod
    def _extract_link_id(response: ApiResponse, operation: str) -> str:
        """从发帖响应里提取 link_id（顶层或 result 包装）。"""
        if "link_id" in response.payload:
            post_id = str(response.payload["link_id"])
        elif isinstance(response.payload.get("result"), dict):
            result = response.payload["result"]
            post_id = str(result.get("link_id", result.get("linkid", "")))
        else:
            raise SendUncertainError(f"{operation}成功响应未提供帖子 ID", category="send_unknown")
        if not post_id.isascii() or not post_id.isdigit():
            raise SendUncertainError(f"{operation}响应未提供有效帖子 ID", category="send_unknown")
        return post_id

    # ===================== 写请求 / 图床请求 =====================

    async def _write_form(self, path: str, form_body: Dict[str, Any], operation: str) -> ApiResponse:
        """写请求（POST 表单）：comment_query 签名，直连 api.xiaoheihe.cn。"""
        await self.start()
        if self.credentials is None:
            raise CredentialInvalidError("账号未登录", category="credential_invalid")
        if self._client is None:
            raise RuntimeError("小黑盒客户端未初始化")
        await self._limiter.wait()
        cookies = getattr(self.credentials, "cookies", {}) or {}
        heybox_id = str(cookies.get("heybox_id") or cookies.get("user_heybox_id") or "")
        try:
            response = await self._client.post(
                "https://api.xiaoheihe.cn" + path,
                params=comment_query(path=path, heybox_id=heybox_id),
                data=form_body,
                headers={
                    "Origin": "https://www.xiaoheihe.cn",
                    "Referer": "https://www.xiaoheihe.cn/",
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                },
            )
        except httpx.HTTPError as exc:
            raise SendUncertainError(f"{operation}请求连接中断或超时", category="send_unknown") from exc
        if response.status_code == 401:
            raise CredentialInvalidError(f"{operation}接口要求重新登录", category="credential_invalid")
        if response.status_code == 429:
            raise RateLimitedError(f"{operation}接口请求频率受限", category="rate_limited")
        if response.status_code >= 500:
            raise SendUncertainError(f"{operation}接口返回 HTTP {response.status_code}", category="send_unknown")
        if not response.is_success:
            raise CommentRejectedError(f"{operation}接口返回 HTTP {response.status_code}", category="http")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SendUncertainError(f"{operation}接口响应不是对象", category="send_unknown")
        if _is_relogin_response(payload):
            raise CredentialInvalidError(f"{operation}接口要求重新登录", category="credential_invalid")
        if error := _upstream_error(payload):
            raise CommentRejectedError(error, category="upstream_rejected")
        return ApiResponse(payload=payload, cookies={}, headers=dict(response.headers))

    async def _upload_request(
        self, action: str, form: Dict[str, str], params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """图床接口请求（info/v2、token/v2、callback/v2）。"""
        if action not in {"info/v2", "token/v2", "callback/v2"}:
            raise ValueError("未知图床上传操作")
        await self.start()
        if self.credentials is None:
            raise CredentialInvalidError("图片上传需要登录", category="credential_invalid")
        if self._client is None:
            raise RuntimeError("小黑盒客户端未初始化")
        path = UPLOAD_PATH + action
        query = {**WEB_CLIENT_PARAMS, "web_version": "3.0", **(params or {})}
        signed = self._signer.sign(
            "POST", path, params=query, json_body=form,
            signing_key=self.credentials.signing_key, device_id=self._device_id,
        )
        await self._limiter.wait()
        try:
            response = await self._client.post(
                "https://api.xiaoheihe.cn" + path, params=signed.params, data=form,
                headers={**signed.headers, "Referer": "https://www.xiaoheihe.cn/", "Origin": "https://www.xiaoheihe.cn"},
            )
        except httpx.HTTPError as exc:
            raise XiaoheiheApiError("图床接口连接失败，未提交评论", category="upload") from exc
        if response.status_code == 401:
            raise CredentialInvalidError("图床接口拒绝登录凭证", category="credential_invalid")
        if response.status_code == 429:
            raise RateLimitedError("图床接口请求频率受限", category="rate_limited")
        if not response.is_success:
            raise XiaoheiheApiError(f"图床接口返回 HTTP {response.status_code}", category="upload")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ResponseShapeError("图床接口响应不是对象")
        if _is_relogin_response(payload):
            raise CredentialInvalidError("图床接口要求重新登录", category="credential_invalid")
        if payload.get("status") != "ok":
            raise XiaoheiheApiError("图床接口未返回成功状态", category="upload")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ResponseShapeError("图床接口缺少 result 对象")
        return result

    # ===================== 评论 / 回读 =====================

    async def send_media_comment(self, route: RoutingTarget, content: str, urls: List[str]) -> SendResult:
        """发送带图评论（图片 URL 内嵌进 imgs 字段）。"""
        body = {
            "is_cy": "0",
            "link_id": route.post_id,
            "reply_id": route.parent_comment_id or "-1",
            "root_id": route.root_comment_id or "-1",
            "text": content,
            "imgs": ";".join(public_media_url(url) for url in urls),
        }
        response = await self._request(EndpointName.CREATE_COMMENT, form_body=body)
        return parse_send_result(response.payload)

    async def _load_context(self, post_id: str, root: str = "") -> CommunityContext:
        params: Dict[str, Any] = {"link_id": post_id, "h_src": "", "page": 1, "limit": 20}
        if root:
            params["root_comment_id"] = root
        response = await self._request(EndpointName.POST_TREE, params=params)
        return parse_community_context(response.payload, post_id)

    async def fetch_thread_context(
        self, post_id: str, *, root_comment_id: str = "", post_context: Optional[ThreadContext] = None
    ) -> ThreadContext:
        """按 postId 回读帖子上下文（发帖后核验用，禁用 search_posts）。"""
        if post_context is None:
            post_context = await self._load_context(post_id)
        if not root_comment_id:
            return post_context
        thread = await self._load_context(post_id, root_comment_id)
        return _merge_post_and_thread_context(post_context, thread)


def _valid_topic_ids_str(value: str) -> bool:
    """校验 topicIds 线格式：非空、逗号分隔、每段都是数字。"""
    if not isinstance(value, str) or not value.strip():
        return False
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return bool(parts) and all(part.isascii() and part.isdigit() for part in parts)


__all__ = [
    "contains_video",
    "CommunityContext",
    "parse_community_context",
    "CommentRejectedError",
    "CommunityClient",
]
