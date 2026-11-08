# -*- coding: utf-8 -*-
"""
回响（local.huixiang-operator）· 配置模型
================================================
【真实 SDK 契约（2026-11-04 在部署环境实测核对，勿凭记忆改）】
- 配置类必须继承 `maibot_sdk.PluginConfigBase`（它本身是 pydantic BaseModel）。
- **本文件禁止写 `from __future__ import annotations`**：pydantic 解析字段类型会失败，
  这是 life-frequency 插件头部明确记录的硬约束，多个插件都遵守。
- 每个字段都要给默认值。
- `[plugin]` 段（config_version）必须由模型定义，只写在 config.toml 里不够。

【界面全中文（2026-11-06 用户反馈：点进去全是英文看不懂）】
宿主生成配置页时：
  - 字段名默认直接用**属性名**（`enabled` / `mode` / `browse_interval_seconds`…），
    用户看到的就是这些英文标识符；
  - 字段说明取 `Field(description=...)`；
  - 真正显示给用户的标题取 `json_schema_extra["label"]`，提示取 `["hint"]`；
  - 下拉选项的候选值取类型注解里的 `Literal`，**没有中文映射机制**。
所以本文件的做法是：
  1. 每个字段都写 `json_schema_extra={"label": 中文, "hint": 中文说明}`；
  2. 所有枚举字段一律用 `str` 而非 `Literal["dry_run"]`，默认值直接写中文
     （如 `"演练"` / `"自动"`），这样下拉框里显示的就是中文；
     取值判断在 `layers/_cfg.py` 与业务层做「中文 → 内部值」的兼容映射，
     老配置里写的 `dry_run` / `auto` / `all` / `selected` 仍然能读。
  3. 段标题用 `__ui_label__`，段说明用类 docstring。

【设计约束】
- 社区（分区）与话题候选「运行时从接口动态拉取」，绝不写死静态白名单。
- 默认 enabled=False、mode="演练"：新插件先不启用、先演练。
- 演练绝不占用真实发帖额度，也不累加发帖间隔。
"""

import logging
from typing import Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

logger = logging.getLogger(__name__)

# 枚举选项一律用 Literal[中文...]，宿主会自动渲染成下拉框：
#   - 用户只能从下拉里选，不可能打错字（用户 2026-11-06 明确要求）；
#   - Literal 的候选值就是界面显示值，所以直接写中文，不用再翻译。
# 下面每个枚举类都配一个 field_validator，把老配置里的英文值（dry_run/auto/all/...）
# 静默转成中文，避免历史配置加载失败。
MODE_CHOICES = Literal["演练", "自动"]
PARTITION_MODE_CHOICES = Literal["全部分区", "仅指定分区"]
TRIGGER_CHOICES = Literal["按帖数", "按时间", "两者都看"]
TONE_CHOICES = Literal["平和", "活泼", "克制", "毒舌"]

# 写内容用哪个模型。
# 【真机踩坑 2026-11-06】宿主 `replyer` 任务的 model_list 是
# ["ds-official", "feiyu-gemini-flash"]，DeepSeek 排第一，插件只传 task_name 时
# 内容必然由 DeepSeek 生成。用户明确要求「不要 DeepSeek，用 GM」，所以这里必须
# 允许直选具体模型名（SDK 契约：传 model_name 一律直选，绕过任务里的优先级）。
# 候选值 = 服务器 model_config.toml 里 model_type="llm" 的 name，改模型时同步这里。
MODEL_CHOICES = Literal[
    "feiyu-gemini-flash",      # Gemini 3.8 Flash（用户说的「GM」）
    "glm-4-flash",             # 智谱 GLM-4 Flash
    "feiyu-ds-flash",          # 飞鱼中转的 DeepSeek
    "deepseek-v4.1-flash",     # 中转 DeepSeek
    "ds-official",             # DeepSeek 官方
    "跟随宿主默认",              # 不指定模型，用 replyer 任务里的第一个
]

_LEGACY_MODE = {"dry_run": "演练", "auto": "自动"}
_LEGACY_PARTITION = {"all": "全部分区", "selected": "仅指定分区"}
_LEGACY_TRIGGER = {"count": "按帖数", "time": "按时间", "both": "两者都看"}


# ---------------------------------------------------------------------------
# 中文标签工具：统一写法，避免每个字段手写一长串 json_schema_extra。
# ---------------------------------------------------------------------------

def L(label: str, hint: str = "", **extra) -> dict:
    """构造字段的中文标签元数据（label / hint，可附加其它 UI 字段）。"""
    data: dict = {"label": label}
    if hint:
        data["hint"] = hint
    data.update(extra)
    return {"json_schema_extra": data}


def C(label: str, hint: str, default: str, **extra):
    """构造「中文字符串字段 + 中文标签」的 Field。

    枚举类配置刻意用 str + 中文默认值：宿主的 select 候选只能来自 Literal，
    没有「值 → 中文」的映射表，所以直接把值本身写成中文，界面上才看得懂。
    """
    return Field(default=default, description=hint, **L(label, hint, **extra))


def B(label: str, hint: str, default: bool):
    """构造「开关字段 + 中文标签」的 Field。"""
    return Field(default=default, description=hint, **L(label, hint))


def N(label: str, hint: str, default: int | float, **extra):
    """构造「数字字段 + 中文标签」的 Field。"""
    return Field(default=default, description=hint, **L(label, hint, **extra))


def T(label: str, hint: str, default: str = "", **extra):
    """构造「文本框字段 + 中文标签」的 Field。"""
    return Field(default=default, description=hint, **L(label, hint, **extra))


# ---------------------------------------------------------------------------
# 社区（分区）/ 话题候选：运行时从接口动态拉取，不落地静态白名单。
# ---------------------------------------------------------------------------

def fetch_communities() -> list[str]:
    """占位：运行时从接口动态拉取社区（分区）候选清单。

    接入后改为调用接入层分区目录（layers/inbound.fetch_partitions）并回填下拉。
    """
    raise NotImplementedError(
        "待接入：社区（分区）候选动态拉取（来源：layers/inbound.fetch_partitions，"
        "运行时从接口拉取，不用静态白名单冒充）"
    )


def fetch_hashtags(partition: str) -> list[str]:
    """占位：按所选分区从接口动态拉取推荐话题。

    接入后改为「选完分区再按分区拉取话题」。
    """
    raise NotImplementedError(
        "待接入：话题候选动态拉取（来源：layers/sense.fetch_hot_topics，"
        "运行时从接口拉取，不用静态白名单冒充）"
    )


# ---------------------------------------------------------------------------
# 十一段配置：basic / login / browse / interact / publish / summarize / image /
#             create / sense / guard / links，另加 plugin 元信息段。
# 全部字段的 label / hint 一律中文。
# ---------------------------------------------------------------------------

class PluginSection(PluginConfigBase):
    """插件元信息。这一段的版本号由插件自己维护，通常不用改。"""

    __ui_label__ = "插件信息"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，由插件维护，用来识别配置格式；通常不需要手动修改。",
        **L("配置版本", "由插件维护，通常不用改", disabled=True),
    )


class BasicConfig(PluginConfigBase):
    """启用开关与运行模式。建议先开着「演练」跑几天，确认没问题再切「自动」。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "settings-2"
    __ui_order__ = 1

    enabled: bool = B("启用插件", "关掉后插件只加载不干活，也不启动后台任务", False)
    mode: MODE_CHOICES = Field(
        default="演练",
        description="演练：完整走一遍流程但不真发、不占额度；自动：会真的发到小黑盒。",
        **L("运行模式", "演练不真发；自动才会真正发布。老配置里的 dry_run / auto 会自动转成中文"),
    )
    profile_id: str = T("账号档案名", "多账号时用来区分数据目录，单账号留空即可", "")

    @field_validator("mode", mode="before")
    @classmethod
    def _legacy_mode(cls, value):
        raw = str(value or "").strip()
        return _LEGACY_MODE.get(raw.lower(), raw) or "演练"


class ModelConfig(PluginConfigBase):
    """写内容（发帖 / 评论）用哪个模型。

    留空或选「跟随宿主默认」时会用宿主 `replyer` 任务里的第一个模型——
    本机那条任务第一位是 DeepSeek，所以想用别的模型必须在这里显式选。
    """

    __ui_label__ = "模型设置"
    __ui_icon__ = "cpu"
    __ui_order__ = 2      # 紧跟基础设置，用户一眼能看到

    text_model: MODEL_CHOICES = Field(
        default="feiyu-gemini-flash",
        description="写帖子、写评论都用这个模型。选「跟随宿主默认」则用 replyer 任务的第一个模型。",
        **L("写内容用哪个模型", "默认 Gemini；不要 DeepSeek 就保持默认，别选「跟随宿主默认」"),
    )
    temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="越高越发散，越低越稳。写帖子建议 0.8~1.1。",
        **L("发散程度", "0~2；越高越发散，越低越稳。默认 1.0"),
    )
    max_tokens: int = N(
        "最多生成多少 token",
        "太小会写到一半被截断；帖子一般 2048 够用",
        2048,
    )


class LoginConfig(PluginConfigBase):
    """小黑盒扫码登录。凭据存在数据目录里，不写进配置文件、不进日志。"""
    __ui_label__ = "登录设置"
    __ui_icon__ = "key"
    __ui_order__ = 3

    auto_login: bool = B(
        "启动时自动检查登录",
        "每次启动检查一次凭据是否还有效，失效会在日志里提示；不会自动弹二维码",
        False,
    )


class BrowseConfig(PluginConfigBase):
    """主动刷小黑盒动态。刷到的东西会存起来，供互动和发帖使用。"""

    __ui_label__ = "自动浏览"
    __ui_icon__ = "list"
    __ui_order__ = 3

    enabled: bool = B("开启自动浏览", "勾选后按下面的间隔自己刷", True)
    partition_mode: PARTITION_MODE_CHOICES = Field(
        default="全部分区",
        description="全部分区：刷到什么读什么；仅指定分区：只刷下面勾选的那几个。",
        **L("浏览范围", "全部分区刷推荐流；仅指定分区只刷下面勾选的分区"),
    )
    selected_partitions: list[str] = Field(
        default_factory=list,
        description="只在上面的浏览范围选「仅指定分区」时生效；分区名由接口动态拉取。",
        **L("指定分区", "从下拉里勾选；范围选「全部分区」时这里不用管"),
    )

    @field_validator("partition_mode", mode="before")
    @classmethod
    def _legacy_partition_mode(cls, value):
        raw = str(value or "").strip()
        return _LEGACY_PARTITION.get(raw.lower(), raw) or "全部分区"

    browse_interval_seconds: int = N("浏览间隔（秒）", "两轮浏览之间隔多久，默认 300 秒（5 分钟）", 300)
    posts_per_round: int = N("每轮看多少条", "每轮拉多少条帖子来判断要不要互动", 10)
    refresh_partitions_interval_minutes: int = N(
        "分区列表刷新间隔（分钟）",
        "分区目录会变，定期重新拉一次，默认 360 分钟（6 小时）",
        360,
    )
    follow_partition_feed: bool = B(
        "跟随分区动态流",
        "开启后跟着分区的动态流走，而不是只看总榜",
        False,
    )


class InteractConfig(PluginConfigBase):
    """看到有意思的帖子去评论，以及别人 @ 你 / 回复你时的自动回应。"""

    __ui_label__ = "互动评论"
    __ui_icon__ = "message-circle"
    __ui_order__ = 4

    comment_enabled: bool = B("开启自动评论", "勾选后会对刷到的帖子留下评论", True)
    comment_interval_seconds: int = N("评论间隔（秒）", "两条评论之间至少隔多久，默认 600 秒", 600)
    max_comments_per_round: int = N("每轮最多评论几条", "防止一轮刷太多把额度用光", 3)
    reply_mentions: bool = B("回应 @ 我的人", "别人 @ 你时自动回复", True)
    reply_replies: bool = B("回应回复我的人", "别人回复了你的评论时自动接话", True)


class PublishConfig(PluginConfigBase):
    """自己发帖的设置：发到哪个分区、挂什么话题、什么时候能发。"""

    __ui_label__ = "发帖① 时段 / 分区 / 话题"
    __ui_icon__ = "send"
    __ui_order__ = 5

    default_partition: str = T(
        "默认发到哪个分区",
        "填分区名（如「数码硬件」「AI」），留空则发到动态、不指定分区",
        "",
    )
    linked_topics_enabled: bool = B(
        "自动多挂几个分区",
        "勾选后除了默认分区，还会按正文提到的内容再挂 1~2 个相关分区；"
        "小黑盒提示「错误勾选会被处理」，所以只挂同类的，不按热度乱凑",
        True,
    )
    max_topics: int = N("最多挂几个分区", "发帖时勾选的社区（小黑盒字段叫 topic_ids）；真实帖子常见 1~2 个", 5)
    community_allowlist: list[str] = Field(
        default_factory=list,
        description="固定要挂的分区名（可选）。留空则按「默认分区 + 正文匹配」挑。",
        **L("固定挂这些分区", "填分区名，逗号分隔，例如 盒友杂谈,PC游戏；留空则自动挑"),
    )
    custom_topics: list[str] = Field(
        default_factory=list,
        description="（同「固定挂这些分区」，老配置项，保留兼容）",
        **L("自定义分区", "老配置项，等同上一条"),
    )
    hashtags_enabled: bool = B(
        "自动带话题标签",
        "勾选后按正文内容，从真实帖子里采到的话题池里挑 #标签；匹配不上就不带",
        True,
    )
    max_hashtags: int = N("最多带几个话题", "话题是 # 标签，和分区不是一回事；真实帖子常见 3~5 个", 5)
    custom_hashtags: list[str] = Field(
        default_factory=list,
        description="每次都带上的话题名（可选）。",
        **L("固定带这些话题", "填话题名，逗号分隔，例如 盒友日常,沙雕；留空则自动挑"),
    )
    readback_verify: bool = B(
        "发完回读确认",
        "发出去之后再读一次，确认真的发出去了；建议保持开启",
        True,
    )


class SummarizeConfig(PluginConfigBase):
    """发帖：刷到有意思的就发一条，没什么想说的就不发。字数段数在这里控制。"""

    __ui_label__ = "发帖② 开关 / 字数"
    __ui_icon__ = "pen-tool"
    __ui_order__ = 6

    enabled: bool = B(
        "允许发帖",
        "到「发帖时段」内、有内容、自己也觉得想说两句，就发一条；"
        "三项缺一就不发，不会硬凑",
        True,
    )
    trigger_by: TRIGGER_CHOICES = Field(
        default="按帖数",
        description="旧版按「攒够 N 条」触发，现在已经不用了，忽略即可。",
        **L("［已作废］触发方式", "旧版本用的，现在不用管，发不发由「发帖时段」和内容决定"),
    )
    count_threshold: int = N("［已作废］攒够多少条开始写", "旧版本用的，现在不用管", 10)
    time_hours: int = N("［已作废］隔多少小时写一次", "旧版本用的，现在不用管", 24)

    @field_validator("trigger_by", mode="before")
    @classmethod
    def _legacy_trigger_by(cls, value):
        raw = str(value or "").strip()
        return _LEGACY_TRIGGER.get(raw.lower(), raw) or "按帖数"



class ImageConfig(PluginConfigBase):
    """发帖时配图。生图接口留空就用 MaiBot 本体「画图」那套配置。"""

    __ui_label__ = "发帖③ 配图"
    __ui_icon__ = "image"
    __ui_order__ = 7

    with_image: bool = B("发帖带配图", "勾选后每篇帖子会配图；生图失败会自动降级成纯文字", True)
    image_count: int = N("配几张", "建议 1~3 张，太多容易发不出去", 1)
    image_api_base_url: str = T("生图接口地址", "留空则用 MaiBot 本体「画图」的配置", "")
    image_api_key: str = T("生图接口密钥", "留空则用本体配置；填了不会写进日志", "", password=True)
    image_model: str = T("生图模型名", "留空则用本体配置", "")
    image_size: str = T(
        "生图尺寸",
        "例如 3840x2160 或 4096x4096（4K 大图）；留空用模型默认。"
        "超过 5 MB 会自动压成 JPEG 再上传，不会因为太大传不上去",
        "",
    )


class CreateConfig(PluginConfigBase):
    """说话的语气和排版。"""

    __ui_label__ = "创作设置"
    __ui_icon__ = "edit-3"
    __ui_order__ = 8

    tone: TONE_CHOICES = Field(
        default="平和",
        description="决定写出来的东西偏哪种感觉；平和最像普通网友，毒舌容易得罪人。",
        **L("说话语气", "从下拉里选；平和最稳，毒舌容易得罪人"),
    )
    no_blank_line_between_paragraphs: bool = B(
        "段与段之间不空行",
        "勾选后段落紧挨着；很多人不喜欢中间空一大块",
        True,
    )


class SenseConfig(PluginConfigBase):
    """看她现在「有没有心情说话」：睡觉的时候不打扰，电量低的时候少说两句。"""

    __ui_label__ = "生活感知"
    __ui_icon__ = "heart"
    __ui_order__ = 9

    use_life_frequency: bool = B(
        "接入生活频率插件",
        "装了 life-frequency 可以勾上，会按她的作息和情绪决定要不要发",
        False,
    )
    pause_on_sleep: bool = B("睡觉时不发", "检测到在睡觉就不发帖不评论", True)
    social_battery_threshold: int = N(
        "社交电量低于多少就少说话",
        "0~100；没接生活频率插件时不生效，不会误拦",
        40,
    )


class GuardConfig(PluginConfigBase):
    """兜底保护：每天最多发多少、隔多久发一次、遇到验证码停手。额度按动作分开计。"""

    __ui_label__ = "安全护栏"
    __ui_icon__ = "shield"
    __ui_order__ = 10

    daily_quota_post: int = N("每天最多发几篇帖", "到量当天不再发", 3)
    daily_quota_comment: int = N("每天最多评论几条", "到量当天不再评论", 20)
    daily_quota_like: int = N("每天最多点赞几次", "到量当天不再点赞", 50)
    daily_quota_reply: int = N("每天最多回复几条", "到量当天不再回复别人", 15)
    de_ai_flavor: bool = B("自动去 AI 味", "把「首先/其次/总之」这类套话和排比句去掉", True)
    ai_flavor_threshold: float = N(
        "AI 味阈值（越高越宽松）",
        "0~100；低于这个分数才允许发。评论固定用更严的标准",
        60,
    )
    captcha_circuit_break: bool = B(
        "遇到验证码就停手",
        "一旦碰上验证码，当天动作全部停止，冷却 5 分钟；强烈建议保持开启",
        True,
    )
    dry_run_no_quota_cost: bool = B(
        "演练不占用真实额度",
        "务必保持开启：演练时不能计入额度，也不能累加发帖间隔",
        True,
    )


class LinksConfig(PluginConfigBase):
    """发出去之后，顺便通知别的插件一起联动。全部可选，关掉也不影响主流程。"""

    __ui_label__ = "联动设置"
    __ui_icon__ = "link"
    __ui_order__ = 11

    voice_broadcast: bool = B("顺便语音播报", "发帖后用语音插件念一句", False)
    qzone_sync: bool = B("同步到 QQ 空间", "同一条内容也发到 QQ 空间", False)
    diary_link: bool = B("记进日记", "把发帖这件事记到日记插件里", False)


class HuiXiangConfig(PluginConfigBase):
    """回响：自动刷小黑盒、评论互动、有想说的就发一条的插件。默认演练模式，不会真发。"""

    __ui_label__ = "回响"
    __ui_icon__ = "zap"
    __ui_order__ = 0

    plugin: PluginSection = Field(default_factory=PluginSection, description="插件信息")
    basic: BasicConfig = Field(default_factory=BasicConfig, description="基础设置")
    model: ModelConfig = Field(default_factory=ModelConfig, description="模型设置")
    login: LoginConfig = Field(default_factory=LoginConfig, description="登录设置")
    browse: BrowseConfig = Field(default_factory=BrowseConfig, description="自动浏览")
    interact: InteractConfig = Field(default_factory=InteractConfig, description="互动评论")
    publish: PublishConfig = Field(default_factory=PublishConfig, description="发帖：时段 / 分区 / 话题")
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig, description="发帖")
    image: ImageConfig = Field(default_factory=ImageConfig, description="发帖：配图")
    create: CreateConfig = Field(default_factory=CreateConfig, description="创作设置")
    sense: SenseConfig = Field(default_factory=SenseConfig, description="生活感知")
    guard: GuardConfig = Field(default_factory=GuardConfig, description="安全护栏")
    links: LinksConfig = Field(default_factory=LinksConfig, description="联动设置")
