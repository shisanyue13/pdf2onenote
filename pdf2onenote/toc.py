# -*- coding: utf-8 -*-
"""
章节结构识别。策略按可靠性排序，识别到就停：

  1) PDF 书签（outline）
  2) 文本层目录页 —— 有文本层的 PDF：读目录页 -> 按左缩进聚类分出章/节 -> 算页码偏移
  3) OCR 目录页   —— 扫描件：先 OCR 出目录页，再走同一套解析
  4) 固定页数     —— 兜底

层级判定用「目录行的左缩进」聚类，而不是正则硬匹配，
所以教材（第X章 / §）和文件汇编（文章标题 / 一、二、）都能认。
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

# 罗马数字页码必须跟前面有分隔：否则英文标题的尾字母会被当成页码
# （Index 的 x、Polynomial 的 l 都是罗马数字，整词会被啃掉一个字母）
RE_TAIL_NUM = re.compile(
    r"([0-9]{1,4}|(?<=[\s.·．•・…‥])[ivxlcdm]{1,7})\s*$", re.I)
RE_DOT = re.compile(r"[.·．•・…‥]{2,}|\s{3,}")

# 目录里「章号独占一行、标题在下一行」的写法：Chapter 1 / 第一章
# 注意：Part I / Appendix A 本身就是完整标题，绝不能和下一行合并，
# 否则会把真正的章（紧跟在 Part 后面的那一章）吞掉，导致整段章节丢失。
RE_HEAD_ONLY = re.compile(
    r"^(Chapter|CHAPTER"
    r"|第\s*[0-9一二三四五六七八九十百]+\s*[章篇讲])"
    r"\s*([0-9IVXLC一二三四五六七八九十]*)\s*$")

# 明确的章 / 附录，命中就是 L1
RE_CHAPTER = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章篇编讲]"
    r"|附\s*[录件]\s*[0-9一二三四五六七八九十]?|附录|附件|索\s*引|参考文献"
    r"|习题答案|习题解答|答案与提示|部分习题"
    r"|Chapter\s+[0-9IVXLC]+|CHAPTER\s+[0-9IVXLC]+"
    r"|Part\s+[0-9IVXLC]+|PART\s+[0-9IVXLC]+)",
    re.I,
)
# 小节特征：一、 / （一） / 1.1 / §1 / $1 / 82（OCR 把 § 认成的样子）
RE_SECTION = re.compile(
    r"^\s*([（(][一二三四五六七八九十]+[)）]"
    r"|[一二三四五六七八九十]+\s*[、.]"
    r"|[0-9]+\.[0-9]+"
    r"|[§$]\s*[0-9一二三四五六七八九十]+"
    r"|8[0-9一二三四五六七八九十](?=[^0-9]))"
)
RE_CN_NO = re.compile(r"第\s*([0-9一二三四五六七八九十百零〇]{1,3})\s*[章篇]")
# 无歧义的中文章标记：「第X章 / 第X篇 / 第X编 / 第X讲」。命中即可定为章，
# 不依赖左缩进——扫描件 OCR 后章标题左边界 x 会在 40~85 大幅抖动（实测一半章在 x≈44、
# 另一半在 x≈80），缩进聚类会把大半章并进小节层级整章丢失。
RE_CN_CHAPTER = re.compile(
    r"^\s*第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章篇编讲](?!\s*的)")

# 纯数字 / 罗马数字编号的章（"1 引言"、"I 数据压缩"），没有 "Chapter / 第X章" 字样。
# 仅靠左缩进聚类时，这类章的缩进常和上面的小节相同（换页后缩进错位），会被误判成小节，
# 所以单独用「数字+空格+文字」的章号前缀直接定为 L1。带小数点的 "1.1" 是节，已在 RE_SECTION 处理。
RE_CHAPTER_NUM = re.compile(r"^\s*[0-9]{1,3}\s+[一-鿿A-Za-z]")
RE_CHAPTER_ROMAN = re.compile(r"^\s*[IVXLC]+\s+[一-鿿A-Za-z]")

# 节下面的附属行，不是章节
STOP_TITLES = {
    "习题", "练习", "小结", "思考题", "本章小结", "习题课", "复习题",
    "目录", "contents", "table of contents",
}
# 书签里每章重复出现的「附属小节」：不是独立章节，切出来只会是一堆碎文件。
# （文本通道用 RE_TOC_SUBITEM 做同一件事）
STOP_SUBTITLES = {
    "exercises", "exercise", "problems", "problem set", "review questions",
    "summary", "summaries", "key terms", "further reading", "additional problems",
    "answers to exercises", "solutions", "solutions to exercises",
    "notes", "chapter summary", "learning objectives",
    "习题", "练习", "小结", "本章小结", "思考题", "复习题",
}
# 书签里这些不是章节
STOP_WORDS = {
    "封面", "书名", "版权", "前言", "序言", "序", "目录", "封底", "扉页",
    "出版说明", "内容提要", "编者的话", "出版者的话", "鸣谢", "致谢",
    "cover", "title", "copyright", "contents", "preface",
}

# 文本通道里这些不算「章」——它们是正文前/后的附属页，且常因罗马数字页码与阿拉伯
# 数字正文页码混用导致排序错乱（例如 Preface/Acknowledgements 被排到 Chapter 1 之后）。
# 正文里的真章、附录、索引、参考文献、答案页等仍保留。
FRONT_MATTER = {
    "contents", "content", "preface", "prefaces", "foreword", "forewords",
    "acknowledgements", "acknowledgments", "dedication", "dedications",
    "序", "序言", "前言", "目录", "致谢", "扉页", "献词", "题献",
}


@dataclass
class Entry:
    level: int                 # 1 = 章/篇，2 = 节
    title: str
    page: int                  # PDF 页序，1-based
    printed: int | None = None
    front: bool = False        # True = 「正文前附属页」这一条（只由 build_plan 注入）


# ---------------------------------------------------------------- 基础工具
def _roman2int(s: str) -> int | None:
    s = s.lower()
    if not s or any(c not in ROMAN for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = ROMAN[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def _to_int(tok: str) -> int | None:
    tok = tok.strip()
    return int(tok) if tok.isdigit() else _roman2int(tok)


CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_DIGIT.get(a, 1) if a else 1) * 10 + (CN_DIGIT.get(b, 0) if b else 0)
    total = 0
    for c in s:
        if c not in CN_DIGIT:
            return None
        total = total * 10 + CN_DIGIT[c]
    return total or None


def chapter_no(title: str) -> int | None:
    m = RE_CN_NO.search(title)
    return cn2int(m.group(1)) if m else None


def _title_ok(title: str) -> bool:
    """
    标题长度上限（噪声过滤用）。正文碎片 / 整段文字会被当成噪声丢掉。

    按字符集分别给上限：
      - 中文标题：50 字已非常长（章节标题极少超过）；
      - 英文标题：120 字符仍属正常（"CHAPTER 11 Reactions of Alkyl Halides:
        Nucleophilic Substitutions and Eliminations" 就有 82 字符），给足余量。
    注意：长度只是「噪声过滤」的第一道关，真正进入章节列表还要求标题
    命中 RE_CHAPTER / RE_SECTION / RE_BACK 等章样模式，所以放宽上限不会
    把正文段落误收为章节。
    """
    n = len(title)
    if n < 2:
        return False
    ascii_ratio = sum(1 for c in title if ord(c) < 128) / n
    return n <= (120 if ascii_ratio > 0.8 else 50)


def _clean_title(s: str) -> str:
    s = RE_DOT.sub(" ", s).strip(" .·． ")
    s = re.sub(r"^(第[0-9一二三四五六七八九十百]+[章篇编讲])\s*", r"\1 ", s)
    return s.strip()


# ---------------------------------------------------------------- 页面行提取
def _rows_xy(doc, idx: int) -> list[tuple[float, float, str]]:
    """文本层：按行取 (左边界 x, 上边界 y, 文本)，单位是 PDF 点"""
    out = []
    try:
        data = doc[idx].get_text("dict")
    except Exception:
        return out
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            if text:
                out.append((min(s["bbox"][0] for s in spans),
                            min(s["bbox"][1] for s in spans), text))
    return out


RE_BARE_NUM = re.compile(r"^[\s.·．•・…‥]*([0-9]{1,4}|[ivxlcdm]{1,7})\s*$", re.I)


def _join_lines(rows, y_tol: float = 5.0) -> list[tuple[float, str]]:
    """
    把同一视觉行上的文本框并成一行。

    目录里的印刷页码常常是独立的文本框（排版软件把页码单独放），
    跟标题不在同一个行对象里，于是「Vector Spaces」和「1」被拆成两行，
    标题那一行就没有页码、整条被丢掉。这里按 y 邻近把它们拼回去。

    只拼「一边是纯页码、另一边不是」的组合——双栏目录里左右两列
    常常恰好同一 y，不加这个限制会把两列并成一行。
    """
    groups: list[dict] = []
    for x, y, t in rows:
        is_num = bool(RE_BARE_NUM.match(t))
        hit = None
        for g in groups:
            if abs(g["y"] - y) > y_tol:
                continue
            if any(is_num != bool(RE_BARE_NUM.match(pt)) for _px, pt in g["parts"]):
                hit = g
                break
        if hit is None:
            groups.append({"y": y, "parts": [(x, t)]})
        else:
            hit["parts"].append((x, t))

    out = []
    for g in groups:
        parts = sorted(g["parts"])
        out.append((parts[0][0], " ".join(t for _, t in parts)))
    return out


def rows_from_text(doc, idx: int) -> list[tuple[float, str]]:
    return _join_lines(_rows_xy(doc, idx))


def _render(doc, idx: int, dpi: int = 150, clip=None):
    import numpy as np

    pix = doc[idx].get_pixmap(dpi=dpi, clip=clip)
    if pix.n == 1:
        return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return img[:, :, ::-1]                      # RGB -> BGR


def _merge_rows(items, y_tol=12, scale=72.0 / 150):
    """OCR 的词级 box 按 y 聚成行，返回 [(x, text)]，x 换算成 PDF 点"""
    rows: list[dict] = []
    for box, text, _score in items:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx, cy = sum(xs) / 4, sum(ys) / 4
        for r in rows:
            if abs(r["y"] - cy) <= y_tol:
                r["parts"].append((cx, text))
                break
        else:
            rows.append({"y": cy, "parts": [(cx, text)]})
    rows.sort(key=lambda r: r["y"])
    out = []
    for r in rows:
        r["parts"].sort()
        out.append((r["parts"][0][0] * scale, "".join(t for _, t in r["parts"])))
    return out


def _ocr_rows(engine, img, dpi=150) -> list[tuple[float, str]]:
    """对一页图做 OCR。关掉角度分类器（use_cls=False）：目录行都是水平的，
    分类器只是白花时间；老版本 RapidOCR 不接受参数时退回默认调用。"""
    try:
        res, _ = engine(img, use_det=True, use_cls=False, use_rec=True)
    except TypeError:
        res, _ = engine(img)
    if not res:
        return []
    return _merge_rows(res, scale=72.0 / dpi)


# ---------------------------------------------------------------- OCR 结果缓存
def _ocr_cache_file(pdf_path: str, dpi: int, idx: int) -> str | None:
    """按「文件路径+大小+修改时间+dpi」做键，把每页 OCR 结果缓存到磁盘。

    同一本书反复识别（先 --dry-run 再切分、网页里切换切分路径）时，
    第二次开始完全不用再跑 OCR。
    """
    try:
        st = os.stat(pdf_path)
        raw = f"{os.path.abspath(pdf_path)}|{st.st_size}|{int(st.st_mtime)}|{dpi}".encode("utf-8")
        key = hashlib.sha1(raw).hexdigest()[:16]
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
                or os.path.expanduser("~/.cache"))
        d = os.path.join(base, "pdf2onenote", "ocr", key)
        return os.path.join(d, f"p{idx}.json")
    except Exception:
        return None


def _load_ocr_cache(path: str | None):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(float(x), str(t)) for x, t in data]
    except Exception:
        return None


def _save_ocr_cache(path: str | None, rows) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([[round(float(x), 2), t] for x, t in rows], f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 目录页定位
def _toc_score(rows) -> int:
    return sum(1 for _, t in rows if RE_TAIL_NUM.search(t) and len(t) > 4)


def _pick_toc_pages(scores: dict, total: int) -> list[int]:
    """取得分最高的连续页段"""
    if not scores:
        return []
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 3:
        return []
    thr = max(2, scores[best] // 4)
    lo = best
    while lo - 1 >= 0 and scores.get(lo - 1, 0) >= thr:
        lo -= 1
    hi = best
    # 旧实现把段尾硬限在 best+10，目录超过 11 页的书会被截断；这里放宽到 40 页
    while hi + 1 < min(total, best + 40) and scores.get(hi + 1, 0) >= thr:
        hi += 1
    return list(range(lo, hi + 1))


# ---------------------------------------------------------------- 解析
def _cluster_groups(xs: list[float], titles=None, gap: float = 8.0) -> list[list[int]]:
    """把左边界聚成若干层，返回**每层的成员下标**（按 x 升序）。

    返回成员而不是只返回中心：调用方要按成员取条目。只按「距均值中心 < gap」
    取条目时，簇被合并后中心会偏移，刚并进来的条目会落在 gap 之外而凭空消失。
    """
    if not xs:
        return []
    if titles is None:
        titles = [None] * len(xs)

    idxs = sorted(range(len(xs)), key=lambda i: xs[i])
    groups = [[idxs[0]]]
    for i in idxs[1:]:
        if xs[i] - xs[groups[-1][-1]] > gap:
            groups.append([i])
        else:
            groups[-1].append(i)

    def _has_chapter(g):
        return any(
            t is not None and (
                RE_CHAPTER.match(t) or (
                    not RE_SECTION.match(t) and RE_CHAPTER_LIKE.match(t)
                )
            ) for t in (titles[i] for i in g)
        )

    if len(groups) > 2:
        # 用绝对条数而不是比例：章本来就比节下面的条目少得多，
        # 按比例判会把真正的章当成噪声剔掉
        thr = max(3, int(len(xs) * 0.02))
        # 孤立小簇只有在「含 >=2 个章样条目」时才算真实的续章/独立栏；
        # 单条噪声行不再被豁免（否则它会劫持章层，把真章整本丢掉）
        kept = [g for g in groups if len(g) >= thr or (len(g) >= 2 and _has_chapter(g))]
        if kept and len(kept) < len(groups):
            for g in groups:
                if g in kept:
                    continue
                cx = sum(xs[i] for i in g) / len(g)
                tgt = min(kept, key=lambda k: abs(sum(xs[i] for i in k) / len(k) - cx))
                tgt.extend(g)
                tgt.sort(key=lambda i: xs[i])
            groups = kept

    return groups[:3]


def _cluster_levels(xs: list[float], titles=None, gap: float = 8.0) -> list[float]:
    """
    把左边界聚成 1~3 个层级中心。

    只有一两条的孤立簇通常是排版噪声，会被并进最近的簇，否则会凭空多撑出
    一个层级、把真正的小节挤到第三级去。

    **例外**：若这个孤立簇里含有多条章样条目（如双栏目录里只有少数章的那一栏，
    或续章栏），它就不是噪声而是真实的「续章」或独立栏，必须保留为独立层级，
    交给 parse_toc_rows 的双栏续章逻辑去判断——否则这些章会被「合并后超出
    gap 容差」而整栏丢失（双栏续章的 2 条小簇就曾因此消失）。
    """
    groups = _cluster_groups(xs, titles, gap)
    return [sum(xs[i] for i in g) / len(g) for g in groups]


# 章标题独占行之后、真正的页码之前，不该被并进标题的「下一条目」：
#   - 小节（N.M / （一） / 8N 等）
#   - 每章重复的附属行（Why This Chapter? / Key Terms / Summary / Additional Problems / Chemistry Matters）
# 命中这些就说明「标题已经结束、页码也读完了」，不能再往下吞。
RE_TOC_SUBITEM = re.compile(
    r"^\s*([0-9]+\.[0-9]+|（[一二三四五六七八九十]+）|[§$]\s*[0-9]+)"
    r"|^\s*(Why\s+This\s+Chapter\?|Key\s+Terms|Summary|Summary\s+of\s+Reactions"
    r"|Additional\s+Problems|Chemistry\s+Matters|Access\s+for\s+free|Glossary)",
    re.I,
)


def _merge_head_lines(rows):
    """「Chapter 1」/「CHAPTER 1」独占一行时，把章号、章标题（可换行）、页码并成一条。

    要点（修过的坑）：
      - 章号本身是标题的一部分，绝不能当成页码抠掉；
      - 页码有时在更靠右的独立文本框里（"CHAPTER 1" / 标题行 / 远右 "7" 三行），
        要一并抓回来；
      - 章标题可能**换行**（CHAPTER 12/13 这种长标题折成两行），
        旧逻辑只并一行标题，导致折行标题被拆成「半截章 + 孤立页码」，
        真实章节整章丢失。这里贪心地往下吞标题行，直到碰到页码 /
        下一个章头 / 小节 / 每章重复的附属行才停。
    形如：
        'CHAPTER 1'                              ->  'CHAPTER 1 Introduction to Psychology 7'
        'Introduction to Psychology'
        '7'        （远右页码框）
        'CHAPTER 12'                             ->  'CHAPTER 12 Structure Determination:
        'Structure Determination: Mass ...'           Mass Spectrometry and Infrared Spectroscopy 383'
        'Spectroscopy'
        '383'
    若 _join_lines 已把页码并进标题行，则标题行里直接带数字，效果一致。
    """
    out = []
    i, n = 0, len(rows)
    while i < n:
        x, raw = rows[i]
        s = raw.strip()
        if RE_HEAD_ONLY.match(s):
            title_parts = []
            page = None
            j = i + 1
            first = True
            # 贪心吞标题：遇到纯页码 / 下一个章头 / 小节 / 附属行就停
            while j < n and len(title_parts) < 6:
                xj, rawj = rows[j]
                t = rawj.strip()
                if RE_BARE_NUM.match(t) and len(t) <= 5:
                    page = t
                    j += 1
                    break
                if RE_HEAD_ONLY.match(t):        # 下一个章头，标题到此为止
                    break
                if not first and RE_TOC_SUBITEM.match(t):
                    break
                title_parts.append(t)
                first = False
                j += 1
            title = " ".join(title_parts).strip()
            if title:
                merged = f"{s} {title}"
                if page:
                    merged = f"{merged} {page}"
                out.append((x, merged))
                i = j
                continue
            out.append((x, s))                    # 章头后无任何标题，原样保留
            i += 1
            continue
        out.append((x, raw))
        i += 1
    return out


# 章样标题：纯数字编号「1 Introduction」或罗马数字编号「I Data Compression」
# （无小数点；带小数点的 N.M 是节，已由 RE_SECTION 处理）。
RE_CHAPTER_LIKE = re.compile(r"^\s*([0-9]{1,3}|[IVXLC]+)\s+[A-Za-z一-鿿]")
# 书末附页（与章节同等保留）：索引 / 参考文献 / 附录 / 答案等。
RE_BACK = re.compile(
    r"^\s*(Bibliography|Index|References|Answer\s*Key|Glossary|Appendix|Appendices"
    r"|索引|参考文献|附录|答案|词汇表|参考书目)",
    re.I,
)
# 字母编号的附录（"A Lie Groups"）：排除 I/V/X/L/C 以免与罗马数字章混淆。
RE_APPENDIX_LETTER = re.compile(r"^\s*[A-HJ-Z]\s+[A-Z一-鿿]")
# 「篇」这一层：书签是 Part > Chapter > Section 三层时，顶层是篇、章在下一层。
RE_PART_TITLE = re.compile(r"^\s*(Part|PART)\b|^\s*第\s*[0-9一二三四五六七八九十]+\s*篇")


def parse_toc_rows(rows, max_level: int = 2, max_page: int | None = None):
    """
    rows: [(x, text)] —— 已按视觉行合并好的目录行
    max_level: 1 = 只出章；2 = 章 + 节（节由 RE_SECTION 识别）
    max_page: 全书页数，用于把「行尾的年份」这类假页码挡掉
    返回 (有页码的 (level, title, printed), 无页码的 (level, title))

    判定「章」的两条互补规则：
      1) 多缩进层级（章比节更靠左）：取【最左、且含任何章样条目】的簇，簇内条目即章；
      2) 单缩进层级（整本左对齐，靠显式标记/编号区分）：只保留章样条目与书末附页，
         N.M 小节、正文前附属页、重复的小节标题自然被排除。
    若单层级几乎没有显式章、却存在大量 N.M 小节，则用 N.M 的整数部分反推章。
    小节（RE_SECTION）只在 max_level >= 2 时作为 level=2 收进结果。
    """
    rows = _merge_head_lines(rows)

    cands = []
    for x, raw in rows:
        m = RE_TAIL_NUM.search(raw)
        printed = _to_int(m.group(1)) if m else None
        if m and printed is None:
            m = None
        title = _clean_title(raw[: m.start()] if m else raw)
        if printed is not None and max_page is not None and printed > max_page:
            # 行尾数字比全书页数还大 → 它是标题的一部分（如 "Chapter 4: The 2024"），
            # 不是页码。拿它去算偏移会得到越界页，这一章会被整条丢掉。
            m = None
            printed = None
            title = _clean_title(raw)
        if not _title_ok(title):
            continue
        # 没页码又没章节样子的行，多半是正文碎片，丢掉
        if printed is None and not (RE_CHAPTER.match(title) or RE_SECTION.match(title)):
            continue
        if title.lower() in STOP_TITLES:
            continue
        cands.append((x, title, printed))

    if not cands:
        return [], []

    norm = lambda t: re.sub(r"\s+", " ", t.strip().lower())
    freq = Counter(norm(t) for _, t, _ in cands)   # 全局「同标题出现次数」，用于剔除重复小节

    def is_chapter_title(t):
        if RE_CHAPTER.match(t):
            return True
        if RE_SECTION.match(t):
            return False
        return bool(RE_CHAPTER_LIKE.match(t))

    groups = _cluster_groups([x for x, _, _ in cands], [t for _, t, _ in cands])

    # 中文「第X章」编号的书：第X章 是无歧义章标记，出现 >=3 个即认定为显式章编号，
    # 直接走下方「单层级·显式标记」筛选（第X章 + 书末附页全部保留为 L1），不再依赖
    # 会因 OCR 左缩进抖动（章标题 x 在 40~85 跳变）而把大半章并进小节层级的缩进聚类。
    explicit_cn = sum(1 for _, t, _ in cands if RE_CN_CHAPTER.match(t)) >= 3

    def _add(level, title, printed, numbered, unnumbered):
        """收集一条条目；没有页码的走 unnumbered（只用于补首章/记录）"""
        if printed is None:
            unnumbered.append((level, title))
        else:
            numbered.append((level, title, printed))

    def _collect_sections(entries, numbered, unnumbered, exclude=()):
        """max_level >= 2 时把小节收成 level=2（跨栏去重、剔除重复附属行）"""
        seen = set(exclude)
        for x, title, printed in entries:
            if not RE_SECTION.match(title):
                continue
            t = norm(title)
            if t in seen:
                continue
            if freq.get(t, 0) >= 3:
                continue
            if title.lower() in FRONT_MATTER:
                continue
            seen.add(t)
            _add(2, title, printed, numbered, unnumbered)

    if len(groups) >= 2 and not explicit_cn:
        # 多缩进层级。要区分三种情形：
        #   (a) 缩进小节：节比章更靠右、且章号复用章号（章 x=148.5、节 x=163.4）；
        #   (b) 双栏续章：右栏是左栏的续章，章号与左栏【不重叠】（章 1-4 在 x=72、
        #       5-8 在 x=143.7）；
        #   (c) 罗马数字 Part 内含阿拉伯数字章（Part 在 x=65、Chapter 在 x=88.3）。
        # 章簇 = 最左章簇 + 章号集合与之【不重叠】的右栏续章；若同时存在阿拉伯数字章与
        # 纯罗马数字 Part，丢弃纯 Part 簇避免重复切分。
        def _entries(g):
            return [cands[i] for i in g]

        chapter_g = None
        for g in groups:
            if any(is_chapter_title(t) for x, t, _p in _entries(g)):
                chapter_g = g
                break
        if chapter_g is None:
            chapter_g = max(
                groups,
                key=lambda g: sum(1 for x, t, _p in _entries(g) if is_chapter_title(t)))

        def _chapterlike(entries):
            return [(x, t, p) for x, t, p in entries
                    if is_chapter_title(t) or RE_BACK.match(t) or RE_APPENDIX_LETTER.match(t)]

        def _numbers(entries):
            nums = set()
            for x, t, p in entries:
                # (?![A-Za-z0-9])：否则 "Chapter 3" 的 C、"Index" 的 I 会被当成罗马数字，
                # 双栏续章判据会误判「章号重叠」而丢掉整栏
                m = re.match(r"^\s*([0-9]{1,3}|[IVXLC]+)(?![A-Za-z0-9])", t)
                if m:
                    nums.add(m.group(1).upper())
            return nums

        def _has_arabic(entries):
            return any(re.match(r"^\s*[0-9]{1,3}", t) for x, t, p in entries)

        def _only_back(g):
            """整簇都是书末附页——不该被「只留阿拉伯章」规则删掉"""
            ent = _chapterlike(_entries(g))
            return bool(ent) and all(RE_BACK.match(t) or RE_APPENDIX_LETTER.match(t)
                                     for x, t, p in ent)

        base_nums = _numbers(_chapterlike(_entries(chapter_g)))
        kept = [chapter_g]
        for g in groups:
            if g == chapter_g:
                continue
            ent = _chapterlike(_entries(g))
            if not ent or (_numbers(ent) & base_nums):
                continue                    # 空簇 / 章号重叠（缩进小节）→ 丢弃
            kept.append(g)                  # 章号不重叠 → 双栏续章，保留

        if any(_has_arabic(_chapterlike(_entries(k))) for k in kept):
            kept = [k for k in kept
                    if _has_arabic(_chapterlike(_entries(k))) or _only_back(k)]

        kept_entries = [e for k in kept for e in _chapterlike(_entries(k))]
        freq = Counter(norm(t) for x, t, p in kept_entries)
        numbered, unnumbered = [], []
        for x, title, printed in kept_entries:
            tl = title.lower()
            if tl in FRONT_MATTER or freq.get(tl, 0) >= 3:
                continue
            _add(1, title, printed, numbered, unnumbered)
        if max_level >= 2:
            _collect_sections([e for g in groups for e in _entries(g)],
                              numbered, unnumbered,
                              exclude={norm(t) for x, t, p in kept_entries})
        return numbered, unnumbered

    # 单缩进层级：只保留「章样」条目与书末附页；其余（N.M 小节、前言、重复小节）丢弃。
    nm_re = re.compile(r"^\s*([0-9]+)\.([0-9]+)\b")
    has_explicit = any(RE_CHAPTER.match(t) for _, t, _ in cands)
    nm_groups = {}
    for x, t, p in cands:
        mm = nm_re.match(t)
        if mm:
            n = int(mm.group(1))
            if n not in nm_groups or (p is not None and p < nm_groups[n][0]):
                nm_groups[n] = (p, t)

    numbered, unnumbered = [], []
    for x, title, printed in cands:
        if not (is_chapter_title(title) or RE_BACK.match(title)):
            if max_level >= 2 and RE_SECTION.match(title):
                if freq.get(norm(title), 0) >= 3 or title.lower() in FRONT_MATTER:
                    continue
                _add(2, title, printed, numbered, unnumbered)
            continue
        # 单缩进层级里，章与小节左对齐混在一起，只能靠「同标题出现 >=3 次」剔除
        # 重复的小节（如每章末尾的 References、Key Terms）。
        if freq.get(norm(title), 0) >= 3:
            continue
        if title.lower() in FRONT_MATTER:
            continue
        _add(1, title, printed, numbered, unnumbered)

    # 兜底：单层级且几乎没有显式章，却存在大量 N.M 小节 —— 用整数部分反推章。
    if len(numbered) < 2 and not has_explicit and len(nm_groups) >= 3:
        front_back = {"preface", "foreword", "acknowledgements", "dedication",
                      "references", "bibliography", "index", "glossary",
                      "answer key", "contents"}
        numbered = [(1, f"Chapter {n}", p) for n, (p, _t) in sorted(nm_groups.items())]
        for x, t, p in cands:
            if nm_re.match(t):
                continue
            if RE_CHAPTER.match(t) or t.lower() in front_back:
                if p is not None:
                    numbered.append((1, t, p))

    return numbered, unnumbered


def _entries_from_raw(raw, unnumbered, offset, total, max_level, log=print):
    """raw/unnumbered -> Entry 列表（补首章、排序、去重）"""
    if raw and raw[0][2] > 1:
        # 补进去的「首章」只能是真正的章（第X章 / Chapter N），不能是索引/参考文献
        # 这类书末附页——否则前置页会被切成一个名叫「索引」的文件。
        head = next(((lv, t) for lv, t in unnumbered
                     if lv == 1 and RE_CHAPTER.match(t) and not RE_BACK.match(t)), None)
        if head:
            raw.insert(0, (1, head[1], 1))
            if log:
                log(f"  首章页码缺失，按印刷页 1 补：{head[1]}")

    entries = []
    for lv, title, printed in raw:
        page = printed + offset
        if 1 <= page <= total:
            entries.append(Entry(lv, title, page, printed))
    # 先按页、同页再按层级：保证「章」排在它自己的「节」前面，
    # 否则同页去重会删掉章而留下节
    entries.sort(key=lambda e: (e.page, e.level))

    dedup, last = [], None
    for e in entries:
        if last and last.page == e.page:
            continue
        dedup.append(e)
        last = e
    return dedup


# ---------------------------------------------------------------- 页码偏移
def corner_numbers(doc, idx: int) -> list[int]:
    """页面左下角 / 右下角（偶尔在页眉）独立成行的数字，就是印刷页码"""
    page = doc[idx]
    h, w = page.rect.height, page.rect.width
    nums = []
    for x, y, t in _rows_xy(doc, idx):
        t = t.strip()
        if not t.isdigit():
            continue
        n = int(t)
        if n <= 0:
            continue
        near_bottom = y > h * 0.82
        near_top = y < h * 0.18
        near_side = x < w * 0.40 or x > w * 0.60
        if (near_bottom or near_top) and near_side:
            nums.append(n)
    return nums


def _offset_from_text(doc, total: int, toc_end: int, log=print) -> int | None:
    """用正文页脚角落的印刷页码和 PDF 页序对减，投票出偏移量"""
    idxs = [i for i in _sample_indices(total, toc_end) if i < total]
    return _vote_offset(((i, corner_numbers(doc, i)) for i in idxs), log=log)


def _sample_indices(total: int, toc_end: int) -> list[int]:
    start = toc_end + 1
    if start >= total:
        return []
    span = total - start
    return sorted({start, start + span // 3, start + 2 * span // 3,
                   start + span // 2, total - 1})


def _vote_offset(samples, log=print) -> int | None:
    """(PDF页序索引, 该页印刷页码候选) -> 偏移量的众数"""
    votes: dict[int, int] = {}
    seen = []
    for i, nums in samples:
        seen.append(i + 1)
        for n in nums:
            diff = (i + 1) - n
            if -2 <= diff <= 60:
                votes[diff] = votes.get(diff, 0) + 1
    if not votes:
        return None
    off = max(votes, key=lambda k: votes[k])
    if log:
        log(f"  页码偏移：{off}（页脚角落页码，抽样 PDF p{seen}）")
    return off


def _offset_from_ocr(engine, doc, total: int, toc_end: int, dpi: int, log=print) -> int | None:
    """扫描件：只 OCR 每一页的左下角和右下角两个小块，省时间也少噪声"""
    idxs = [i for i in _sample_indices(total, toc_end) if i < total]

    def scan(i):
        page = doc[i]
        h, w = page.rect.height, page.rect.width
        nums = []
        for clip in ((0, h * 0.86, w * 0.40, h), (w * 0.60, h * 0.86, w, h)):
            try:
                rows = _ocr_rows(engine, _render(doc, i, dpi=dpi, clip=clip), dpi)
            except Exception:
                continue
            for _, t in rows:
                nums += [int(x) for x in re.findall(r"\b([0-9]{1,4})\b", t)]
        return nums

    return _vote_offset((i, scan(i)) for i in idxs)


# ---------------------------------------------------------------- 策略 1：书签
def outline_is_chaptered(entries, total_bookmarks: int, nested: bool = False,
                         n_top: int = 0) -> bool:
    """
    书签分两种：
      - 有层级的（章 -> 节 -> 小节）：顶层就是章，可直接信；
      - 平铺一串的（封面/版权/…/1/2/3）：多半只是扫描件页码，要看内容判断。
    """
    if len(entries) < 2:            # 与 build_plan 的「>=2 条目」门槛保持一致
        return False
    if nested and n_top >= 3:
        return True
    if any(RE_CHAPTER.match(e.title) for e in entries):
        return True
    return len(entries) / max(1, total_bookmarks) >= 0.5


def plan_from_outline(pdf_path: str, max_level: int) -> list[Entry]:
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)

    def walk(items, depth=0):
        """展平书签；pypdf 里子条目的 list 紧跟在父条目后面，据此判断有没有子节点"""
        res = []
        for i, it in enumerate(items):
            if isinstance(it, list):
                res.extend(walk(it, depth + 1))
            else:
                res.append((depth, it,
                            i + 1 < len(items) and isinstance(items[i + 1], list)))
        return res

    all_items = walk(reader.outline)
    if not all_items:
        return []
    nested = max(d for d, _t, _c in all_items) >= 1

    # 章所在的层级。多数书顶层就是章；但 Part > Chapter > Section 这种三层书签里，
    # 顶层是「篇」，章在 depth 1（Part I/II 下面才是 1–12 章）。
    # 只看顶层标题判断：顶层过半是「Part/篇」就把章层下移一层。
    chapter_depth = 0
    depths = {d for d, _t, _c in all_items}
    if 0 in depths and 1 in depths:
        top_titles = [(dest.title or "").strip() for d, dest, _c in all_items if d == 0]
        top_titles = [t for t in top_titles
                      if t and t.lower() not in STOP_WORDS and t not in STOP_WORDS
                      and t.lower() not in FRONT_MATTER]
        if top_titles and sum(1 for t in top_titles if RE_PART_TITLE.match(t)) * 2 >= len(top_titles):
            chapter_depth = 1

    named, n_top = [], 0
    # 章层里「每章重复出现」的附属小节（Exercises / Summary / Key Terms 等）不是章节：
    # 一次算出频次，出现 >=3 次的标题就丢掉（书末唯一的 References/Index 只有 1 次，保留）
    _norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    _chap_freq = Counter(
        _norm((dest.title or "")) for d, dest, _c in all_items if d == chapter_depth)
    for depth, dest, has_child in all_items:
        title = (dest.title or "").strip()
        if not title or title.isdigit() or title.lower() in STOP_WORDS or title in STOP_WORDS:
            continue
        try:
            page = reader.get_destination_page_number(dest) + 1
        except Exception:
            continue
        if depth < chapter_depth:
            continue                      # 「篇」这一层：不单独切文件
        if _norm(title) in STOP_SUBTITLES or _chap_freq[_norm(title)] >= 3:
            continue                      # 每章重复的附属小节
        if depth == chapter_depth:
            # 层级书签里，封面/书名/版权/前言这些是没有子条目的叶子；但「附录 / 索引 /
            # 参考文献」同样是叶子，它们是正文后要切出来的内容，不能一起丢。
            if nested and not has_child and (
                    title.lower() in STOP_WORDS or title in STOP_WORDS
                    or title.lower() in FRONT_MATTER):
                continue
            n_top += 1
        lv = 1 if depth == chapter_depth else 2
        if lv <= max_level:
            named.append(Entry(lv, title, page, None))

    if not outline_is_chaptered(named, len(all_items), nested, n_top):
        return []

    named.sort(key=lambda e: (e.page, e.level))
    dedup, last = [], None
    for e in named:
        # 同页去重：一章的第 1 节通常就和章标题同页，切成独立文件会是空文件，
        # 所以只保留层级高的（章）；它的内容自然落在章文件里。
        if last and last.page == e.page:
            continue
        dedup.append(e)
        last = e
    return dedup


# ---------------------------------------------------------------- 策略 2：文本层目录
def plan_from_text(pdf_path: str, max_level: int, toc_scan: int = 30, log=print) -> list[Entry]:
    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    scan = min(total, toc_scan)

    scores = {}
    rows_cache = {}
    for i in range(scan):
        rows_cache[i] = rows_from_text(doc, i)
        scores[i] = _toc_score(rows_cache[i])
        if log:
            log(f"  扫描 p{i + 1}: 候选目录行 {scores[i]}")

    toc_pages = _pick_toc_pages(scores, total)
    if not toc_pages:
        doc.close()
        return []
    if log:
        log(f"  目录页：PDF p{[p + 1 for p in toc_pages]}")

    rows = [r for p in toc_pages for r in rows_cache[p]]
    raw, unnumbered = parse_toc_rows(rows, max_level, max_page=total)
    if not raw:
        doc.close()
        return []

    offset = _offset_from_text(doc, total, toc_pages[-1], log)
    if offset is None:
        # 目录末页索引 +1 = 正文首页的偏移（该页按印刷页 1 计）
        offset = toc_pages[-1] + 1
        if log:
            log(f"  页眉页脚没读到页码，按目录末页推算偏移 {offset}")

    out = _entries_from_raw(raw, unnumbered, offset, total, max_level, log)
    doc.close()
    return out


# ---------------------------------------------------------------- 策略 3：OCR
def plan_from_ocr(pdf_path: str, max_level: int, engine, toc_scan: int = 30,
                  dpi: int = 130, workers: int = 1, log=print, progress=None,
                  use_cache: bool = True) -> list[Entry]:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _local = threading.local()

    def thread_engine():
        if workers <= 1:
            return engine
        e = getattr(_local, "engine", None)
        if e is None:
            from rapidocr_onnxruntime import RapidOCR
            e = RapidOCR()
            _local.engine = e
        return e

    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    scan = min(total, toc_scan)

    # 逐页（可选并行）OCR，并且**找到目录段就提前停止**：
    # 以前是把前 toc_scan 页全部 OCR 完再挑目录页，目录在第 15 页也要把 30 页扫满。
    step = max(1, int(workers))
    results: dict[int, list] = {}
    scores: dict[int, int] = {}
    high_cnt = 0
    last_high = -10
    hits = {"n": 0}
    i = 0
    while i < scan:
        batch = list(range(i, min(scan, i + step)))

        def work(j):
            cache_file = _ocr_cache_file(pdf_path, dpi, j) if use_cache else None
            cached = _load_ocr_cache(cache_file)
            if cached is not None:
                hits["n"] += 1
                return j, cached
            try:
                img = _render(doc, j, dpi=dpi)
            except Exception:
                return j, []
            try:
                rows = _ocr_rows(thread_engine(), img, dpi)
            except Exception:
                return j, []
            _save_ocr_cache(cache_file, rows)
            return j, rows

        if step > 1:
            with ThreadPoolExecutor(max_workers=step) as ex:
                pairs = list(ex.map(work, batch))
        else:
            pairs = [work(j) for j in batch]

        for j, rs in pairs:
            results[j] = rs
            scores[j] = _toc_score(rs)
        if progress:
            try:
                progress(min(scan, i + step), scan)
            except Exception:
                pass
        for j in batch:
            if scores.get(j, 0) >= 3:
                high_cnt += 1
                last_high = j
        i = min(scan, i + step)
        # 已经找到至少 2 页目录、且后面连续 3 页都不像目录 → 目录段结束，不必再扫
        if high_cnt >= 2 and (i - 1 - last_high) >= 3:
            if log:
                log(f"  目录段结束（最后一页 p{last_high + 1}），提前停止扫描（共扫 {i} 页）")
            break

    if log:
        for i2 in sorted(scores):
            log(f"  扫描 p{i2 + 1}: 候选目录行 {scores[i2]}")
        if hits["n"]:
            log(f"  其中 {hits['n']} 页来自 OCR 缓存（跳过识别）")

    toc_pages = _pick_toc_pages(scores, total)
    if not toc_pages:
        doc.close()
        return []
    if log:
        log(f"  目录页：PDF p{[p + 1 for p in toc_pages]}")

    rows = [r for p in toc_pages for r in results.get(p, [])]
    raw, unnumbered = parse_toc_rows(rows, max_level, max_page=total)
    if not raw:
        doc.close()
        return []

    offset = _offset_from_ocr(engine, doc, total, toc_pages[-1], dpi, log)
    if offset is None:
        offset = toc_pages[-1] + 1
        if log:
            log(f"  页脚 OCR 没取到页码，按目录末页推算偏移 {offset}")

    out = _entries_from_raw(raw, unnumbered, offset, total, max_level, log)
    doc.close()
    return out


# ---------------------------------------------------------------- 策略 4：兜底
def plan_from_pages(total: int, every: int) -> list[Entry]:
    return [Entry(1, f"第{i // every + 1:02d}部分", i + 1) for i in range(0, total, every)]


# ---------------------------------------------------------------- 入口
def has_text_layer(pdf_path: str, probe_pages: int = 5) -> bool:
    doc = pymupdf.open(pdf_path)
    n = 0
    for i in range(min(probe_pages, doc.page_count)):
        try:
            n += len(doc[i].get_text() or "")
        except Exception:
            pass
    doc.close()
    return n > 300


def build_plan(pdf_path, max_level=1, ocr=True, every=30, toc_scan=30,
               workers: int = 2, front: bool = True, log=print, progress=None,
               ocr_dpi: int = 130, ocr_cache: bool = True):
    """返回 (entries, strategy_name)。

    front=True 时，如果第一个条目不是第 1 页，会在最前面补一条
    「前置页（封面·版权·目录）」，否则封面/版权/目录这些页不会产出任何文件。
    """

    def _finalize(entries, strategy):
        entries = list(entries)
        if front and entries and entries[0].page > 1:
            entries.insert(0, Entry(1, "前置页（封面·版权·目录）", 1, front=True))
        return entries, strategy

    e = plan_from_outline(pdf_path, max_level)
    if len(e) >= 2:
        return _finalize(e, "书签")

    if has_text_layer(pdf_path):
        e = plan_from_text(pdf_path, max_level, toc_scan=toc_scan, log=log)
        if len(e) >= 2:
            return _finalize(e, "文本层目录页")
        if log:
            log("  文本层里没找到目录页")
        if not ocr:
            import pymupdf as _m
            _d = _m.open(pdf_path)
            _t = _d.page_count
            _d.close()
            return _finalize(plan_from_pages(_t, every), "固定页数")

    if ocr:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception:
            if log:
                log("  未安装 rapidocr-onnxruntime，跳过 OCR（pip install rapidocr-onnxruntime）")
        else:
            try:
                e = plan_from_ocr(pdf_path, max_level, RapidOCR(), toc_scan=toc_scan,
                                  workers=workers, dpi=ocr_dpi, log=log,
                                  progress=progress, use_cache=ocr_cache)
            except Exception as ex:
                if log:
                    log(f"  OCR 识别失败：{ex}")
            if len(e) >= 2:
                return _finalize(e, "OCR 目录页")

    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    doc.close()
    return _finalize(plan_from_pages(total, every), "固定页数")
