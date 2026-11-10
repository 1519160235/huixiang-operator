# -*- coding: utf-8 -*-
"""系列常识：这款游戏属于哪个系列、谁是谁的续作、哪来的「本体」。

【真机缺陷 2026-11-10 用户报障「人家说这个 AI 连本体都没搞明白」】
帖子是《丝之歌》实体版手册曝光，我们连发两条：
    「不急，先把本体端上来再说吧」
    「等本体都等成化石了，现在居然连都有饼了」
在玩家眼里这两句等于「丝之歌还没发售」——而丝之歌 2025-09 就发了。
评论区当场炸了：「你是 ai 吗」「现在 AI 模仿傻子这么像了吗」「本体又来本传的」。

根因不是语气，是**世界知识**：
- 帖子里的「本体」指《丝之歌》这款游戏自己（相对于 DLC / 内容包）；
- 模型把「本体」理解成了《空洞骑士》初代（也就是它嘴里的「本传」）；
- 提示词只给了帖子文本和黑话表，从来没告诉它「这是哪个系列、谁是谁的续作」。

所以这里做两件事：
1. `build_hint()` —— 认到游戏就把该系列的常识塞进提示词，让它有据可依；
2. `guard_relations()` —— 认不到系列时，不许它用「本体 / 本传 / 前作 / 续作」这类
   相对说法。宁可不说，也不要瞎认亲戚。

知识库是 `data/series_kb.json`（人写的种子 + Steam 事实补充），
`format_for_prompt()` 输出给模型，`lookup()` 给代码判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 这些词全都隐含「我知道它和别的游戏是什么关系」。
# 认不到系列时用它们，就是这次的翻车方式。
RISKY_RELATION_TERMS = (
    "本体",
    "本传",
    "前作",
    "续作",
    "正统续作",
    "初代",
    "原版",
    "第一部",
    "上一代",
    "系列新作",
)

KB_FILE = "series_kb.json"


@dataclass
class Work:
    """系列里的一部作品。"""

    name: str = ""
    aliases: list[str] = field(default_factory=list)
    relation: str = ""      # 初代 / 续作 / 外传 / 重制 …
    year: str = ""          # 发售年份，未知留空
    note: str = ""          # 「独立续作，不是 DLC」这类关键说明

    def matches(self, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        for key in [self.name, *self.aliases]:
            if key and key.lower() in low:
                return True
        return False


@dataclass
class Series:
    """一个游戏系列。"""

    id: str = ""
    name: str = ""
    works: list[Work] = field(default_factory=list)
    notes: str = ""

    def find_work(self, text: str) -> Work | None:
        # 名字长的优先，避免「空洞骑士」截胡「空洞骑士：丝之歌」。
        for work in sorted(self.works, key=lambda w: len(w.name), reverse=True):
            if work.matches(text):
                return work
        return None


def _kb_path(ctx: Any = None) -> Path:
    """知识库路径：数据目录里的可覆盖，插件自带的兜底。

    【为什么要有回落】知识库是随插件分发的资源（`series_kb.json` 就在插件根目录），
    但用户可能想自己加词条。所以先看数据目录有没有自建/改过的版本，
    没有就用插件自带那份 —— 否则新装用户会读到 0 条（踩过：真机上就是 0）。
    """
    bundled = Path(__file__).resolve().parent.parent / KB_FILE
    paths = getattr(ctx, "paths", None) if ctx is not None else None
    data_dir = getattr(paths, "data_dir", None) or getattr(paths, "state_dir", None)
    if data_dir:
        override = Path(data_dir) / KB_FILE
        if override.exists():
            return override
    return bundled


def load(ctx: Any = None) -> list[Series]:
    """读知识库；文件不存在 / 坏了都返回空列表（不拖垮评论）。"""
    path = _kb_path(ctx)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Series] = []
    for item in raw.get("series") or []:
        if not isinstance(item, dict):
            continue
        works = [
            Work(
                name=str(w.get("name") or ""),
                aliases=[str(a) for a in (w.get("aliases") or []) if a],
                relation=str(w.get("relation") or ""),
                year=str(w.get("year") or ""),
                note=str(w.get("note") or ""),
            )
            for w in (item.get("works") or [])
            if isinstance(w, dict) and w.get("name")
        ]
        if not works:
            continue
        out.append(
            Series(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                works=works,
                notes=str(item.get("notes") or ""),
            )
        )
    return out


def lookup(ctx: Any, *texts: str) -> tuple[Series, Work] | None:
    """在给定文本里找这是哪一款游戏、属于哪个系列。"""
    blob = " ".join(str(t) for t in texts if t)
    if not blob.strip():
        return None
    for series in load(ctx):
        work = series.find_work(blob)
        if work is not None:
            return series, work
    return None


def build_hint(ctx: Any, *texts: str) -> str:
    """给提示词用的「本系列常识」段落；认不出来返回空串。

    输出尽量短：模型不需要百科，只需要不把亲戚认错。
    """
    found = lookup(ctx, *texts)
    if found is None:
        return ""
    series, work = found

    lines = [f"这条帖子说的是《{work.name}》，属于「{series.name}」系列。"]
    if work.relation or work.year:
        bits = [b for b in (work.relation, f"{work.year} 年发售" if work.year else "") if b]
        lines.append(f"它在本系列里的位置：{'，'.join(bits)}。")

    # 同一系列的其他作品，帮它认清「本体 / 前作」到底指谁。
    others = [w for w in series.works if w.name != work.name]
    if others:
        lines.append(
            "同系列还有："
            + "；".join(
                f"{w.name}（{w.relation}{'，' + w.year + ' 年' if w.year else ''}）"
                for w in others
            )
            + "。"
        )
    for w in series.works:
        if w.note and w is work:
            lines.append(f"关于这一部：{w.note}")
    if series.notes:
        lines.append(f"系列背景：{series.notes}")

    lines.append(
        "**说「本体」「前作」「续作」之前先确认指哪一部**："
        "这里的「本体」通常指上面提到的那款已经发售的游戏本身，不是它的前作。"
    )
    return "\n".join(lines)


def has_risky_terms(text: str) -> bool:
    t = str(text or "")
    return any(term in t for term in RISKY_RELATION_TERMS)


def guard_relations(text: str, known: bool) -> str:
    """认不到系列却仍写了「本体 / 前作 / 续作」时，整条作废。

    【为什么整条丢，而不是切掉那半句】
    试过按分句切，结果是「等本体都等成化石了，现在居然连都有饼了」
    → 切完剩「现在居然连都有饼了」，残句比不说更露怯。
    评论本来就短，丢掉这一条最多是少说一句；发出去却是当众出丑。

    认到系列（known=True）时一律放行 —— 那时候它说的话有依据。
    """
    t = str(text or "").strip()
    if not t or known:
        return t
    if has_risky_terms(t):
        return ""
    return t


__all__ = [
    "RISKY_RELATION_TERMS",
    "Series",
    "Work",
    "build_hint",
    "guard_relations",
    "has_risky_terms",
    "load",
    "lookup",
]
