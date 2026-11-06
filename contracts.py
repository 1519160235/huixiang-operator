# -*- coding: utf-8 -*-
r"""
回响插件：对外依赖契约（骨架阶段）。

【两类依赖（口径统一，勿混为一谈；见 DESIGN.md「发布层：直连小黑盒」）】

A 类「宿主能力，走 ctx.api.call(...) 复用现有插件」——仅感知层与外围联动：
- 感知层：org.orge-8.life-frequency（get_current_context 读生活状态 + life_state.json 回退）。
- 外围联动：宿主生图、语音播报、QQ 空间、日记（两阶段素材 / 成文）、WebUI 设置读取。
以上统一以 `ctx.api.call(插件ID.名称, 参数)` 复用，换提供方只改配置里的名字、不重写；
接口名字符串常量与降级策略见下方登记。

B 类「本插件自带客户端，直连小黑盒 API」——发布层与接入层，不经 MCP 8757 中转：
- 设计决策：发布层与接入层的读写由本插件自带的签名客户端直连小黑盒 API，明确「不经过
  127.0.0.1:8757 的 MCP 中转」（与 A 类通道无关，见 DESIGN.md「为什么不用 MCP 中转」）。
- 签名客户端来自适配器 vendor/（`tmp/fusion-src/xiaoheihe-adapter/vendor/`，以
  request_signing.py 的 hkey 算法为准，依赖最少、只对齐一套，不复制三套）。
- 参考来源（实现路径，仅注释备忘，骨架阶段不搬代码、不接真实接口）：
  - `tmp/fusion-src/xiaoheihe-adapter/vendor/request_signing.py`（hkey / 设备 ID / Cookie 四字段）
  - `tmp/fusion-src/xiaoheihe-adapter/vendor/community_client.py`（帖子 / 评论 / 通知）
  - `tmp/fusion-src/xiaoheihe-adapter/vendor/media_upload.py`（图床上传）
  不接 127.0.0.1:8757 的小黑盒 MCP 中转。

【参考来源（A 类，实现路径）】
- 生活轨迹能力：org.orge-8.life-frequency 插件（get_current_context 接口 + life_state.json 回退）。
- 宿主生图能力：宿主内置生图 API（经 ctx.api.call 调用，真实名待与宿主插件清单对齐）。
- WebUI 设置读取：webui_get_settings 类 WebUI API（经 ctx.api.call 调用）。
- 通知出口：QQ 空间 / 语音播报 / 日记相关插件 API（经 ctx.api.call 调用）。

【本文件职责】
只登记「A 类要复用的现有插件 API 名字符串常量」「B 类直连的来源路径备忘」与「降级策略」。
本版仍只是骨架：不接真实 API、不发起任何 HTTP 调用、不实现签名算法；B 类的读写域名分流
与 hkey 重算留待后续阶段由自带签名客户端实现。所有接口在本骨架中都是占位。

【路径约定 / 历史技术债（重要）】
历史二创曾把模型配置文件的绝对路径（含盘符）写死在代码里，属技术债。本骨架一律禁止
写死绝对路径：文件位置通过 `ctx.paths.*` 解析，或由构造函数注入；常量里只保留「相对名」。
上列 B 类来源路径仅作「未来从哪儿搬」的注释备忘，不是运行时读取路径。
"""

from __future__ import annotations

# 插件标识：对外注册、日志前缀、额度命名空间统一使用此 ID。
PLUGIN_ID = "local.huixiang-operator"

# A 类宿主能力调用通道：感知层与外围联动（life-frequency / 生图 / 语音 / QQ 空间 / 日记）
# 复用现有插件的入口（骨架阶段不真正调用），以 `ctx.api.call(插件ID.名称, 参数)` 形式复用。
# 注意：B 类发布层不走此通道，而是由本插件自带签名客户端直连小黑盒 API（见模块注释、layers/publish.py）。
CALL_CHANNEL = "ctx.api.call"

# ===== 感知层依赖 =====

# 生活轨迹：已知可用的现有插件接口。返回作息 / 情绪 / 体力 / 社交电量等上下文。
API_LIFE_CONTEXT = "org.orge-8.life-frequency.get_current_context"

# 上一条接口失败时的回退目标：直读生活状态文件。
# 注意：这里只登记「相对名」，真实路径须经 ctx.paths.state_dir 之类注入解析，禁止写绝对路径。
FALLBACK_LIFE_STATE_FILE = "life_state.json"

# 热门话题拉取：名称待与宿主 / 现有插件清单对齐（骨架阶段不调用）。
# TODO 待接入：从现有小黑盒相关插件的能力清单取真实 API 名后替换。
API_HOT_TOPICS = "TODO:host.topic.hot_list"

# 分区目录拉取：名称待对齐（骨架阶段不调用）。
# TODO 待接入：从现有分区目录能力取真实 API 名后替换。
API_PARTITION_CATALOG = "TODO:host.partition.catalog"

# ===== 创作层依赖 =====

# 宿主生图 API：名称待与宿主插件清单对齐（骨架阶段不调用）。
# TODO 待接入：确认宿主生图入口名后替换；生图失败走 create.fallback_text_only 降级纯文字。
API_HOST_IMAGE_GEN = "TODO:host.image.generate"

# ===== WebUI 依赖 =====

# WebUI 设置读取类 API（用户给的线索名），供配置面板查询使用。
API_WEBUI_GET_SETTINGS = "webui_get_settings"

# ===== 通知出口依赖 =====

# QQ 空间通知 API：名称待对齐（骨架阶段不调用）。
# TODO 待接入：从现有 QQ 空间插件能力清单取真实 API 名后替换。
API_NOTIFY_QQZONE = "TODO:host.notify.qqzone"

# 语音播报 API：名称待对齐（骨架阶段不调用）。
# TODO 待接入：从现有语音播报插件能力清单取真实 API 名后替换。
API_NOTIFY_VOICE = "TODO:host.notify.voice"

# 日记联动 API：名称待对齐（骨架阶段不调用）。
# TODO 待接入：从现有日记插件能力清单取真实 API 名后替换。
API_DIARY_LINK = "TODO:host.diary.link"

# ===== 降级策略集中登记 =====
# 键为 API 名常量，值为中文降级说明。护栏层与创作层据此决定失败时如何兜底。
FALLBACK_POLICY: dict[str, str] = {
    API_LIFE_CONTEXT: (
        "调用失败时回退直读 life_state.json（相对名经 ctx.paths.* 注入解析），"
        "仍失败则判定『现在不宜开口』，本轮不发布。"
    ),
    API_HOST_IMAGE_GEN: (
        "生图失败时丢弃配图、降级为纯文字帖（见 create.fallback_text_only），"
        "不阻断发布流程，也不额外占用额度。"
    ),
    API_WEBUI_GET_SETTINGS: "读取失败时回退到配置模型默认值，不向用户抛异常。",
    API_NOTIFY_QQZONE: "通知失败只记中文日志，不影响主发布结果。",
    API_NOTIFY_VOICE: "播报失败只记中文日志，不影响主发布结果。",
    API_DIARY_LINK: "日记联动失败只记中文日志，不影响主发布结果。",
}

# ===== 关键事实契约（历史踩坑，集中备忘为常量） =====
# 说明：以下数字 / 字段名均为「待填实现时遵守的契约常量」，B 类直连实现按此组装 payload；
# 更完整的逐条说明见 layers/publish.py 模块注释。

# ---- 发布类型 link_tag（二创线上验证 27/28，MCP 验证 27/11）----
LINK_TAG_IMAGE_TEXT = 27   # 图文帖
LINK_TAG_DYNAMIC = 28      # 动态（无图）
LINK_TAG_ARTICLE = 11      # 文章

# ---- 话题 ID 线格式：topicIds 是「逗号分隔的数字字符串」，不是 int 数组！----
# 旧骨架曾误写「必须是 int 数组」，属错误，已纠正。示例："12,34,56"。
TOPIC_IDS_WIRE_FORMAT_EXAMPLE = "12,34,56"  # 发布时把内部 list[int] 拼成这种逗号字符串
TOPIC_IDS_SEPARATOR = ","                   # 话题 ID 之间用英文逗号连接

# ---- 签名分流（读 / 写走不同域名与 hkey 算法）----
SIGN_READ_DOMAIN = "api.xiaoheihe.cn"        # 读请求（GET）走此域名
SIGN_READ_ALGO = "generate_hkey(path, stamp, nonce)"  # 读请求 hkey 算法
SIGN_WRITE_ALGO = "comment_query"            # 写请求（POST 表单）的签名通道
SIGN_WRITE_HKEY_OFFSET = "stamp+1"           # 写请求 hkey 用 stamp+1
SIGN_WRITE_EXTRA_FIELD = "_rnd"              # 写请求额外携带 _rnd 随机字段

# ---- 图床上传（COS 三步）----
IMAGE_UPLOAD_STEPS = "info/v2 → token/v2 → PUT → callback/v2"

# ---- 楼中楼回复：必须显式带 rootId（root_id），否则上游报 error root ----
REPLY_FLOOR_ROOT_FIELD = "rootId"

# ---- 发帖回读核验：用 get_post / fetch_thread_context，禁用 search_posts ----
VERIFY_GET_POST = "get_post"                              # 按 postId 回读
VERIFY_FETCH_THREAD_CONTEXT = "fetch_thread_context"      # 拉帖上下文回读
VERIFY_FORBIDDEN_SEARCH = "search_posts"                  # 搜索不索引新帖，禁止用于核验

# ---- 分区动态拉取：不写死 Literal ----
PARTITION_CATALOG_SOURCE = "TOPIC_INDEX → parse_catalog 或 searchEditorTopics"

# ---- 护栏阈值（MCP gap.ts / aiflavor.ts / captcha.ts / ratelimit.ts 的具体规则）----
GUARD_AI_COMMENT_THRESHOLD = 45   # 去 AI 味：评论评分 ≥45 判机器
GUARD_AI_BODY_THRESHOLD = 60      # 去 AI 味：正文评分 ≥60 判机器
GUARD_POST_INTERVAL_MINUTES = 30  # 发帖间隔：30 分钟
GUARD_CAPTCHA_COOLDOWN_MINUTES = 5  # 验证码熔断：5 分钟冷却
RATE_LIMIT_PER_ACTION_SECONDS = 5   # 限流：单操作 5 秒
RATE_LIMIT_PER_HOUR = 30            # 限流：每小时 30 次
