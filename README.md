# 回响

面向 MaiBot 的小黑盒社区运营插件，专注于小黑盒平台的自动化运营。名称取意「自己的分享有回响」。

插件 ID：`local.huixiang-operator`。独立仓库：`1519160235/huixiang-operator`。

## 当前进度

七个阶段的代码均已实现：涵盖扫码登录、分区发帖、回读核验、评论互动、感想总结、后台 Worker 与外围联动。纯逻辑测试全部通过。

- 直连签名客户端已搬入（vendor/），读走 generate_hkey、写走 comment_query
- 扫码登录与四字段凭据校验已落地（auth.py），二维码过期能重新生成
- 发帖 / 图床 / 回读 / 分区目录已接好（community_client.py + media_upload.py + layers/publish.py + layers/inbound.py）
- 护栏六道闸 + 发帖时段闸门、演练不占额度、AI 味打分（直译 aiflavor.ts）已落地并测通
- 评论 / 楼中楼（带 rootId）/ 通知响应已落地（layers/publish.py + layers/interact.py），同一通知不重复处理
- 自动浏览支持勾选分区只刷勾选分区，分区目录定时刷新（layers/inbound.py）
- 感想总结已落地（layers/summarize.py）：字数段数代码侧收敛、count/time/both 触发判定、浏览摘要去重
- 后台 Worker 已落地（worker.py）：浏览→互动→总结按间隔循环，作息判停、单轮异常不中断、操作记录留痕
- 外围联动已落地（layers/links.py）：语音 / QQ 空间 / 日记三个旁路出口，关掉任一个不影响发帖闭环
- 不连真实账号，只跑合成数据纯逻辑自检
- 不上服务器，不改任何运行中的服务
- 不启用，不发一篇帖子，不写一条评论
- 不保存任何真实凭据

文档索引：融合依据见 `docs/融合映射.md`，架构见 `DESIGN.md`，字段口径见 `docs/接口契约.md`，已知问题见 `docs/已知问题清单.md`，推进顺序见 `docs/开发路线.md`，配置说明见 `docs/配置说明.md`。

## 架构概览

```
接入层（登录/收@回复/主动浏览/分区动态）
  → 感知层（作息情绪/热门有奖话题）
  → 创作层（内容生成/中文口语/AI配图/感想总结）
  → 护栏层（额度/去重/限流/去AI味/熔断）
  → 发布层（直连签名/发帖/评论/图床/回读核验）
  ↕ 状态存储（贯穿全程）
```

四源合成：小黑盒 MCP（TS）+ 小黑盒适配器（Python vendor 直连）+ 二创小黑盒（Python 生图）+ 回响骨架。

## 目录结构

```
huixiang-operator/
├─ README.md            这份。是什么、到哪一步、架构、目录、自检、参考了谁
├─ DESIGN.md            核心。为什么推倒重来、六块架构、融合清单、关键决策、审美约束
├─ CHANGELOG.md         变更记录。每条按「改了什么 / 为什么 / 怎么验证」写
├─ _manifest.json       插件清单。ID、版本、入口、配置模型、宿主要求
├─ config.toml          配置示例（11 个业务段 + [plugin]）
├─ config_model.py      配置模型（11 段）
├─ models.py            纯数据结构。草稿、帖子、评论、额度、状态与枚举
├─ state.py             状态机与幂等存储（落地）。状态迁移、额度分桶、去重指纹、JSON 存储
├─ contracts.py         依赖契约。A 类宿主能力与 B 类直连客户端的口径
├─ vendor/              第三方签名客户端（request_signing/api_client/security/endpoints/parsers/rate_limiter/models），直接搬自适配器
├─ comment_signing.py   写请求（POST）签名：comment_query，hkey 用 stamp+1
├─ auth.py              扫码登录、四字段凭据校验、凭据落盘、二维码过期重新生成
├─ media_upload.py      图床三步（info/v2 → token/v2 → COS PUT → callback/v2）
├─ community_client.py  社区客户端：发帖/发文章/图床/回读/带图评论（直连小黑盒）
├─ worker.py            后台运营 Worker：浏览→互动→总结按间隔循环，作息判停、操作记录
├─ plugin.py            插件入口。工厂函数、主类、run_once 编排与注册骨架
├─ webui.json           WebUI 定义（基础/登录/浏览/互动/发布/总结/配图/创作/感知/护栏/联动 11 区块）
├─ .gitignore           忽略规则。缓存、令牌与备份文件
├─ docs/
│  ├─ 融合映射.md       四源→六块→函数级搬运方式，融合的权威依据
│  ├─ 接口契约.md       小黑盒字段与流程的中文对照表
│  ├─ 已知问题清单.md       历史已知问题，逐条「问题 → 表现 → 根因 → 规避方式」
│  ├─ 开发路线.md       七个阶段，每阶段写产出、验收、依赖
│  └─ 配置说明.md       10 个配置段、默认值、分区话题联动、演练额度口径
├─ layers/
│  ├─ __init__.py       层包（六块 + 互动编排）
│  ├─ inbound.py        接入层：登录委托、通知拉取、浏览、分区动态拉取与定时刷新
│  ├─ sense.py          感知层：生活轨迹、能否开口、热门话题
│  ├─ create.py         创作层：中文口语、AI 味打分、内容生成、配图、降级
│  ├─ guard.py          护栏层：额度、间隔、去重、AI 味、熔断
│  ├─ publish.py        发布层：发帖、评论、楼中楼、通知回复、图床、回读
│  ├─ interact.py       互动编排层：通知响应、主动浏览评论
│  ├─ summarize.py      总结编排层：触发判定、字数段数校验、总结发帖
│  └─ links.py          外围联动：语音播报 / QQ 空间 / 日记（旁路，失败不影响主闭环）
├─ tests/
│  ├─ test_skeleton.py       骨架自检单测。unittest 本地跑
│  ├─ test_stage2_signing.py 第二阶段签名自查。读/写签名、四字段、脱敏
│  ├─ test_stage3_closed_loop.py 第三阶段最小闭环自查。口语清洗、AI 味、护栏、额度隔离、topicIds
│  ├─ test_stage4_interact.py    第四阶段互动自查。楼中楼 rootId、通知去重、分区过滤、评论护栏
│  └─ test_stage5_summary.py     第五阶段总结自查。字数段数收敛、触发判定、浏览摘要、总结帖格式
│  ├─ test_stage6_worker.py      第六阶段 Worker 自查。作息判停、发帖时段、操作记录、限流熔断、额度
│  └─ test_stage7_links.py       第七阶段联动自查。开关短路、失败降级、解耦闭环
└─ webui/
   └─ 控制台.html       浅色中文工作台骨架
```

各文件职责见上表，推进顺序见 `docs/开发路线.md`。

## 本地自检

无需安装依赖、无需联网、无需启动 MaiBot。以单元测试为主，文档检查为辅。

1. 主自检：跑 `python -m unittest discover -s tests`，全过即通过。
2. 语言检查：确认无成句英文。专有名词（MaiBot、API、TOML、Git）与字段名（`postId`、`topicIds`、`dryRun`）可保留，句子须为中文。
3. 残留检查：确认无「基于」「二创于」「上游说明」等表述，未引用第三方 README 作为开篇。参考来源统一置于文末。
4. 一致性检查：`docs/融合映射.md` 中四类来源的每个文件均有对应搬运方式；`docs/接口契约.md` 中 `topicIds` 为数字字符串、图床上限 5 MB；`docs/已知问题清单.md` 中 `link_tag`、`postId`、`rootId`、四字段凭据、演练额度均有对应条目。
5. 脱敏检查：文档中不得出现真实服务器地址、真实账号、cookie 或 token 明文。

如需确认编码，可执行以下只读命令（不修改任何文件）：

```
python -c "import pathlib; [p.read_text(encoding='utf-8') for p in pathlib.Path('.').rglob('*.md')]; print('所有 .md 都能按 utf-8 读')"
```

自检未通过时修改文档，不要调整运行环境。

## 发布状态说明

**当前不具备发布条件，也不应执行发布。**

发帖、图床、回读、护栏、状态存储、评论、楼中楼、通知响应的代码均已接入（community_client.py + media_upload.py + layers/），纯逻辑测试全部通过。但本版本不执行任何实际发布操作。

发布条件：需完成 `docs/开发路线.md` 第 3 阶段「最小闭环」，在部署环境人工监督下发布一篇，并通过 `fetch_thread_context(postId)` 回读确认标题、正文与配图一致，再由人工决定是否切换为自动模式。默认始终为演练模式；演练不占用额度、不上传图片、不发起请求。

## 参考实现

以下列出参考的实现路径与参考原因，未直接沿用任何第三方说明文档。

| 参考的实现路径 | 参考了什么 | 为什么参考 |
|---|---|---|
| 小黑盒 MCP（TS） | 业务动作边界、护栏四件套（gap/aiflavor/captcha/ratelimit） | 动作边界照它对齐；四件套纯算法直译成 Python |
| 小黑盒适配器（Python，vendor 直连） | 签名客户端、图床三步、扫码登录、主动浏览、分区动态 | 这是直连发布的地基，依赖最少、最好读 |
| 二创小黑盒（Python） | 直连生图、内容生成、`life_state.json` 回退 | 配图和感知早接早验证 |
| 回响骨架（本目录） | 六块接口、配置模型、状态机底座 | 不推倒重写，兜底底座 |
| 旧 `xhh-lite` 的中文清洗规则 | 去空白、去句末句号、短句逗号断开、段间不空行 | 这就是要的口吻 |
| MaiBot 插件开发指南 | `create_plugin`、`config_model`、`[plugin]` 段、`config_version` | 少一个插件组件就是 0，硬要求 |
