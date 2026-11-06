# 变更记录

> 规则：每次改动只写三段——改了什么 / 为什么 / 怎么验证。写完再排下一条。
> 只记「回响」自己的事。不抄上游说明，不贴服务器上的旧账。

## 2026-11-06 · 删掉「分区目录」面板 + 补两个自查按钮

**用户反馈**：「最下面那个实时更新的分区，我觉得没什么用。我当时的想法是我选哪个分区时
下拉它就刷新了，所以那个东西该去掉。」

**排查**：那块面板只是把缓存里的 94 个分区列出来（前 20 个），纯展示、不可点。
用户真正要的「选分区时下拉里就是最新的分区」其实已经满足——
`_cached_partition_names()` 在每次生成配置 Schema 时都按当时缓存重算 `choices`，
而缓存由 worker 每轮浏览自动刷新。面板是重复信息。

**改法**：
- 删掉「分区目录」子卡片、`renderPartitions()`、`lastPartsSignature`、
  以及刷新队列里的 `partitions` 查询（页面不再白跑一次请求）；
- 顶部折叠标题的「分区」字样一并去掉；
- 后端 `hx_partitions` 接口**保留**（排障时还能手动查缓存），只是页面不调了。
- 顺手补两个自查按钮：「预览一版总结」（演练，不占额度不发帖，能看出重写几次、
  AI 味多少）、「试写一段看用哪个模型」（真调一次 LLM，摊开配置模型 vs 实际模型）。

**怎么验证**：`grep` 确认页面无 `分区目录 / renderPartitions / lastPartsSignature /
$('partitions')` 残留；页面查询集合与 `webui.json` 的 `queries` 完全一致
（5 个），动作集合同样一致（7 个）；括号配平；184 项单测 + 两道预检全过；
线上重启后 `LOADS: mode=real / ERRS: NONE`，页面确认已无分区面板、两个按钮都在。

## 2026-11-06 · 模型选错（用了 DeepSeek）+ 感想总结「建议重写却没动静」+ 模式又变演练

**用户反馈（三条一起报）**：
1. 「发帖之前配置的是用 GM，不要给我用 Deepseek」；
2. 「写了个感想总结太像机器，建议重写，然后没动静了」；
3. 「后边的感想总结又是演练」。

**排查（日志逐条对上，不是猜的）**：
- 服务器 `model_config.toml` 里 `[model_task_config.replyer]` 的
  `model_list = ["ds-official", "feiyu-gemini-flash"]`，**DeepSeek 排第一**。
  插件只传 `task_name="replyer"`，宿主按顺序取第一个 → 内容永远是 DeepSeek。
  配置里根本没有「用哪个模型」这一项，用户没法改。
- `guard.check_ai_flavor` 判 `ai_flavor_block` 时只返回一句「建议重写」，
  **上一层直接 `return {"ok": False}`，全代码库没有任何一处真的执行重写**。
  操作记录里就是一条 `ok=False 文字太像机器（AI 味 62 ≥ 60），建议重写`，
  下一条直接跳到成功 —— 用户看到的就是「提示完没下文」。
- worker 的 `mode` 在 `__init__` 时固化，配置改「自动」后靠
  `asyncio.create_task` 异步重建；没有事件循环时抛 `RuntimeError`，
  异常被吞掉 → 旧 worker 继续用 `DRY_RUN` 跑，日志里就是「先自动、后演练」。
- 另有一个隐藏杀手：宿主 `cap.call` 默认超时只有 **30000ms**，写 200~400 字
  经常跑不完，日志里是 `[E_TIMEOUT] 请求 cap.call 超时 (30000ms)`。
  上层只看到「生成失败」，同样表现为「没动静」。

**改法**：
1. **新增 `[model]` 配置段**（`text_model` / `temperature` / `max_tokens`）：
   默认 `feiyu-gemini-flash`（用户说的「GM」），下拉可选 GLM / DeepSeek / 跟随宿主默认。
   `Runtime.generate_text` 改用 `model_name=` **直选具体模型**（SDK 契约：传
   `model_name` 一律直选，绕过任务里的优先级顺序）。
2. **补上真正的重写闭环**：`summarize.run_summary_once` 改成循环，
   AI 味超标就把「上一版正文 + 去 AI 味的具体要求」塞回提示词重新生成，
   最多 `REWRITE_ROUNDS=2` 轮。字数/段数不合规也走同一条重写路径。
   额度/间隔/去重/时段这类闸门重写没用，**立刻失败不浪费模型调用**。
3. **worker 每轮重读模式**：`run_round` 开头调 `_current_mode()` 按当前配置
   重新判定，不再依赖异步重建成功。
4. **`_start_worker` 无事件循环时安全降级**：先 `get_running_loop()` 判断，
   没有就同步替换 worker；`start()` 失败也不再留下半死状态。
5. **给宿主生成留足时间**：`llm.generate(..., timeout_ms=180000)`。
6. **诊断入口**：新增 `hx_test_model` API + 页面「试写一段看用哪个模型」按钮，
   把「配置指定 / 实际回报 / 样本」摊开，用户可随时自证。
7. **状态页补额度上限**：原来只报「真实已用 4 次」，看不出「3 条早用完了」；
   现在显示 `4 / 3` 并在用完时变黄提示。

**怎么验证**：
- 184 项单测 + 两道预检全过（新增 `tests/test_model_and_rewrite.py` 9 项：
  模型名透传、跟随默认不传、配置坏了兜底、AI 味触发真重写、重写有上限、
  不可重写的闸门快速失败、worker 每轮重读模式、取模式异常兜底）。
- 线上日志实测：`选择请求模型: feiyu-gemini-flash (策略: sequential)`，
  `类型: plugin.local.huixiang-operator`，`完成: 1041 / 282 token` ——
  确认走 Gemini、且不再 30 秒超时。
- 线上操作记录全部是 `[real]`，`mode=real`，不再出现「自动里混演练」。

## 2026-11-06 · 「指定分区」多选下拉改成搜索式复选框列表

**用户反馈**：新页面里「指定分区」那种下拉框全都失效了。

**排查**：字段数据本身没问题——服务器上实测 `selected_partitions` 是
`type=select / multiple=True / choices=94`。失效的是**交互**：我用了
`<select multiple size="5">`，在沙箱 iframe 里要按住 Ctrl 才能多选，
94 个分区还挤在一个 5 行小框里，看起来就是点不动。

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
4. 「后台设置不给提示怎么知道怎么配」+「插件扩展那个页面别搞那么多信息」→
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
   **用户说得对**。之前后台只能看状态，配置要跳到宿主「插件管理」页。
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

**用户反馈**：进后台看到 `Error: Theme font unavailable: JetBrainsMono-Medium.ttf`。

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

**用户反馈**：进后台后一直转「读取中」，运行状态没数据，手动操作也报
`Two requests are already running`。

**根因（查宿主前端源码确认）**

`plugin-webui-C1enjH3M.js` 的 bridge 里写死：
```js
if (pending.size >= 2) throw new Error('Two requests are already running');
```
同一插件页面**最多 2 个未完成请求**。而我的页面用 `Promise.all` 一次并发发了 4 个查询
（总览 / 操作记录 / 分区 / 登录状态），再叠上 10 秒自动刷新和「未登录自动弹二维码」，
必然撞上限。第一个请求失败 → 整页卡在「读取中」。

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
  分区缓存由 worker 每轮 / 后台「立刻浏览一轮」自动刷新，不是写死白名单。

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

**为什么（上一次 webui.json 把整个 WebUI 搞崩的复盘）**

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

见 `docs/避坑清单.md` 第 33~35 条：`task_name` 不是自由标签、`未知` 不能兜底成 `0`、
模型写不够字数光加重试没用。

## 2026-11-06 · 真机跑通完整闭环（浏览 → 互动 → 总结 → 演练发帖）

**改了什么**

- `runtime.py`：`llm.generate` 的 `task_name` 从自造的 `"huixiang"` 改成宿主的合法任务名
  `replyer`，并加 `replyer → utils → fast_model → planner` 四级回退。
- `runtime.py`：宿主「不抛异常只回空结果」时显式报中文错，不再让上层误判成「模型话少」。
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
  表面看像模型话少，实际是**一次模型都没调成**。这种「错误信息指向错方向」最难查。
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

- `plugin.py`：路径自举（把插件根目录塞进 `sys.path`），修 `No module named 'contracts'`。
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
- 命令「执行成功却没回复」是最容易误判的一类：日志显示成功，用户那头什么都看不到。
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
  `.config / .paths / .store / .api / .xhh_client / .generate_text`，业务层彻底不知道宿主长什么样。
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

- 用户问「直接接进去跑一遍就知道了」，这个判断是对的：一接就暴露了 6 处「按想象写」的硬伤，
  其中 3 处是**装上就加载失败**级别（构造签名、只读 property、同步生命周期方法）。
- 之前 146 项测试全过，是因为它们测的全是业务逻辑，没有一条测「宿主能不能加载」。
  这是测试盲区：**业务逻辑对 ≠ 插件能装上**。

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
- 单轮异常不打死循环：`_loop` 捕获异常记日志后继续；`_step` 捕获子步骤异常，后续步骤照跑。
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
- 楼中楼「error root」是历史坑（避坑清单第 7 条）：回楼中楼必带通知里的 rootCommentId 作 rootId，顶层评论才用 -1。
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
- 扩充 `docs/避坑清单.md`：在旧 16 条上补 6 条四源新坑（17~22）。
- 重写 `docs/开发路线.md`：六阶段改七阶段，补「感想总结发帖」阶段。
- 重写 `docs/配置说明.md`：6 段改 11 段（basic/login/browse/interact/publish/summarize/image/create/sense/guard/links）。
- 重写 `README.md`：定位、六块简图、目录树、自检、参考来源同步更新。

**为什么**

- 四源深读结论出来了：旧文档的 `topicIds` 类型、图床尺寸、签名分流都写错，不改成对的，发布层照写必踩坑。
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
- 把历史契约整理成中文表，把踩过的坑整理成「坑 → 表现 → 根因 → 新版本怎么避免」，把推进顺序拆成六个阶段。
- 全部文件已推送 `main`。

**为什么**

- 昨天那版只改了 4 个文件、新增 1 个，2393 行主体仍是上游代码。等于换壳，毛病一个没动。
- 社区名写死在 `Literal` 里，还重复定义了两次；绝对路径硬编码；三套社区枚举互相打架，同一篇帖子三处能解出三个 ID。
- README 和清单还是上游那份说明，写的是别人的功能和默认值，看了反而带偏。
- 私信写了半套又关掉，留着只读会话列表和没有稳定契约的发送口，属于半截状态。
- 在别人骨架上打补丁，补丁会越堆越厚。这回只留结论、不留代码，按自己的四层重新写。

**怎么验证**

- `python -m unittest discover -s tests` 跑通，6 项自检全过。
- 23 个文件齐全；所有 `.md` 按 utf-8 能读。
- 全中文：除 MaiBot、API、TOML、Git 和字段名外，没有成句的英文；没有「基于某某二创」当开篇，没有上游说明残留。
- `docs/接口契约.md` 里踩过坑的字段，在 `docs/避坑清单.md` 里都能对上；`docs/开发路线.md` 六阶段的「依赖上一阶段」能从头串到尾。
- 文档里没有真实凭据、真实服务器路径；本次没连过 API，没动过服务器。
