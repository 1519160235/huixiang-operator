# -*- coding: utf-8 -*-
"""
回响插件：创作层（六块第三块，落地版）。

【职责边界】
据感知层给的状态与话题产出「草稿」，但不决定能不能发（那是护栏层的事）：
- 生成帖子 / 评论 / 感想总结（A 类宿主能力，走 ctx 注入的生成器）；
- 中文口语清洗（去楼层词、破折号转逗号、去句号、段间不空行）；
- AI 味打分（0–100，直译 MCP aiflavor.ts）；
- 配图生成（1~N 张），以及生图失败时降级为纯文字帖。

【复用约定】
生成与生图通过 `ctx` 注入的生成器复用（优先 `ctx.generate_text` / `ctx.generate_image`，
否则 `ctx.api.call(...)`）；口语清洗与 AI 味打分是纯本地文本处理，不联网。

【AI 味打分口径（直译 MCP aiflavor.ts scoreText，阈值对齐 contracts.py）】
评论 ≥45 判机器、正文 ≥60 判机器。返回 0–100 的 float。
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Union

from models import (
    CommentTarget,
    ImageAsset,
    ImageStatus,
    LifeTrace,
    LinkTag,
    PostDraft,
    SummaryDraft,
    Topic,
)
from layers._cfg import read as _read, read_bool as _read_bool, read_str as _read_str, section as _section
from layers.emojis import EMOJI_HINT, sanitize_marks as _sanitize_marks
from layers.voices import (
    format_brief as _format_brief,
    get_format as _get_format,
    recent_titles_block as _recent_titles_block,
    similar_title as _similar_title,
)

# ===================== AI 味词表（2026-11-07 融合升级） =====================
# 【为什么要重写】2026-11-07 用 10 篇真实人类文章实测，发现老打分器两头都失灵：
#   ① 长文误杀：10 篇里 4 篇被判 ≥45（评论阈值），而误判来源是「而已/究竟/固然/因此」
#      这些人类正式文章里再正常不过的词，单个词就加 10-12 分，长文累积必然爆表。
#   ② 短评论漏判：AI 腔评论「值得一提的是…」「总而言之…」几乎全打 0 分，因为
#      老词表根本没有这些 AI 高频开场词。
#
# 【新方案融合两个开源项目】
#   - De-AI-Prompt-Enhancer（OUBIGFA）：词表分「AI 痕迹」与「人类正常语域」两类，
#     并给出密度阈值思路（成簇才罚，单次出现不算）——解决误杀。
#   - shuorenhua（MrGeDiao）：客观指标（句长变异系数、显性连词密度、动词名词化、
#     借喻场混用）——这些是结构层的机器特征，短文本也能算，解决漏判。
#
# 设计原则（两边共识）：
#   1. 单个词出现一次不算 AI 证据，成簇/连段才对；
#   2. 用密度（每千字/每句）而不是绝对命中数，天然适配长短文本；
#   3. 人类写作的各项指标在「中间区间」，不是 AI 的反面——不追求零命中。

# ---- A 类：AI 高置信痕迹词（命中即说明在「替读者宣布结论」或「提纲扩写」）----
# 这些词几乎只在模型输出里成簇出现，人类正式写作很少用。
AI_MARKER_WORDS = (
    # 讲义式开场/路标（De-AI V10 + V5）
    "值得注意的是", "值得一提的是", "需要注意的是", "需要指出的是", "不可否认",
    "毋庸置疑", "众所周知", "不言而喻",
    "总而言之", "综上所述", "总的来说", "总体而言", "简而言之", "换言之",
    "换句话说", "与此同时", "在此背景下",
    # 伪深度/伪客观开头（老版本漏判大户）
    "整体而言", "从某种意义上", "在一定程度上", "客观来说", "客观地讲",
    # 课程式序数（「首先…其次…最后」是提纲扩写铁证）
    "首先，", "其次，", "最后，", "第一，", "第二，", "其一", "其二",
    # 协作口吻（De-AI V12）—— 人不会这样写
    "让我们", "接下来我们", "下面我们", "本文将", "综上所述",
    # 客服腔/谄媚（De-AI V14）
    "感谢分享", "谢谢分享", "很高兴", "希望对你有帮助", "希望这些对",
    "一起加油", "共勉", "祝你好运", "加油哦", "期待你的", "欢迎交流",
    "如果有任何", "随时告诉我", "随时问我",
    # 伪深刻/宣传腔（De-AI V1 + V3）
    "象征着", "标志着", "体现了", "彰显", "里程碑", "不可磨灭",
    "无缝", "极致", "革命性", "颠覆性", "引领", "卓越",
    "赋能", "助力", "践行", "洞察", "深耕", "打造",
    # 伪深度术语壳（De-AI V7）
    "底层逻辑", "顶层设计", "认知升级", "降维打击", "抓手", "闭环",
    "范式", "路径依赖", "舆论场", "宏大叙事", "话语权",
    # 伪口语拆解腔（De-AI V6）—— "分析师带你看"的口吻
    "拆一拆", "盘一盘", "捋一捋", "聊一聊", "划重点", "敲黑板",
    "拆解", "梳理", "剖析", "解构",
    # 空泛评价（原 EMPTY_WORDS，保留）
    "值得思考", "很有意义", "非常有价值", "发人深省", "引人深思",
    "见仁见智", "各有各的", "因人而异", "视情况而定", "各方面", "多角度",
    # 自我暴露（模型在说自己的参数）
    "上下文窗口", "上下文长度", "系统提示", "人设卡", "我的参数", "我的配置",
    "模型参数", "推理速度", "显存", "token数", "token 数",
)

# ---- B 类：结构性套壳句式（正则，AI 的「模板指纹」）----
# De-AI 的核心洞察：这些句式本身不算错，但成簇出现就是提纲扩写的铁证。
AI_SHELL_PATTERNS: tuple[tuple[str, str], ...] = (
    # 二分对照壳「不是 A 而是 B」—— 单个正常，连用就是模板
    # 二分对照壳「不是 A 而是 B」—— 单个正常，连用就是模板。
    # 中间允许 30 字：AI 常写成「不仅仅是一次优化，而是…」，中间隔着一截。
    (r"不是[^。！？\n]{1,30}而是", "二分对照"),
    (r"不仅(?:仅)?[^。！？\n]{1,30}(?:更是|而是)", "二分递进"),
    (r"不在于[^。！？\n]{1,30}而在于", "二分对照"),
    # 伪深度尾缀「…，象征着/反映了/体现了…」
    (r"[，,]\s*(?:象征着|标志着|体现了|彰显了|反映出|折射出)", "伪深度尾缀"),
    # 万能关联对（成簇即模板）
    (r"随着[^。！？\n]{1,15}的(?:发展|推进|深入|普及)", "万能关联"),
    (r"通过[^。！？\n]{1,15}来(?:实现|提升|达成|完成)", "万能关联"),
    # 戏剧化揭露
    (r"(?:揭开|戳穿|撕开)[^。！？\n]{0,6}(?:真相|面纱|遮羞布)", "戏剧化揭露"),
)

# ---- C 类：人类正常语域（**不再计分**，仅作「放回来」提示）----
# 老版本把「而已/究竟/固然/因此/机制/规范」当 AI 证据，是人类语料误杀的主因。
# 这里保留常量用于「人类区间」判断，但 ai_flavor_score 不再因其加分。
HUMAN_NORMAL_WORDS = (
    # De-AI V17「应该放回来的中文人类语域」——这些是人类日常用得很多的表达
    "其实", "说白了", "说到底", "总的来说", "总之", "因此", "从而",
    "而已", "罢了", "究竟", "毕竟", "固然", "或许", "恐怕", "无疑",
    "就算", "颇", "所谓", "某种程度上", "之类", "必然", "终究", "维持",
    "反正", "算了", "行吧", "行啊", "倒也",
    # 正常技术/书面词——人类正式文章常用，不能当 AI 证据
    "机制", "规范", "维度", "层面", "范畴", "配置", "参数", "阈值",
    "总之", "综上",
)

# ---- D 类：显性连词（shuorenhua conjunction_density）----
# 密度过高说明在搬运 if/then/otherwise 的骨架，而不是自然行文。
CONJUNCTIONS_ZH = (
    "因为", "所以", "但是", "然而", "同时", "此外", "而且", "并且",
    "因此", "不仅", "于是", "另外", "总之", "首先", "其次",
    "如果", "否则",
)

# ---- E 类：动词名词化（shuorenhua NOMINALIZATION_PATTERNS）----
# 「进行了/实现了/完成了对…的」把短句撑长、信息没变，是典型的机器行文。
NOMINALIZATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"进行(?:了|一次|一场|着)?[^。，！？\n]{0,10}(?:调整|优化|升级|分析|讨论|沟通|梳理|复盘|迭代|探索|尝试|思考|规划|布局|评估|排查|验证)"),
    re.compile(r"实现了?[^。，！？\n]{0,14}的?[^。，！？\n]{0,6}(?:提升|增长|突破|转变|跃升|落地|改善)"),
    re.compile(r"完成了?对[^。，！？\n]{0,12}的"),
    re.compile(r"起到了?[^。，！？\n]{0,12}的?作用"),
    re.compile(r"具有[^。，！？\n]{0,10}(?:意义|价值)"),
    re.compile(r"开展[^。！？\n]{0,10}(?:工作|建设|研究|合作)"),
)

# ---- F 类：借喻场（shuorenhua METAPHOR_FIELDS）----
# 同一段里从多套不相干的比喻系统借词，是用比喻替代说明的信号。单套没问题。
METAPHOR_FIELDS: dict[str, tuple[str, ...]] = {
    "道路竞赛": ("赛道", "跑道", "岔路", "十字路口", "终点线", "起跑线", "弯道超车"),
    "战争攻防": ("护城河", "壁垒", "厮杀", "血战", "阵地", "弹药", "突围"),
    "建筑灾害": ("坍塌", "崩塌", "地基", "支柱", "废墟", "基石"),
    "温度": ("降温", "升温", "冷却", "余温", "白热化", "点燃"),
    "海洋航行": ("蓝海", "红海", "浪潮", "潮水", "灯塔", "彼岸", "风口"),
    "机器器官": ("齿轮", "引擎", "发动机", "血管", "骨架", "神经末梢"),
}

# 借喻词的字面用法排除（搜索引擎、代码仓库、游戏引擎…不算借喻）。
METAPHOR_LITERAL_PREFIX: dict[str, tuple[str, ...]] = {
    "引擎": ("搜索", "渲染", "游戏", "物理", "推荐", "规则", "模板", "查询", "存储"),
    "发动机": ("汽车", "飞机", "柴油", "航空"),
    "仓库": ("代码", "git", "Git", "远程", "本地", "私有", "镜像"),
    "阵地": ("前沿",),
    "风口": ("出", "通", "进"),
}

# 楼层词（真楼层前缀，必须清）。
FLOOR_WORDS = ("几楼", "楼说", "楼主", "楼上", "楼下", "沙发", "前排", "板凳", "顶帖", "楼层")

# ===================== 中文口语清洗 =====================

# 行首楼层词（1楼 / 二楼 / #3 / 楼主 / 沙发 …）。
_FLOOR_LINE_RE = re.compile(
    r"(?m)^\s*(?:第?[0-9一二三四五六七八九十百]+\s*(?:楼|层|層)|#[0-9]+|"
    r"楼主|楼上|楼下|沙发|前排|板凳|顶帖|楼层)\s*[:：、．.]?\s*"
)
# 各种破折号（含连续多个）统一转一个中文逗号。
_DASH_RE = re.compile(r"[—–―]+")
# 句末句号。
_PERIOD_RE = re.compile(r"。")
# 段间连续空行压成单换行（段间不留空行）。
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def polish_chinese(
    text: str,
    keep_paragraphs: bool = False,
    blank_between_paragraphs: bool = True,
    keep_periods: bool = False,
) -> str:
    """中文口语清洗（纯本地，不联网）。

    规则：去楼层词 / 破折号转逗号 / 句号按需 / 段间空行按需。

    :param keep_paragraphs: 是否保留分段结构。
        默认 False（评论用：评论就是一句话，不需要分段）。
        总结/长正文必须传 True —— 原来一律压平会把 3 段正文压成 1 段
        （真机 2026-11-07 实测：段数从 3 变 1，触发「段数不足」反复重写）。
    :param blank_between_paragraphs: 段与段之间是否留空行。
        默认 True（段间空一行）。发帖/总结要「段间不空行」时传 False——
        设计文档（DESIGN.md 硬规矩「短句、段间不空行」）与配置项
        `[create] no_blank_line_between_paragraphs` 都是这个口径。
    :param keep_periods: 是否保留句号。
        默认 False（评论用：短评论去了句号更像随手打字）。
        **长文（帖子 / 总结）必须传 True** —— 300 字正文把所有「。」删掉后，
        每段会变成一串逗号连到底的巨型长句，正是用户说的「分段看着有问题」。
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _FLOOR_LINE_RE.sub("", value)
    value = _DASH_RE.sub("，", value)
    if not keep_periods:
        value = _PERIOD_RE.sub("", value)
    if keep_paragraphs:
        if blank_between_paragraphs:
            # 只折叠「3 个以上换行」，保留正常的段间空行。
            value = re.sub(r"\n{3,}", "\n\n", value)
        else:
            # 段间不留空行：所有连续换行折叠成单个换行（段落仍各自成行）。
            value = re.sub(r"\n+", "\n", value)
    else:
        value = _BLANK_LINE_RE.sub("\n", value)
    return value.strip()


# 开头「可打印 ASCII + 空白」前缀（>= 15 字符），用于识别英文思考段。
_REASONING_ASCII_RE = re.compile(r"^[\x20-\x7e\s]{15,}$")


def strip_reasoning_prefix(text: str) -> str:
    """剥离开头的英文思考段（推理模型内联思考，无 <think> 标签）。

    【真机踩坑（2026-11-06）】切到 feiyu-gemini-flash 后，评论前会带一大段
    英文思考（如 "The post title ... suggests ..."），宿主 reason_parse_mode
    只剥 `<think>` 标签，剥不掉这种纯文本内联思考。

    策略：找到第一个中文字符，若它前面是「>= 15 个纯 ASCII 字符」的英文段，
    就认定为思考、只保留中文起；短英文（如 PUBG、iOS 这类正常词）不误伤。
    """
    t = str(text or "").strip()
    m = re.search(r"[\u4e00-\u9fff]", t)
    if not m:
        return t
    idx = m.start()
    prefix = t[:idx].strip()
    if len(prefix) >= 15 and _REASONING_ASCII_RE.match(prefix):
        return t[idx:].strip()
    return t


# ===================== 评论强清洗（2026-11-07 用户报障） =====================
# 用户 2026-11-07 报障：昨晚评论有的重复四句话、有的简繁体混用、有的带一堆英文。
# 根因：generate_comment 只做了 strip_reasoning_prefix + polish_chinese，没有去重、
# 没有繁简统一、英文只剥「开头思考段」剥不掉夹在中间/后段的英文。这里补一道强清洗。

# 高频繁体 → 简体映射。服务器无 opencc，内置兜底（覆盖评论/游戏高频字，单字对单字）。
_T2S = {
    "說": "说", "話": "话", "這": "这", "們": "们", "來": "来", "個": "个",
    "麼": "么", "嗎": "吗", "沒": "没", "還": "还", "時": "时", "間": "间",
    "讓": "让", "給": "给", "帶": "带", "對": "对", "發": "发", "現": "现",
    "實": "实", "覺": "觉", "開": "开", "關": "关", "點": "点", "買": "买",
    "賣": "卖", "錢": "钱", "為": "为", "應": "应", "該": "该", "從": "从",
    "會": "会", "裡": "里", "後": "后", "邊": "边", "認": "认", "識": "识",
    "願": "愿", "歡": "欢", "聽": "听", "見": "见", "體": "体", "驗": "验",
    "過": "过", "頭": "头", "腦": "脑", "樣": "样", "種": "种", "類": "类",
    "機": "机", "動": "动", "畫": "画", "戰": "战", "鬥": "斗", "團": "团",
    "隊": "队", "賽": "赛", "勝": "胜", "敗": "败", "輸": "输", "贏": "赢",
    "難": "难", "簡": "简", "單": "单", "錯": "错", "問": "问", "題": "题",
    "記": "记", "憶": "忆", "讚": "赞", "評": "评", "論": "论", "復": "复",
    "貼": "贴", "圖": "图", "視": "视", "頻": "频", "語": "语", "氣": "气",
    "壓": "压", "結": "结", "註": "注", "絲": "丝", "禮": "礼", "領": "领",
    "務": "务", "強": "强", "組": "组", "衝": "冲", "號": "号", "掛": "挂",
    "舉": "举", "報": "报", "遲": "迟", "頓": "顿", "線": "线", "網": "网",
    "絡": "络", "潰": "溃", "閃": "闪", "啟": "启", "錄": "录", "賬": "账",
    "碼": "码", "證": "证", "綁": "绑", "電": "电", "顯": "显", "質": "质",
    "載": "载", "進": "进", "檔": "档", "讀": "读", "選": "选", "項": "项",
    "設": "设", "樂": "乐", "請": "请", "續": "续", "暫": "暂", "獎": "奖",
    "勵": "励", "兌": "兑", "換": "换", "購": "购", "訂": "订", "價": "价",
    "優": "优", "費": "费", "員": "员", "幣": "币", "資": "资", "源": "源",
    "築": "筑", "養": "养", "賦": "赋", "屬": "属", "數": "数", "擊": "击",
    "傷": "伤", "療": "疗", "護": "护", "暈": "晕", "減": "减", "諷": "讽",
    "淨": "净", "輔": "辅", "師": "师", "區": "区", "濟": "济", "補": "补",
    "殺": "杀", "盤": "盘", "飛": "飞", "臺": "台", "遊": "游", "戲": "戏",
    "幹": "干", "隻": "只", "裏": "里", "髮": "发", "麵": "面", "穀": "谷",
}
_T2S_TABLE = str.maketrans(_T2S)

# 保留的英文术语/缩写（游戏/平台常见，清洗时不误删）。
_KEEP_EN = frozenset({
    "PUBG", "CS", "CS2", "CSGO", "DNF", "LOL", "DOTA", "WOW", "CF", "GTA",
    "COD", "APEX", "Apex", "NBA", "FIFA", "RPG", "MMO", "FPS", "MOBA", "BGM",
    "MVP", "KDA", "GG", "PC", "PS5", "PS4", "NS", "Switch", "Steam", "iOS",
    "Android", "APP", "App", "ID", "PVP", "PVE", "SSR", "UR", "BOSS", "Boss",
    "buff", "CD", "DPS", "HP", "MP", "EXP", "AI", "OK", "OP", "op", "DIY",
    "Xbox", "WiFi", "HUD", "DLC", "Valorant", "OW", "FF14", "2K", "EA",
})

# 整段英文句子（含空格/标点的连续 ASCII），推理模型内联思考的典型形态。
_EN_SENTENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9 ,.;:'\"!?()\-]{6,}")
# 夹在中文里的孤立英文单词（前后非字母），白名单外的删掉。
_EN_WORD_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,})(?![A-Za-z])")


def _to_simplified(text: str) -> str:
    """繁体 → 简体（内置映射表，单字+常用词两级）。"""
    return str(text).translate(_T2S_TABLE)


# 小黑盒表情/贴纸标记，形如 `[cube_喜欢]` `[cube_666]` `[cube_捂脸哭]`。
# 【真机缺陷 2026-11-07 用户报障「评论带表情包的，怎么之前有现在没了」】
# 我上一版加英文清洗时，`cube_喜欢` 里的 `cube_` 被当成英文单词删掉了，
# 只剩 `[_喜欢]` —— 表情全废。这类标记必须整段保护起来，清洗完再还原。
# 字符集要**包含中文**（表情名常是中文，如 喜欢 / 捂脸哭），只写 [A-Za-z0-9_] 匹配不到。
# 也要包含短横线：官方活动表情有 `[cube_上学-乐]` 这种名字，漏了会被英文清洗拆成 `[cube_上学-乐]`。
_EMOJI_MARK_RE = re.compile(r"\[[A-Za-z0-9_\-\u4e00-\u9fff]{1,20}\]")


def _strip_english_noise(text: str) -> str:
    """剥掉评论里的英文残留：整段英文句子 + 白名单外的英文单词。

    注意：`[cube_xxx]` 这类表情标记先占位保护，否则里面的 `cube_` 会被当英文删掉。
    """
    t = str(text or "")
    # 1) 表情标记占位（用不含 ASCII 字母的中文占位符，避免被下面的英文正则碰到）。
    marks: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        marks.append(m.group(0))
        return f"\u0000{len(marks) - 1}\u0000"

    t = _EMOJI_MARK_RE.sub(_hold, t)

    # 2) 剥英文。
    t = _EN_SENTENCE_RE.sub(" ", t)

    def _repl(m: re.Match[str]) -> str:
        w = m.group(0)
        return w if w in _KEEP_EN else ""

    t = _EN_WORD_RE.sub(_repl, t)
    t = re.sub(r"[ \t]+", "", t)

    # 3) 还原表情标记。
    for i, mark in enumerate(marks):
        t = t.replace(f"\u0000{i}\u0000", mark)
    return t


def _collapse_halves(text: str) -> str:
    """整段「短语重复 N 遍」折叠：两半相同留一半、三份相同留一份，递归。"""
    t = text
    n = len(t)
    if n < 4:
        return t
    half = n // 2
    if t[:half] == t[half:] and half >= 2:
        return _collapse_halves(t[:half])
    third = n // 3
    if third >= 2:
        a, b, c = t[:third], t[third : 2 * third], t[2 * third :]
        if a == b == c:
            return _collapse_halves(a)
    return t


def _dedupe_repeated(text: str) -> str:
    """去重复：折叠重复标点/连击字 + 去相邻重复句 + 折叠整段重复短语 + 近重复句。"""
    t = str(text or "")
    # 重复标点折叠（！！！→！）。
    t = re.sub(r"([！!？?。，,；;~～])\1{1,}", r"\1", t)
    # 连续重复中文字符折叠（哈哈哈…→哈哈，保留两个更自然）。
    t = re.sub(r"([\u4e00-\u9fff])\1{2,}", r"\1\1", t)
    # 按句切分，去相邻重复句。
    parts = re.split(r"([。！？!?；;~～\n])", t)
    out: list[str] = []
    prev = ""
    for seg in parts:
        if seg == "":
            continue
        norm = seg.strip()
        if norm and norm == prev:
            continue
        out.append(seg)
        if norm:
            prev = norm
    t = _collapse_halves("".join(out))
    return _dedupe_near_sentences(t)


def _similarity(a: str, b: str) -> float:
    """两段文字的字符级相似度（0~1），用较长的作分母。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _dedupe_near_sentences(text: str, threshold: float = 0.72) -> str:
    """去掉「意思重复但措辞略有不同」的句子。

    【真机缺陷 2026-11-07】模型常把同一句话换个说法再写一遍，例如
    「运气真好，我工坊开箱就没见过金光」+「运气真挺好的，我工坊开箱就没怎么见过金光」。
    精确去重（字符串相等）抓不到，必须按字符重叠率判近重复。

    做法：按句切开，逐句与「已保留句中最近 3 句」比相似度，超过阈值就丢弃。
    只比最近 3 句而不是全篇——同一篇文章里前后呼应地重复同一个词是正常的，
    紧挨着复述一遍才是机器的毛病。
    """
    t = str(text or "").strip()
    if not t:
        return t
    # 中文句末标点（保留换行，评论里换行也等于断句）。
    sentences = re.split(r"(?<=[。！？!?；;\n])", t)
    kept: list[str] = []
    for seg in sentences:
        s = seg.strip()
        if not s:
            continue
        # 太短的片段（标点、单字）不参与相似度判断，直接留。
        if len(re.sub(r"[^\u4e00-\u9fff]", "", s)) < 6:
            kept.append(seg)
            continue
        dup = False
        for prev in kept[-3:]:
            p = re.sub(r"[^\u4e00-\u9fff]", "", prev)
            c = re.sub(r"[^\u4e00-\u9fff]", "", s)
            if p and c and _similarity(p, c) >= threshold:
                dup = True
                break
        if not dup:
            kept.append(seg)
    return "".join(kept).strip()


def sanitize_comment(text: str) -> str:
    """评论专用强清洗：转简体 + 剥英文残留 + 剥语气标签 + 去重复。

    【真机缺陷 2026-11-07】昨晚评论出现「重复四句话 / 简繁混 / 一堆英文」，
    都是 generate_comment 只有 polish_chinese、缺这道强清洗导致的。
    在 polish_chinese 之后调用，纯本地、不联网、不引入新依赖。

    【真机缺陷 2026-11-07 补充】模型还会把提示词里的语气要求抄进输出，
    例如「平和,\n紫色这位站教室里…」——「平和」是提示词里的话，不是评论内容。
    这里按「句首语气标签 + 标点」的形态剥掉。
    """
    t = _to_simplified(str(text or ""))
    t = _strip_english_noise(t)
    t = _strip_tone_label(t)
    t = _strip_model_noise(t)
    t = _pick_final_line(t)
    t = _dedupe_repeated(t)
    # 最后兜底：表情名必须是小黑盒真实存在的，否则客户端画不出来（只剩文字或空白）。
    # 【真机缺陷 2026-11-08 用户报障「刷小黑盒回复有时候表情加载不出来」】
    t = _sanitize_marks(t)
    return t.strip()


def _pick_final_line(text: str) -> str:
    """多行输出里挑「真正的那句评论」。

    【真机缺陷 2026-11-07】推理模型输出常是「草稿 N 行 + 定稿 1 行」，实测：
        `紫衣角色…营造了幽默感…\n紫衣服那个站得挺老实的，看来灭建国家教确实严`
        `一发入魂\n运气真不错，我工坊开出来的…\n运气真好，吸吸欧气…`
    模型的定稿习惯放在最后一行，前面的都是自我推演。评论这种一句话的东西，
    直接取「最后一个像样子的句子」比整段拼接靠谱得多。

    判定「像样子」：汉字数 >= 6 且不含结构残渣。全都不合格就原样返回，交给上层。
    """
    t = str(text or "").strip()
    if not t:
        return t
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return t
    # 从后往前找第一条像个句子的行。
    for line in reversed(lines):
        han = len(re.findall(r"[\u4e00-\u9fff]", line))
        if han >= 6:
            return line
    # 没有合格行（都是碎片），退回最长的那行。
    return max(lines, key=lambda s: len(re.findall(r"[\u4e00-\u9fff]", s)))


# ===================== 清理模型「自言自语」残渣 =====================
# 【真机缺陷 2026-11-07】推理模型会把「思考 + 草稿 + 自我点评」一起吐出来，实测原始输出：
#   `家村我探索到80%卡住了监牢打完给什么东西吗.\n了，这监牢里的怪好打不"(24>"好打不"\n*`
#   `紫衣角色…营造了幽默感考虑将评论聚焦于教室场景、角色服装，以及名字的调侃\n紫衣服那个…`
# 第 2 条里「考虑将评论聚焦于…」是模型在跟自己讨论写作方案，不是评论内容。
# 第 1 条残留了引号、`(24>`、`*` 这些结构化噪声。
#
# 策略：只清「明显的机器残渣」，不碰正常评论——
#   1. 删孤立的结构符号（成对的引号、星号、尖括号、markdown 残留）；
#   2. 删「考虑将评论…/让我们…/我需要…」这类自我指示句；
#   3. 删孤立不成句的碎片（含符号、长度过短的片断）。

# 自我指示句：模型在跟自己讨论怎么写，不该出现在评论里。
_SELF_TALK_RE = re.compile(
    r"(?:考虑|可以|应该|需要|打算|准备|尝试)\s*"
    r"(?:将|把)?\s*(?:评论|回复|内容|这段|文字|表达)\s*"
    r"(?:聚焦|集中|围绕|改写|调整|改|写)"
    r"[^。！？\n]*[。！？]?"
    r"|(?:让我们|我们需要|我需要|我要|接下来)\s*(?:聚焦|围绕|考虑|确保|注意)[^。！？\n]*[。！？]?"
    r"|^(?:好|好的|那么|嗯)[，,]\s*"
)

# 结构性噪声符号：引号（中英文）、星号、尖括号、markdown 井号等。
_NOISE_CHARS_RE = re.compile(r'["\'*<>#`~^|\\]+')
# 中文引号也一起清（评论里不需要引号，留着重写像引用）。
_CN_QUOTE_RE = re.compile(r"[“”‘’「」『』]")


def _strip_model_noise(text: str) -> str:
    """清掉模型输出的结构残渣与自我指示句。"""
    t = str(text or "")
    # 1) 自我指示句（模型在跟自己讨论写作方案，不是评论内容）。
    t = _SELF_TALK_RE.sub("", t)
    # 2) 结构符号：引号/星号/尖括号/markdown 标记。
    #    【注意】不能把换行一起吃掉——换行是「草稿行 vs 定稿行」的分界，
    #    吃掉就会把两行拼成一行（实测出现「好打不好打不」这种叠字）。
    #    中文引号直接删（评论里不需要）；英文引号/星号替换成换行，
    #    因为它们在原文里就是「两个候选片段的分隔」，删掉会把两段黏成一坨。
    t = _CN_QUOTE_RE.sub("", t)
    t = _NOISE_CHARS_RE.sub("\n", t)
    # 3) 括注里的数字编号残渣（如 `(24>`、`[1]`）。
    #    用 [ \t]* 而不是 \s*：\s 会吃掉换行，把草稿行和定稿行拼成一行。
    t = re.sub(r"[（(\[【][ \t]*\d+[ \t]*[）)\]】]?", "", t)
    # 4) 半角句点当句号用（英文残留的尾巴）。
    t = re.sub(r"(?<=[\u4e00-\u9fff])\.(?=\s|$|\n)", "", t)
    # 5) 行首的孤立符号/碎片（只剩符号或 <2 个汉字的行）。
    lines = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        han = len(re.findall(r"[\u4e00-\u9fff]", s))
        if han < 2 and len(s) <= 4:
            continue
        lines.append(s)
    t = "\n".join(lines)
    # 6) 多余空白。
    t = re.sub(r"[ \t]+", "", t)
    return t.strip()


# 提示词里的语气词被模型抄进输出时的形态：句首「平和,」/「冷静：」/「客观，」。
# 只认「标签 + 标点」这一种形态，避免误伤正常以「平和」开头的句子。
_TONE_LABEL_RE = re.compile(
    r"^\s*(?:平和|冷静|客观|中立|温和|克制|口语化|随意|轻松|友好)"
    r"\s*[,，:：。]\s*"
)


def _strip_tone_label(text: str) -> str:
    """剥掉句首被模型抄出来的语气标签（如「平和,」）。"""
    t = str(text or "").lstrip("\n ")
    prev = None
    # 连续剥（模型可能抄两个标签），但必须每次都有标点，防止吃掉正常句子。
    while prev != t:
        prev = t
        t = _TONE_LABEL_RE.sub("", t, count=1).lstrip("\n ")
    return t


# ===================== AI 味打分（2026-11-07 融合重写） =====================

def _han_count(text: str) -> int:
    """汉字数（不含标点/英文）。密度指标的分母。"""
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _non_overlapping(text: str, terms: tuple[str, ...]) -> list[tuple[int, str]]:
    """长词优先的不重叠匹配，避免「因此」被「因」重复计数。"""
    matches: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for m in re.finditer(re.escape(term), text):
            start, end = m.span()
            if any(start < oe and end > os_ for os_, oe in occupied):
                continue
            matches.append((start, term))
            occupied.append((start, end))
    return sorted(matches)


def _sentence_length_cv(text: str, min_sentences: int = 6) -> float | None:
    """句长变异系数：人写的长短句差距大，模型的句长彼此接近。

    返回 None 表示句数不足，这个量在短文本上没有意义（不做外推）。
    短评论（句数 < 6）本来就没有节奏可言，不该被这条判据罚。
    """
    lengths = [
        _han_count(m.group())
        for m in re.finditer(r"[^。！？!?\n]+[。！？!?]", text)
        if _han_count(m.group()) >= 4
    ]
    if len(lengths) < min_sentences:
        return None
    mean = sum(lengths) / len(lengths)
    if mean == 0:
        return None
    variance = sum((v - mean) ** 2 for v in lengths) / len(lengths)
    return (variance ** 0.5) / mean


def _conjunction_density(text: str) -> tuple[float | None, int, int]:
    """显性连词密度（每千字）。返回 (密度, 命中数, 汉字数)。"""
    han = _han_count(text)
    if han == 0:
        return None, 0, 0
    hits = _non_overlapping(text, CONJUNCTIONS_ZH)
    return len(hits) * 1000.0 / han, len(hits), han


def _nominalization_hits(text: str) -> list[str]:
    """动词名词化命中片段（「进行了…的调整」这类撑长度的写法）。"""
    out: list[str] = []
    for pattern in NOMINALIZATION_PATTERNS:
        for m in pattern.finditer(text):
            out.append(m.group())
    return out


def _metaphor_field_count(text: str, window: int = 800) -> tuple[int, list[str]]:
    """借喻场混用数：同窗口内从几套不相干比喻系统借词。

    单套用得准没问题；多套混用才说明在用比喻替代说明。
    返回 (最大同窗口场数, 命中词样本)。
    """
    hits: list[tuple[int, str, str]] = []
    for field, words in METAPHOR_FIELDS.items():
        for word in words:
            for m in re.finditer(re.escape(word), text):
                prefixes = METAPHOR_LITERAL_PREFIX.get(word, ())
                head = text[max(0, m.start() - 4):m.start()]
                if any(head.endswith(p) for p in prefixes):
                    continue
                hits.append((m.start(), field, word))
    hits.sort()
    best = 0
    for i, (start, _, _) in enumerate(hits):
        fields = {h[1] for h in hits[i:] if h[0] - start <= window}
        best = max(best, len(fields))
    return best, sorted({w for _, _, w in hits})


def ai_flavor_score(text: str) -> float:
    """AI 味打分（0–100，越高越像机器）。

    【2026-11-07 重写】老版本是「单词命中即重罚」，实测两头都失灵：
    - 长文误杀：人类正式文章里的「而已/究竟/固然/因此/机制/规范」被当成 AI 证据，
      10 篇真实人类文章 4 篇被判 ≥45；
    - 短评论漏判：AI 腔的「值得一提的是/总而言之」不在词表里，几乎全打 0 分。

    新算法按「痕迹密度」评分，而不是「命中数」：同样 3 个词，出现在 50 字评论里
    比出现在 3000 字文章里严重得多。同时引入结构层指标（句长节奏、连词密度、
    动词名词化、借喻场混用），这些短文本也算得出来，补上漏判。

    设计原则来自两个开源项目（De-AI-Prompt-Enhancer / shuorenhua）的共识：
    单个词出现一次不算证据，成簇、连段、明显在替读者宣布结论才罚；
    不追求零命中——人类写作的各项指标在中间区间，不是 AI 的反面。
    """
    t = str(text or "")
    han = _han_count(t)
    if han == 0:
        # 纯英文/纯标点：不做中文 AI 味判断，直接放行。
        return 0.0

    score = 0.0
    detail: dict[str, float] = {}

    # ---- 1) A 类痕迹词的密度 ----
    # 每千字命中数；再按「是否成簇」加权。短评论里 1 个 = 高密度，长文里 1 个 = 忽略。
    markers = _non_overlapping(t, AI_MARKER_WORDS)
    marker_n = len(markers)
    if marker_n:
        per_1k = marker_n * 1000.0 / han
        # 【关键】判定「成簇」必须同时看长度和密度，不能只看绝对命中数。
        # 2026-11-07 实测教训：6819 字长文里「深耕」（农业本义）、「其一」（序数）
        # 各出现 1 次，若按「命中 2 个 = 35 分」直接罚，就把人类文章误杀了。
        # 长文里两个痕迹词散落在几千字中，是正常人类写作的中间区间。
        dense = per_1k >= 8            # 密度极高：短文本里扎堆
        clustered = per_1k >= 3        # 密度可疑：明显扎堆
        if dense or (marker_n >= 3 and clustered):
            add = 55.0          # 成簇：几乎可判机器
        elif clustered:
            add = 35.0          # 明显可疑
        else:
            # 稀疏（长文里零星出现）：按密度轻计分，不成簇就不重罚。
            add = min(12.0, per_1k * 1.5)
        score += add
        detail["markers"] = add

    # ---- 2) B 类套壳句式的簇密度 ----
    shell_hits: list[str] = []
    for pattern, label in AI_SHELL_PATTERNS:
        shell_hits.extend([label] * len(re.findall(pattern, t)))
    if shell_hits:
        # 套壳句式 1 个是正常修辞，2 个开始像模板，3 个以上基本是提纲扩写。
        if len(shell_hits) >= 3:
            add = 30.0
        elif len(shell_hits) == 2:
            add = 18.0
        else:
            add = 6.0
        score += add
        detail["shells"] = add

    # ---- 3) 句长节奏（结构层，仅长文有效）----
    cv = _sentence_length_cv(t)
    if cv is not None:
        # 人类实测区间 0.43~0.71（shuorenhua 人类语料）。低于 0.38 说明句长过于均匀。
        if cv < 0.30:
            add = 22.0
        elif cv < 0.38:
            add = 12.0
        else:
            add = 0.0
        score += add
        detail["cv"] = add

    # ---- 4) 显性连词密度（每千字）----
    conj_density, conj_n, _ = _conjunction_density(t)
    if conj_density is not None and conj_n >= 3:
        # 人类实测 0.0~8.94/千字。超过 12 且至少 3 处，才算在搬骨架。
        if conj_density > 22:
            add = 18.0
        elif conj_density > 12:
            add = 10.0
        else:
            add = 0.0
        score += add
        detail["conj"] = add

    # ---- 5) 动词名词化 ----
    noms = _nominalization_hits(t)
    if noms:
        add = min(20.0, 7.0 * len(noms))
        score += add
        detail["nominal"] = add

    # ---- 6) 借喻场混用 ----
    fields, _ = _metaphor_field_count(t)
    if fields >= 3:
        add = 15.0
        score += add
        detail["metaphor"] = add

    # ---- 7) 原有的几条硬信号（保留，但对短文本放宽）----
    # 长破折号：单次不算，两次以上像模板。
    if t.count("——") >= 2:
        score += 10.0
    # 一段话里逗号过多且句子超长（机器爱写长句）。
    if han > 120 and t.count("，") >= 6:
        score += 6.0
    # 超过 20 字却完全没有中文标点：一口气说完，机器特征。
    if han > 20 and not re.search(r"[，。？！、；]", t):
        score += 20.0
    # 段间空行：帖子正文里出现，说明在套段落模板（评论不会有）。
    if han > 100 and re.search(r"\n\s*\n", t):
        score += 8.0

    return min(100.0, score)


# ===================== 降级 =====================

async def fallback_text_only(draft: PostDraft) -> PostDraft:
    """生图失败降级：丢弃所有配图，仍按图文帖（27）发纯文字。

    【重要】不再把 link_tag 降级成 28（纯动态）——真机实测 28 会被小黑盒拒
    （任何分区都返回「错误的动态分区」）。发布层 `_effective_link_tag` 永远返回 27，
    无图也照发图文帖，这里只清 images。
    """
    draft.images = []
    return draft


# ===================== 生成能力（A 类，走 ctx 注入） =====================

def _maybe_await(value: Union[str, Awaitable[str]]) -> "Awaitable[str]":
    import asyncio

    if inspect.isawaitable(value):
        async def _run() -> str:
            return str(await value)

        return _run()

    async def _identity() -> str:
        return str(value)

    return _identity()


async def _generate_text(ctx: Any, prompt: str) -> str:
    """经宿主 LLM 生成文本：优先 ctx.generate_text，否则 ctx.api.call。"""
    generator: Any = getattr(ctx, "generate_text", None)
    if callable(generator):
        return await _maybe_await(generator(prompt))
    api: Any = getattr(ctx, "api", None)
    if api is not None and hasattr(api, "call"):
        result = await api.call("maisaka.reply.generate", prompt=prompt)
        if isinstance(result, dict):
            result = result.get("text", result.get("content", result))
        return str(result)
    raise NotImplementedError(
        "创作层需要宿主大模型能力：请提供 ctx.generate_text(prompt) 或 ctx.api.call 能力"
    )


async def _generate_images(ctx: Any, title: str, body: str, count: int) -> list[bytes]:
    """生成 count 张配图，返回图片字节列表（任一失败跳过，整体不抛）。

    【2026-11-07 接入】原来这里用 TODO 占位的 `host.image.generate`，配图从未生效。
    现改走 layers.imagegen：直连 OpenAI 兼容生图 / 麦麦绘图插件，返回真实图片字节。
    """
    from layers import imagegen

    out: list[bytes] = []
    for _ in range(max(1, count)):
        try:
            data = await imagegen.generate_image_bytes(ctx, title=title, body=body)
        except Exception:  # noqa: BLE001 —— 单张失败不影响整篇发帖。
            data = None
        if data:
            out.append(data)
    return out


# 英文思考里常见的模板词：出现就该整行判为「模型在自言自语」，
# 哪怕这行里夹了中文（如 `Title options considered: "标题A" or "标题B"`）。
_THOUGHT_MARKERS: tuple[str, ...] = (
    "title options considered", "title option", "options considered",
    "the task involves", "the user wants", "the request", "let me", "i should",
    "i'll", "i will", "we need", "the content should", "focusing on",
    "considering the", "based on the", "looking at", "the post", "the title",
    "similar to", "this is a", "here's", "drafting", "rewriting",
)


def _strip_english_thought(raw: str) -> str:
    """剥掉正文/标题里的英文思考段落（推理模型的内联思考）。

    【真机缺陷 2026-11-07 用户报障「总结到底是写什么玩意」】
    用户贴出来的总结正文是这样开头的：
        The task involves generating a
        Title options considered: "順手刷了会儿动态，大家都在忙自己的事" or "..."
        Focusing on how diverse online content can be, with people engaged in ...

    这根本不是总结，是**模型的英文思考过程**被当成正文发出去了。

    【为什么之前只在评论里修过】
    `strip_reasoning_prefix` 只处理「开头一段英文、后面接中文」这一种形态，
    而总结这里的思考形态更复杂：
      - 跨了很多行；
      - 中间夹着中文候选标题（`Title options considered: "..."`）；
      - 结尾的英文没说完就被截断。
    只靠「找到第一个中文字符」会把思考里那句中英混排的候选标题也留下（实测就是这样，
    中文候选标题把汉字占比拉高了，于是整行被判成正文）。

    处理办法（逐行判定，比整段正则稳）：
      1. 命中 `_THOUGHT_MARKERS` 模板词的行 → 直接判思考（无视汉字占比）；
      2. 以英文字母开头、且汉字占比低（< 30%）的行 → 判思考；
      3. 连续思考行整段丢弃，直到出现「以中文为主体」的行；
      4. 剥完还剩中文就返回剥完的结果，否则原样返回（避免把纯英文正文吃光）。
    """
    text = str(raw or "").replace("\r\n", "\n")
    lines = text.split("\n")
    out: list[str] = []
    in_thought = True     # 开头就处在「可能的思考段」里
    dropped = 0
    for line in lines:
        s = line.strip()
        if not s:
            # 思考段里的空行直接丢；正文里的空行要保留（分段）。
            if in_thought:
                dropped += 1
                continue
            out.append("")
            continue
        han = len(re.findall(r"[\u4e00-\u9fff]", s))
        ascii_letters = len(re.findall(r"[A-Za-z]", s))
        ratio = han / max(1, han + ascii_letters)
        lowered = s.lower()
        has_marker = any(m in lowered for m in _THOUGHT_MARKERS)
        looks_english = ascii_letters > 0 and ratio < 0.3
        if in_thought and (has_marker or looks_english):
            dropped += 1
            continue
        # 一旦出现「中文为主体」的行，思考段结束，后面全部保留。
        in_thought = False
        out.append(line)
    stripped = "\n".join(out).strip()
    # 剥完什么都不剩，说明原文本来就没有中文正文 —— 别把内容吃光，原样返回。
    if not re.search(r"[\u4e00-\u9fff]", stripped):
        return text.strip()
    return stripped


def _clean_generated(raw: str) -> str:
    """生成内容的统一清洗入口（**发帖 / 总结用**，保留段落结构）。

    顺序：
      1. `_strip_english_thought`：先剥英文思考（必须在拆标题之前，
         否则首行的英文思考会被当成标题）；
      2. `strip_reasoning_prefix`：再兜一层「开头英文段 + 中文」的形态；
      3. `_to_simplified`：繁转简；
      4. `_strip_english_noise`：剥白名单外的英文单词。

    【为什么不能用 sanitize_comment】它为了评论场景会「去重复 + 只保留最后一行」，
    还会把换行吃掉（`_dedupe_repeated` 按行切分后重拼）——套在总结上
    会把 3 段正文压成 1 段（实测）。评论和正文的清洗诉求不同，必须分开。
    """
    t = _strip_english_thought(str(raw or ""))
    t = strip_reasoning_prefix(t)
    t = _to_simplified(t)
    # 逐行剥英文，保留换行结构。
    kept = []
    for line in t.replace("\r\n", "\n").split("\n"):
        if line.strip():
            kept.append(_strip_english_noise(line))
        else:
            kept.append("")
    out = "\n".join(kept)
    # 段内多余空格清掉，但不动换行。
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # 正文里的表情同样要在白名单内，否则客户端渲染不出来（正文可以多带几个）。
    out = _sanitize_marks(out, max_marks=3)
    return out.strip()


def _derive_title_from_body(body: str, limit: int = 20) -> str:
    """从正文里提炼一个短标题（模型没给标题时的兜底）。

    做法：取第一句里第一个逗号/句号之前的部分；太长就截断。
    宁可朴素，也不要空标题（空标题发不出去），也不要拿英文思考当标题。
    """
    text = str(body or "").strip()
    if not text:
        return ""
    first = re.split(r"[。！？\n]", text, maxsplit=1)[0].strip()
    # 取逗号前的主干（标题要短）
    head = re.split(r"[，,]", first, maxsplit=1)[0].strip()
    head = head or first
    head = head.strip("「」“”\"'（）()[]【】 ")
    if len(head) > limit:
        head = head[:limit]
    # 标题不该带句末标点
    return head.rstrip("。！？.!?").strip()


def _split_title_body(raw: str) -> tuple[str, str]:
    """从生成文本拆标题与正文：首行标题，其余为正文。

    【真机缺陷 2026-11-07】原实现直接拿首行当标题，两个问题：
    1. 模型把英文思考放在最前面时，**整段英文思考会被当成标题**
       （用户看到的“总结”完全不是总结）；
    2. 模型没给标题、直接写正文时，**正文第一句会被当成标题吃掉**，
       正文少一段、标题还是个带句号的句子。
    所以这里先剥思考，再判断首行到底像不像标题。
    """
    cleaned = _strip_english_thought(str(raw or ""))
    lines = [line.strip() for line in cleaned.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""
    if len(lines) == 1:
        return lines[0][:30], lines[0]

    head = lines[0]
    # 首行不像标题的迹象：以句号结尾、或太长（标题上限 30 字）、或是完整句子。
    # 这时把它当正文第一段，标题留空由上层补（总结可以无标题，宁可不给也不给错的）。
    looks_like_prose = (
        head.endswith(("。", "！", "？", ".", "!", "?"))
        or len(head) > 30
        or ("，" in head and len(head) > 18)
    )
    if looks_like_prose:
        return "", "\n".join(lines)
    return head[:30], "\n".join(lines[1:])


def _persona_block(ctx: Any) -> str:
    """把宿主配的人设带进正文提示词。

    【2026-11-08 用户要求「按自己的人设来生成」】
    之前写帖的提示词只有一句「用「平和」的语气」，人设（阿水、17 岁、高中生、
    QQ 群和小黑盒两头晃、怕被当成工具…）完全没传进去，所以写出来像通用小编。
    这里复用 imagegen 的人设读取（同一个 bot_config.toml 的 [personality]）。
    """
    try:
        from layers import imagegen

        hint = str(imagegen._persona_hint(ctx) or "").strip()
    except Exception:  # noqa: BLE001 —— 读不到人设就按普通用户写，不阻塞。
        hint = ""
    if not hint:
        return ""
    return f"你是谁：\n{hint}\n"


def _clean_title_line(raw: str) -> str:
    """把模型输出的一行洗成干净标题（去前缀 / 引号 / 句末标点，限 30 字）。"""
    text = _strip_english_thought(str(raw or ""))
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    head = re.sub(r"^\s*(标题|题目|title)\s*[:：]\s*", "", head, flags=re.I)
    head = head.strip(" \"'“”「」『』《》")
    head = head.rstrip("。.！!？?")
    return head[:30].strip()


async def _retitle(
    ctx: Any,
    *,
    body: str,
    known_titles: list[str],
    fmt: dict[str, Any],
    attempts: int = 2,
) -> str:
    """正文不动，只重起一个不撞车的标题。返回空串表示没起出来（上层保留原标题）。"""
    for _ in range(max(1, attempts)):
        prompt = (
            "给下面这篇已经写好的帖子起一个标题。\n"
            f"帖子正文：\n{str(body or '')[:800]}\n"
            f"标题方向：{fmt.get('title_hint', '')}\n"
            + _recent_titles_block(known_titles)
            + "只输出标题本身：不超过 20 字，不要引号、不要句号、不要解释。"
        )
        try:
            raw = await _generate_text(ctx, prompt)
        except Exception:  # noqa: BLE001 —— 起不出标题不该让整篇作废。
            return ""
        title = _clean_title_line(raw)
        if not title:
            continue
        if _similar_title(title, known_titles):
            continue
        return title
    return ""


# ===================== 想不想发 =====================

_WANT_PROMPT = (
    "{persona}"
    "你刚在小黑盒刷了一圈，看到这些：\n"
    "{material}\n"
    "\n现在问你自己一句：**你想发点什么吗？**\n"
    "就跟发朋友圈、发 QQ 空间一样 —— 有想分享的就发，没什么想说的就不发，没人逼你。\n"
    "如果某个帖子让你有点想法、有点想说话，就说想发，并说一句你想说的那个点是什么；\n"
    "如果看下来平平无奇、提不起劲、或者今天不想刷手机，就直说不想发。\n"
    "不要为了发帖而找理由。\n"
    "只回两行，格式：\n"
    "第一行：想发 / 不想发\n"
    "第二行：一句理由（想发就写你想说的那个点，不想发就写为什么没劲）"
)


def _parse_want(text: str) -> tuple[bool, str]:
    """解析「想发 / 不想发」的判断结果。解析不出来按「不想发」处理，宁可不发。"""
    lines = [ln.strip() for ln in str(text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return False, ""
    head = lines[0]
    reason = lines[1] if len(lines) > 1 else ""
    # 「不想发」必须先判，否则会被"想发"子串误命中。
    if "不想发" in head or head.startswith("否") or "没想" in head:
        return False, reason
    if "想发" in head or head.startswith("是") or "发" == head.strip():
        return True, reason
    return False, reason


async def decide_want_to_post(ctx: Any, *, items: list[Any], fmt: dict[str, Any]) -> dict[str, Any]:
    """"看着这些内容，你想发吗？"——想发才发，不想发就不发。

    【2026-11-08 用户要求】
    「不是要他总结，他觉得好玩他就发帖，他觉得不好玩就不发帖。」
    「就跟你发抖音、发朋友圈、发 QQ 空间，有分享就发呗，乐意发发呗，
    没什么特定要求，爱发发呗，不发不发呗。」

    原来的逻辑是：到了时段 + 有素材 → 必然生成一条发出去。哪怕内容平平无奇，
    也要硬写一篇，这就是「交作业」，不是发朋友圈。这里补上「想不想发」这一步：
    让模型照着人设看一遍素材，自己决定这次有没有想说的。

    :return: {"want": bool, "reason": str}
    """
    material = build_summary_material(items, limit=8)
    if not material and str(fmt.get("material")) != "life":
        return {"want": False, "reason": "这轮没什么可说的"}
    generate = getattr(ctx, "generate_text", None)
    if not callable(generate):
        # 没有模型能力时不能替用户决定「不发」，保持原行为放行。
        return {"want": True, "reason": ""}
    prompt = _WANT_PROMPT.format(
        persona=_persona_block(ctx),
        material=material or "（这轮没刷到什么）",
    )
    try:
        raw = generate(prompt)
        if hasattr(raw, "__await__"):
            raw = await raw
    except Exception:  # noqa: BLE001 —— 判断失败就按原逻辑放行，别把发帖卡死。
        return {"want": True, "reason": ""}
    want, reason = _parse_want(str(raw or ""))
    return {"want": want, "reason": reason}


# ===================== 帖子 / 评论 / 总结生成 =====================

def _post_prompt(topic: Topic, trace: LifeTrace, tone: str) -> str:
    topic_name = topic.name or "日常"
    return (
        f"你是小黑盒社区的一个普通用户，用「{tone}」的语气，围绕「{topic_name}」随手写一条帖子。\n"
        "要求：说人话、口语化，别讲道理，别解释自己怎么想的，别客套，别用破折号，别空行分段，"
        "别用「首先/其次/总之」这类套话。\n"
        "输出格式：第一行是标题（30 字以内），从第二行开始是正文，正文 2 段以上。"
    )


async def generate_post(ctx: Any, topic: Topic, trace: LifeTrace) -> PostDraft:
    """生成一条帖子草稿（标题 + 口语化正文 + AI 味打分）。"""
    tone = _read_str(_section(ctx, "create"), "tone", "平和")
    raw = await _generate_text(ctx, _post_prompt(topic, trace, tone))
    title, body = _split_title_body(raw)
    # 张贴也要剥掉模型残留的英文/思考，否则整条帖子像机翻。
    # keep_paragraphs=True：帖子是多段正文，不能被压成一段。
    # blank_between_paragraphs 由 [create].no_blank_line_between_paragraphs 决定，
    # 默认「段间不空行」（DESIGN.md 硬规矩），符合小黑盒帖子紧凑分段的观感。
    no_blank = _read_bool(_section(ctx, "create"), "no_blank_line_between_paragraphs", True)
    body = polish_chinese(
        _clean_generated(body),
        keep_paragraphs=True,
        blank_between_paragraphs=not no_blank,
        keep_periods=True,
    )
    draft = PostDraft(
        title=title,
        text=body,
        link_tag=LinkTag.IMAGE_TEXT,
        topic_ids=[topic.id] if topic.id else [],
        polished=True,
    )
    draft.ai_flavor_score = ai_flavor_score(body)
    return draft


# 常见游戏名 → 别名线索。只用于「认游戏」，不是黑话词典。
# 黑话正文一律来自宿主 `jargons` 表，这里只解决「这条帖子属于哪款游戏」。
_GAME_ALIASES: dict[str, tuple[str, ...]] = {
    "博德之门3": ("博德之门", "bg3", "博启", "博德"),
    "原神": ("原神", "genshin", "提瓦特"),
    "鸣潮": ("鸣潮", "wuthering", "今汐", "忌炎"),
    "崩坏星穹铁道": ("星穹铁道", "星铁", "崩铁", "honkai star rail"),
    "英雄联盟": ("英雄联盟", "lol", "撸啊撸", "峡谷", "排位"),
    "CS2": ("cs2", "csgo", "反恐精英"),
    "绝地求生": ("绝地求生", "pubg", "吃鸡"),
    "无畏契约": ("无畏契约", "valorant", "瓦罗兰特"),
    "明日方舟": ("明日方舟", "arknights", "舟游", "干员"),
    "鬼谷八荒": ("鬼谷八荒",),
    "我的世界": ("我的世界", "minecraft"),
    "黑神话悟空": ("黑神话", "悟空", "黑猴"),
    "艾尔登法环": ("艾尔登法环", "老头环", "elden"),
    "赛博朋克2077": ("赛博朋克", "2077", "边缘行者"),
    "守望先锋": ("守望先锋", "overwatch"),
    "永劫无间": ("永劫无间", "naraka"),
    "三角洲行动": ("三角洲", "delta force"),
    "暗区突围": ("暗区突围", "暗区"),
    "Apex英雄": ("apex英雄",),
}


def _guess_game_name(sections: list[str], title: str, body: str, image_desc: str) -> str:
    """从分区标签 / 标题 / 正文 / 识图描述里认出这是哪款游戏。

    【为什么需要】小黑盒大量帖子的标题是黑话或梗（如「一发入魂」「博启」），
    光看标题模型根本不知道是哪个游戏；而分区标签（topics/hashtags）通常是准的。
    不知道游戏就点评，圈内人一眼看出是外人——这正是用户 2026-11-07 的报障。

    【关键坑：标签会骗人】一条帖子常挂多个标签，比如「网文」帖子里会带「原神」「鸣潮」
    ——因为那篇同人文写的是原神。若直接按标签认游戏，就会把一篇网文帖判成原神帖，
    评论变成对着原神说事，反而更外行。所以：
      - 先看**主分区**（第一个标签）是不是游戏分区，是就认它；
      - 主分区是「小说/网文/影视/动漫」这类泛分区时，**不认游戏**，交给内容判断；
      - 标题/正文里明确提到的游戏，可信度高于后面的关联标签。

    认不出来返回空串——**宁可不说，也不要猜错游戏**（猜错比不说更像外人）。
    """
    title_low = title.lower()

    # 1) 主分区（第一个标签）就是游戏名 → 最可靠。
    first = str(sections[0] if sections else "").strip().lower()
    if first:
        for name in _GAME_ALIASES:
            if first == name.lower():
                return name

    # 2) 别的标签是不是游戏：只有在**主分区不是泛分区**时才采信，
    #    避免「网文」帖子因为带了「原神」标签就被判成原神帖。
    generic = {
        "盒友杂谈", "沙雕日常", "百科知识", "美图自拍", "情投一盒", "职场工作",
        "校园生活", "体育运动", "动漫", "影视", "小说", "网文", "网文小说",
        "美食", "汽车", "萌宠", "绘画", "周边", "桌游", "数码硬件", "ai",
        "主机游戏", "感情", "辩论赛", "单机游戏", "手游", "电竞", "游戏杂谈",
        "提问求助", "生活", "情感",
    }
    if first and first not in generic:
        # 主分区不是泛分区，允许用其余标签认游戏。
        for s_raw in sections:
            s = str(s_raw or "").strip().lower()
            for name in _GAME_ALIASES:
                if s == name.lower():
                    return name

    # 3) 标题里明确提到某款游戏 → 可信（标题是作者自己的话）。
    for name, aliases in _GAME_ALIASES.items():
        if any(a in title_low for a in aliases):
            return name

    # 4) 正文 / 识图描述里提到（比标签里的顺带关联更可靠）。
    blob = (body + " " + image_desc).lower()
    for name, aliases in _GAME_ALIASES.items():
        if any(a in blob for a in aliases):
            return name

    # 5) 主分区本身像个游戏分区（既不是泛分区也不在已知游戏表里）就直接用它。
    if first and first not in generic and len(str(sections[0]).strip()) <= 15:
        return str(sections[0]).strip()
    return ""


async def enrich_comment_target(ctx: Any, item: Any) -> CommentTarget:
    """把一条浏览项整理成「带内容的评论目标」，必要时先识别配图。

    【为什么要单独一步】小黑盒很多帖子正文只有一句话，信息全在图上。
    不先看图，评论就无从谈起。这里按「标题 → 正文 → 配图」的顺序补素材：
    - 正文太短（< 30 字）且帖子有图时，才去调视觉模型（省调用、省时间）；
    - 识图失败不影响主流程，只是少一份素材；
    - 素材全空时返回空目标，上层据此跳过这条，不发无关内容。
    """
    title = str(getattr(item, "title", "") or "").strip()
    body = str(getattr(item, "excerpt", "") or "").strip()
    author = str(getattr(item, "author_name", "") or "").strip()
    images = list(getattr(item, "image_urls", []) or [])
    image_desc = str(getattr(item, "image_desc", "") or "").strip()

    # 正文够长就不必看图；正文短/没正文才靠图补信息。
    if images and not image_desc and len(body) < 30:
        describe = getattr(ctx, "describe_image", None)
        if callable(describe):
            hints: list[str] = []
            if title:
                hints.append(title)
            if body:
                hints.append(body)
            try:
                got = describe(images[0], "；".join(hints))
                if inspect.isawaitable(got):
                    got = await got
                image_desc = str(got or "").strip()
            except Exception:  # noqa: BLE001 —— 识图失败按没有图处理。
                image_desc = ""
            if image_desc:
                # 回写到浏览项，同一帖后续复用（总结、再评论）不必重复识图。
                try:
                    setattr(item, "image_desc", image_desc)
                except Exception:  # noqa: BLE001 —— 只读对象写不进去就算了。
                    pass

    # ---- 社区语境：拿分区标签、认游戏、捞黑话 ----
    # 【真机缺陷 2026-11-07 用户报障「评论不涉及游戏黑话，人家一眼就辨别出来了」】
    # 小黑盒帖子的 `topics` / `hashtags` 里带着分区和话题（如「鸣潮」「原神」「博德之门3」），
    # 解析器产出在 section_names 里，但一直没接进提示词。模型不知道自己在评哪款游戏，
    # 只能照着标题字面瞎评。这里把语境补齐：
    #   1) 分区标签 → 猜游戏名；
    #   2) 用帖子文本去**宿主自带黑话表**里机械匹配，命中的词连含义一起带进提示词。
    # 黑话**不自建**：直接读宿主 MaiBot.db 的 `jargons` 表（用户可在宿主后台管理）。
    sections = [str(s).strip() for s in (getattr(item, "section_names", []) or []) if str(s or "").strip()]
    game = _guess_game_name(sections, title, body, image_desc)
    jargon_hint = ""
    lookup = getattr(ctx, "look_up_jargon", None)
    if callable(lookup):
        try:
            got = lookup([title, body, image_desc, " ".join(sections)])
            if inspect.isawaitable(got):
                got = await got
            jargon_hint = str(got or "").strip()
        except Exception:  # noqa: BLE001 —— 黑话查不到就当没有，不影响评论。
            jargon_hint = ""

    # 拉评论区：看别人在聊什么，自己挑一个**没人说过的角度**。
    # 【用户要求 2026-11-07】「你再看一下其他人评论的啥，你再结合一下，
    #  别人评论一个方面你一个人单独评论一个方面」。
    existing: list[str] = []
    fetch_comments = getattr(ctx, "fetch_existing_comments", None)
    if callable(fetch_comments):
        try:
            got = fetch_comments(str(getattr(item, "post_id", "") or ""))
            if inspect.isawaitable(got):
                got = await got
            if isinstance(got, list):
                existing = [str(c).strip() for c in got if str(c).strip()][:8]
        except Exception:  # noqa: BLE001 —— 拉不到评论就当没人评论，不影响发评论。
            existing = []

    return CommentTarget(
        post_id=str(getattr(item, "post_id", "") or ""),
        title=title,
        body=body,
        author_name=author,
        image_desc=image_desc,
        section_names=sections,
        game_name=game,
        jargon_hint=jargon_hint,
        existing_comments=existing,
    )


async def generate_comment(
    ctx: Any,
    target: CommentTarget,
    trace: LifeTrace,
    *,
    for_reply: bool = False,
) -> str:
    """生成一条评论文本（口语化短评）。

    【真机缺陷 2026-11-06 用户报障「回复的跟帖子一点关系都没有」】
    原实现只有一句「你在小黑盒上看到一条帖子，随手回一句短评」，
    **既没把标题也没把正文传进去**，模型只能凭空编 —— 生成什么全看运气。
    这与早期 MCP 版本踩过的是同一个问题：提示词里没有上下文。

    现在的做法：
    - 必带帖子标题与正文（有图则带图片内容描述）；
    - 回复通知时额外带被回复的评论原文，做到「接得上话」；
    - 素材完全为空时**不硬编**，直接返回空串让上层跳过这条，
      宁可不评论，也不发一句与帖子无关的话。
    """
    title = str(getattr(target, "title", "") or "").strip()
    body = str(getattr(target, "body", "") or "").strip()
    image_desc = str(getattr(target, "image_desc", "") or "").strip()
    author = str(getattr(target, "author_name", "") or "").strip()
    comment_text = str(getattr(target, "comment_text", "") or "").strip()
    # 社区语境：哪款游戏 + 圈内黑话（黑话来自宿主自带黑话表，不自建）。
    game = str(getattr(target, "game_name", "") or "").strip()
    jargon = str(getattr(target, "jargon_hint", "") or "").strip()
    sections = [str(s).strip() for s in (getattr(target, "section_names", []) or []) if str(s or "").strip()]

    # 一点素材都没有就别编了：宁可跳过，也不发无关内容。
    if not (title or body or image_desc or comment_text):
        return ""

    parts: list[str] = []
    if for_reply:
        parts.append("你在小黑盒上收到一条回复，用「平和」的语气接一句话。")
        if comment_text:
            parts.append(f"对方说的是：「{comment_text[:300]}」")
    else:
        parts.append("你在小黑盒上刷到一条帖子，用「平和」的语气留一句评论。")

    # 【2026-11-07 用户报障「不涉及黑话就瞎评，人家一眼就辨别出来了」】
    # 先告诉模型「这是哪个游戏、混的是哪个圈子」，再给它圈内词的正确含义，
    # 它才可能说出内行话；不然只能照着标题字面讲外行话。
    if game:
        parts.append(f"这是《{game}》相关的帖子。")
    if sections:
        parts.append(f"帖子所属分区/标签：{'、'.join(sections[:6])}")

    if title:
        parts.append(f"帖子标题：{title[:120]}")
    if body:
        parts.append(f"帖子正文：{body[:600]}")
    if image_desc:
        parts.append(f"帖子配图内容：{image_desc[:400]}")
    if jargon:
        parts.append(
            "这个圈子里这些词是这个意思（用得着就自然带进去，别硬塞）：\n" + jargon[:600]
        )
    if author and for_reply:
        parts.append(f"发帖人昵称：{author}")

    # 【用户要求 2026-11-07，两次纠正后定稿】
#   ❌ 我第一版写的是「不要重复他们、换一个没人说的角度」—— 错的。
#      用户原话：「不是避开他们的话题，是要融进去，你不能一个人单独写一个全新的话题。
#      人家都评论这个游戏怎么买划算，你来一句啊可以借这个机会补充仓库了，这不扯淡吗？」
#   ✅ 正确做法：先看清大家在聊什么（共同焦点），**顺着同一个话题往下接**，
#      像走进一群人正在聊的局，插一句话进去，而不是另开一桌。
    existing = [str(c).strip() for c in (getattr(target, "existing_comments", []) or []) if str(c or "").strip()]
    if existing and not for_reply:
        parts.append(
            "这个帖子下面别人是这么评论的：\n"
            + "\n".join(f"- {c[:60]}" for c in existing[:8])
        )
        parts.append(
            "先看懂他们在聊什么：大家共同关注的是哪个点、在讨论什么具体问题。\n"
            "你的评论要**顺着这个正在聊的话题往下接**——补充一个细节、附和一句、"
            "或者就着他们说的那个点讲自己的情况。\n"
            "**绝对不要另起一个新话题**：如果有人在聊价格，你就聊价格；"
            "有人在聊玩法，你就聊玩法。突然岔到无关的事，一眼就能看出你不在局里。"
        )

    parts.append(
        "要求：\n"
        "- 必须紧扣上面的具体内容说话，**优先接住评论区正在聊的那个点**；\n"
        "- 用圈内人的说法，别用书面腔、别用游戏资讯稿的口气；上面给了本圈黑话就按那个含义用；\n"
        "- 不确定的词不要硬编，宁可少说一个词，也别用错含义（用错比不用更露馅）；\n"
        "- 绝对不要写和帖子内容无关的泛泛之谈（比如「说得好」「有道理」「支持一下」）；\n"
        "- **像真实评论区那样说话**：小黑盒评论区大多数就是 5-20 字的一句大白话，"
        "不用把话讲完整、不用交代前因后果，想到哪说哪就行；\n"
        "- 可以带 1 个表情（最多 2 个），但**只能从下面这份清单里选**，"
        "名字写错小黑盒就渲染不出来（用户看到的是空白），绝对不许自创、不许改字：\n"
        + EMOJI_HINT
        + "\n"
        "- 表情要和语气对得上，别每句都带，也别连用多个；没有合适的就不带；\n"
        "- 不要标点堆砌，不要破折号，不要空行，不要分段；\n"
        "- 只输出评论本身，不要引号、不要解释、不要「评论：」这类前缀。"
    )
    raw = await _generate_text(ctx, "\n".join(parts))
    raw = strip_reasoning_prefix(str(raw))
    text = polish_chinese(raw)
    # 强清洗：转简体 + 剥英文残留 + 去重复（2026-11-07 评论质量问题兜底）。
    return sanitize_comment(text)


def _data_dir(ctx: Any) -> Path:
    """拿数据目录（写生图临时文件用）。"""
    paths = getattr(ctx, "paths", None)
    if paths is not None:
        d = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
        if d:
            return Path(d)
    return Path(".")


async def generate_images(ctx: Any, draft: PostDraft, count: int = 1) -> list[ImageAsset]:
    """为帖子草稿生成配图（1~N 张），返回 ImageAsset 列表。

    生图（layers.imagegen）→ 写数据目录临时文件 → 返回 ImageAsset(local_ref=相对文件名)。
    上传图床由 publish.upload_image 用 local_ref 读文件完成。
    """
    data_dir = _data_dir(ctx)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    assets: list[ImageAsset] = []
    for image_bytes in await _generate_images(ctx, draft.title, draft.text, count):
        fname = f"hx_img_{uuid.uuid4().hex[:12]}.png"
        try:
            (data_dir / fname).write_bytes(image_bytes)
        except OSError:
            continue
        assets.append(ImageAsset(local_ref=fname, status=ImageStatus.PENDING))
    return assets


# ===================== 感想总结：字数 / 段数收敛（纯逻辑，可测） =====================

# 中文句读：截断与拆段都优先落在这些字符后面，避免句子断一半。
_SENTENCE_BREAKS = "，、；：,;:！？!?"


def count_chars(text: str) -> int:
    """统计中文字数（按非空白字符计，标点也算，符合「200–400 字」的口语口径）。"""
    return sum(1 for ch in str(text or "") if not ch.isspace())


def count_paragraphs(text: str) -> int:
    """统计段数（按换行切分，只数非空段）。"""
    return len([p for p in re.split(r"\n+", str(text or "")) if p.strip()])


def clamp_chars(text: str, max_chars: int) -> str:
    """把正文压到 max_chars 以内；尽量切在句读处，不把句子砍半。"""
    if max_chars <= 0:
        return ""
    value = str(text or "")
    if count_chars(value) <= max_chars:
        return value
    out: list[str] = []
    counted = 0
    for ch in value:
        if not ch.isspace():
            counted += 1
        if counted > max_chars:
            break
        out.append(ch)
    truncated = "".join(out)
    # 从尾部往前找最近的句读，最多回退 40 字，避免整段只剩标点。
    floor = max(0, len(truncated) - 40)
    for idx in range(len(truncated) - 1, floor, -1):
        if truncated[idx] in _SENTENCE_BREAKS:
            return truncated[: idx + 1].strip()
    return truncated.strip()


def _split_once(paragraph: str) -> tuple[str, str] | None:
    """把一段按中间位置的句读拆成两段；整段没有标点时按中点硬拆。"""
    text = paragraph.strip()
    if len(text) < 20:
        return None
    mid = len(text) // 2
    # 先找中点右侧最近的句读，再找左侧。
    for idx in range(mid, len(text) - 1):
        if text[idx] in _SENTENCE_BREAKS and idx + 1 < len(text):
            return text[: idx + 1].strip(), text[idx + 1 :].strip()
    for idx in range(mid - 1, 0, -1):
        if text[idx] in _SENTENCE_BREAKS:
            return text[: idx + 1].strip(), text[idx + 1 :].strip()
    # 整段没标点（或只有开头/结尾有）：按中点硬拆，保证段数下限。
    return text[:mid].strip(), text[mid:].strip()


def fit_summary_shape(
    text: str,
    min_chars: int = 200,
    max_chars: int = 400,
    min_paragraphs: int = 2,
    max_paragraphs: int = 4,
) -> str:
    """把总结正文收敛到「字数 / 段数」约束内（纯逻辑，可测）。

    规则：
    - 超长按句读截到 max_chars 以内；
    - 段数多于 max_paragraphs：保留前 max-1 段，其余并入最后一段；
    - 段数少于 min_paragraphs：反复把最长段按句读拆开，拆不动就停。
    字数下限无法靠裁剪补齐，由调用方重试生成（本函数只管「不超上限、段数落区间」）。
    """
    paragraphs = [p.strip() for p in re.split(r"\n+", str(text or "")) if p.strip()]
    if not paragraphs:
        return ""
    paragraphs = [clamp_chars(p, max_chars) for p in paragraphs]

    # 段数过多：多出来的并进最后一段。
    if max_paragraphs > 0 and len(paragraphs) > max_paragraphs:
        head = paragraphs[: max_paragraphs - 1]
        tail = "".join(paragraphs[max_paragraphs - 1 :])
        paragraphs = head + [tail]

    # 段数过少：拆最长的一段。
    guard = 0
    while min_paragraphs > 0 and len(paragraphs) < min_paragraphs and guard < 12:
        guard += 1
        longest_idx = max(range(len(paragraphs)), key=lambda i: len(paragraphs[i]))
        split = _split_once(paragraphs[longest_idx])
        if split is None:
            break
        first, second = split
        paragraphs = paragraphs[:longest_idx] + [first, second] + paragraphs[longest_idx + 1 :]

    body = "\n".join(p for p in paragraphs if p)
    body = clamp_chars(body, max_chars)

    # 整体裁剪可能把末尾段落砍掉，导致段数不足；再拆一次（拆不增加字数）。
    guard = 0
    while min_paragraphs > 0 and count_paragraphs(body) < min_paragraphs and guard < 6:
        guard += 1
        rows = [p for p in re.split(r"\n+", body) if p.strip()]
        if not rows:
            break
        longest_idx = max(range(len(rows)), key=lambda i: len(rows[i]))
        split = _split_once(rows[longest_idx])
        if split is None:
            break
        rows = rows[:longest_idx] + [split[0], split[1]] + rows[longest_idx + 1 :]
        body = "\n".join(rows)
    return body


def _summary_cfg(ctx: Any) -> dict[str, int]:
    """读取 [summarize] 的字数 / 段数约束（缺省 200–400 字、2–4 段）。

    【2026-11-08 已停用】用户明确要求「字数多少也就不要设置了」，
    发帖不再按字数区间卡。这个函数保留是因为 `_read_section_value` 之类的
    兼容读取还在用；新代码请用 `_unbounded_shape()`。
    """
    defaults = {"min_chars": 200, "max_chars": 400, "min_paragraphs": 2, "max_paragraphs": 4}
    section = _section(ctx, "summarize")
    for key in defaults:
        try:
            defaults[key] = int(_read(section, key, defaults[key]))
        except (TypeError, ValueError):
            pass
    return defaults


def _unbounded_shape() -> dict[str, int]:
    """「不限制篇幅」的占位结构：下限 0、上限给一个不会被撞到的天花板。

    【2026-11-08 用户要求「字数多少也就不要设置了」】
    想写一句就一句，想写十段就十段，长度由内容决定，不由配置决定。
    上限 10000 是小黑盒接口本身的正文长度限制，不是我们的创作规则。
    """
    return {"min_chars": 0, "max_chars": 10000, "min_paragraphs": 1, "max_paragraphs": 20}


def build_summary_material(items: list[Any], limit: int = 24) -> str:
    """把浏览摘要整理成提示词素材（纯逻辑，可测）。

    【2026-11-07 扩大素材量】原来只给 12 条，模型写 200–400 字时素材不够，
    就凭空补出「没刷到过的游戏和事件」（真机实测：编了活侠传/霍格沃茨/工程软件三条）。
    现在给 24 条、摘要放宽到 80 字，让它有足够真实内容可写。

    :param items: 浏览摘要行（dict，含 title / excerpt / partition_name），或纯 post_id 字符串。
    """
    lines: list[str] = []
    for item in list(items)[: max(1, limit)]:
        if isinstance(item, dict):
            title = str(item.get("title", "") or "").strip()
            excerpt = str(item.get("excerpt", "") or "").strip()
            partition = str(item.get("partition_name", "") or "").strip()
            if not title and not excerpt:
                continue
            prefix = f"【{partition}】" if partition else ""
            lines.append(f"{prefix}{title}" + (f"：{excerpt[:80]}" if excerpt else ""))
        else:
            text = str(item or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


# 「有戏」的标记词：带这些词的帖子更容易让人想发一条。
_JUICE_WORDS = (
    "笑", "离谱", "崩", "破防", "无语", "逆天", "绝了", "服了", "哭", "震惊",
    "居然", "竟然", "求", "怎么办", "值不值", "翻车", "坑", "吐槽", "兄弟们",
)


def pick_focus_item(items: list[Any]) -> Any:
    """从待总结素材里挑**一条**最适合单独发帖的（单条驱动的动机用）。

    打分：带疑问/感叹、有数字、有情绪词、摘要信息量足的优先；
    再加一点随机扰动，避免每次都是同一条。
    """
    import random

    candidates = [it for it in (items or []) if isinstance(it, dict)]
    if not candidates:
        return None

    def score(item: dict[str, Any]) -> float:
        text = f"{item.get('title', '')} {item.get('excerpt', '')}"
        value = 0.0
        if re.search(r"[？！?!]", text):
            value += 1.5
        if re.search(r"\d", text):
            value += 0.8
        if any(w in text for w in _JUICE_WORDS):
            value += 2.5
        value += min(len(text) / 90.0, 1.6)
        value += random.random() * 2.2
        return value

    return max(candidates, key=score)


def build_life_material(trace: Any) -> str:
    """把生活轨迹整理成「日记」素材（没有刷帖内容时用）。"""
    if trace is None:
        return ""
    extra = getattr(trace, "extra", None) or {}
    bits: list[str] = []
    scene = str(extra.get("scene") or "").strip()
    activity = str(extra.get("activity") or "").strip()
    note = str(extra.get("activity_note") or "").strip()
    routine = str(getattr(trace, "routine", "") or "").strip()
    mood = str(getattr(trace, "mood", "") or "").strip()
    if scene:
        bits.append(f"你正在做的事：{scene}")
    elif activity:
        bits.append(f"你正在做的事：{activity}")
    if note and note not in {"切换活动", "自然切换"}:
        bits.append(f"细节：{note}")
    if routine:
        bits.append(f"作息：{routine}")
    if mood:
        bits.append(f"情绪：{mood}")
    energy = getattr(trace, "energy", None)
    if isinstance(energy, (int, float)):
        bits.append(f"体力：{round(float(energy) * 100)}%")
    return "\n".join(f"- {b}" for b in bits)


def pad_summary_to_min(
    text: str,
    items: list[Any],
    min_chars: int = 200,
    max_chars: int = 400,
) -> str:
    """字数不足时，用真实素材补一段「具体看到了什么」（纯逻辑，可测）。

    【为什么需要这一步（真机踩坑 2026-11-06）】
    模型经常只写 100~130 字就收尾，重试也压不出更多内容，而配置要求 200 字下限，
    结果整条总结链路直接判失败。单纯加次数不管用——模型就是「话少」。
    这里不编造，只做一件诚实的事：把已经刷到的真实标题铺开，
    用口语说「还刷到些别的」，补到下限为止（超过上限就停）。

    :return: 补足后的正文（未补足也原样返回，调用方再判）。
    """
    body = str(text or "").strip()
    if not body or count_chars(body) >= min_chars:
        return body

    # 抽真实标题（去掉【分区】前缀，避免每句都带括号）。
    titles: list[str] = []
    for item in list(items)[:12]:
        if isinstance(item, dict):
            title = str(item.get("title", "") or "").strip()
        else:
            title = str(item or "").strip()
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return body

    # 候选句按「信息量递减」排：先列标题（只列一次），后续只补感受，绝不重复同一批标题。
    # 【2026-11-07】去 AI 腔：候选句不能出现「大家…都…」这类上帝视角总结腔，
    # 只说自己刷到/划走这类具体动作。
    listed = "、".join(f"「{t}」" for t in titles)
    candidates = [
        f"另外还刷到 {listed}，没挨个点开。",
        "再往下翻基本还是这些，刷到后面就划走了。",
        "有几条标题挺唬人的，点进去也就那样，没往下看。",
        "刷到一半就有点腻了，随手划过去了。",
    ]
    filler = ""
    for piece in candidates:
        candidate = body + "\n" + filler + piece
        if count_chars(candidate) <= max_chars:
            filler += piece
            if count_chars(body + "\n" + filler) >= min_chars:
                break
        else:
            break

    if not filler:
        return body
    return body + "\n" + filler


# ===================== 总结：真实时间 / 去重（纯逻辑，可测） =====================

_WEEKDAY_CN = "一二三四五六日"


def _now_context(now: datetime | None = None) -> str:
    """当前真实时间的一句话描述，注入提示词防止模型虚构时段。

    【真机缺陷 2026-11-07 用户报障】中午都还没到，模型却写「大半夜随手刷了会儿」——
    提示词里没有任何当前时间，模型只能瞎编时段。把真实时间摆进提示词，
    并明确禁止编造时段，从根上解决时间幻觉。

    :param now: 可选，测试时注入固定时间；缺省取服务器本地时间。
    """
    dt = now or datetime.now()
    hour = dt.hour
    if 5 <= hour < 11:
        period = "早上"
    elif 11 <= hour < 13:
        period = "中午"
    elif 13 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "凌晨"
    return (
        f"现在是 {dt.year} 年 {dt.month} 月 {dt.day} 日，"
        f"星期{_WEEKDAY_CN[dt.weekday()]}，{period} {hour}:{dt.minute:02d}。"
    )


def _strip_title_from_body(title: str, body: str) -> str:
    """标题若成了正文首句的开头，把正文里这段重复的引子剥掉。

    【真机缺陷 2026-11-07】模型常给出「标题 = 正文首句前半截」的重复：
        标题：大半夜随手刷了会儿
        正文：大半夜随手刷了会儿，大家的生活都挺有意思的
    正文开头和标题一字不差地复读，用户一眼看出是机器。这里把重叠部分吃掉。

    注意：只在「模型自己给的标题」时调用；兜底标题本身是从正文提炼的，
    剥了反而丢内容（由调用方用 `title_from_model` 区分）。
    """
    title = str(title or "").strip()
    text = str(body or "")
    if len(title) < 4 or not text:
        return text
    stripped = text.lstrip("\n ")
    first_line = stripped.split("\n", 1)[0].strip()
    if not first_line.startswith(title):
        return text
    rest = first_line[len(title):].lstrip("，,。、；;：: \t")
    if not rest:
        # 第一段正好等于标题，整段删掉。
        return stripped[len(first_line):].lstrip("\n ").strip()
    # 剥掉重叠后还有内容，剩下的接回正文。
    tail = stripped[len(first_line):]
    return (rest + tail).strip()


def dedupe_summary(text: str, threshold: float = 0.72) -> str:
    """总结专用去重：段级近重复折叠 + 段内近重复句，保留分段结构。

    【真机缺陷 2026-11-07】用户贴出的总结第一、二行重复「大半夜随手刷了会儿」。
    评论用的 `_dedupe_repeated` 会把换行压平，不能直接套在多段总结上；
    这里按「段 → 句」两级去重，且不碰分段。
    """
    t = str(text or "").strip()
    if not t:
        return t
    paragraphs = [p.strip() for p in re.split(r"\n+", t) if p.strip()]
    # 段级：与最近 2 段比字符重叠率，近重复的整段折叠。
    kept: list[str] = []
    for para in paragraphs:
        han = re.sub(r"[^\u4e00-\u9fff]", "", para)
        if len(han) < 8:
            kept.append(para)  # 太短的段（表情、短句）不判重，直接留。
            continue
        dup = False
        for prev in kept[-2:]:
            ph = re.sub(r"[^\u4e00-\u9fff]", "", prev)
            if ph and _similarity(han, ph) >= threshold:
                dup = True
                break
        if not dup:
            kept.append(para)
    # 句级：每段内近重复句折叠。
    return "\n".join(_dedupe_near_sentences(p, threshold=threshold) for p in kept).strip()


# ===================== 总结接地检测：不许写「没刷到过的东西」 =====================

# 常见游戏 / 作品名。只用于核对「素材里到底有没有出现过」，不是知识库。
_GROUNDING_NAMES: tuple[str, ...] = tuple(_GAME_ALIASES.keys()) + (
    "霍格沃茨",
    "塞尔达",
    "宝可梦",
    "最终幻想",
    "怪物猎人",
    "只狼",
    "巫师3",
    "荒野大镖客",
    "GTA",
    "使命召唤",
    "战地",
    "星际争霸",
    "魔兽世界",
)


def _ngram_overlap(text: str, material: str, n: int = 4) -> int:
    """统计总结与素材共享的 n 字片段数（粗粒度接地检测）。"""
    def _norm(value: str) -> str:
        return re.sub(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]《》<>…—\-]+", "", str(value or ""))

    t, m = _norm(text), _norm(material)
    if len(t) < n or len(m) < n:
        return 0
    grams = {m[i : i + n] for i in range(len(m) - n + 1)}
    return sum(1 for i in range(len(t) - n + 1) if t[i : i + n] in grams)


def summary_grounding_issues(text: str, material: str) -> list[str]:
    """检查总结里有没有「素材里根本没有」的内容（防编造刷到的东西）。

    【真机缺陷 2026-11-07 用户报障「你没刷到这个怎么能说呢」】
    模型拿到 12 条真实素材（GTA6 / GPT 封号 / FF7 打折…），却写出
    「活侠传改存档」「工程软件报错」「霍格沃茨支持 PS5」三条 —— 200 条浏览记录里
    一条都没有。这不是配图假，是**总结本身在编**，比文风问题严重得多。

    这里做三道粗检（宁可误报触发重写，也不让编造内容发出去）：
      1. 书名号里的作品名必须原样出现在素材里；
      2. 已知游戏/作品名若被提到，素材里必须有；
      3. 整篇与素材几乎没有 4 字以上重合 → 判定整篇凭空生成。
    """
    body = str(text or "")
    issues: list[str] = []
    for name in re.findall(r"《([^》]{1,24})》", body):
        if name and name not in material:
            issues.append(f"《{name}》")
    for name in _GROUNDING_NAMES:
        if name and name in body and name not in material:
            issues.append(name)
    if _ngram_overlap(body, material) < 2:
        issues.append("整篇与素材几乎无重合")
    return sorted(set(issues))


async def generate_summary(
    ctx: Any,
    items: list[Any],
    trace: LifeTrace,
    max_attempts: int = 3,
    rewrite_hint: str = "",
    previous_text: str = "",
    format_id: str = "",
    recent_titles: list[str] | None = None,
    material_mode: str = "",
    life_trace: Any = None,
) -> SummaryDraft:
    """生成一条要发出去的内容（帖子 / 日记 / 刷帖随手记）。

    :param format_id: 本篇动机（见 layers/voices.py）。决定「为什么发、怎么写」，
        连着几篇不会用同一种写法。
    :param material_mode: single（只聊一条）/ multi（一批汇总）/ life（写自己的状态）。
        留空则按 format_id 自动判断。
    :param life_trace: 生活轨迹（life 模式写日记用）。None 则用调用方传的 trace。
    :param recent_titles: 最近发过的标题。喂给模型避开，生成后再做一次相似度校验。

    【2026-11-08 用户反馈「为什么是发感想呢？他觉得好玩就发一个，或者发个日记」】
    原来只支持「把这一批素材汇总成一篇感想」。现在按动机取素材：
    单条驱动只给一条（模型自然就着那一条写），日记模式不给刷帖素材、只给生活状态。
    """
    tone = _read_str(_section(ctx, "create"), "tone", "平和")
    no_blank = _read_bool(_section(ctx, "create"), "no_blank_line_between_paragraphs", True)
    fmt = _get_format(format_id)
    mode = str(material_mode or fmt.get("material") or "multi")
    # 【2026-11-08 用户要求「字数多少也就不要设置了」】
    # 篇幅不再由配置卡区间：想写几句写几句，长帖短帖都由内容决定。
    fmt_block = _format_brief(fmt)
    history_block = _recent_titles_block(recent_titles or [])
    known_titles = [str(t) for t in (recent_titles or []) if str(t).strip()]

    # 素材按动机取：单条只给一条、汇总给一批、日记只给生活状态。
    if mode == "life":
        material = build_life_material(life_trace if life_trace is not None else trace)
        items = []
    elif mode == "single":
        focus = pick_focus_item(list(items or []))
        items = [focus] if focus else []
        material = build_summary_material(items, limit=1)
    else:
        material = build_summary_material(items)
    source_ids = [str(i.get("post_id", "")) for i in items if isinstance(i, dict) and i.get("post_id")]

    best_text = ""
    best_title = ""
    best_title_from_model = False
    last_chars = 0
    grounding_hint = ""
    now_ctx = _now_context()
    for attempt in range(max(1, max_attempts)):
        # 篇幅不设限，所以没有「字数不够」这条重试理由；
        # 重写只由「AI 味」和「接地失败」触发。
        shortfall = ""
        # 接地失败（写了没刷到的东西）时的重写要求。
        grounding_block = ""
        if grounding_hint:
            grounding_block = (
                f"\n【上一版作废】上一版写了素材里根本没有的内容：{grounding_hint}。"
                "这次只准写素材里出现的帖子，一个没刷到的名字都不许提。\n"
            )
        # 定向重写：护栏说「太像机器」时，把上一版原文和去 AI 味的具体要求一起给模型。
        rewrite_block = ""
        if rewrite_hint:
            rewrite_block = (
                f"\n【必须重写】上一版被判定为「{rewrite_hint}」，这次要改写，不能原样复述。\n"
                + (f"上一版正文：\n{previous_text}\n" if previous_text else "")
                + "改写要求：\n"
                "- 把「总的来说 / 不得不说 / 让人不禁 / 在这个快节奏的时代」这类套话全部删掉；\n"
                "- 少用四字成语堆砌，改成具体的小事、具体的人名地名、具体的时间；\n"
                "- 句子长短交错，允许口语、允许不完整的句子，别每句都一样长；\n"
                "- 少用「不仅…而且」「既…又」这种对仗结构；\n"
                "- 保留原来的意思和素材，只换说法。\n"
            )
        # 开场与铁律按动机分：日记类不写刷帖内容，单条类只准聊一条。
        if mode == "life":
            opening = (
                f"你要发一条自己的日常，语气「{tone}」。\n"
                f"{now_ctx}\n"
                f"你现在的状态：\n{material or '（没什么特别的状态，就写此刻在干嘛、什么心情）'}\n"
            )
            rules = (
                "铁律（违反整篇作废）：\n"
                "- 就写你自己此刻的状态、在做的事和想法，**不要提刷到了什么帖子**；\n"
                "- 不许编造具体事件（去哪了、见了谁、买了什么），没发生就是没发生；\n"
            )
        elif mode == "single":
            opening = (
                f"你刚在小黑盒刷到一条内容，想就着它发一条，语气「{tone}」。\n"
                f"{now_ctx}\n"
                f"这条内容：\n{material or '（这轮没刷到什么，就写点最近的状态）'}\n"
            )
            rules = (
                "铁律（违反整篇作废）：\n"
                "- **只聊这一条**，别扩写成一批帖子的汇总，也别提素材之外的东西；\n"
                "- 素材里没有的游戏名、作品名、产品名、价格、版本号、事件，一个字都不许写；\n"
            )
        else:
            opening = (
                f"你刚在小黑盒刷了下面这些内容，用「{tone}」的语气写一篇发出去。\n"
                f"{now_ctx}\n"
                f"素材（这是你这轮真实刷到的全部内容）：\n{material or '（这轮没刷到什么，就写点最近的状态）'}\n"
            )
            rules = (
                "铁律（违反整篇作废）：\n"
                "- 只能写素材里出现的帖子。素材里没有的游戏名、作品名、产品名、价格、版本号、事件，一个字都不许写；\n"
                "- 不许凭印象补「我还看到…」。没刷到就是没刷到；素材不够就写你刷手机的动作和感受（划过去、点开看了两眼、没细看）；\n"
            )
        prompt = (
            opening
            + _persona_block(ctx)
            + fmt_block
            + history_block
            + "篇幅：写多少都行，一句两句也可以，觉得有话说就多说几句，不用凑字数。\n"
            + rules
            + "写法上：\n"
            "- 只写你自己真实的想法，别替「大家」下结论、别点评全体用户；\n"
            "- 禁止总结腔：「大家的生活都挺有意思的」「看着都挺替他们高兴的」「大家最近都在聊XX」「看完了准备睡了」这类话一句都不能有；\n"
            "- 说人话、口语化，别讲道理、别客套、别用破折号、别空行分段、别用「首先/其次/总之」这类套话；\n"
            "- **一篇只抓一两个点写透**，别把刷到的挨个点一遍。"
            "「翻着翻着」「还有人」「中间还夹着」「另外看到」这种逐条清点的句式一句都不要；\n"
            "- 如果要提时间，只能照上面给的真实时间写，禁止编造「大半夜」「凌晨」「清晨」这类和当前对不上的时段；\n"
            "- 标题和正文第一句不要重复，别把标题原样抄进正文开头。\n"
            f"{shortfall}{grounding_block}{rewrite_block}\n"
            "输出格式：第一行标题（30 字以内），从第二行开始是正文。"
        )
        raw = await _generate_text(ctx, prompt)
        title, body = _split_title_body(raw)
        # 【真机缺陷 2026-11-07】总结也必须走统一清洗：飞鱼 Gemini 会把英文思考
        # 内联在正文最前面，用户看到的「总结」其实是思考过程。
        # 注意不能用 sanitize_comment（它会只保留最后一行，把多段正文压没）；
        # keep_paragraphs=True 保住分段，否则段数不足会触发反复重写。
        # 段间是否空行由 [create].no_blank_line_between_paragraphs 决定（默认不空行）。
        body = polish_chinese(
            _clean_generated(body),
            keep_paragraphs=True,
            blank_between_paragraphs=not no_blank,
            keep_periods=True,
        )
        # 篇幅不设限：不做字数/段数收敛，只做去重和接地的清洗。
        # （2026-11-08 用户要求「字数多少也就不要设置了」）
        # 【真机缺陷 2026-11-07】段级 + 句级去重：模型常把同一句换个说法再写一遍。
        body = dedupe_summary(body)
        if not body:
            continue
        # 【真机缺陷 2026-11-07 用户报障「你没刷到这个怎么能说呢」】
        # 接地检查：写了素材里没有的作品/事件就整篇作废重写。
        # 宁可这一轮不发，也不能发一条「我刷到 XXX」的假总结。
        # 日记模式没有帖子素材，不适用这条（否则每篇都会被判「与素材无重合」）。
        if mode != "life":
            issues = summary_grounding_issues(body, material)
            if issues:
                grounding_hint = "、".join(issues[:4])
                last_chars = 0
                continue
        last_chars = count_chars(body)
        if last_chars > count_chars(best_text):
            best_text, best_title = body, title
            best_title_from_model = bool(str(title or "").strip())
        break  # 篇幅不设限，第一版通过接地检查就用它，不再重试凑字数

    # 不再用素材「补到下限」——话少就是话少，不该拿刷到的标题往里灌。
    if best_text:
        best_text = dedupe_summary(best_text)

    # 标题兜底：模型没给标题（或给的是句子，被判定为正文）时，
    # 从正文里提炼一个短标题。发帖必须有标题，空标题会直接被小黑盒拒。
    if best_text and not best_title_from_model:
        best_title = _derive_title_from_body(best_text)
    # 标题去重：跟最近发过的比一遍，太像就只换标题、正文不动。
    # 【2026-11-08 用户报障「连续三个帖子标题一模一样」】
    if best_text and best_title and known_titles:
        clash = _similar_title(best_title, known_titles)
        if clash:
            replacement = await _retitle(ctx, body=best_text, known_titles=known_titles, fmt=fmt)
            if replacement:
                best_title = replacement
                best_title_from_model = True
    # 只有「模型自己给的标题」和正文首句重复时才剥；兜底标题本身是从正文提炼的，
    # 剥了会丢内容（见 _strip_title_from_body 的注释）。
    if best_text and best_title_from_model:
        best_text = _strip_title_from_body(best_title, best_text)
    if not best_text and grounding_hint:
        _logger = getattr(ctx, "logger", None)
        if _logger is not None:
            _logger.warning(
                "回响：总结连续 %d 轮提到素材里没有的内容（%s），本轮不发，避免编造。",
                max(1, max_attempts),
                grounding_hint,
            )

    return SummaryDraft(
        title=best_title,
        text=best_text,
        source_post_ids=source_ids,
        topic_ids=[],
        paragraphs=count_paragraphs(best_text),
        chars=count_chars(best_text),
        ai_flavor_score=ai_flavor_score(best_text),
    )


__all__ = [
    "polish_chinese",
    "strip_reasoning_prefix",
    "sanitize_comment",
    "ai_flavor_score",
    "fallback_text_only",
    "generate_post",
    "enrich_comment_target",
    "generate_comment",
    "generate_summary",
    "generate_images",
    "count_chars",
    "count_paragraphs",
    "clamp_chars",
    "fit_summary_shape",
    "build_summary_material",
    "pad_summary_to_min",
]
