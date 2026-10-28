# -*- coding: utf-8 -*-
"""
pdf2onenote 自检脚本

特点：全部用**合成数据**验证，不需要任何外部教材 PDF。
运行：在本文件所在目录执行  python selfcheck.py
依赖：pymupdf（requirements.txt 已含）
退出码：0 = 全部通过；1 = 有失败项

覆盖点：
  1. 三个模块能正常导入
  2. 关键函数/常量存在（防止被误删或改名）
  3. 孤立小簇修复：含章样条目的孤立小簇不被当噪声吞掉
  4. 扫描件修复：章标题 OCR 缩进抖动时仍能全部识别（第X章显式路由）
  5. build_names：英文书按出现顺序编号、附录/索引排到 90 之后
  6. split_pdf：最后一页不被吃掉（off-by-one 回归）
  7. has_text_layer：有文字层的 PDF 能被正确判定
  8. 文本层通道端到端：页码偏移（含页脚页码居中的兜底路径）
  9. 双栏目录「Chapter N」两栏都不丢
 10. 单条噪声行不会劫持章层（聚类成员归属）
 11. 首章补页只能补真章，不能拿「索引」当首章
 12. build_names：重名自动加后缀、前置页出 00_
 13. 命令行：all 子命令带 --limit，且能解析
 14. --level 2：文本层通道真的产出 level=2
 15. 书签通道：2 章也能用；顶层附录/索引不被当成封面剔除
 16. 行尾年份不当页码；RE_CN_CHAPTER 不误命中正文句
 17. split_pdf：起始页重复/乱序时不崩、不产出垃圾文件
"""
import os
import sys
import shutil
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容两种布局：包与本脚本同级，或包在 ./pdf2onenote/ 子目录下
for _cand in (_HERE, os.path.join(_HERE, "pdf2onenote")):
    if os.path.isfile(os.path.join(_cand, "pdf2onenote", "__init__.py")):
        sys.path.insert(0, _cand)
        break
else:
    sys.path.insert(0, _HERE)

RESULTS = []


def check(name):
    """装饰器：把函数登记为一项检查。"""
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def eq(actual, expected, msg=""):
    assert actual == expected, f"{msg} 期望={expected!r} 实际={actual!r}"


# ---------------------------------------------------------------- 1. 导入
@check("模块导入")
def t_import():
    from pdf2onenote import toc, splitter, cli  # noqa: F401
    eq(toc.__name__, "pdf2onenote.toc", "toc 模块名不对")


# ---------------------------------------------------------------- 2. 符号存在
@check("关键符号存在")
def t_symbols():
    from pdf2onenote import toc, splitter
    for n in ("RE_CN_CHAPTER", "_cluster_levels", "parse_toc_rows", "build_plan",
              "plan_from_outline", "plan_from_text", "plan_from_ocr",
              "plan_from_pages", "has_text_layer", "chapter_no",
              "_merge_head_lines", "_title_ok"):
        assert hasattr(toc, n), f"toc 缺少 {n}"
    for n in ("build_names", "split_pdf", "safe_name"):
        assert hasattr(splitter, n), f"splitter 缺少 {n}"


# ---------------------------------------------------------------- 3. 孤立小簇保留
@check("孤立含章小簇不被吞")
def t_cluster_keeps_chapter_cluster():
    from pdf2onenote.toc import _cluster_levels
    # 三个簇：x≈90 只有 2 条（含章）、x≈40 三条、x≈60 十条（小节）
    xs = [90.0, 91.0] + [40.0, 41.0, 42.0] + [60.0 + i * 0.5 for i in range(10)]
    titles = (["第一章 甲", "第二章 乙"]
              + ["小节一", "小节二", "小节三"]
              + [f"sec{i}" for i in range(10)])
    centers = _cluster_levels(xs, titles)
    assert any(abs(c - 90.5) < 2.0 for c in centers), \
        f"含章样条目的孤立小簇被当噪声吞掉了，centers={centers}"


# ---------------------------------------------------------------- 4. 中文第X章路由
@check("中文第X章显式路由（扫描件缩进抖动）")
def t_cn_chapter_routing():
    from pdf2onenote.toc import parse_toc_rows
    # 交替 x=44 / x=80，模拟扫描件 OCR 左边界抖动；8 章必须全部识别
    rows = [
        (44.0, "第一章 集合与映射 1"),
        (80.0, "第二章 数列极限 21"),
        (44.0, "第三章 函数极限与连续函数 60"),
        (80.0, "第四章 微分 100"),
        (44.0, "第五章 微分中值定理及其应用 140"),
        (80.0, "第六章 不定积分 205"),
        (44.0, "第七章 定积分 232"),
        (80.0, "第八章 反常积分 305"),
    ]
    numbered, unnumbered = parse_toc_rows(rows, 1)
    total = len(numbered) + len(unnumbered)
    eq(total, 8, f"应识别 8 章，实际 {total}；numbered={numbered} unnumbered={unnumbered}")


# ---------------------------------------------------------------- 5. 文件名编号
@check("文件名编号：英文书按序 + 附录排 90 后")
def t_build_names():
    from pdf2onenote.toc import Entry
    from pdf2onenote.splitter import build_names
    entries = [
        Entry(1, "Chapter 1: Functions", 1),
        Entry(1, "Chapter 2: Limits", 20),
        Entry(1, "Appendix A: Tables", 40),
        Entry(1, "Index", 50),
    ]
    names = build_names(entries)
    assert names[0].startswith("01_"), f"第1章应为 01_，实际 {names[0]}"
    assert names[1].startswith("02_"), f"第2章应为 02_，实际 {names[1]}"
    assert names[2].startswith("91_"), f"附录应在 90 之后（91_），实际 {names[2]}"
    assert names[3].startswith("92_"), f"索引应在 90 之后（92_），实际 {names[3]}"


# ---------------------------------------------------------------- 6. 末页不丢
@check("拆分末页不被吃掉（off-by-one 回归）")
def t_split_last_page():
    import pymupdf
    from pathlib import Path
    from pdf2onenote.toc import Entry
    from pdf2onenote.splitter import split_pdf

    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "src.pdf")
        d = pymupdf.open()
        for _ in range(10):
            d.new_page()
        d.save(src)
        d.close()

        entries = [Entry(1, "第一章 甲", 1), Entry(1, "第二章 乙", 6)]
        files = split_pdf(src, entries, Path(tmp) / "out")
        eq(len(files), 2, "应产出 2 个文件")
        pages = []
        for f in files:
            dd = pymupdf.open(f)
            pages.append(dd.page_count)
            dd.close()
        eq(pages, [5, 5], f"10 页应切成 [5,5]（末页不能被吃），实际 {pages}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. 文本层检测
@check("有文字层 PDF 的判定")
def t_text_layer_detect():
    import pymupdf
    from pdf2onenote.toc import has_text_layer

    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "text.pdf")
        d = pymupdf.open()
        pg = d.new_page()
        # has_text_layer 的阈值是前 5 页累计 > 300 字符，故用文本框写入足量多行文本
        # （单行 insert_text 超出页宽会被截断）
        pg.insert_textbox(pymupdf.Rect(72, 72, 520, 700),
                          "pdf2onenote text layer detection test line.\n" * 12,
                          fontsize=10)
        d.save(p)
        d.close()
        eq(has_text_layer(p), True, "有文字层却判为 False")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 8-17 回归
W, H = 595.0, 842.0


def _mk_book(path, footer_x="right", extra_front=0):
    """合成教材：封面/版权 + 2 页目录 + 40 页正文（4 章，每章 2 节）。

    章印刷页 1/11/21/31，正文从 PDF p5 开始 -> 期望起始页 5/15/25/35。
    """
    import pymupdf
    doc = pymupdf.open()
    doc.new_page(width=W, height=H).insert_text(
        (60, 100), "教材标题", fontsize=11, fontname="china-s")
    # 版权页：写足量文字，保证 has_text_layer 判为 True（阈值前 5 页累计 > 300 字符）
    doc.new_page(width=W, height=H).insert_textbox(
        pymupdf.Rect(60, 80, W - 60, H - 80),
        "版权所有 出版社发行 高等教育教材系列 图书在版编目数据 内容提要\n" * 12,
        fontsize=10, fontname="china-s")
    toc1, toc2 = [], []
    for i, (n, name, p) in enumerate([(1, "集合", 1), (2, "数列极限", 11), (3, "函数极限", 21)]):
        toc1.append((60.0, f"第{n}章 {name} {p}"))
        toc1.append((75.0, f"§{n}.1 {name}基础 {p + 1}"))
        toc1.append((75.0, f"§{n}.2 {name}应用 {p + 3}"))
    toc2.append((60.0, "第4章 微分 31"))
    toc2.append((75.0, "§4.1 微分基础 32"))
    toc2.append((75.0, "§4.2 微分应用 34"))
    for rows in (toc1, toc2):
        pg = doc.new_page(width=W, height=H)
        for k, (x, t) in enumerate(rows):
            pg.insert_text((x, 100 + k * 22), t, fontsize=11, fontname="china-s")
    for _ in range(extra_front):
        doc.new_page(width=W, height=H)
    for pr in range(1, 41):
        pg = doc.new_page(width=W, height=H)
        pg.insert_text((60, 120), "正文内容 " * 6, fontsize=11, fontname="china-s")
        fx = W - 55 if footer_x == "right" else W / 2 - 8      # center = 页码居中
        pg.insert_text((fx, H - 28), str(pr), fontsize=9, fontname="helv")
    doc.save(path)
    doc.close()


@check("文本层通道端到端 + 页码偏移")
def t_text_channel_offset():
    from pdf2onenote import toc
    tmp = tempfile.mkdtemp()
    try:
        # 页脚页码在右下角：走投票
        p = os.path.join(tmp, "a.pdf")
        _mk_book(p)
        e = toc.plan_from_text(p, 1, toc_scan=10, log=lambda *a: None)
        eq([x.page for x in e], [5, 15, 25, 35], "右下角页码：章节起始页不对")
        # 页码居中（corner_numbers 采不到）→ 必须走「目录末页 +1」的兜底，且不能少 1 页
        p2 = os.path.join(tmp, "b.pdf")
        _mk_book(p2, footer_x="center")
        e2 = toc.plan_from_text(p2, 1, toc_scan=10, log=lambda *a: None)
        eq([x.page for x in e2], [5, 15, 25, 35], "页码居中：偏移兜底 off-by-one 又回来了")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("双栏目录 Chapter N 两栏都不丢")
def t_two_column_chapter():
    from pdf2onenote.toc import parse_toc_rows
    rows = [(72.0, f"Chapter {n} {t} {p}") for n, t, p in
            [(1, "Alpha", 1), (2, "Beta", 9)]] + \
           [(300.0, f"Chapter {n} {t} {p}") for n, t, p in
            [(3, "Gamma", 17), (4, "Delta", 25)]]
    num, un = parse_toc_rows(rows, 1)
    eq(len(num) + len(un), 4, f"右栏章被当成缩进小节丢掉了：{num}{un}")


@check("单条噪声行不劫持章层")
def t_noise_not_hijack():
    from pdf2onenote.toc import parse_toc_rows
    rows = [(40.0, "1 Introduction 3")] + \
           [(100.0, f"Chapter {n} {t} {p}") for n, t, p in
            [(1, "Cells", 10), (2, "Genes", 30), (3, "Ecology", 50)]] + \
           [(120.0, f"{c}.{s} Section {c * 10 + s}") for c in (1, 2, 3) for s in (1, 2, 3)]
    num, un = parse_toc_rows(rows, 1)
    pages = sorted(p for _l, _t, p in num)
    assert 10 in pages and 30 in pages and 50 in pages, f"真章被一条噪声行挤掉了：{num}{un}"


@check("首章补页不吃「索引」")
def t_head_not_backmatter():
    from pdf2onenote.toc import _entries_from_raw
    e = _entries_from_raw([(1, "第一章 集合", 20)], [(1, "索引")], offset=5, total=100,
                          max_level=1, log=lambda *a: None)
    eq([x.title for x in e], ["第一章 集合"], "把书末附页补成了首章")
    e2 = _entries_from_raw([(1, "第二章 数列", 20)], [(1, "第一章 集合")], offset=5, total=100,
                           max_level=1, log=lambda *a: None)
    eq([x.page for x in e2], [6, 25], "缺页码的真首章没被补回来")


@check("build_names：重名加后缀 + 前置页 00_")
def t_names_dedup_front():
    from pdf2onenote.toc import Entry
    from pdf2onenote.splitter import build_names
    names = build_names([Entry(1, "第一章 集合", 1), Entry(1, "第一章 集合", 30)])
    assert names[0] != names[1], f"重名会让 split_pdf 静默覆盖：{names}"
    names = build_names([Entry(1, "前置页（封面·版权·目录）", 1, front=True),
                         Entry(1, "第一章 集合", 5)])
    assert names[0].startswith("00_") and names[1].startswith("01_"), f"前置页编号不对：{names}"


@check("命令行 all 子命令可解析（--limit）")
def t_all_subcommand():
    from pdf2onenote.cli import build_parser
    a = build_parser().parse_args(["all", "x.pdf", "--limit", "3", "--no-front"])
    eq(a.limit, 3, "all 没有 --limit 参数")
    eq(a.no_front, True, "all 没有 --no-front 参数")
    eq(build_parser().parse_args(["split", "x.pdf", "--level", "2"]).level, 2)
    assert callable(a.func), "all 没有绑定处理函数"


@check("--level 2 在文本层通道生效")
def t_level2():
    from pdf2onenote import toc
    from pdf2onenote.splitter import build_names
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "a.pdf")
        _mk_book(p)
        e1, _ = toc.build_plan(p, max_level=1, ocr=False, front=False, log=lambda *a: None)
        e2, _ = toc.build_plan(p, max_level=2, ocr=False, front=False, log=lambda *a: None)
        l1 = [x for x in e2 if x.level == 1]
        l2 = [x for x in e2 if x.level == 2]
        eq(len(e1), 4, f"level 1 条目数不对：{len(e1)}")
        eq(len(l1), 4, f"level 2 模式下章数量变了：{len(l1)}")
        assert len(l2) >= 4, f"level 2 没产出节条目：{len(l2)}"
        names = build_names(e2)
        assert any(n.startswith("01-1_") for n in names), f"节的文件名格式不对：{names[:6]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("书签通道：2 章可用 + 顶层附录/索引保留")
def t_outline_channel():
    import pymupdf
    from pdf2onenote import toc
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "o.pdf")
        d = pymupdf.open()
        for _ in range(30):
            d.new_page()
        d.set_toc([[1, "Cover", 1],
                   [1, "Chapter 1 Cells", 2], [2, "1.1 Introduction", 2], [2, "1.2 Methods", 4],
                   [1, "Chapter 2 Genes", 8], [2, "2.1 Introduction", 8], [2, "2.2 DNA", 11],
                   [1, "Appendix A Tables", 20], [1, "Index", 25]])
        d.save(p)
        d.close()
        e = toc.plan_from_outline(p, 1)
        titles = [x.title for x in e]
        assert len(e) >= 2, f"--level 1 下书签通道整条被丢弃：{titles}"
        assert "Appendix A Tables" in titles, f"顶层附录被当封面剔除：{titles}"
        assert "Index" in titles, f"顶层索引被当封面剔除：{titles}"
        assert "Cover" not in titles, f"封面不该成章：{titles}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("行尾年份不当页码 + 中文章标记不误命中")
def t_tail_year_and_regex():
    from pdf2onenote.toc import parse_toc_rows, RE_CN_CHAPTER
    num, un = parse_toc_rows([(72.0, "Chapter 4: The 2024")], 1, max_page=300)
    items = num + un
    eq(len(items), 1, "行尾年份把整行吃了")
    assert (num[0][2] if num else None) != 2024, f"年份被当成页码：{num}"
    assert not RE_CN_CHAPTER.match("第三章的证明见附录"), "RE_CN_CHAPTER 误命中正文句"
    assert RE_CN_CHAPTER.match("第三章 函数极限"), "RE_CN_CHAPTER 漏掉真章标题"


@check("split_pdf：起始页重复/乱序不崩")
def t_split_bad_starts():
    import pymupdf
    from pathlib import Path
    from pdf2onenote.toc import Entry
    from pdf2onenote.splitter import split_pdf
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "s.pdf")
        d = pymupdf.open()
        for _ in range(10):
            d.new_page()
        d.save(src)
        d.close()
        files = split_pdf(src, [Entry(1, "A", 5), Entry(1, "B", 5), Entry(1, "C", 9)],
                          Path(tmp) / "o", log=None)
        eq(len(files), 2, f"重复起始页应被跳过，实际产出 {len(files)} 个")
        total = 0
        for f in files:
            dd = pymupdf.open(f)
            assert dd.page_count > 0, "产出了空文件"
            total += dd.page_count
            dd.close()
        eq(total, 6, f"页数覆盖不对（应 5-10 共 6 页）：{total}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("导入链路：_log 能吃 flush=True")
def t_log_flush():
    """真机导入时踩到的崩溃：onenote.py 用 log(..., flush=True) 调用 cli._log(msg)，
    _log 不接受关键字参数 → UI/COM 通道在画第一页之前就 TypeError。这里守住签名。"""
    from pdf2onenote import cli, onenote
    cli._log("selfcheck: log(flush=True) ok", flush=True)
    for fn in (onenote.com_import, onenote.ui_import, onenote.import_files):
        assert callable(fn), f"{fn} 不可调用"
    # 这三个函数的日志点必须都能被 _log 吃掉：直接按它们的写法调一次
    cli._log(f"[页] test  6 页", flush=True)
    cli._log(f"\n[新建页] test  (6 页)", flush=True)


@check("书签 Part > 章 > 节 三层：章在第二层")
def t_outline_parts():
    """真实书踩到的坑：书签顶层是 Part I/II，章在 depth 1。
    旧逻辑把顶层当章，--level 1 只会切出 2 个几百页的大文件；而每章重复的
    Exercises / Summary 也不能当章。"""
    import pymupdf
    from pdf2onenote import toc
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "parts.pdf")
        d = pymupdf.open()
        for _ in range(20):
            d.new_page()
        d.set_toc([[1, "Part I Foundations", 1],
                   [2, "Chapter 1 Cells", 2], [3, "1.1 Intro", 2], [3, "1.2 Methods", 4],
                   [2, "Exercises", 6],
                   [1, "Part II Applications", 8],
                   [2, "Chapter 2 Genes", 9], [3, "2.1 Intro", 9],
                   [2, "Exercises", 12],
                   [2, "References", 14], [2, "Index", 16]])
        d.save(p)
        d.close()
        e = toc.plan_from_outline(p, 1)
        titles = [x.title for x in e]
        eq(len(e), 4, f"应得到 2 章 + References + Index，实际 {titles}")
        eq(titles[0], "Chapter 1 Cells", f"章层没从 Part 下移：{titles}")
        assert "Part I Foundations" not in titles, f"Part 被当成章：{titles}"
        assert "Exercises" not in titles, f"每章重复的 Exercises 被当成章：{titles}"
        assert "References" in titles and "Index" in titles, f"书末附页丢了：{titles}"
        eq([x.page for x in e], [2, 9, 14, 16], f"页码不对：{[(x.title, x.page) for x in e]}")
        e2 = toc.plan_from_outline(p, 2)
        assert any(x.level == 2 for x in e2), "--level 2 没出节"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("文件名不重复编号（01_1 Introduction → 01_Introduction）")
def t_name_no_double_number():
    from pdf2onenote.toc import Entry
    from pdf2onenote.splitter import build_names
    names = build_names([Entry(1, "1 Introduction and Motivation", 1),
                         Entry(1, "02 Linear Algebra", 2),
                         Entry(1, "4 Vector Calculus", 3)])   # 第 3 章缺失时的错位场景
    eq(names[0], "01_Introduction and Motivation", f"没去掉重复章号：{names}")
    eq(names[1], "02_Linear Algebra", f"标题号与文件号相同也该去掉：{names}")
    eq(names[2], "03_4 Vector Calculus", f"标题号与文件号不同时不能乱改：{names}")


# ---------------------------------------------------------------- 运行
def main():
    print("=" * 60)
    print("pdf2onenote 自检")
    print("=" * 60)
    passed = failed = 0
    for name, fn in RESULTS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}\n         -> {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name}\n         -> {type(e).__name__}: {e}")
            failed += 1
    print("-" * 60)
    print(f"通过 {passed} / 失败 {failed} / 共 {len(RESULTS)}")
    if failed:
        print("结论：存在失败项，请对照上方 FAIL 信息定位。")
        return 1
    print("结论：全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
