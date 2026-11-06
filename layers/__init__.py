# -*- coding: utf-8 -*-
"""
回响插件：六块架构包（接入 → 感知 → 创作 → 护栏 → 发布 + 状态/存储）。

【依赖方向（单向，不可倒挂）】
    inbound（接入层）→ sense（感知层）→ create（创作层）→ guard（护栏层）→ publish（发布层）
    状态/存储（state.py）为横切底座，各层读写均经其接口，不直连持久化。
- 接入层负责与小黑盒的会话与信息流接入（扫码登录 / 通知 / 浏览 / 分区）。
- 感知层只读「现在是什么状态、外面有什么可聊的」，不产出内容。
- 创作层据状态产出草稿，不决定是否发布。
- 护栏层决定能不能发、要不要拦，不改内容。
- 发布层只负责把已放行的草稿送出去并回读核验，不做业务判断。

【复用约定：两类依赖，别混】
A 类宿主能力（生活轨迹、宿主生图、语音、QQ 空间、日记）：感知层与创作层走
`ctx.api.call(...)` 复用现有插件，不自己发请求；名字与降级策略见 contracts.py。
B 类小黑盒读写：接入层与发布层由本插件自带的签名客户端直连（vendor 来自适配器
`tmp/fusion-src/xiaoheihe-adapter/vendor/`），不经 127.0.0.1:8757 的 MCP 中转；
骨架阶段不实现，未来按 contracts.py 的关键事实常量逐条接入。
发布层涉及的历史字段契约（link_tag / topicIds / postId / img 段 / get_post / rootId）
统一写在 layers/publish.py 的模块注释中。

【骨架状态】
本包内所有接口均为占位：async 签名 + 中文 docstring + NotImplementedError，
待后续按 contracts.py 契约逐条接入实现。
"""

from . import inbound, sense, create, guard, publish, interact, summarize, links

# 六块执行顺序，供编排与自检使用。
# 说明：「state」为状态/存储底座，位于顶层模块（state.py），不在本包内，
#       此处以字符串纳入顺序以表达「六块」完整流水线；各层读写均经 state 接口。
#       interact（阶段 4 互动）、summarize（阶段 5 感想总结）、links（阶段 7 外围联动）
#       是子流水线 / 旁路编排层，不属于六块。
LAYER_ORDER = ("inbound", "sense", "create", "guard", "publish", "state")

__all__ = [
    "inbound",
    "sense",
    "create",
    "guard",
    "publish",
    "interact",
    "summarize",
    "links",
    "LAYER_ORDER",
]
