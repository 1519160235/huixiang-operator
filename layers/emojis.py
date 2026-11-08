# -*- coding: utf-8 -*-
"""小黑盒表情白名单与清洗。

【真机缺陷 2026-11-08 用户报障「刷小黑盒回复有时候表情加载不出来」】

根因（已在真机验证）：
- 小黑盒评论接口 `/bbs/app/link/tree` 返回的评论**没有** `emojis` 字段，
  服务端只把 `[cube_xxx]` 当普通文本原样存回，解析与渲染全在客户端完成；
- 客户端按「表情名」查内置表情表（雪碧图 cube_emoji_vN.png + background-position），
  名字查不到就不画图 —— 用户看到的就是「表情加载不出来」；
- 本插件提示词原先只给了 3 个示例（`[cube_喜欢]` / `[cube_doge]` / `[cube_捂脸哭]`），
  模型会自由发挥编造 `[cube_无语]`、`[cube_吃瓜看戏]` 这类不存在的名字，于是「有时候」坏。

【白名单怎么来的】
2026-11-08 用真机登录态扫了 60 个真实帖子、1606 条真实评论，
统计出社区实际在用的 57 个表情名（共 1058 次）。这些名字都由真实用户从小黑盒
表情面板发出、在客户端能正常显示，属于「已被验证存在」的集合。
只收这批，不臆造、不猜测官方全集。

【策略】
- 提示词里按情绪分组给出可选清单，要求「只能从这里选」；
- 发送前再兜底一次：白名单外的标记直接删掉（宁可不带表情，也不留坏表情）；
- 同时把全角括号、缺括号等错误写法归一化，避免残渣。

【白名单会过期，所以分两层】
- 内置 57 个：代码里的兜底，保证任何时候都有可用集合；
- 运行时扩充：`emoji_whitelist.json` 存自动学到的新表情（小黑盒上新表情后，
  社区会先用起来，扫真实评论就能采到），启动时加载、worker 定期刷新。
  只增不减 —— 老表情不会因为一次采样没出现就被判定失效。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 实测白名单：60 个真实帖子 / 1606 条评论里出现过的全部表情名（按频次降序）。
# 增删请以真机采样为准，不要凭印象加。
_ALLOWED_ORDER: tuple[str, ...] = (
    "cube_鳄鱼", "cube_惊讶", "cube_笑cry", "cube_喜欢", "cube_滑稽", "cube_捂脸哭",
    "cube_哭泣", "cube_doge", "cube_摘墨镜", "cube_僵尸", "cube_感动", "cube_欧润吉",
    "cube_超人", "cube_沧桑", "cube_开心", "cube_握草", "cube_微笑", "cube_抱拳",
    "cube_打脸", "cube_黑人问号", "cube_生气", "cube_预言", "cube_酷", "cube_并不简单",
    "cube_闭嘴", "cube_吐", "cube_吃瓜", "cube_汗", "cube_乖", "cube_菜doge",
    "cube_窝囊", "cube_委屈", "cube_加油", "cube_困", "cube_比心", "cube_点赞",
    "cube_摸摸头", "cube_小鸡", "cube_电牛", "cube_哇", "cube_怒", "cube_凄凉",
    "cube_傲娇助手", "cube_认真记录", "cube_吓", "cube_晕", "cube_wota", "cube_喷水",
    "cube_上学-乐", "cube_赞", "cube_坏笑", "cube_爱心", "cube_上学-丧", "cube_笑哭",
    "cube_害羞", "cube_悟空", "cube_鹅",
)

ALLOWED: frozenset[str] = frozenset(_ALLOWED_ORDER)

# 运行时学到的表情（从真实评论采样得来），与内置集合取并集使用。
_EXTRA: set[str] = set()
WHITELIST_FILE = "emoji_whitelist.json"


def allowed_names() -> frozenset[str]:
    """当前生效的白名单（内置 ∪ 运行时扩充）。"""
    return ALLOWED if not _EXTRA else frozenset(ALLOWED | _EXTRA)


def load_extra(data_dir: str | Path | None) -> int:
    """从数据目录加载运行时扩充的表情名，返回加载条数。"""
    global _EXTRA
    if not data_dir:
        return 0
    path = Path(data_dir) / WHITELIST_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    names = payload.get("names") if isinstance(payload, dict) else payload
    if not isinstance(names, list):
        return 0
    cleaned = {str(n).strip() for n in names if str(n).strip().startswith("cube_")}
    _EXTRA = cleaned
    return len(cleaned)


def merge_allowed(names: list[str], data_dir: str | Path | None = None) -> list[str]:
    """把新发现的表情并入白名单（只增不减），返回真正新增的那些。"""
    global _EXTRA
    incoming = {str(n).strip() for n in names if str(n).strip().startswith("cube_")}
    fresh = sorted(incoming - ALLOWED - _EXTRA)
    if not fresh:
        return []
    _EXTRA |= incoming - ALLOWED
    if data_dir:
        path = Path(data_dir) / WHITELIST_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "names": sorted(_EXTRA),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "sampled_from_real_comments",
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 落盘失败不影响本次运行（内存里已经生效）。
    return fresh

# 提示词里给模型看的清单：只挑语义清楚、适合评论场景的常用项，按情绪分组。
# 冷门活动表情（上学-乐 / 傲娇助手 / wota 等）不给模型，避免它硬凑。
EMOJI_HINT = (
    "开心 / 好笑：`[cube_笑cry]` `[cube_滑稽]` `[cube_doge]` `[cube_开心]` `[cube_坏笑]`\n"
    "喜欢 / 认可：`[cube_喜欢]` `[cube_爱心]` `[cube_比心]` `[cube_点赞]` `[cube_抱拳]` `[cube_酷]`\n"
    "无奈 / 委屈：`[cube_捂脸哭]` `[cube_委屈]` `[cube_哭泣]` `[cube_汗]` `[cube_窝囊]` `[cube_黑人问号]`\n"
    "惊讶 / 围观：`[cube_惊讶]` `[cube_握草]` `[cube_哇]` `[cube_吃瓜]` `[cube_并不简单]`\n"
    "吐槽 / 生气：`[cube_生气]` `[cube_打脸]` `[cube_吐]` `[cube_僵尸]` `[cube_欧润吉]` `[cube_沧桑]`\n"
    "其他：`[cube_感动]` `[cube_超人]` `[cube_鳄鱼]` `[cube_加油]` `[cube_乖]` `[cube_困]`"
)

# 各种括号写法都认（模型偶尔会用全角括号或圆括号）。
_BRACKETED_RE = re.compile(
    r"[\[【（(]\s*(cube_[A-Za-z0-9_\-\u4e00-\u9fff]{1,24})\s*[\]】）)]"
)
# 残渣兜底：括号缺失 / 只写了一半的 cube_ 片段（含空格变体）。
_RESIDUE_RE = re.compile(r"[\[【（(]?\s*cube_[A-Za-z0-9_\-\u4e00-\u9fff]{0,24}\s*[\]】）)]?")

# 删除标记后留下的多余空格（表情前后常见 `  [cube_x] `）。
_SPACE_AROUND_RE = re.compile(r"[ \t]{2,}")


def normalize_marks(text: str) -> str:
    """把各种括号写法统一成 `[cube_xxx]`（只做形态归一，不做合法性判断）。"""
    return _BRACKETED_RE.sub(lambda m: f"[{m.group(1)}]", str(text or ""))


def sanitize_marks(text: str, *, max_marks: int = 1) -> str:
    """只保留白名单内的表情标记，其余删除，并限制条数。

    - 白名单外：删（不猜、不映射 —— 猜错名字同样渲染不出来）；
    - 超过 `max_marks`：删掉多余的；
    - 半残写法（缺括号 / 空格）：一并清理，避免在小黑盒里露出 `[cube_` 这种残渣。
    """
    t = normalize_marks(text)

    kept: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in allowed_names() and len(kept) < max_marks:
            kept.append(name)
            return f"\u0000{len(kept) - 1}\u0000"
        return ""

    t = _BRACKETED_RE.sub(_hold, t)
    # 合法标记已换成占位符，剩下的 cube_ 片段一律是残渣，抹掉。
    t = _RESIDUE_RE.sub("", t)

    for i, name in enumerate(kept):
        t = t.replace(f"\u0000{i}\u0000", f"[{name}]")

    # 表情被删后可能留下双空格 / 行尾空格，收一下。
    t = _SPACE_AROUND_RE.sub(" ", t)
    return t.strip()


def extract_marks(text: str) -> list[str]:
    """取出文本里的表情名（调试 / 统计用，不做过滤）。"""
    return [m.group(1) for m in _BRACKETED_RE.finditer(str(text or ""))]
