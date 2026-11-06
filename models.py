# -*- coding: utf-8 -*-
"""
回响插件：全部数据结构（dataclass）与枚举。

【职责边界】
本文件只放「纯数据」：分区、话题、帖子草稿、配图、评论目标、通知、动作记录、额度计数、
核验结果、发布结果、生活轨迹、游标，以及动作状态 / 运行模式 / 链接类型等枚举。
状态「迁移规则」与「持久化接口」放在 state.py，本文件不写任何逻辑与 IO。

【导入约定】
插件目录由宿主加入 sys.path，各模块以顶层模块名互引（目录名含连字符，不作为包导入）。
下层通过宿主 ctx.api.call(...) 复用现有能力，本文件不涉及任何网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ===================== 枚举 =====================

class RunMode(str, Enum):
    """运行模式：演练与真实必须严格隔离（额度、去重、状态各走各的桶）。"""

    DRY_RUN = "dry_run"  # 演练：只走流程、不真正发布、不占用真实额度
    REAL = "real"        # 真实：真正发布，占用每日真实额度


class ActionState(str, Enum):
    """动作状态机的四个状态（迁移规则见 state.ALLOWED_TRANSITIONS）。"""

    DRAFT = "draft"                  # 草稿：已生成内容，尚未通过护栏核验
    VERIFIED = "verified"            # 已核验：护栏放行，可进入发送
    SEND_UNKNOWN = "send_unknown"    # 发送结果未知：请求已发出但回读超时 / 未确认，禁止盲目重试
    FAILED = "failed"                # 失败：明确失败，可回到草稿重来


class LinkTag(int, Enum):
    """发布类型标记（历史踩坑契约：数值不能写错）。"""

    IMAGE_TEXT = 27  # 图文帖
    DYNAMIC = 28     # 动态
    ARTICLE = 11     # 文章


class ImageStatus(str, Enum):
    """配图生命周期状态。"""

    PENDING = "pending"    # 待生成 / 待上传
    READY = "ready"        # 已就绪，可写入正文 img 段
    FAILED = "failed"      # 生成或上传失败，触发纯文字降级


# ===================== 感知层数据 =====================

@dataclass
class LifeTrace:
    """生活轨迹：感知层读出的「现在这个人是什么状态」，决定能不能开口、用什么语气。

    【社交电量的两种「未知」必须区分（真机踩坑 2026-11-06）】
    - None = 没接生活频率插件 / 接口没返回 → **未知**，不该拦截任何动作；
    - 0.0  = 明确读到「没电了」 → 该拦。
    之前统一用 0.0 兜底，导致没接 life-frequency 时社交电量恒为 0，
    can_speak_now 永远返回「当前不宜开口」，发帖流水线全被挡死。
    """

    routine: str = ""                 # 作息：如「深夜」「刚起床」「午后」
    mood: str = ""                    # 情绪：如「平静」「低落」「兴奋」
    energy: float | None = None        # 体力：0.0~1.0；None 表示未知
    social_battery: float | None = None  # 社交电量：0.0~1.0；None 表示未知（不拦截）
    captured_at: str = ""             # 采集时间（ISO 字符串，由感知层填充）
    extra: dict[str, Any] = field(default_factory=dict)  # 预留扩展字段


@dataclass
class Partition:
    """分区：发帖时必须落到的板块目录项。"""

    id: int = 0                       # 分区 ID（数字）
    name: str = ""                    # 分区中文名
    topic_ids: list[int] = field(default_factory=list)  # 该分区下可选话题 ID（内部 list[int]）
    # 契约备忘：topicIds 在小黑盒接口是「逗号分隔的数字字符串」（如 "12,34,56"），
    # 不是 int 数组；本字段内部用 list[int]，发布层负责拼成逗号字符串（见 contracts.py）。


@dataclass
class Topic:
    """话题：热门话题列表中的一项。"""

    id: int = 0                       # 话题 ID（数字，见 Partition 的契约备忘：发布时拼逗号字符串）
    name: str = ""                    # 话题名
    heat: float = 0.0                 # 热度，供创作层挑选


# ===================== 创作层数据 =====================

@dataclass
class ImageAsset:
    """配图：一张准备随帖发布的图片。"""

    local_ref: str = ""               # 本地引用 / 相对名（路径经 ctx.paths.* 注入，禁止绝对路径）
    alt_text: str = ""                # 图片说明（无障碍文案）
    status: ImageStatus = ImageStatus.PENDING
    remote_ref: str = ""              # 上传成功后的远端句柄
    width: int = 0                    # 图片宽（上传后写入正文 img 段）
    height: int = 0                   # 图片高（上传后写入正文 img 段）
    # 契约备忘：图片最终要放进正文 text 的 img 段，而不是单独字段。


@dataclass
class PostDraft:
    """帖子草稿：创作层产出、护栏层核验、发布层消费的核心载体。"""

    title: str = ""                                   # 标题
    text: str = ""                                    # 正文（图片以 img 段内嵌于此）
    link_tag: LinkTag = LinkTag.IMAGE_TEXT            # 发布类型：图文=27 / 动态(无图)=28 / 文章=11
    partition: Partition | None = None                # 目标分区
    topic_ids: list[int] = field(default_factory=list)  # 话题 ID（内部 list[int]；发布时拼成逗号分隔字符串）
    images: list[ImageAsset] = field(default_factory=list)
    polished: bool = False                            # 是否已过中文口语清洗
    ai_flavor_score: float = 0.0                      # AI 味打分（越高越像机器，护栏层据此拦截）


@dataclass
class CommentTarget:
    """评论目标：评论 / 回复 / 点赞所指向的帖子与楼层。"""

    post_id: str = ""                 # 帖子 ID
    # 契约备忘：推荐流里帖子 ID 字段是 postId 而不是 id，取值时别拿错。
    root_id: str | None = None        # 根楼 ID：回复楼中楼时必须带上，否则发错层级
    parent_id: str | None = None      # 直接回复的那条评论 ID（楼中楼里 parent ≠ root）
    floor: int | None = None          # 目标楼层号（可选）
    reply_to: str | None = None       # 被回复用户标识（可选）


@dataclass
class SummaryDraft:
    """感想总结草稿：对一段时间浏览 / 互动内容的总结帖（200-400 字、2-4 段）。"""

    title: str = ""                   # 标题
    text: str = ""                    # 正文（200-400 字、2-4 段）
    source_post_ids: list[str] = field(default_factory=list)  # 汇总的来源帖子 ID
    partition: Partition | None = None  # 目标分区
    topic_ids: list[int] = field(default_factory=list)  # 话题 ID（内部 list[int]，发布时拼逗号字符串）
    paragraphs: int = 0               # 段数（约束 2-4 段）
    chars: int = 0                    # 字数（约束 200-400 字，按中文口径统计）
    ai_flavor_score: float = 0.0      # AI 味打分（0-100，护栏层按正文 60 阈值判）


# ===================== 接入层数据 =====================

@dataclass
class Credentials:
    """登录凭据：扫码登录成功后的会话凭据（只存宿主数据目录，绝不进 config）。"""

    user_id: str = ""                 # 用户 ID
    device_id: str = ""               # 设备 ID（签名客户端需要的字段之一）
    cookie: str = ""                  # Cookie（签名客户端四字段之一，不写进日志）
    token: str = ""                   # 令牌（如有）
    logged_in: bool = False           # 是否已登录
    expires_at: str = ""              # 过期时间（ISO 字符串）


@dataclass
class BrowseItem:
    """浏览项：接入层主动浏览 / 分区动态拉取读到的一条内容。"""

    post_id: str = ""                 # 帖子 ID（推荐流取 postId 字段）
    title: str = ""                   # 标题
    excerpt: str = ""                 # 摘要 / 正文开头
    partition_id: int = 0             # 所属分区 ID
    partition_name: str = ""          # 所属分区名
    author_name: str = ""             # 作者名
    heat: float = 0.0                 # 热度
    published_at: str = ""            # 发布时间（ISO 字符串）
    raw: dict[str, Any] = field(default_factory=dict)  # 原始数据，预留扩展


# ===================== 通知 / 记录 / 额度 =====================

@dataclass
class Notification:
    """通知：需要送到 QQ 空间 / 语音播报等出口的一条消息。"""

    channel: str = ""                                  # 出口通道名（取自 contracts 常量）
    level: str = "info"                                # 级别：info / warn / error
    message: str = ""                                  # 中文通知正文
    created_at: str = ""                               # 生成时间
    payload: dict[str, Any] = field(default_factory=dict)  # 附加数据


@dataclass
class ActionRecord:
    """动作记录：一次发布动作在状态机里的完整留痕，用于幂等与排障。"""

    action_id: str = ""                 # 动作唯一 ID
    fingerprint: str = ""              # 去重指纹（见 state.make_fingerprint）
    state: ActionState = ActionState.DRAFT
    mode: RunMode = RunMode.DRY_RUN     # 演练 / 真实隔离的关键字段
    created_at: str = ""
    updated_at: str = ""
    note: str = ""                     # 中文备注


@dataclass
class QuotaCount:
    """每日额度计数：按 (模式, 日期) 分桶。"""

    mode: RunMode = RunMode.DRY_RUN     # 演练与真实必须落在不同键，互不扣减
    day: str = ""                      # 日期，格式 YYYY-MM-DD
    used: int = 0                      # 已用次数
    limit: int = 0                     # 上限次数


@dataclass
class VerifyResult:
    """核验结果：护栏放行判定 / 发帖后回读核验的通用返回体。"""

    ok: bool = False                   # 是否通过
    code: str = ""                     # 结果码（如 quota_ok / captcha_block / verify_timeout）
    message: str = ""                  # 中文说明，便于日志与排障
    post_id: str | None = None         # 回读核验到的帖子 ID（若适用）
    detail: dict[str, Any] = field(default_factory=dict)  # 附加明细


@dataclass
class PublishResult:
    """发布结果：发布层出口的返回体，含状态机落点与回读结论。"""

    action_id: str = ""
    state: ActionState = ActionState.DRAFT
    post_id: str | None = None
    verified: VerifyResult | None = None  # 发帖后回读核验的结果
    note: str = ""                        # 中文备注


@dataclass
class Cursor:
    """游标：增量拉取热门话题 / 分区目录时的位置标记（幂等骨架用）。"""

    name: str = ""                     # 游标名（如 hot_topics / partitions）
    value: str = ""                    # 游标值（由上游返回的下一页标记）
    updated_at: str = ""
