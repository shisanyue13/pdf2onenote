# -*- coding: utf-8 -*-
"""按章节条目把 PDF 切成多个文件。"""
from __future__ import annotations
import re
from pathlib import Path

import pymupdf

from .toc import Entry, chapter_no

ILLEGAL = re.compile(r'[\\/:*?"<>|]')

# 标题自带的章号前缀（用于避免「01_1 Introduction」这种重复编号）
RE_LEAD_CN_NO = re.compile(r"^第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章篇编讲]\s*")
RE_LEAD_CHAPTER = re.compile(r"^(\d{1,3})\s+")

# 正文之后的东西：附录、索引、答案、版权页……排到 90 之后
RE_BACK = re.compile(
    r"附\s*[录件]|索\s*引|参考文献|习题答|答案与提示|后记|编后"
    r"|appendix|index|references|bibliography|glossary|colophon"
    r"|photo\s*credits|credits|answers", re.I)


def safe_name(s: str, maxlen: int = 40) -> str:
    s = ILLEGAL.sub("_", s).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen] if len(s) > maxlen else s) or "未命名"


def _strip_own_number(title: str, ch: int) -> str:
    """标题自带和文件号相同的章号时去掉，免得出现「01_1 Introduction」这种重复编号。"""
    t = RE_LEAD_CN_NO.sub("", title) if RE_LEAD_CN_NO.match(title) else title
    m = RE_LEAD_CHAPTER.match(t)
    if m:
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else None
        if n == ch:
            t = t[m.end():]
    return t.strip() or title


def build_names(entries: list[Entry]) -> list[str]:
    """
    生成文件名（不含扩展名）：
      前置页 -> 00_xxx（只由 build_plan 注入的那一条）
      章     -> 01_xxx
      节     -> 03-2_xxx
    章号优先取标题里的「第X章」；英文书没有这个字样就按出现顺序编号；
    附录/索引这类正文之后的内容排在 90 之后（从 91 起）。
    重名会被自动加 _2 / _3 后缀——否则 split_pdf 会静默覆盖同名文件。
    """
    names, ch, sec, extra = [], 0, 0, 0
    for e in entries:
        if getattr(e, "front", False):
            names.append(f"00_{safe_name(e.title)}")
            continue
        if e.level == 1:
            sec = 0
            no = chapter_no(e.title)
            if no is not None:
                ch = no
            elif RE_BACK.search(e.title):
                extra += 1
                ch = 90 + extra
            else:
                ch += 1
            names.append(f"{ch:02d}_{safe_name(_strip_own_number(e.title, ch))}")
        else:
            sec += 1
            names.append(f"{ch:02d}-{sec}_{safe_name(e.title)}")

    seen: dict[str, int] = {}
    for i, n in enumerate(names):
        seen[n] = seen.get(n, 0) + 1
        if seen[n] > 1:
            names[i] = f"{n}_{seen[n]}"
    return names


# 书签标题里至少有一个字母或汉字才算「有信息」，用来丢掉「1」「2」「380」这种页码占位书签
_TEXTY = re.compile(r"[A-Za-z\u4e00-\u9fff]")


def _old_named_toc(src) -> list[tuple[int, str, int]]:
    """原 PDF 书签里「有字」的条目（封面 / 前言 / 目录 …）。

    扫描件常见的情况是书签 367 条、361 条是纯页码占位符——那些一律丢掉，
    否则导进笔记 App 之后大纲栏全是噪音。
    """
    out: list[tuple[int, str, int]] = []
    try:
        raw = src.get_toc(simple=True)
    except Exception:                              # noqa: BLE001
        return out
    for row in raw:
        lvl, title, page = (list(row) + [1, "", 1])[:3]
        t = (title or "").strip()
        if not t or not _TEXTY.search(t):
            continue
        if not isinstance(page, int) or not (1 <= page <= src.page_count):
            continue
        out.append((max(1, min(6, int(lvl or 1))), t, page))
    return out


def rebuild_toc(pdf_path: str, entries: list[Entry], out_path: Path,
                keep_old: bool = True, log=print) -> tuple[Path, int]:
    """导出「整本 PDF + 正确书签目录」。

    页面一个字节都不改（扫描件的清晰度、体积都保持原样），只重写大纲：
    识别出来的章节 + 原书签里那些有字的条目（封面/前言/目录 …），
    按页排序、页码型噪音丢弃、同页同名去重。
    """
    src = pymupdf.open(pdf_path)
    total = src.page_count
    rows: list[tuple[int, str, int]] = []
    if keep_old:
        rows.extend(_old_named_toc(src))

    # 原书签里已经有第 1 页的前置条目时，不再塞合成的「前置页」那一条
    has_cover = any(p == 1 for _, _, p in rows)
    for e in entries:
        if getattr(e, "front", False) and has_cover:
            continue
        p = getattr(e, "page", 0)
        if not isinstance(p, int) or not (1 <= p <= total):
            if log:
                log(f"  书签跳过（页码 {p} 越界）：{e.title}")
            continue
        title = (e.title or "").strip() or "未命名"
        lvl = 1 if getattr(e, "front", False) else getattr(e, "level", 1)
        rows.append((max(1, min(6, int(lvl or 1))), title, p))

    seen, clean = set(), []
    for row in sorted(rows, key=lambda r: r[2]):     # 按页号稳定排序，大纲才跟书序一致
        key = (row[1], row[2])
        if key in seen:
            continue
        seen.add(key)
        clean.append(row)

    src.set_toc([[lvl, title, page] for lvl, title, page in clean])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src.save(str(out_path), garbage=3, deflate=True)
    src.close()
    if log:
        log(f"  整本带目录：{len(clean)} 条大纲 -> {out_path.name}")
    return out_path, len(clean)


def zip_bytes(src_dir: Path) -> tuple[bytes, int]:
    """把目录里的文件打成一个 zip（内存里，供 CLI 落盘 / 网页直接下载共用）。"""
    import io
    import zipfile

    src_dir = Path(src_dir)
    files = sorted(p for p in src_dir.iterdir() if p.is_file())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            z.write(f, f.name)
    return buf.getvalue(), len(files)


def pack_zip(src_dir: Path, out_zip: Path, log=print) -> tuple[Path, int]:
    """把输出目录里的东西打包，方便一次传到 iPad（AirDrop / iCloud / 网盘）。"""
    data, n = zip_bytes(src_dir)
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(data)
    if log:
        log(f"  已打包：{out_zip.name}（{n} 个文件，{len(data) / 1024 / 1024:.1f}MB）")
    return out_zip, n


def split_pdf(pdf_path: str, entries: list[Entry], out_dir: Path, log=print) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = pymupdf.open(pdf_path)
    total = src.page_count

    # 只保留「起始页严格递增」的条目：同页的节切出来会是空文件（或越界页），
    # 它的内容本来就在上一份文件里。手工构造 entries 时也不会因此崩。
    ordered, last_page = [], 0
    for e in entries:
        p = e.page
        if not isinstance(p, int) or p < 1 or p <= last_page:
            if log:
                log(f"  跳过（起始页 {p} 不递增）：{e.title}")
            continue
        ordered.append(e)
        last_page = p

    names = build_names(ordered)
    starts = [e.page for e in ordered]
    # ends 是「下一条目的起始页」，最后一条的结束用 total + 1，
    # 否则下面的 b - 1 会把整本书的最后一页吃掉
    ends = starts[1:] + [total + 1]

    produced, lines = [], []
    for (e, name, a, b) in zip(ordered, names, starts, ends):
        if a > total:
            continue
        b = min(b - 1, total)
        if b < a:                       # 理论上不会发生（上面已保证递增）
            continue
        doc = pymupdf.open()
        doc.insert_pdf(src, from_page=a - 1, to_page=b - 1)
        dst = out_dir / f"{name}.pdf"
        doc.save(dst, garbage=3, deflate=True)
        doc.close()
        size = dst.stat().st_size / 1024 / 1024
        produced.append(dst)
        lines.append(f"{name}\tPDF {a}-{b}\t{b - a + 1}页\t{size:.1f}MB")
        if log:
            log(f"  {name}.pdf  PDF {a}-{b}  {b - a + 1}页  {size:.1f}MB")

    (out_dir / "_章节对照.txt").write_text("\n".join(lines), encoding="utf-8-sig")
    src.close()
    return produced
