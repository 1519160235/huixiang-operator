# 回响

> 一个会自己刷小黑盒、自己找话说、自己发帖的 MaiBot 插件。

回响（`local.huixiang-operator`）是跑在 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 上的小黑盒社区运营插件，按 MaiBot 的插件规范开发：用它的插件生命周期、配置模型、模型调用能力和后台页面机制，配置页与命令都由宿主渲染。

它把「运营一个小黑盒账号」拆成六块能力：接入、感知、创作、护栏、发布、状态。它不是定时群发工具 —— 刷到什么、想不想说、说什么、配什么图，都按账号自己的人设来定；没什么想说的，这一轮就不发。

名称取意「自己的分享有回响」。

- 插件 ID：`local.huixiang-operator`
- 仓库：<https://github.com/1519160235/huixiang-operator>
- 运行宿主：MaiBot ≥ 1.2.3（宿主 SDK ≥ 2.8.0）
- 许可证：MIT

---

## 特性

### 发帖

- **想发才发**。生成前先自问一句「你想发点什么吗」，不想发就跳过这一轮，不硬凑。
- **8 种发帖动机**轮换：刷到好玩的、想吐槽、想问、想安利、一句话梗、写自己的状态、刷帖随手记、两相对比；最近几篇不重复同一种。
- **不设时段、不设字数**。什么时候发、写多长，由内容决定；防失控只靠每日上限和生活状态（睡觉、累垮不发）。
- **标题去重**。与最近 6 篇做相似度校验，撞车只换标题、正文不动。
- **接地检查**。写了素材里没有的作品名、事件，整篇作废重写，宁可不发也不编。

### 分区与话题

- **分区（`topics`）和话题（`hashtags`）分开处理**。分区是「发到哪个板块」，话题是帖子上的 `#` 标签。
- 分区按「指定 → 默认 → 正文命中 → 同组」挑，不按全站热度乱挂（错误勾选会被平台处理）。
- 话题从真实帖子采样成池子（`data/hashtags.json`），发帖时按正文匹配；匹配不上就不带。
- 带话题发帖失败会自动去掉话题重试，不连累整条帖子。

### 配图

- 按人设与正文自己写画面描述，交给生图模型；代码不附加风格与构图限制。
- 支持 4K 尺寸（`[image].image_size`），尺寸不被支持时自动降级。
- 超过 5 MB 自动压成 JPEG 再上传（4K 输出约 6.4 MB，小黑盒图床上限 5 MB）。

### 互动

- 主动浏览推荐流与指定分区，定时刷新分区目录。
- 评论、楼中楼（带 `rootId`）、通知响应；同一通知不重复处理。
- 评论表情使用真实社区表情名，白名单可从真实评论自动扩充（每 24 小时一次）。

### 安全与可观测

- 多道护栏：每日额度（发帖 / 评论 / 点赞 / 回复分开计）、去重、AI 味打分、验证码熔断。
- **演练模式不占额度、不发起写入请求**，可随时验证整条链路。
- 操作记录落 `data/action_log.json`，后台可查最近 500 条。

---

## 架构

```
接入层（登录 / 通知 / 浏览 / 分区目录）
  → 感知层（作息情绪 / 生活状态 / 热门内容）
  → 创作层（动机选择 / 内容生成 / 画面描述 / 配图）
  → 护栏层（额度 / 去重 / AI 味 / 熔断）
  → 发布层（签名直连 / 发帖 / 评论 / 图床 / 回读核验）
  ↕ 状态存储（贯穿全程：浏览摘要、额度、历史、去重指纹）
```

发帖链路的实际控制流：

```
定时轮询
  → 有没有素材、额度够不够、生活状态允不允许
  → 挑一种发帖动机（最近没用过的那种）
  → 问自己「想发吗」── 不想发 → 结束，本轮不发
  → 生成标题与正文 → 接地检查 / AI 味检查 ── 不过 → 重写或放弃
  → 挑分区 + 挑话题 + 生配图
  → 发布 → 回读核验 → 记入历史
```

---

## 目录结构

```
huixiang-operator/
├─ plugin.py              插件入口：生命周期、命令、后台接口、配置页 Schema
├─ runtime.py             宿主能力适配：模型调用、日志、路径、配置读取
├─ worker.py              后台轮询：浏览 → 互动 → 发帖，异常不中断
├─ config_model.py        配置模型（13 个配置段，字段标签全中文）
├─ config.toml            配置示例
├─ models.py              数据结构：草稿、帖子、评论、额度、状态枚举
├─ state.py               状态与幂等存储：额度分桶、去重指纹、历史记录
├─ contracts.py           依赖契约：宿主能力与直连客户端的口径
├─ auth.py                扫码登录、凭据校验与落盘
├─ community_client.py    社区客户端：发帖 / 评论 / 回读 / 图床（直连）
├─ media_upload.py        图床三步上传
├─ comment_signing.py     写请求签名
├─ vendor/                第三方签名客户端（直连地基）
├─ layers/
│  ├─ inbound.py          接入：登录委托、通知拉取、浏览、分区目录刷新
│  ├─ sense.py            感知：生活轨迹、能否开口、热门内容
│  ├─ voices.py           发帖动机定义与轮换
│  ├─ create.py           创作：内容生成、口语清洗、AI 味打分、接地检查
│  ├─ imagegen.py         配图：画面描述、生图、尺寸降级、超限压缩
│  ├─ emojis.py           表情白名单
│  ├─ emoji_refresh.py    表情采样（从真实评论扩充）
│  ├─ topics.py           分区选择
│  ├─ hashtags.py         话题采样与匹配
│  ├─ schedule.py         发帖判定：额度、生活状态
│  ├─ guard.py            护栏：额度、去重、AI 味、熔断
│  ├─ publish.py          发布：发帖、评论、楼中楼、图床、回读
│  ├─ interact.py         互动编排
│  ├─ summarize.py        发帖编排：想不想发、动机、护栏、发布、回读
│  ├─ links.py            外围联动（语音 / QQ 空间 / 日记，旁路）
│  └─ _cfg.py             配置读取工具
├─ tests/                 单元测试（354 条，纯逻辑，不需要联网）
├─ docs/                  设计与契约文档
├─ webui.json             后台页面定义（声明式）
└─ CHANGELOG.md           变更记录
```

---

## 安装

### 环境要求

| 项 | 要求 |
|---|---|
| MaiBot | ≥ 1.2.3 |
| 宿主 SDK | ≥ 2.8.0 |
| Python | ≥ 3.11（依赖内置 `tomllib`） |

依赖包：`httpx`、`qrcode`、`pillow`、`pydantic`（由宿主统一安装）。

### 安装步骤

1. 把整个目录放到 MaiBot 的插件目录下，确认目录名为 `huixiang-operator`。
2. 重启 MaiBot，日志出现「回响：插件已加载」即为加载成功。
3. 打开插件设置页，先保持 **运行模式 = 演练**，跑几轮确认行为符合预期后再切「自动」。

### 登录

首次使用需要扫码登录小黑盒：

```
/回响登录
```

按提示扫码，凭据保存在数据目录，不写进配置文件、不进日志。

---

## 配置

配置分 13 段，全部中文字段标签。最常改的几项：

| 配置项 | 说明 | 默认 |
|---|---|---|
| `[basic].enabled` | 启用插件 | `false` |
| `[basic].mode` | 运行模式：演练 / 自动 | `演练` |
| `[browse].browse_interval_seconds` | 浏览间隔 | `300` |
| `[publish].default_partition` | 默认发帖分区 | `""` |
| `[publish].hashtags_enabled` | 自动带话题标签 | `true` |
| `[image].with_image` | 发帖带配图 | `true` |
| `[image].image_size` | 生图尺寸（4K 可填 `3840x2160`） | `""` |
| `[guard].daily_quota_post` | 每日发帖上限 | `3` |
| `[sense].pause_on_sleep` | 睡觉时不发 | `true` |

完整字段说明见 [`docs/配置说明.md`](docs/配置说明.md)。

### 配图用的生图接口

配图走 OpenAI 兼容接口，**地址和模型名已经内置**，开箱就是飞鱼中转的 `gpt-image-2.6`，装完只差一个 key：

```toml
[image]
image_api_base_url = "https://ai.feiyuapi.top/v1"   # 已预填，一般不用改
image_api_key = ""                                  # ← 只需要填这个
image_model = "gpt-image-2.6"                       # 已预填，支持 4K
image_size = "3840x2160"                            # 想要 4K 大图才填，留空用默认
```

key 在这里注册后生成（作者在用的中转站，`gpt-image-2.x` 系列可出 4K）：

> **https://ai.feiyuapi.top/register?aff=2U5HGJJJABLJ**
>
> 说明一下：这是带邀请码的注册链接，通过它注册，注册方和作者各得少量额度返还，
> 不影响你实际付的钱。

key 不随插件分发，原因很简单：仓库是公开的，写死 key 等于把额度敞开给所有人。

不想用这个中转站也完全没问题 —— 任何 OpenAI 兼容接口都能接，改
`image_api_base_url` / `image_model` 即可，本地部署的接口一样能跑。
连 key 都不填也不会报错：插件会回退用 MaiBot 本体「画图」里配的模型。
实在不想配图，把 `[image].with_image` 设为 `false`，发帖就只发文字。

> 文本模型（写帖子、写评论）不在这里配，走的是 MaiBot 自己的模型设置：
> 在 MaiBot 的模型管理里把服务商地址指向同一个中转站，
> 插件 `[model].text_model` 的默认值 `feiyu-gemini-flash` 就对应它的模型名。

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `/hx_status` | 查看状态：今日额度、熔断、后台轮询、已开启的联动 |
| `/hx_login` | 生成扫码登录二维码（直接在聊天里出图） |
| `/hx_login_check` | 检查扫码登录是否完成 |
| `/hx_logout` | 清除登录凭据 |
| `/hx_browse` | 立刻跑一轮浏览（只读，不互动） |
| `/hx_interact` | 响应通知并对浏览到的帖子互动 |
| `/hx_summarize` | 演练跑一次发帖，看这轮会不会想发、会写成什么样 |
| `/hx_try_publish` | 演练试发一条，不占额度、不发真实请求 |
| `/hx_log` | 查看最近操作记录 |

后台页面（`webui.json`）提供总览、手动触发与操作记录查询。

---

## 开发

### 跑测试

不需要联网、不需要启动 MaiBot、不需要真实账号：

```bash
python -m unittest discover -s tests
```

结果全绿即通过（本地缺少宿主 SDK 时，`test_mode_switch` / `test_partition_tolerance` 会因导入失败而报错，属环境缺失，服务器上正常）。

### 文档

| 文档 | 内容 |
|---|---|
| [`DESIGN.md`](DESIGN.md) | 架构决策、分层理由、关键取舍 |
| [`docs/配置说明.md`](docs/配置说明.md) | 全部配置段与默认值 |
| [`docs/接口契约.md`](docs/接口契约.md) | 小黑盒字段与流程对照 |
| [`docs/已知问题清单.md`](docs/已知问题清单.md) | 历史问题：现象 → 根因 → 规避 |
| [`docs/开发路线.md`](docs/开发路线.md) | 阶段划分与验收标准 |
| [`CHANGELOG.md`](CHANGELOG.md) | 每次改动的原因与验证方式 |

### 代码约定

- 分层的依赖方向只能自上而下：接入 → 感知 → 创作 → 护栏 → 发布。
- 纯逻辑与 IO 分离，判断类函数不碰网络，方便单测。
- 平台字段口径集中在 `contracts.py` 与 `vendor/endpoints.py`，不散落在业务代码里。
- 注释写「为什么这么改」，不写「这行在做什么」。

---

## 项目状态

已实现并在真实账号上运行：扫码登录、自动浏览、评论互动、发帖（含配图、分区、话题）、后台轮询、操作记录、配置页。

已知限制：

- 小黑盒没有公开 API，写请求依赖逆向出的签名方式，平台改版会导致失效。
- 话题没有公开列表接口，只能从真实帖子采样，池子大小受采样量限制。
- 配图走第三方 OpenAI 兼容接口，图片质量取决于所选模型。

---

## 参考实现

本插件的四个来源，参考点如下。代码层面具体的文件级搬运方式见 [`docs/融合映射.md`](docs/融合映射.md)。

| 来源 | 语言 | 项目地址 | 参考了什么 |
|---|---|---|---|
| 小黑盒 MCP | TypeScript | [Insect-rippler/XIAOHEIHE_MCP](https://github.com/Insect-rippler/XIAOHEIHE_MCP) | **以上游为起点继续开发**：上游提供业务动作、签名、图床、评论、验证码熔断与限流；发帖间隔（gap）与去 AI 味打分（aiflavor）两个模块为本项目补充，上游没有。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |
| 小黑盒适配器 | Python | [SengokuCola/xiaoheihe_adapter](https://github.com/SengokuCola/xiaoheihe_adapter) | 签名客户端（`vendor/`）、图床三步、扫码登录、主动浏览、分区动态 |
| 二创小黑盒 | Python | [1519160235/heybox-operator](https://github.com/1519160235/heybox-operator) | 直连生图、内容生成、`life_state.json` 回退；上游原作见 [HannJia/xiaoheihe-operator](https://github.com/HannJia/xiaoheihe-operator) |
| 回响骨架 | Python | 本仓库前身 | 六块接口划分、配置模型、状态机底座 |

另有几处只作协议与字段口径核对、未包含代码的参考：
[AaronZ345/xiaoheihe-cli](https://github.com/AaronZ345/xiaoheihe-cli)（图床协议、评论回读边界）、
[674537331/astrbot_plugin_xiaoheihe_adapter](https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter)、
[Whereis-Alice/astrbot_plugin_xhhrobot](https://github.com/Whereis-Alice/astrbot_plugin_xhhrobot)、
[lumingfei004/dph-heybox](https://github.com/lumingfei004/dph-heybox)、
[k1m0206/better-XiaoHeiHe](https://github.com/k1m0206/better-XiaoHeiHe)、
[fbbDirge/dsh-xhh](https://github.com/fbbDirge/dsh-xhh)。

签名算法另参考 [XiaHouSheng/heybox-core](https://github.com/XiaHouSheng/heybox-core)。

各来源的许可证与版权声明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 许可

本插件代码以 MIT 发布，见 [`LICENSE`](LICENSE)。

运行宿主 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 以 GPL-3.0 发布，
本仓库不包含其源码。
