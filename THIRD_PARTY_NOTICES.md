# 第三方声明

本仓库包含或参考了以下 MIT 许可项目的代码与协议描述。原作者版权归各上游项目所有。

## 直接包含的代码

### `vendor/` 目录

小黑盒 HTTP 客户端，源自 MIT 许可的
[`SengokuCola/xiaoheihe_adapter`](https://github.com/SengokuCola/xiaoheihe_adapter)
（Copyright (c) 2026 MaiBot Contributors）。

其中签名算法部分独立移植自以下 MIT 许可项目：

- [`XiaHouSheng/heybox-core`](https://github.com/XiaHouSheng/heybox-core)
  —— hkey 算法的 Python 实现参考
- MIT 许可的 heybox-bot 客户端 —— 请求签名结构参考（仅参考结构，
  随机化处理，未复制任何未授权来源的常量）

### `auth.py` / `media_upload.py` / `comment_signing.py` / `community_client.py`

同源移植自上述适配器项目，接口路径与表单字段口径与之一致。

## 基于上游继续开发的项目

### 小黑盒 MCP（TypeScript）

以 [`Insect-rippler/XIAOHEIHE_MCP`](https://github.com/Insect-rippler/XIAOHEIHE_MCP)
为基础继续开发。上游以 MIT 许可发布，`Copyright (c) 2026 Insect-rippler`。

**上游已有**（`src/` 下，本项目未改动其逻辑，仅移植到 Python）：

```
auth/credentials.ts     client/{errors,factory,http,xhh}.ts
signing/{hkey,index}.ts
services/{actions,captcha,comments,context,post,publish,ratelimit,search,upload}.ts
mcp/server.ts           index.ts
```

**本项目新增**（上游没有这两个文件）：

| 文件 | 作用 | 移植结果 |
|---|---|---|
| `services/gap.ts` | 额度与发帖间隔策略 | 落到本仓库 `layers/schedule.py`、`layers/guard.py` |
| `services/aiflavor.ts` | 去 AI 味打分（评论阈值 45、正文阈值 60） | 落到本仓库 `layers/create.py` 的打分与去味逻辑 |

也就是说，护栏四件套里：验证码熔断（captcha）与限流（ratelimit）来自上游，
发帖间隔（gap）与 AI 味打分（aiflavor）是本项目补写的。

**关于这份改动的发布状态**：本项目对 MCP 的修改没有单独开源，
本仓库只包含由它移植到 Python 的实现。如需上游原始代码，请访问上面的仓库。
若要复用本项目在 MCP 上新增的 gap / aiflavor 设计，视同上表所列模块，
按 MIT 使用即可。

## 仅作行为与协议参考（未包含代码）

| 项目 | 参考内容 |
|---|---|
| [`1519160235/heybox-operator`](https://github.com/1519160235/heybox-operator) | 直连生图流程、内容生成结构、`life_state.json` 回退口径；上游原作见 [`HannJia/xiaoheihe-operator`](https://github.com/HannJia/xiaoheihe-operator) |
| [`AaronZ345/xiaoheihe-cli`](https://github.com/AaronZ345/xiaoheihe-cli) | 图床上传协议、评论演练/回读边界、帖子正文 `img` 块结构 |
| [`674537331/astrbot_plugin_xiaoheihe_adapter`](https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter) | 接口字段口径交叉核对 |
| [`Whereis-Alice/astrbot_plugin_xhhrobot`](https://github.com/Whereis-Alice/astrbot_plugin_xhhrobot) | 表情目录与互动边界交叉核对 |
| [`lumingfei004/dph-heybox`](https://github.com/lumingfei004/dph-heybox) | 点赞接口口径（`award_type`） |
| [`k1m0206/better-XiaoHeiHe`](https://github.com/k1m0206/better-XiaoHeiHe) | 网页端字段与渲染方式核对 |
| [`fbbDirge/dsh-xhh`](https://github.com/fbbDirge/dsh-xhh) | 话题与社区结构的交叉核对 |

以上项目均只作行为与字段口径参考，本仓库不包含其源码。

## 框架

本插件基于 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 插件框架开发，
使用其插件生命周期、配置模型、模型调用与后台页面机制。

MaiBot 本体以 **GPL-3.0** 发布，与本仓库的 MIT 许可是两个独立的许可范围：
本仓库只包含插件代码，不包含 MaiBot 本体源码，运行时由宿主提供框架能力。
如需分发包含 MaiBot 的完整运行环境，请遵循其 GPL-3.0 条款。
