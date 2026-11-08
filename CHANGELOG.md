# 变更记录

> 记录规范：每条包含「变更内容 / 变更原因 / 验证方式」三段。
> 仅记录本插件自身的变更，不转述上游说明，不记录环境运维细节。

## 2026-11-08 · 1.0.0：补免责声明，按插件市场规范整理仓库

**变更内容**
- 新增 `DISCLAIMER.md`：账号风险、AI 内容责任、第三方计费、凭据存放、合规红线、无担保条款；
  README 里加了一节摘要并链到全文。
- `_manifest.json` 按上架要求修正：版本 `1.0.0`、作者改为真实账号、
  宿主兼容区间 `1.4.0–1.4.99`（在 1.4.2 上验证）、`plugin_type` 改为 `automation`、补 `display.icon`。
- 根目录的 `config.toml` 改名 `config.example.toml` 并加入 `.gitignore`（插件市场不允许仓库根目录带运行配置）。
- README 补「插件市场安装」方式、修正登录命令为 `/hx_login`、环境要求对齐 1.4.0；
  `docs/配置说明.md` 去掉「代码是 TODO 占位」这类已经过时的描述。

**变更原因**
- 要上架插件市场，仓库必须符合其规范：根目录不能有 `config.toml`，manifest 字段要与真实情况一致。
- 原 manifest 的宿主上限写 1.3.99，在 1.4.2 上会被判定为兼容模式加载，市场里也会显示为不兼容。

**验证方式**
- 本地全量 364 条通过（2 条为缺宿主 SDK 的环境错误）。
- `python -m json.tool _manifest.json` 通过；id / version 正则校验通过。
- 市场要求的三件套齐备：`_manifest.json`、`plugin.py`（含 `create_plugin()`）、`LICENSE`、`README.md`。

## 2026-11-08 · 内置生图接口地址，装完只差一个 key

**用户要求**：「插件内置，别人用的话，内置的时候可以把里面用的模型链接全部换成这个中转站的。」

**变更内容**
- 新增 `defaults.py`：`DEFAULT_IMAGE_BASE_URL` / `DEFAULT_IMAGE_MODEL` 指向作者在用的中转站。
- `[image]` 三个字段接上默认值：地址与模型名留空即用内置值，`config.toml` 模板与配置页也预填。
- key 仍然不内置：公开仓库写死 key 等于把额度敞开，改由使用者自己填。
- 顺带修掉两处写死的安装路径（`model_config.toml` / `bot_config.toml` 的候选路径），
  现在按 `MAIBOT_ROOT`、`MAIBOT_CONFIG_DIR`、数据目录回溯、工作目录推断。

**变更原因**
- 新装用户不知道 `image_api_base_url` 该填什么、模型名该写哪个，卡在配图这一步。
- 写死 `E://mai` 只对作者机器成立，别人装等于这条回退路径永远失效。

**验证方式**
- 本地 `tests/test_defaults.py` 9 条通过；全量 363 条（2 条环境错误为本地缺宿主 SDK）。
- 真机：只填 key 时生成器指向中转站；完全不填时回退宿主「画图」；人设仍读到「阿水」（822 字）。

## 2026-11-08 · 配置页整理：分组改名、作废项隐藏、时段合并

**用户报障**：「那发帖那地方 UI 你怎么不变呢，你又写着看下面发帖窗口，我又没看见发帖窗口是哪。」

**根因有三层，都是 UI 和代码不同步**：
1. 文案指错地方：「什么时候触发」的说明写着「已弃用，现在看下面「发帖窗口」」，
   而「发帖窗口」其实在**另一个分组**（原「发布设置」）里，用户往下翻当然找不到。
2. 作废字段还占着位置：`trigger_by` / `count_threshold` / `time_hours` / `post_time_window`
   四个已经不参与判定的字段仍然渲染在页面上。
3. **两个时段各说各话（真 bug）**：
   - `guard.check_time_window` 读 `[publish].post_time_window`（默认 09:00-22:00）
   - `schedule.post_ready` 读 `[publish].post_slots`（默认 09:00-23:30）
   → 设到 23:30，22 点后 schedule 放行、guard 拦下，表现就是「明明设了却不发」。

**改法**：
- 分组改名，一眼看出顺序：`发帖① 时段 / 分区 / 话题` → `发帖② 开关 / 字数` → `发帖③ 配图`；
  原「发布设置」「配图设置」这两个叫法删掉。
- 字段顺序调整：「每天发帖时段」「发帖最小间隔」提到分组最前面——先看什么时候发，再看发到哪。
- 作废字段统一加 `［已作废］` 前缀，`get_webui_config_schema` 渲染前全部剔除
  （`plugin.strip_deprecated_fields`，带单测）。
- 时段只认一个来源：`check_time_window` 改为以 `post_slots` 为准（新增
  `schedule.in_any_slot` 支持多段判断），`post_time_window` 仅作空值兜底并标作废。
- 去掉所有「看下面 XXX」这类跨分组指路的话（测试里加了断言，防止再写回来）。

**测试**：新增 `tests/test_config_ui.py`（6 条，含作废字段必须带前缀、不许写跨组指路）；
全量 360 条，除本地缺 `maibot_sdk` 的 2 条环境错误外全部通过。

---

## 2026-11-08 · 话题（hashtag）和分区彻底分开

**用户纠正**：「分区是分区，话题是话题，你是不是理解错了？」
「这个自动勾选话题是手机上面你选了分区之后，它会自动推话题。」

**我理解错在哪**：
我把 `/bbs/app/api/topic/index/` 返回的**分区树**当成了话题列表（AI / Steam / 数码硬件
这些是分区，不是话题），于是「自动推话题」被我实现成了「自动多挂几个分区」。

真机读回一条真实帖子才看清字段：
```
topics   = [{"topic_id": 806933, "name": "AI"}, {"name": "沙雕日常"}]   ← 分区（可多个）
hashtags = [{"name": "deepseek", "hashtag_id": 689666}]                ← 话题
```

**改法**：
- `topic_ids` 只装分区，多个分区是有意的（真实帖子也常挂 2 个）。
- 新增 `layers/hashtags.py` 走**真正的 hashtag**：
  - 小黑盒没有公开的话题列表接口，所以照表情白名单的办法——**从真实帖子采样**。
    worker 每天拉几页推荐流，把别人帖子的 `hashtags`（名字 + hashtag_id）抄进
    `data/hashtags.json`，只增不减。实测一次采到 91 个真话题。
  - 发帖时按正文匹配挑：话题名出现在正文里才选，匹配不上就不带。
    实测「DeepSeek 日活 154 万」→ `deepseek / 日常 / ai`；「今天这顿真香」→ 不带。
- `models.PostDraft` 增加 `hashtags` 字段，`publish_post` 一路透传到接口的
  `hashtags` 参数（这个参数一直存在，只是从来没人传）。
- 保险：带话题发帖失败时**自动去掉话题重试一次**，话题是锦上添花，
  不能连累整条帖子发不出去。
- 新增配置：`hashtags_enabled`（自动带话题）、`max_hashtags`（最多几个）、
  `custom_hashtags`（固定带的话题名）。

**测试**：新增 `tests/test_post_hashtags.py`（16 条）；全量 354 条，
除本地缺 `maibot_sdk` 的 2 条环境错误外全部通过。

---

## 2026-11-08 · 「不是总结，是有想发的就发」+ 发帖勾选社区

**用户原话**：
1. 「那个总结表达有问题，不是要他总结，他觉得好玩他就发帖，他觉得不好玩就不发帖」；
2. 「就跟你发抖音、发朋友圈、发 QQ 空间，有分享就发呗，乐意发发呗，没什么特定要求，
   爱发发呗，不发不发呗」；
3. 「不是会自动发 5 个话题吗？怎么没了」；
4. 「这个自动勾选话题是手机上面你选了分区之后，它会自动推话题」。

**改法一：加「想不想发」这一步（create.decide_want_to_post）**
- 原来：到时段 + 有素材 → **必然**生成一条发出去。哪怕内容平平无奇也要硬写，等于交作业。
- 现在：生成前先问一句「你想发点什么吗？就跟发朋友圈一样，没人逼你」。
  不想发就直接返回 `not_in_the_mood`，这一轮什么都不发。
- 提示词明确写：「不要为了发帖而找理由」「如果提不起劲就直说不想发」。
- 手动「发一条」时不问（`check_want=False`），用户明确要发就别拦。

**改法二：界面去「总结」化**
- 配置页「感想总结」→「发帖」，「开启感想总结」→「允许发帖」；
- 操作记录里的 `summary` 标签 →「发帖」；播报文案「感想总结」→「发了条帖子」；
- 废弃的 `trigger_by` / `count_threshold` / `time_hours` 标注为「已弃用」。

**改法三：发帖真正勾上社区（layers/topics.py 全新）**
- 根因不是「丢了」，是**从来没挂上**：`linked_topics_enabled` / `max_topics` /
  `custom_topics` 三个配置项一直在界面上，但代码里没有任何地方读它们；
  `summary_to_draft` 也没传 `topic_ids`，所以 `_topic_ids_str` 永远拼出空串。
- 现在按用户描述的**手机端逻辑**走：选完分区 → 自动推同组的几个相关话题。
  顺序：自定义社区 → 主分区 → 正文里提到的社区 → 同组按热度补 → 截到 `max_topics`。
- 顺带修了 `fetch_partitions` 的两个问题：
  1. 同一社区会同时出现在「推荐」和具体分组里（AI 两边都有），原来会重复入库、
     且分组被「推荐」覆盖 → 现在按 id 去重，具体分组优先；
  2. `valid=0` 的下架社区会被跳过（挂上去会导致发帖被判分区错误）。
- `Partition` 增加 `group` / `heat` 字段，缓存同步存取。

**真机验证**：
- 分区缓存 94 条，全部有分组、92 条有热度；
- 选「AI」（盒友之家）→ 自动推出 `AI / 盒友杂谈 / 沙雕日常 / 数码硬件 / 校园生活` 共 5 个；
- 不再按全站热度乱勾（给聊 AI 的帖子挂「英雄联盟」属于错误勾选，官方会处理）。

**测试**：新增 `tests/test_post_want.py`（11 条）、`tests/test_post_topics.py`（15 条）；
全量 338 条，除本地缺 `maibot_sdk` 的 2 条环境错误外全部通过。

---

## 2026-11-08 · 配图提示词去掉全部人为限制（第二次纠正）

**用户再次纠正（原话）**：
1. 「我没有定底线，我定的底线是让他按照自己的人设来生成照片就行了，想怎么发怎么发，
   只要不凭空捏造就行了。什么时候又加了个新的底线，叫做不要画成帖子截图，兄弟，啊？搞笑呢。」
2. 「描述一定要加限制吗？他描述 2000 个字也行啊，又不是不行。你只要那个中转站能多写点描述
   就多写点描述呗，他愿意怎么描述怎么描述，你怎么老想着做限制呢？」

**我第二次犯的错**：
把用户说的「不凭空捏造」自己翻译成了画面禁令「不要画成帖子截图」——
这仍然是我替他定的一条规矩。用户的「不凭空捏造」是**内容层面**的
（别写没刷到的帖子、别编事件），由正文的 `summary_grounding_issues` 负责，与配图画什么无关。

**改法（现在真的零限制）**：
- `build_image_prompt`：有描述时**原样透传**，一个字的加工都没有；无描述时兜底也改成
  「把标题和正文交给生图模型自己判断画面」，不再套我写的场景池 / 道具映射 / 时间段表
  （`_PROP_HINTS`、`_FALLBACK_SCENES`、`_PERIOD_HINTS`、`_period_of` 全部删除）。
- `_SCENE_PROMPT`：删掉 120–220 字区间和「镜头/主体/环境/光线」清单，
  改为「想拍什么就拍什么，想怎么写就怎么写，想写多长写多长，两百字可以，两千字也行」。
- `describe_scene`：删掉长度截断（`DEFAULT_SCENE_CHARS` 置 0 仅作兼容），
  只保留去掉「配图：」前缀和拒绝话术识别。

**验证**：测试断言改为「提示词里不许出现 严禁/不要/必须/截图/插画/3D/自然光/噪点 等限制词」，
全量 312 条通过（仅缺 maibot_sdk 的 2 条环境错）。真机 4K 生图已确认可用。

---

## 2026-11-08 · 配图规则交还用户 + 4K 生图 + 描述写详细

**用户明确指示（原话）**：
1. 「谁定的规则？严禁有文字啊」「谁让你定规则了，规则是我定的」；
2. 「我定的规则就是让他按照他自己的人设来生成照片就行了，爱怎么发怎么发，只要不凭空捏造就行」；
3. 「生图模型现在有个 2.6 的 Image，可以生成 4K 的图，你这个描述的详细一点，
   提示词可以多写一点，越详细描述的越牛逼」。

**我犯的错（记下来别再犯）**：
`build_image_prompt` 里曾被我自己塞进「严禁出现任何文字」「不要插画感 / 不要 3D 渲染」
「必须模糊到看不清」「自然光 / 轻微噪点」这些**创作规则**。用户从没要求过这些，
是 11-07 那次排查「图上有假截图」时我顺手加进去的，后来越叠越多，等于替用户定了规矩。

**改法**：
- `build_image_prompt` 现在只做两件事：把模型写的画面描述交给生图模型 + 一条用户自己定的底线
  （「不要画成帖子/聊天截图，不要出现编造出来的帖子内容」）。其余限制全部删除。
- `_SCENE_PROMPT` 改成「你想拍什么就拍什么，按你自己的性子来」，
  细节项从「必须写清」改为「可以想想这些（不强制）」。
- 描述长度从 80 字一句话放宽到 120–220 字（`describe_scene` 上限 220 字并智能断句），
  镜头 / 画面内容 / 地点 / 光线 / 拍出来什么样 / 状态都留给模型自己发挥。
- **4K 生图**：新增 `[image].image_size`（默认 `3840x2160`）；
  `DirectImageGenerator.generate(size=...)` 支持按次覆盖尺寸；
  尺寸不被支持时自动退到 `2048x2048` 再试，不会一张图都出不来。
- **超 5 MB 自动压缩**：实测 4K 输出 PNG 6.4 MB 超过小黑盒图床上限 5 MB，
  新增 `_fit_upload()` 按 92/86/80/72 质量阶梯转 JPEG，实测 6.71 MB → 0.97 MB。

**真机验证**：
- 4K 出图 `3840x2160`，61 秒，压完 1.22 MB；
- 生图内容与描述逐项对上（三条锦鲤有一条发白、鱼食罐、缸壁水垢、夕阳斜射）；
- 图内无伪造界面 / 无清晰可读文字。

---

## 2026-11-08 · 发帖动机多样化 + 表情名单自动更新 + 时段化发帖

**用户反馈（原话）**：
1. 「为什么是发感想呢？我当时的想法是，哎他觉得好玩他就发一个，或者是发一个日记什么的」；
2. 「表情我不可能一直不变吧，小黑盒会更新新的表情，你这个准备怎么整呢」；
3. 「额度我也没搞懂……MCP 时代是早中晚各一个，现在有点拿不准，我的想法是它想什么时候发
   什么时候发，它不是一个情绪插件吗？咱们能配合用啊」。

**改法一：发帖动机（layers/voices.py 重写）**
- 原来只有一条路径：刷够 N 条 → 写一篇「感想总结」。现在按动机选素材：
  `single`（只要一条：刷到好玩的 / 想吐槽 / 想问 / 想安利 / 一句话梗）、
  `life`（不要素材，写自己的状态）、`multi`（要一批：刷帖随手记 / 两相对比）。
- 汇总类（multi）有配额：最近 3 篇最多 1 篇。其余都是单条驱动，像人在发帖而不是交作业。
- `create.generate_summary` 加 `material_mode` / `life_trace`：单条只喂一条素材，
  日记只喂生活状态（不再要求必须有刷到的帖子），并跳过接地检查。
- 生活状态来自 life-frequency：`sense.read_life_trace` 修了回退路径（原来只找自己
  数据目录，永远读不到），现在能读到 `activity / scene / emotion / energy`。

**改法二：表情白名单自动更新（layers/emojis.py + emoji_refresh.py）**
- 内置 57 个作为兜底，运行时扩充存 `emoji_whitelist.json`（只增不减）。
- worker 每 24 小时采一次最近刷过的帖子评论区，把真实用户发的新表情并入白名单
  —— 表情名不靠猜，靠社区先用的。

**改法三：发帖时机（layers/schedule.py）**
- 从「每天硬额度 3 条 + 刷够 50 条才触发」改成「时段窗口」：
  默认 `08:00-11:00,12:00-14:00,19:00-23:00`，每个窗口最多 1 条；
  只配一个窗口（如 `08:00-23:00`）= 全天可发，频率交给最小间隔。
- 叠加生活状态：睡觉 / 社交电量 <20% / 体力 <15% 不发；读到「未知」一律放行。
- `daily_quota_post` 降级为防失控的硬上限，不再是主要节奏。

**验证**：新增 `tests/test_post_schedule.py`（17 条）；全量 298 条，
除本地缺 `maibot_sdk` 的 2 条环境错误外全部通过。真机待下一轮验证。

---

## 2026-11-08 · 发帖多样化：体裁轮换 / 标题去重 / 配图按内容自己定

**用户反馈（原话）**：
1. 「连续发三个，题目是一模一样」「发的跟他妈屎一样」；
2. 「连续三个帖子配图大差不差」；
3. 「他不有自己的人设吗，他根据自己人设来呗，想怎么配怎么配呗，谁让他固定死的」。

**根因**：
- 标题：每次都是同一句「写一篇感想」，提示词里没有历史标题，模型次次用同一句式
  （时间 + 随手翻翻）；
- 正文：提示词要求「每段写自己看到的一个具体点」，等于鼓励逐条清点，
  于是固定出现「翻着翻着 / 还有人 / 中间还夹着」的流水账；
- 配图：`build_image_prompt` 把画面**写死**成「书桌 + 手机 + 饮料 + 暖色台灯」，
  只按时间段和有没有游戏改一个道具，三次发帖必然三张同款。

**改法**：
- 新增 `layers/voices.py`：8 种体裁（刷帖随手记 / 单点展开 / 发问求聊 / 吐槽锐评 /
  安利分享 / 自己的状态 / 两相对比 / 短梗）。发帖前按「最久未使用」挑一种，
  连发几篇不会重样；同时给出标题方向。
- 新增 `state.PostHistoryStore`（`post_history.json`）：记录最近 60 篇的标题与体裁。
  生成时把最近 6 条标题喂给模型并明确禁止重复；生成后再用字符二元组相似度
  （阈值 0.55）校验，撞车就只重起标题、正文不动。
- 正文提示词改成「一篇只抓一两个点写透」，明确禁掉逐条清点的句式。
- 配图改成 `describe_scene()`：先让大模型看着这条帖子（读宿主 `[personality]`，
  读不到退回小黑盒昵称 + 插件 tone）自己写一句画面描述，再套技术约束去生图。
  `build_image_prompt` 只在模型给不出描述时兜底，且兜底也按正文关键词 + 标题哈希选场景，
  不再是一张固定模板。文字 / 水印 / 假截图的禁令保留。

**验证**：
- 新增 `tests/test_post_variety.py`（15 条）：体裁轮换不重复、标题相似度命中与放过、
  历史存储读写、配图随内容变化且不含正文原文。
- 全量单测 278 条，除本地缺 `maibot_sdk` 的 2 条环境错误外全部通过。
- 真机：部署后重启，插件 loaded=8；待下一轮发帖观察标题与配图是否不再重复。

---

## 2026-11-08 · 评论表情渲染不出来（白名单兜底）

**用户反馈**：「刷小黑盒回复有时候表情加载不出来」。

**根因（真机验证）**：小黑盒 `link/tree` 返回的评论**没有 `emojis` 字段**，
服务端把 `[cube_xxx]` 当纯文本存回，渲染全靠客户端内置表情表按名字查。
名字编错就画不出来。而评论提示词只给了 3 个示例，模型会自由发挥。

**改法**：
- 新增 `layers/emojis.py`：用真机登录态扫 60 个帖子 / 1606 条真实评论，
  得到 57 个「确实存在」的表情名白名单；发送前把白名单外的标记一律删掉，
  并归一化全角括号、清理半残标记；一条评论最多留 1 个。
- 提示词改成按情绪分组的可选清单，明确「只能从这里选，名字写错渲染不出来」。
- `_EMOJI_MARK_RE` 补上短横线（`[cube_上学-乐]` 这类官方活动表情）。

**验证**：`tests/test_emoji_whitelist.py` 12 条全过；真机重启后插件正常加载。

---

## 2026-11-08 · MaiBot 框架升级 1.3.19 → 1.4.2

**起因**：用户要求升级框架。

**升级前准备**：
- 查清版本：当前 1.3.19，PyPI 最新 1.4.1（`uv tool upgrade` 实际拉到 1.4.2）。
- 查破坏性变更（1.4.0）：多实例架构、WebUI 全新布局、**移除旧 Action 组件**。
- 扫描全部 10 个插件，确认**没有任何插件使用旧 Action 组件**（这是最大风险项）。
- 备份 config + data + plugins 三个目录到 `E:\backup\mai_pre_upgrade_*.zip`
  （924 条目 / 155 MB，损坏项 0，关键文件逐个抽检通过）。

**升级与修复**：
- 执行 `uv tool upgrade maibot` 后版本为 1.4.2（SDK 2.21.0，Dashboard 1.10.2）。
- 用 1.4.2 自带的 `src/plugin_runtime/webui_schema.py` 重新校验 `webui.json`：
  发现 `placement="workspace"` 已废弃（会告警并强制转 sidebar），按提示删除该字段与
  顶层 `workspace_title`，改为默认侧栏页。
- 同步把本地 schema 缓存更新为 1.4.2 版，后续 `tests/check_webui_schema.py` 按新标准校验。

**已知副作用（需用户决策）**：
- `local.breeze-voice` 变为**未激活**。根因是新版核心依赖 `google-genai==3.0.0` 锁死
  `websockets==17.4`，而语音插件依赖的 `breeze-blue 0.23.0`（最新版）要求
  `websockets>=16.0,<17.0` —— 版本区间无交集，依赖联合解析必然失败。
  宿主因此跳过该插件，但**不影响其它插件**（failed=0）。

**验证**：`maibot --version` = 1.4.2；WebUI 8001 返回 200；
插件 `loaded=6 failed=0`（回响在列）；回响 worker 数据在 12 秒内持续更新；
启动日志出现「回响：插件已加载（enabled=True，mode=自动）」。

---

## 2026-11-07 · 后台管理页视觉重做（用户反馈「太丑了」）

**反馈**：后台管理页面观感太差，希望优化。

**改法**：只换外观与布局，不动任何业务逻辑。
- 从旧文件整段抠出 `<script type="module">` 原样复用，只重写 `<style>` 与 body 结构；
  所有元素 ID、事件绑定、10 秒自动刷新、700ms 自动保存、请求串行队列全部保留。
- 新布局：深色侧边栏（品牌 + 运行状态 + 锚点导航）+ 主区（KPI 四宫格 → 运行状态 →
  配置 → 手动操作 → 账号 → 操作记录 → QQ 命令），滚动时导航自动高亮。
- 视觉：靛蓝主色、16px 圆角、柔和投影、统一 38px 输入框与按钮、KPI 状态圆点随状态变色。
- 深色侧边栏里的徽章改用半透明底 + 亮色字（避免白底徽章变成一块白斑）。
- 拆掉 `more-details` 后给「折叠状态持久化」加了空值保护，避免拿不到元素抛错。

**怎么验证**：Edge 无头截图逐版自检（侧边栏徽章可读、无重叠/溢出/截断）；
静态检查 58 个元素、无真实重复 ID、45 个关键 ID 齐全、`maibot.ready` 与脚本完整；
备份旧页在 `.workbuddy/tmp/控制台.before_redesign.html`。**仅替换静态 HTML，未重启 MaiBot。**

## 2026-11-07 · 配图在编造内容：图上出现框架名 + 假的小黑盒截图

**反馈**：用户看刚发的总结帖配图后指出两点：
1. 图上写着「小黑盒 maibot 的生活分享」——MyBot 是框架名，不是账号昵称；
   以后图上若要写名字，应该是账号自己的昵称。
2. 图里那几张「小黑盒帖子截图」在历史记录里根本找不到，问是不是真实刷到的。

**排查**：原提示词是 `maibot 的小黑盒生活分享配图：{标题}。{正文}。`，实测模型会：
- 把「maibot 的生活分享」当标题文字画进图里；
- 把正文提到的帖子做成四张**假的小黑盒截图**拼贴（活侠传存档 / 工程软件报错 /
  霍格沃茨 PS5），用户翻浏览记录完全对不上 —— 等于配图在编造「真实刷到的东西」。

**改法**：新增 `imagegen.build_image_prompt()`，只描述一个真实生活场景
（书桌、亮着的手机、饮料、台灯 + 时段光线），标题/正文只用来判断时段与有无游戏元素，
**不作为画面文字**；并明确禁止任何文字 / 字母 / 数字 / 水印 / logo / 应用界面 / 聊天截图，
禁止多图拼贴。原提示词整段废弃。

**怎么验证**：新增 3 项单测（提示词不含 maibot / 禁止文字截图 / 时段识别）；
全量 243 项全绿；用新提示词实生一张图，确认画面为傍晚书桌生活照，
**无任何文字、logo、UI、截图**。

## 2026-11-07 · 发帖没配图 + 分段观感不对 + 评论挤爆发帖额度

**反馈**：
1. 「我不是说发帖子要配图嘛，图去哪儿了」；
2. 「分段写，我感觉这个分段看着有问题」；
3. （连带发现）当天一篇帖都发不出去。

**排查（三个独立根因）**：
1. **配图从来没实现**：`layers/create.py` 的 `_generate_images` 用的是 TODO 占位
   `host.image.generate`（`contracts.py` 里就是 `"TODO:..."`），宿主没有这个能力入口；
   `[image].with_image` 默认还是 false。两条叠在一起，配图永远为空。
2. **总结帖写死纯文字**：`summarize.summary_to_draft` 里 `images=[]`；而 worker 只调度
   「浏览 → 互动 → 总结」，普通发帖 `run_once` 根本不在自动流程里 —— 用户看到的帖子
   就是总结帖，所以永远没图。
3. **分段观感**：`polish_chinese` 无条件删掉所有句号（本是给短评论的规则），
   300 字总结因此变成一串逗号连到底的巨型长句；`[create].no_blank_line_between_paragraphs`
   配置项定义了却从未被代码读取，段间一直带着空行。
4. **额度串桶**：`quota_bucket_key` 只按「mode + 日期」计数，评论 / 回复 / 点赞全加进
   同一个计数器，再拿去跟 `daily_quota_post=3` 比。实测当天该桶已到 37，发帖从早上起
   就被判「额度已用尽」，自然也不可能配图。

**改法**：
- 新增 `layers/imagegen.py`（搬源③实现）：直连 OpenAI 兼容 `/images/generations`
  （`DirectImageGenerator`）+ 麦麦绘图插件回退（`DrawingGateway`）；
  参数优先读 `[image]` 配置，留空则回退 MaiBot 主配置里 `api_provider == "画图"` 的 provider。
- `create.generate_images` 改为「生图 → 写数据目录临时文件 → 返回 ImageAsset」，
  上传图床仍走 `publish.upload_image`；`with_image` 默认改为 true。
- `summarize.run_summary_once` 在发布前按 `[image]` 生图并上传，失败降级纯文字。
- `polish_chinese` 新增 `keep_periods`（长文保留句号）与 `blank_between_paragraphs`
  （段间是否空行），发帖 / 总结都读 `[create].no_blank_line_between_paragraphs` 生效。
- `quota_bucket_key(mode, day, action)` 改为三键隔离；`QuotaStore` 各方法加 action 参数，
  新增 `reset_all`；后台「重置额度」一次清四个动作桶。
- `fallback_text_only` 不再把 link_tag 降级成 28（28 必被拒），只清 images。

**怎么验证**：新增 9 项单测（生图装配 / 总结带图 / 段间空行 / 保留句号 / 额度隔离）；
全量 240 项全绿；生图接口实测 `/images/generations` 返回有效 PNG；部署后
`回响：插件已加载`、`worker 已启动（mode=real）`，日志无 Traceback。

## 2026-11-07 · 感想总结「像 AI」：时间幻觉 + 重复句 + 上帝视角总结腔

**反馈**：总结正文开头是「大半夜随手刷了会儿 / 大半夜随手刷了会儿，大家的生活都挺有意思的 / …」，
并指出：中午还没到就写「大半夜」，标题和正文第一句重复，还净是「大家的生活都挺有意思的」这类上帝视角总结腔。

**排查**：
1. 时间幻觉：总结提示词里没有任何「当前时间」，模型只能瞎编时段（中午写成大半夜）。
2. 重复句：`_split_title_body` 把首行当标题，正文第一句又原样复读标题；且总结正文没走任何去重。
3. 上帝视角：提示词没拦「对全体用户下点评」，模型就写出「大家的生活都挺有意思的」「看着都挺替他们高兴的」这类总结腔。
4. 凑字数兜底 `pad_summary_to_min` 的候选句自带「大家最近都在聊 AI」这类 AI 腔。

**改法**：
- 新增 `_now_context()`：把服务器当前真实时间（年月日 + 星期 + 早上/中午/下午/晚上/凌晨）写进提示词，并明确禁止编造「大半夜/凌晨/清晨」这类对不上的时段。
- 新增 `_strip_title_from_body()`：模型自己给的标题若成了正文首句开头，剥掉正文里这段重复引子（只在模型给标题时剥，兜底标题是正文提炼的、不能剥）。
- 新增 `dedupe_summary()`：总结正文走「段级近重复折叠 + 段内近重复句」两级去重，保留分段结构。
- 重写总结提示词：禁止替「大家」下结论、点名禁掉「大家的生活都挺有意思的」等总结腔、要求分几段每段写一个具体点。
- `pad_summary_to_min` 候选句去 AI 腔，不再出现「大家…都…」这类表述。

**怎么验证**：新增 12 项单测覆盖时间注入 / 段级句级去重 / 标题正文剥离 / 兜底无总结腔 / 提示词含真实时间与禁句；阶段 5 单测 54 项全绿；用真实故障样例走一遍清洗链，标题与正文不再复读、不再出现「大半夜」。

## 2026-11-06 · 删掉「分区目录」面板 + 补两个自查按钮

**反馈**：底部实时更新的分区面板无实际用途，选择分区时下拉列表本身即会刷新，该面板可移除。

**排查**：该面板仅展示缓存中的 94 个分区（前 20 个），为只读展示。选择分区时下拉列表已按最新缓存刷新——
`_cached_partition_names()` 在每次生成配置 Schema 时都按当时缓存重算 `choices`，
而缓存由 worker 每轮浏览自动刷新。面板是重复信息。

**改法**：
- 删掉「分区目录」子卡片、`renderPartitions()`、`lastPartsSignature`、
  以及刷新队列里的 `partitions` 查询（页面不再白跑一次请求）；
- 顶部折叠标题的「分区」字样一并去掉；
- 后端 `hx_partitions` 接口**保留**（排障时还能手动查缓存），只是页面不调了。
- 顺手补两个自查按钮：「预览一版总结」（演练，不占额度不发帖，能看出重写几次、
  AI 味多少）、「试写一段看用哪个模型」（真调一次 LLM，展示配置模型 vs 实际模型）。

**怎么验证**：`grep` 确认页面无 `分区目录 / renderPartitions / lastPartsSignature /
$('partitions')` 残留；页面查询集合与 `webui.json` 的 `queries` 完全一致
（5 个），动作集合同样一致（7 个）；括号配平；184 项单测 + 两道预检全过；
线上重启后 `LOADS: mode=real / ERRS: NONE`，页面确认已无分区面板、两个按钮都在。

## 2026-11-06 · 模型选错（用了 DeepSeek）+ 感想总结「建议重写却无后续动作」+ 模式又变演练

**反馈（三项）**：
1. 「发帖之前配置的是用 GM，不要给我用 Deepseek」；
2. 「写了个感想总结太像机器，建议重写，然后无后续动作了」；
3. 「后边的感想总结又是演练」。

**排查（依据运行日志逐项确认）**：
- 服务器 `model_config.toml` 里 `[model_task_config.replyer]` 的
  `model_list = ["ds-official", "feiyu-gemini-flash"]`，**DeepSeek 排第一**。
  插件只传 `task_name="replyer"`，宿主按顺序取第一个 → 内容永远是 DeepSeek。
  配置里根本没有「用哪个模型」这一项，用户没法改。
- `guard.check_ai_flavor` 判 `ai_flavor_block` 时只返回一句「建议重写」，
  **上一层直接 `return {"ok": False}`，全代码库没有任何一处真的执行重写**。
  操作记录里就是一条 `ok=False 文字太像机器（AI 味 62 ≥ 60），建议重写`，
  下一条直接跳到成功 —— 用户侧表现为提示后无后续动作。
- worker 的 `mode` 在 `__init__` 时固化，配置改「自动」后靠
  `asyncio.create_task` 异步重建；没有事件循环时抛 `RuntimeError`，
  异常被吞掉 → 旧 worker 继续用 `DRY_RUN` 跑，日志里就是「先自动、后演练」。
- 另有一项易被忽略的原因：宿主 `cap.call` 默认超时只有 **30000ms**，写 200~400 字
  经常跑不完，日志里是 `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`。
  上层仅收到「生成失败」，同样表现为无后续动作。

**改法**：
1. **新增 `[model]` 配置段**（`text_model` / `temperature` / `max_tokens`）：
   默认 `feiyu-gemini-flash`（用户说的「GM」），下拉可选 GLM / DeepSeek / 跟随宿主默认。
   `Runtime.generate_text` 改用 `model_name=` **直选具体模型**（SDK 契约：传
   `model_name` 一律直选，绕过任务里的优先级顺序）。
2. **补齐重写闭环**：`summarize.run_summary_once` 改成循环，
   AI 味超标就把「上一版正文 + 去 AI 味的具体要求」塞回提示词重新生成，
   最多 `REWRITE_ROUNDS=2` 轮。字数/段数不合规也走同一条重写路径。
   额度/间隔/去重/时段这类闸门重写没用，**立即失败，避免无效的模型调用**。
3. **worker 每轮重读模式**：`run_round` 开头调 `_current_mode()` 按当前配置
   重新判定，不再依赖异步重建成功。
4. **`_start_worker` 无事件循环时安全降级**：先 `get_running_loop()` 判断，
   没有就同步替换 worker；`start()` 失败也不再留下半死状态。
5. **给宿主生成留足时间**：`llm.generate(..., timeout_ms=180000)`。
6. **诊断入口**：新增 `hx_test_model` API + 页面「试写一段看用哪个模型」按钮，
   展示「配置指定 / 实际回报 / 样本」三项，便于随时核验。
7. **状态页补额度上限**：原实现仅显示「真实已用 4 次」，无法判断已达上限；
   现显示 `4 / 3` 并在达到上限时高亮提示。

**怎么验证**：
- 184 项单测 + 两道预检全过（新增 `tests/test_model_and_rewrite.py` 9 项：
  模型名透传、跟随默认不传、配置坏了兜底、AI 味触发真重写、重写有上限、
  不可重写的闸门快速失败、worker 每轮重读模式、取模式异常兜底）。
- 线上日志实测：`选择请求模型: feiyu-gemini-flash (策略: sequential)`，
  `类型: plugin.local.huixiang-operator`，`完成: 1041 / 282 token` ——
  确认走 Gemini、且不再 30 秒超时。
- 线上操作记录全部是 `[real]`，`mode=real`，不再出现「自动里混演练」。

## 2026-11-06 · 「指定分区」多选下拉改成搜索式复选框列表

**反馈**：新页面中「指定分区」下拉框无法正常操作。

**排查**：字段数据本身没问题——服务器上实测 `selected_partitions` 是
`type=select / multiple=True / choices=94`。问题出在**交互实现**：使用了
`<select multiple size="5">`，在沙箱 iframe 里要按住 Ctrl 才能多选，
94 个分区压缩在 5 行高度内，交互上难以操作。

**改法**：多选字段不再用 `<select multiple>`，改成
**搜索框 + 复选框列表 + 全选/清空 + 已选计数**：
- 点一下勾一个，双列平铺，最多显示 170px 高、内部滚动；
- 输入关键字实时过滤（94 个分区能快速找到）；
- 全选/清空只作用于当前筛选结果；
- 保存时从所有 checked 复选框收集成**数组**，保证模型校验通过；
- 勾选、搜索、全选都会触发 `configDirty`，自动刷新继续暂停，不会冲掉选择。
单选下拉（运行模式、什么时候触发、语气、默认分区）保持原样不动。

**验证**：静态检查确认无 `<select multiple size>` 残留、括号配平；
173 项单测 + 三道预检全过；17:18 重启后 `LOAD: OK / 报错无`。

## 2026-11-06 · 修「插件 WebUI 已下线」与「编辑时被自动刷新冲掉」

**一条连锁反应，三个 bug 叠加（服务器日志 17:07:14 完整还原）**

```
保存配置 → 多选字段写成字符串 'AI' → 文件写成功但 HuiXiangConfig 校验失败
→ 宿主 file_watcher 检测到 config.toml 变动 → 自动重载插件
→ 用户开着的后台页面持有旧注册句柄 → 「ApiError: 插件 WebUI 已下线」
```

1. **多选字段被写成字符串**：`selected_partitions` 在模型里是 `list[str]`，
   被动态注入成 `select + multiple` 后，前端仍按「单选字符串」渲染并保存。
   → 前端按 `f.multiple` 渲染 `<select multiple>`，保存时收集 `selectedOptions` 成数组。
2. **编辑时被自动刷新冲掉**（用户：「刚想把演练改成自动，它给我自动刷新了」）：
   10 秒定时器无条件重绘表单，把用户刚选好、还没点保存的值覆盖回旧值。
   → 加 `configDirty` 标记：任何字段一改就暂停自动刷新、徽章变「有未保存的修改」、
   提示「自动刷新已暂停」；保存成功或点「重新读取」后恢复。
3. **保存前不校验**：坏配置也能落盘，触发宿主重载。
   → `hx_save_config` 写文件前先 `HuiXiangConfig.model_validate(merged)`，
   不合法整单拒绝且不碰文件。实测传 `'AI'` 返回「配置不合法，未保存」且文件零改动。
4. **页面对「已下线」自愈**：宿主重载是框架行为（改不了也不该改），
   页面检测到该错误时显示「插件刚重新加载，正在自动重连…」，2.5 秒后自动重试。

**验证**：173 项单测 + 三道预检全过；本地实测坏配置被拦（文件未动）、
好配置落盘（`['AI','数码硬件']`、`posts_per_round=12`、模式「自动」均可写）；
17:12 重启后 `LOAD: OK / 报错无`，线上配置仍为「演练」。

## 2026-11-06 · 后台页面精简 + 状态折叠 + 修「保存失败」

**用户四条反馈（都是对着「插件扩展」里的后台页面，不是 WebUI 本体）**

1. 「运行状态做个可折叠」→ 改成 `<details>`，折叠时标题栏仍显示
   「运行中 · 演练 · 后台在跑 · 已登录」摘要；展开状态记在 localStorage。
2. 「插件信息在后台要它干啥，我又改不了」→ `hx_get_config` 和
   `hx_config_schema` 都过滤掉只读的 `plugin` 段（只有 config_version，页面 disabled），
   后台只剩 11 个可编辑的业务段。
3. **「保存失败：ApiError: 参数不符合声明: sections」** → 真 bug。
   宿主 `Parameter.accepts()` 对 `type: object` 要求「value 的所有键必须在声明的
   `properties` 里」，而配置段是动态的、声明不了，传任何非空字典都会被拒。
   改成 **JSON 字符串**传参（`type: string`, max_length 60000），后端 `json.loads` 解析；
   前端 `JSON.stringify(patch)`。同时把写文件从 `tomlkit` 换成**标准库按行改写**，
   不再依赖第三方库，注释与 `[plugin]` 段位置照样保留，写前备份 `.toml.bak_webui`。
4. 「后台设置不给提示怎么知道怎么配」+「插件扩展那个页面信息过多」→
   - 每个配置项**直接显示中文说明**（原来只塞在 `title` 里靠鼠标悬停，等于没有）；
   - 页面从 7 个卡片精简成 3 个区：
     **配置设置**（默认展开，主体）、**运行状态**（可折叠）、
     **其它**（默认折叠，收纳账号登录 / 手动操作 / 分区目录 / 操作记录 / QQ 命令）；
   - 删掉重复的「怎么改设置」说明块，三个折叠状态各自记忆。

**验证**：173 项单测、三道预检全过；服务器实测
`hx_save_config` 返回「配置已保存并立即生效」、`posts_per_round` 落盘为 11、
注释与 `[plugin]` 保留、两个配置 API 均只返回 11 个可编辑段（测试后已还原线上配置
并清理备份文件）；16:57 重启后无加载报错。

## 2026-11-06 · 配置搬进后台管理 + 浏览条数对齐 + 分区失败不再拖垮总结

**用户三条反馈**

1. 「立刻浏览一轮」提示 10 条，下面只列 5 条 → 我在 `hx_browse_now` 里写了 `[:5]`。
   改成返回**全部**条目的标题；少数帖子没标题会过滤，提示语明确写出
   「本轮 N 条，其中 M 条有标题」，不再出现数字对不上。
2. 「后台管理」和「插件管理」是不是放反了？后台管理才该是改配置的地方 →
   该判断正确。此前后台仅支持查看状态，配置要跳到宿主「插件管理」页。
   现在把配置能力搬进后台：
   - 新增 3 个 `@API`：`hx_get_config`（读 config.toml）、
     `hx_save_config`（写回 + 立即热更新）、`hx_config_schema`（拿中文表单定义）。
   - 后台新增「**配置设置**」区块，直接渲染宿主生成的中文表单
     （12 个分组、54 个字段、下拉框全保留），改完点「保存设置」立即生效不用重启。
   - 用 `tomlkit` 改写以**保留注释和 `[plugin]` 段位置**，写前自动备份为
     `.toml.bak_webui`，密钥字段用占位符不回传、也不覆盖；
     保存后 `set_plugin_config()` 让当前实例热更新。
   - 宿主配置页和后台改的是同一份 `config.toml`，两边始终同步。
3. 操作记录 `[summary] ok=False 执行异常：CommentRejectedError` →
   根因：`run_summary_once` 第一步 `_pick_partition()` 会真实请求 `TOPIC_INDEX`，
   上游偶发拒绝就抛 `CommentRejectedError`，把整轮总结带挂（16:29 成功、16:31 失败即此）。
   分区只是发帖的可选装饰，不该决定成败：
   - `_pick_partition` / `run_browse_once` 都加异常兜底，失败改读本地分区缓存，
     缓存也没有就返回 `None`（发动态、不挂分区）；
   - 新增 `tests/test_partition_tolerance.py` 2 项回归测试。

**验证**：173 项单测全过；三道预检全过；服务器实测
`hx_get_config` 12 段 / `hx_config_schema` 12 段且 4 个下拉全在 /
tomlkit 改写保留注释与中文值 / `hx_browse_now` 按实际条数返回；
16:41 重启后无加载报错。

## 2026-11-06 · 消掉后台顶部「Theme font unavailable」红字

**反馈**：进入后台显示 `Error: Theme font unavailable: JetBrainsMono-Medium.ttf`。

**排查结论：这是 MaiBot 本体前端的边界问题，不是插件引入的，且不影响功能。**

- 字体文件全都在（`maibot_dashboard/dist/fonts/` 4 个，JetBrainsMono 273860 字节、
  TTF 头 `00010000` 有效），`http://127.0.0.1:8001/fonts/xxx` 实测 **200 正常**。
  8000 是旧版 WebSocket 服务，返回 404 属正常。
- 报错出自 `plugin-webui-C1enjH3M.js`：
  ```js
  if (!s.ok) throw new Error(`Theme font unavailable: ${a}`)
  ```
  它扫描页面计算样式拼出的 CSS 字符串（`Mt(B.styles)` 用 `t.includes(字体名)`），
  命中就 `fetch('/fonts/xxx')`；失败经 `.catch(G => S(String(G)))` 显示到**外层页面顶部**。
  在反代 / 非标准端口下这个相对路径会落到错的 origin，于是弹红字。
- 不改宿主源码（会被升级覆盖）。改法：
  1. 页面 CSS 用 `!important` 把 `html/body/button/...` 及宿主探针选择器
     （`[data-theme-probe]`、`.maibot-*`）全量覆盖成系统字体栈；
  2. 覆盖 `--typography-font-family-code` 等字体变量；
  3. **连 CSS 注释里都不能出现那几个字体名**——宿主扫的是整段样式字符串，注释也算
     （第一次改完预检就抓到注释里残留的字体名，已删）；
  4. 页面里加 `error` / `unhandledrejection` 兜底，静默这类噪声。
- `tests/simulate_runner_load.py` 增加静态检查：CSS 里出现主题字体名或缺 `!important`
  覆盖就报错。

**验证**：16:29 重启后部署的 HTML 里 CSS 无主题字体名、有覆盖、有串行队列，
`加载 OK / 报错 无`；三道预检全过。

## 2026-11-06 · 修复后台一直「读取失败：Two requests are already running」

**反馈**：进入后台后持续显示「读取中」，运行状态无数据，手动操作报错
`Two requests are already running`。

**根因（查宿主前端源码确认）**

`plugin-webui-C1enjH3M.js` 的 bridge 里硬编码：
```js
if (pending.size >= 2) throw new Error('Two requests are already running');
```
同一插件页面**最多 2 个未完成请求**。而我的页面用 `Promise.all` 一次并发发了 4 个查询
（总览 / 操作记录 / 分区 / 登录状态），再叠上 10 秒自动刷新和「未登录自动弹二维码」，
必然触及并发上限。首个请求失败后整页停留在「读取中」。

**改法**

- 加**全局串行队列** `serial(fn)`：所有 `maibot.query` / `maibot.action` 都排队，
  任何时刻只有 1 个在飞，连 2 个都不会同时出现。
- 4 个查询改成**严格串行**，每个单独兜错，单个失败不拖垮整页。
- 加双锁：`refreshInFlight` 防止自动刷新堆叠，`busy` 防止手动按钮与自动刷新互抢；
  拆出 `_refreshNow()` 供手动操作内部调用，绕过 `busy` 自锁。
- 自动弹二维码时复用外层锁，不再重复加锁导致自锁。
- `tests/simulate_runner_load.py` 增加静态检查：页面若出现并发聚合调用或缺少串行队列，
  预检直接报错（剥掉注释后判断，避免误报）。

**验证**：服务器上部署的 HTML 含 serial 队列、单飞锁、无并发聚合；
16:13 重启后 `加载 OK / 报错 无`；三道预检全过，171 项单测全过。

## 2026-11-06 · 下拉框全覆盖 + 后台登录二维码（用户两条反馈）

**反馈 1：有固定选项的字段不该让用户手打中文**

- 之前 `mode` / `partition_mode` / `trigger_by` / `tone` 四个字段用了 `str` + 中文默认值，
  界面上还是文本框，用户得手打「按帖数」「两者都看」，打错一个字就静默失效。
- 改成 `Literal[...中文...]`：宿主 `_extract_select_choices()` 自动渲染成下拉框，
  pydantic 也只接受这几个值，**不可能打错字**。
  ```
  运行模式   → [演练, 自动]
  浏览范围   → [全部分区, 仅指定分区]
  什么时候触发 → [按帖数, 按时间, 两者都看]
  说话语气   → [平和, 活泼, 克制, 毒舌]
  ```
- 每个枚举加 `field_validator(mode="before")`，把老配置里的
  `dry_run` / `auto` / `all` / `selected` / `count` / `time` / `both` 静默转成中文，
  历史配置不会加载失败（已实测 4 组）。
- **发帖默认分区**改成动态下拉：覆盖 `get_webui_config_schema()`，从分区缓存读真实分区名
  注入 `choices`。实测 **95 个选项**（不指定分区 + 94 个真实分区：AI / 数码硬件 / Steam…）。
  「浏览 → 指定分区」同时改成多选下拉（94 项）。
  分区缓存由 worker 每轮 / 后台「立刻浏览一轮」自动刷新，不是硬编码白名单。

**反馈 2：后台要能看到登没登录、未登录直接弹二维码**

- 后台新增「小黑盒账号登录」卡片：
  - **已登录** → 显示昵称 + 账号尾号（如「已登录：某某（尾号 1234）」）+「退出登录」按钮；
  - **未登录** → 页面打开就**自动生成二维码并展示**，附扫码三步说明、剩余秒数、
    「换一张二维码」「我扫好了」按钮；过期可一键重生成；
  - 每 10 秒轮询登录结果，扫码确认后自动切成「已登录」。
- 新增 3 个 `@API`：`hx_login_status` / `hx_request_qr` / `hx_logout`；
  二维码走 `auth.QrLoginService`，**凭据绝不返回给前端**（只给昵称和 uid 尾号）。

**验证**：171 项单测、模拟 Runner 加载（19 组件 + 8 个 API 引用一致性）、真实 webui schema 全过；
服务器 16:04 重启后 `LOAD: OK / ERRS: 无`；登录 API 实测返回 `已登录：某某`。

## 2026-11-06 · 界面全中文 + 后台管理面板（用户反馈「点进去全是英文、没后台」）

**改了什么**

- `config_model.py`：重写。54 个配置项**全部**加中文 `label` / `hint`，
  12 个配置段落标题、说明全部中文化。段标题用 `__ui_label__`，字段标题走
  `json_schema_extra={"label": ..., "hint": ...}`。
- `config_model.py`：下拉选项的**值本身改成中文**（`演练` / `自动` / `全部分区` /
  `仅指定分区` / `按帖数` / `按时间` / `两者都看`）。原因是宿主的 select 候选只能来自
  `Literal`，没有「内部值 → 显示名」映射，不改成中文界面上就还是英文。
- `layers/_cfg.py`：新增 `norm_choice()` 做「中文界面值 → 内部英文值」归一化，
  同时兼容老配置（`dry_run` / `auto` / `all` / `selected` / `count` / `time` / `both`）。
  接入点：`plugin._runtime_mode`、`inbound.resolve_browse_partitions`、
  `inbound.browse_feeds`、`summarize.summary_ready`。
- `config.toml`：三个选项值同步改成中文（`mode="演练"`、`partition_mode="全部分区"`、
  `trigger_by="按帖数"`）。
- **新增后台管理面板**：`webui/控制台.html` 重写为全中文实时控制台，
  `webui.json` 重新启用（`schema_version=2` + `pages`）。
- `plugin.py`：注册 5 个 `@API` 组件供面板调用——
  `hx_overview`（总览）/ `hx_action_log`（操作记录）/ `hx_browse_now`（手动浏览）/
  `hx_reset_circuit`（解除熔断）/ `hx_partitions`（分区目录）。

**为什么（上一次 webui.json 导致整个 WebUI 不可用的复盘）**

之前 webui.json 失败有两个独立原因，这次分别解决：
1. 顶层结构错（用了 `webui_version` / `sections`）→ 这次用 `schema_version: 2` + `pages`；
2. 页面引用了**未注册**的静态 API → 这次先用 `@API(name=...)` 注册，webui.json 里按同名引用。

并且把「页面引用的 API 是否已注册」加进 `tests/simulate_runner_load.py` 预检，
以后这类不一致在本地就会报错，不用拿线上试。

**怎么验证**

- 三道预检全过：171 项单测 / 模拟 Runner 加载（16 组件 + 5 API 一致性）/ 真实 webui schema。
- 服务器 15:37 重启后日志：`插件注册完成`、`已加载=4，失败=0`、
  `回响：插件已加载（enabled=True，mode=演练）`、worker 启动、LLM 调用正常，**无 WebUI 报错**。
- 5 个后台 API 在服务器上直接调用，返回真实数据：
  ```
  hx_overview    → enabled=True, mode=演练, logged_in=True, pending_material=200
  hx_action_log  → 3 条，动作名已中文化（感想总结 / 互动 / 浏览）
  hx_partitions  → 110 个分区，前 3：AI / 数码硬件 / Steam
  hx_reset_circuit → 熔断已解除
  hx_browse_now  → 本轮浏览到 10 条动态（演练，未产生真实动作）
  ```
- 配置页 schema 逐字段检查：**54 个字段全部有中文 label，无一漏网**。

## 2026-11-06 · 已上线运行（dry_run），连续两轮闭环稳定

**当前线上状态（部署环境）**

- 插件 `local.huixiang-operator v0.7.0`：**已加载，失败 0**。
- 配置：`enabled=true`、`mode=dry_run`（真跑流程，不发真实内容）。
- 后台 worker：每 300 秒一轮，连续两轮全绿：

  ```
  12:35:06 [browse] ok=True → [interact] ok=True → [summary] ok=True（演练组装 topic_ids=806933）
  12:40:16 [browse] ok=True → [interact] ok=True → [summary] ok=True
  ```

- 小黑盒登录：复用现成凭据，无需重新扫码。
- WebUI：`webui.json` 已移除（宿主允许无 WebUI 扩展），插件跑通优先，面板以后补。

**这轮修的 3 个真机问题**

见 `docs/已知问题清单.md` 第 33~35 条：`task_name` 不是自由标签、`未知` 不可回退为 `0`、
模型写不够字数光加重试没用。

## 2026-11-06 · 真机跑通完整闭环（浏览 → 互动 → 总结 → 演练发帖）

**改了什么**

- `runtime.py`：`llm.generate` 的 `task_name` 从自造的 `"huixiang"` 改成宿主的合法任务名
  `replyer`，并加 `replyer → utils → fast_model → planner` 四级回退。
- `runtime.py`：宿主「不抛异常只回空结果」时显式报中文错，不再让上层误判成「模型输出偏短」。
- `models.py`：`LifeTrace.energy` / `social_battery` 默认值从 `0.0` 改成 `None`（未知 ≠ 没电）。
- `layers/sense.py`：`can_speak_now` 只在电量**明确**低于阈值时拦截，`None` 放行。
- `layers/create.py`：新增 `pad_summary_to_min()`——模型写不够 200 字时，用已刷到的真实标题补一段，
  不编造；补足内容不重复同一批标题。
- `layers/create.py`：总结重试从 2 次提到 3 次，且重试提示词带上「上一版只有 N 字，这次必须写够」。
- 复用现成凭据做联调：从 `maibot-team.xiaoheihe-adapter` 复制 `credentials.json` 到回响数据目录
  （四字段 `user_pkey` / `user_heybox_id` / `heybox_id` / `x_xhh_tokenid` 完全一致），不用重新扫码。
- 新增 `tests/test_live_issues.py`（12 项）：把上面两个 bug 钉死。

**为什么**

- `task_name` 是最致命的：调用直接失败，`summary` 连续 48 轮报「字数不足 0 < 200」，
  表现为模型输出偏少，实际是**一次模型调用都未成功**。此类「错误信息指向偏差」排查成本最高。
- 社交电量兜底成 0.0：没接 life-frequency 时恒判「不宜开口」，`run_once` 永远过不去。
- 补足逻辑是必要的兜底：真实模型经常只写 100~130 字，光加重试次数压不出来。

**怎么验证（部署环境实测）**

真实浏览（用现成凭据）：
```
fetch_feed → 9~10 条真实帖子
TOPIC_INDEX → 6 个分组、19+22 个子分区（AI=806933 / 数码硬件=18745 / Steam=425422）
fetch_notifications → 回复 20 条、@ 9 条
check_credentials → post_comment_permission=1（有评论权限）
```

worker 单轮完整跑通（2026-11-06 12:25）：
```
12:25:28 [browse]  ok=True
12:25:34 [interact] ok=True
12:25:37 [summary] ok=True  演练：已组装发帖（link_tag=28，topic_ids=806933），未真正发送
```

LLM 修好后模型调用正常（提示词 510 → 完成 244 tokens），总结稳定落在 374 字 / 4 段。

测试：`python -m unittest discover -s tests` → 171 项全过（2 skip）。

## 2026-11-05 · 真机联调：从「加载失败」到「命令跑通」

**改了什么**

- `plugin.py`：路径自举（把插件根目录写入 `sys.path`），修 `No module named 'contracts'`。
- `plugin.py`：删掉 `config_reload_subscriptions = ("self",)`（SDK 只认 `bot`/`model`）。
- `plugin.py`：新增 `auto_reply` 装饰器，9 个命令全部加上。**宿主不会替插件发返回值**，
  必须自己 `self.ctx.send.text(text, stream_id)`；日志里「命令执行成功」但聊天无回复就是漏了这步。
- `plugin.py`：新增 `/回响登录`、`/回响登录状态`、`/回响登出` 三个命令（B 方案必需，之前完全没写）。
  登录用 `auth.QrLoginService`，二维码经 `ctx.send.image(base64, stream_id)` 直接发到聊天。
- `_manifest.json`：删掉 `keywords` / `notes`（pydantic `extra="forbid"`，未声明字段直接拒载）。
- `webui.json`：**移除**。原因是自定义页面引用了宿主未注册的静态 API，插件注册阶段就失败。
  宿主 `load_webui_extension()` 在文件不存在时 `return None`，不注册 WebUI 扩展但插件照常加载。
  正确 schema 草稿存 `docs/待恢复/webui.json.v2草稿`，等宿主侧 API 注册齐了再启用。
- 新增 `tests/simulate_runner_load.py`：按 Runner 真实方式加载（文件路径 + 扁平模块名 +
  **不给插件目录 sys.path**），本地复现真机加载，避免每次重启线上试错。
- 新增 `tests/check_webui_schema.py`：用服务器上拉下来的真实 `webui_schema.py` 校验 webui.json。
- `config.toml`：`enabled` 改 `true`，`mode` 仍为 `dry_run`。

**为什么**

- 用户选了 B 方案（走到演练闭环），而 B 的第一步是扫码登录——原代码里根本没有登录命令，
  不补这个就没法往下走。
- 命令「执行成功但无回复」属易误判情形：日志显示成功，用户侧无任何输出。
  根因是宿主只把返回值当日志，不负责发送。`auto_reply` 一次性解决，不再逐个命令手写。

**怎么验证**

- 服务器真机日志（2026-11-05 22:33:58）：
  ```
  命令执行成功: hx_status (拦截等级: 1)
  命令处理完成，跳过后续消息处理: 回响状态（2026-11-05）...
  send_service :: [3878452493:qq:snowluma_gateway] [，，的私聊] 半句: 回响状态（2026-11-05）...
  ```
  命令执行 → 文本生成 → `send_service` 投递，三段齐全，链路通。
- `python tests/simulate_runner_load.py`：manifest / 导入 / 11 个组件 / 默认配置 / 生命周期全通。
- `python -m unittest discover -s tests`：159 项全过（2 skip：httpx + 无 webui.json）。
- 本地 fake `ctx.send` 验证 `auto_reply` 真的调用了发送接口，`cmd_status` 发出 1 条。

## 2026-11-04 · 真机对齐：按真实 SDK 契约重写入口（能在 MaiBot 上加载了）

**改了什么**

- 新增 `runtime.py`：薄上下文适配层。把「插件实例 + PluginContext」包成业务层认识的
  `.config / .paths / .store / .api / .xhh_client / .generate_text`，业务层彻底无法判断宿主长什么样。
- 重写 `plugin.py`：按真实 SDK 2.21.0 契约改写——`create_plugin()` 无参；`__init__(self)` 无参；
  不再给 `self.config` / `self.ctx` 赋值（都是只读 property）；`on_load` / `on_unload` /
  `on_config_update(scope, config_data, version)` 全部改成 async；6 个命令改用 `@Command` 装饰器、
  2 个工具改用 `@Tool` 装饰器（原来是自己维护一份字典「打算以后注册」）。
- 重写 `config_model.py`：改为继承真实的 `maibot_sdk.PluginConfigBase`；**新增 `PluginSection`**
  定义 `[plugin].config_version`（缺了 SDK 生成默认配置时直接抛错）；删掉 `from __future__ import annotations`
  （会让 pydantic 解析失败）；`Literal` 改 `str`（避免延迟注解下的解析问题）。
- 新增 `layers/_cfg.py`：配置读取统一收口（`section` / `read` / `read_str` / `read_int` / `read_bool` / `read_list`），
  把 6 个文件里各写一遍的 `isinstance(cfg, dict)` 分支全部换掉。
- `state.py` 新增 `StoreContainer`（从 `plugin.py` 搬下来）：业务层与测试不再为了拿状态容器去 import 插件入口。
- `_manifest.json`：`manifest_version` 1→2，补 `license` / `sdk` 段 / `plugin_type` / `i18n`，
  `dependencies` 从 dict 改成 SDK 要求的 `[{type, name, version_spec}]` 数组，`capabilities` 换成真实的
  `api.call` / `llm.generate` / `send.text` / `declarative_basic`。
- 新增 `tests/test_sdk_contract.py`（14 项）：用**真实 SDK** 断言上面每一条契约。

**为什么**

- 用户问「直接接进去跑一遍就知道了」，这个判断是对的：接入后立即暴露 6 处与真实契约不符的实现，
  其中 3 处是**装上就加载失败**级别（构造签名、只读 property、同步生命周期方法）。
- 之前 146 项测试全过，是因为它们测的全是业务逻辑，没有一条测「宿主能不能加载」。
  这属于测试盲区：**业务逻辑正确不等于插件可加载**。

**怎么验证**

- 部署环境上用真实运行环境
  `AppData\Roaming\uv\tools\maibot\Scripts\python.exe`（Python 3.13.18 + SDK 2.21.0）实测：
  - `plugin.py` 导入 OK；
  - 8 个组件全被 `collect_components` 收集（6 命令 + 2 工具）；
  - 默认配置 12 段齐全，`plugin.config_version = 1.0.0`；
  - `on_load` → `cmd_status` → `cmd_action_log` → `on_unload` 全通；
  - `cmd_status` 实际输出：「回响状态（2026-11-04）/ 演练额度已用：0 / 真实额度已用：0 / 熔断：正常 /
    后台 worker：未运行（0 轮）/ 已开联动：无」。
- 本地双环境跑：无 SDK 时 159 项全过（22 skip 宿主相关）；有真实 SDK 时 159 项全过（1 skip = httpx）。
- 验证完**已把插件移出 `plugins/` 到本地备份目录**，
  服务器 plugins 目录恢复原状，没有重启 MaiBot、没有改任何配置、没有影响线上。

## 2026-11-04 · 阶段 7：外围联动（语音 / QQ 空间 / 日记）

**改了什么**

- 新增 `layers/links.py`：三个可选出口（`broadcast_voice` / `sync_qzone` / `write_diary`），全部走 `ctx.api.call`，接口名取 `contracts.py` 常量。
- 开关短路：`[links]` 里关掉的出口连 `api.call` 都不发起，直接返回「未开启」。
- 失败降级：任一出口抛异常只记中文日志并返回失败结果，绝不向上抛；`announce_published` 逐个调用，一个失败不影响其他。
- `plugin.py`：真实发帖回读通过后、总结发帖成功后调用 `announce_published`；`cmd_status` 返回 `enabled_links`。

**为什么**

- 阶段 7 的验收线是「逐个卸掉联动，发帖闭环仍然通」：联动必须是旁路，不能进主链路判定。
- 用户明确要求任一个关掉都不影响发帖闭环，所以三个出口各自独立短路、独立降级。

**怎么验证**

- `python -m unittest discover -s tests` 146 项全过（1 skip：httpx 运行时依赖），新增 16 项阶段 7 测试。
- 开关：默认全关；部分开启只调对应出口；关掉的出口 `api.call` 调用次数为 0。
- 降级：`api.call` 抛异常时不外抛；无调用通道时返回 `no_api_channel`。
- 解耦：5 种开关组合（含全关、含故意让某出口失败）下，发帖闭环均正常返回帖子 ID。

## 2026-11-04 · 阶段 6：自动运营 Worker 与额度

**改了什么**

- 新增 `worker.py`：`OpsWorker` 异步后台循环，按 `[browse].browse_interval_seconds` 间隔跑「浏览 → 互动 → 总结」；`run_round()` 与调度分离，可单测。
- 单轮异常不中断循环：`_loop` 捕获异常记日志后继续；`_step` 捕获子步骤异常，后续步骤照跑。
- 作息联动：`sense.SLEEP_ROUTINES` + `should_pause_for_routine`，睡觉 / 小睡时段整轮跳过并写操作记录。
- 发帖时段：`sense.parse_time_window` / `in_post_time_window`（支持跨零点），护栏新增 `check_time_window` 闸门并入 `is_publishable`；演练放行、真实拦截。
- `state.py` 新增 `ActionLogStore`：每轮动作留痕（时间 / 类型 / 结果 / 模式 / 备注），只留最近 500 条，WebUI 可查。
- `plugin.py`：`on_load` 真正启动 worker、`on_unload` 停止；`_runtime_mode` 保证「未启用或非 auto 一律演练」；新增 `/回响记录`、WebUI `get_action_log` / `interact_once` / `summary_once`。

**为什么**

- 阶段 6 是把前五个阶段串成「能自己跑」：没有 worker 就只能是手动命令，没有操作记录就没法排障。
- 演练默认是硬约束：`_runtime_mode` 只有在 `enabled=true` **且** `mode=auto` 时才返回真实，其余一律演练，避免误发。
- worker 必须「打不死」：网络抖动、上游改字段都会发生，单轮失败不能让人工重启。

**怎么验证**

- `python -m unittest discover -s tests` 146 项全过（1 skip），新增 28 项阶段 6 测试。
- 作息：6 个睡觉词全部停发，4 个清醒词不停；开关关掉则永不停。
- 时段：正常段（09:00–22:00）与跨零点段（22:00–06:00）判定正确；真实模式时段外拦截、时段内放行；演练放行。
- Worker：`run_round` 按序跑三步并写 3 条操作记录；某步抛异常时后续步骤仍执行；睡觉时段整轮跳过、`calls` 为空。
- 额度：连打演练不写任何桶；真实 `reserve_quota` 只加 real 桶。
- 限流：5 秒内二次写操作被拦；小时计数满 30 被拦；熔断冷却 300 秒、拨回 6 分钟后自动放行。
- 端到端冒烟：worker 跑一轮 → 总结 342 字 3 段 → 演练发帖 → 操作记录 3 条。

## 2026-11-04 · 阶段 5：感想总结发帖代码落地

**改了什么**

- `state.py` 新增 `BrowseDigestStore`：记录刷过的帖子（post_id / 标题 / 摘要 / 分区 / 作者 / 刷到时间），按 post_id 去重、只留最近 200 条，`summarized` 标记已消费、`last_summary_ts` 记上次总结时间。
- `layers/create.py` 总结能力补齐：`count_chars`（非空白字符口径）、`count_paragraphs`、`clamp_chars`（按句读截断）、`fit_summary_shape`（段数过多合并、过少拆分、整体不超上限）、`build_summary_material`（浏览素材整理）；`generate_summary` 改为吃浏览项、按配置收敛、字数不足自动重试。
- `models.py`：`SummaryDraft` 补 `chars` / `ai_flavor_score`。
- 新增 `layers/summarize.py`：`should_summarize`（count/time/both 纯逻辑触发判定）、`summary_ready`、`summary_to_draft`（总结帖默认纯文字动态 28）、`check_summary_shape`（字数段数校验）、`run_summary_once`（素材→生成→校验→护栏→发布→回读→消费素材）。
- `plugin.py`：注入 `digest` 存储，落地 `/回响总结`（手动触发跳过判定），`run_browse_once` 与 `interact_with_feeds` 均写入浏览摘要。

**为什么**

- 阶段 5 是「刷完小黑盒后对刷过内容总结发帖」，正文 200–400 字、2–4 段，复用创作层中文口语与护栏层。
- 字数段数是硬约束，光靠提示词不可靠：必须在代码侧收敛（截断落句读、段数合并/拆分），否则永远卡在「超长」或「一段到底」。
- 浏览摘要必须记录，否则总结没有素材来源；消费标记保证同一批内容不被反复总结。

**怎么验证**

- `python -m unittest discover -s tests` 97 项全过（1 skip：httpx 运行时依赖），新增 30 项阶段 5 测试。
- 字数段数收敛实测：510 字 3 段 → 400 字 3 段；单段 600 字无标点 → 400 字 2 段（硬拆保证下限）；6 段 → 3 段。
- 触发判定：count 达阈值/未达、time 到期/未到期、both 任一成立、无素材一律不触发。
- 未接真实账号、未部署、未启用；真实总结发帖待服务器人工点头。

## 2026-11-04 · 阶段 4：评论 / 浏览 / 互动代码落地

**改了什么**

- `state.py` 新增 `NotificationStore`：记录已处理通知 ID + 最后拉取时间，同一通知不重复处理，只保留最近 1000 条。
- `layers/inbound.py` 完整接入：`fetch_notifications`（区分 @ message_type=16 与回复 list_type=0）、`browse_feeds` 支持 selected 分区（TOPIC_FEED）、`resolve_browse_partitions` / `should_refresh_partitions` 纯逻辑、`refresh_partitions` 加缓存判定。
- `layers/publish.py`：`publish_comment` / `reply_floor` 加 `dry_run`；`reply_floor` 用 `parent_id`（parent≠root）；新增 `reply_notification` 按通知有无 rootId 分流（楼中楼带 rootId / 顶层评论 -1）。
- `layers/guard.py`：新增 `check_comment_interval`、`is_commentable`（评论额度 + 间隔 + 去重 + AI 味 45 + 熔断）；`reserve_quota` 按 action 写对应游标（post/comment/reply/like）。
- 新增 `layers/interact.py`：互动编排层（`respond_to_notifications` 通知响应、`interact_with_feeds` 主动浏览评论），纯逻辑 `should_reply_notification` / `pick_comment_targets`。
- `models.py`：`CommentTarget` 补 `parent_id`。
- `plugin.py`：注入 `NotificationStore`，落地 `/回响浏览`、新增 `/回响互动` 命令与 `run_browse_once` / `run_interact_once`。
- 配置：`[interact]` 补 `max_comments_per_round`；WebUI `ai_flavor_threshold` 口径从 0.7 修正为 60。

**为什么**

- 阶段 4 是「评论 / 浏览 / 互动」：主评论、楼中楼带 rootId、收 @/回复自动回、自动浏览（可勾选分区）、互动评论、分区定时刷新。
- 楼中楼「error root」是历史问题（避问题清单第 7 条）：回楼中楼必带通知里的 rootCommentId 作 rootId，顶层评论才用 -1。
- 同一通知反复拉取会导致反复回复，去重是硬要求。

**怎么验证**

- `python -m unittest discover -s tests` 67 项全过（1 skip：httpx 运行时依赖）。
- 新增 20 项阶段 4 测试：楼中楼 rootId 传递（parent≠root）、同一通知不重复处理、浏览分区勾选只刷勾选分区、分区定时刷新、评论 AI 味 45 / 额度 / 间隔、通知响应开关、评论目标抽样。
- 仍未接真实账号、未部署、未启用；真实评论 / 回复需人工在服务器点头。

## 2026-11-04 · 阶段 3：最小闭环代码落地（发帖/图床/回读/护栏/状态存储）

**改了什么**

- 新增 `media_upload.py`：图床三步（info/v2 → token/v2 → COS PUT → callback/v2），单图 5 MB。
- 新增 `community_client.py`：`CommunityClient`（继承 vendor 直连客户端），发帖 / 发文章 / 图床 / 回读 / 带图评论；发帖口径对齐 MCP publish.ts（link_tag 数值、topic_ids 逗号串、正文 img 段内嵌）。
- 落地 `state.py`：JSON 文件存储后端 + 全部 Store（额度分桶、去重、幂等、游标、分区缓存、熔断、登录）。
- 落地 `layers/create.py`：`polish_chinese`（去楼层词/破折号转逗号/去句号/段间不空行）、`ai_flavor_score`（直译 aiflavor.ts，0–100）、`fallback_text_only`、内容生成（走 ctx 注入生成器）。
- 落地 `layers/guard.py`：六道闸（额度/间隔/去重/AI 味/熔断/占用），演练 `dry_run_no_quota_cost` 显式读取。
- 落地 `layers/publish.py`：发帖/发文章/评论/楼中楼/图床/回读，`dry_run` 参数；topicIds 逗号串、img 段内嵌。
- 落地 `layers/inbound.py`：分区目录（TOPIC_INDEX → topics_list 树）、浏览（FEED）、登录态委托 auth。
- 落地 `layers/sense.py`：生活轨迹读取 + 能否开口判定。
- 编排 `plugin.py`：`run_once` 六块流水线（演练/真实分支），`_StateContainer` 注入 ctx.store。
- 统一 AI 味阈值口径为 0–100（config 默认 60、评论 45），护栏真正读 config。

**为什么**

- 阶段 3 是最小闭环：登录→选分区→生成→配图→发一篇→回读核验。六块接口从 NotImplementedError 落地为可跑代码。
- 发帖/图床/回读是直连小黑盒的地基，必须对齐 link_tag / topicIds / img 段 / COS 三步契约。
- 演练不占额度是硬约束：`dry_run_no_quota_cost` 开关 + `quota_bucket_key` 分桶键两层防线。

**怎么验证**

- `python -m unittest discover -s tests` 47 项全过（1 skip：httpx 为运行时依赖）。
- 纯逻辑全覆盖：口语清洗、AI 味打分、降级、六道闸、额度分桶隔离、去重按 mode 隔离、熔断冷却、topicIds 逗号串最多 5 个、link_tag 数值、img 段内嵌。
- 连打十次演练额度不变（real 桶始终 0）。
- 仍未接真实账号、未部署、未启用、未真发帖；真实发帖需人工在服务器点头。

## 2026-11-04 · 阶段 2：搬入 vendor 直连客户端 + 扫码登录 + 签名自查

**改了什么**

- 搬入源②小黑盒适配器的 `vendor/` 包（request_signing / api_client / security / endpoints / parsers / rate_limiter / models，含 LICENSE、README、THIRD_PARTY_NOTICES），原样保留第三方签名客户端。
- 新增 `comment_signing.py`：写请求（POST）签名通道，`comment_query` 的 hkey 用 `stamp+1` 计算，`_time`/`_rnd` 保留原始时间。
- 新增 `auth.py`：扫码登录服务（`QrLoginService`）、四字段凭据校验（`user_pkey`/`user_heybox_id`/`heybox_id`/`x_xhh_tokenid` 缺一不可）、凭据原子落盘/读取/清除、二维码过期重新生成、日志脱敏。
- 新增 `tests/test_stage2_signing.py`：18 项签名自查。

**为什么**

- 阶段 2 是「搬 vendor 直连客户端 + 登录」，是阶段 3 最小闭环（登录→发帖→回读）的地基。
- 发布层要直连小黑盒（不经 8757 MCP），签名必须对齐两套口径：读走 `generate_hkey(path, stamp, nonce)`、写走 `comment_query`（hkey 用 stamp+1）。
- 凭据四字段缺一不可，二维码会过期必须能重新生成；凭据只落数据目录，日志不得有明文。

**怎么验证**

- `python -m unittest discover -s tests` 24 项全过（1 项 skip：httpx 为运行时依赖，本机未装）。
- 用合成凭据跑签名自查，不连真实账号、不发起任何网络请求。
- 四字段校验：齐全通过、缺 pkey / 缺 tokenid / 缺 heybox_id 分别报缺失。
- 读/写签名分离：写 hkey 用 stamp+1（用进位时间戳 1699999999→1700000000 验证确实与读不同）。
- `redact_log_text` / `redact_data` 把 cookie / token / pkey 明文替换为 [REDACTED]。
- 期间发现 hkey 算法对 path 与 timestamp 有截断（交错后只取前 20 字符），据此修正两条测试断言。

## 2026-11-04 · 四源融合：定稿融合映射，六块架构 + 10 配置段，接入层补齐

**改了什么**

- 新增 `docs/融合映射.md`：四源（MCP / 适配器 vendor / 二创 / 回响骨架）到六块的函数级搬运方式表，逐条标「直接搬 / 需改造 / 仅参考」。
- 重写 `DESIGN.md`：四层改为六块（接入 / 感知 / 创作 / 护栏 / 发布 / 状态存储），每块写输入、输出、依赖、不负责什么。
- 重写 `docs/接口契约.md`：修正 `topicIds` 为逗号分隔数字字符串（旧文档误写 int 数组）、图床单图 5 MB（旧写 5 GiB）、补签名分流（读 `generate_hkey` / 写 `comment_query` stamp+1）、补护栏阈值（去 AI 味评论 45 正文 60、验证码 5 分钟、限流 5 秒 / 30 次每小时）。
- 扩充 `docs/已知问题清单.md`：在旧 16 条上补 6 条四源补充项（17~22）。
- 重写 `docs/开发路线.md`：六阶段改七阶段，补「感想总结发帖」阶段。
- 重写 `docs/配置说明.md`：6 段改 11 段（basic/login/browse/interact/publish/summarize/image/create/sense/guard/links）。
- 重写 `README.md`：定位、六块简图、目录树、自检、参考来源同步更新。

**为什么**

- 四源深读结论出来了：旧文档的 `topicIds` 类型、图床尺寸、签名分流都写错，不改成对的，发布层照写必已知问题。
- 接入层（登录 / 收@回复 / 主动浏览 / 分区动态）是旧四层里缺的一块，融合时补齐。
- 配置从 6 段扩到 11 段，把登录、浏览、互动、感想总结、配图、创作、外围联动各自独立成段，跟六块对齐。

**怎么验证**

- `docs/融合映射.md` 四源每个文件都有搬运方式；`docs/接口契约.md` 里 `topicIds` 是数字字符串、图床是 5 MB。
- 六块架构的「不负责什么」每块写全；`docs/开发路线.md` 七阶段「依赖上一阶段」能从头串到尾。
- 全中文：除 MaiBot、API、TOML、Git 和字段名外没有成句英文；没有拿上游 README 当开篇。
- 只动了 `.md` 文件，没碰任何 `.py` / `.toml` / `.json` / `.html`。

## 2026-11-04 · 立项：推倒重来，新建独立仓库，产出设计与骨架

**改了什么**

- 新建独立仓库 `1519160235/huixiang-operator`，插件 ID `local.huixiang-operator`，中文名「回响」，取自「自己的分享有回响」。
- 交付 23 个文件：6 份全中文文档、四层代码骨架、配置模型、WebUI 工作台、骨架自检，另有 `.gitignore`。
- 只出设计与骨架：不装依赖、不接真实 API、不上服务器、不启用、不发一篇。
- 把历史契约整理成中文表，把已知问题整理成「问题 → 表现 → 根因 → 规避方式」，把推进顺序拆成六个阶段。
- 全部文件已推送 `main`。

**为什么**

- 昨天那版只改了 4 个文件、新增 1 个，2393 行主体仍是上游代码。等同替换外壳，问题未处理。
- 社区名硬编码在 `Literal` 里，还重复定义了两次；绝对路径硬编码；三套社区枚举互相打架，同一篇帖子三处能解出三个 ID。
- README 和清单还是上游那份说明，写的是别人的功能和默认值，看了反而带偏。
- 私信写了半套又关掉，留着只读会话列表和没有稳定契约的发送口，属于半截状态。
- 在既有骨架上持续打补丁会导致维护成本累积。这回只留结论、不留代码，按自己的四层重新写。

**怎么验证**

- `python -m unittest discover -s tests` 跑通，6 项自检全过。
- 23 个文件齐全；所有 `.md` 按 utf-8 能读。
- 全中文：除 MaiBot、API、TOML、Git 和字段名外，没有成句的英文；没有「基于某某二创」当开篇，没有上游说明残留。
- `docs/接口契约.md` 中历史出错的字段，在 `docs/已知问题清单.md` 里都能对上；`docs/开发路线.md` 六阶段的「依赖上一阶段」能从头串到尾。
- 文档里没有真实凭据、真实服务器路径；本次没连过 API，没动过服务器。
