# -*- coding: utf-8 -*-
"""命令行入口：split / import / all"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

from . import __version__

# 01_章.pdf / 01-2_节.pdf / 00_前置页.pdf / 91_附录.pdf
RE_NAME_ORDER = re.compile(r"^(\d{1,3})(?:-(\d{1,3}))?")


def _log(msg="", **_kw):
    """导入流程里 onenote.py 会带 flush=True 调它，所以必须能吞掉额外关键字参数
    （否则 UI / COM 通道在画第一页之前就会 TypeError）。"""
    print(msg, flush=True)


def name_order_key(p: Path):
    """按「章号, 节号」排序：章永远排在它自己的节前面，节按数字而不是字典序。"""
    m = RE_NAME_ORDER.match(p.name)
    if not m:
        return (10 ** 6, 0, p.name)
    return (int(m.group(1)),
            int(m.group(2)) if m.group(2) else -1,
            p.name)


def cmd_split(a):
    from . import toc, splitter

    pdf = str(Path(a.pdf).resolve())
    if not Path(pdf).is_file():
        _log(f"文件不存在：{pdf}")
        return 1

    import pymupdf
    doc = pymupdf.open(pdf)
    total = doc.page_count
    doc.close()
    _log(f"《{Path(pdf).stem}》 共 {total} 页，识别章节中…")

    entries, strategy = toc.build_plan(
        pdf, max_level=a.level, ocr=not a.no_ocr, every=a.every,
        toc_scan=a.toc_scan, workers=getattr(a, "ocr_workers", 1),
        front=not getattr(a, "no_front", False),
        ocr_dpi=getattr(a, "ocr_dpi", 130),
        ocr_cache=not getattr(a, "no_ocr_cache", False), log=_log,
    )
    _log(f"识别方式：{strategy}，得到 {len(entries)} 个条目")

    if getattr(a, "dry_run", False):
        for e in entries:
            _log(f"  [L{e.level}] {e.title}  -> PDF p{e.page}")
        return 0

    out = Path(a.out).resolve() if a.out else Path(pdf).parent / f"{Path(pdf).stem}_章节"
    files = splitter.split_pdf(pdf, entries, out, log=_log)
    _log(f"\n完成：{len(files)} 个文件 -> {out}")
    return 0


def cmd_import(a):
    from . import onenote

    folder = Path(a.dir).resolve()
    files = sorted((p for p in folder.glob("*.pdf") if not p.name.startswith("_")),
                   key=name_order_key)
    limit = getattr(a, "limit", 0) or 0
    if limit:
        files = files[: limit]
    if not files:
        _log(f"目录里没有 PDF：{folder}")
        return 1

    total_pages = 0
    import pymupdf
    for f in files:
        with pymupdf.open(f) as d:
            total_pages += d.page_count

    _log(f"待导入 {len(files)} 个文件 / 共 {total_pages} 页")
    for f in files:
        _log(f"   {f.name}")
    if getattr(a, "dry_run", False):
        return 0
    if not getattr(a, "yes", False):
        input("\n先把 OneNote 切到目标分区，然后按 Enter 开始（5 秒内点一下 OneNote 窗口）… ")
        import time
        time.sleep(5)

    method = onenote.import_files(
        files, dpi=a.dpi, color=a.color, delay=a.delay,
        section=a.section, notebook=a.notebook,
        prefer_com=not a.ui, onenote_exe=a.onenote_exe, log=_log,
    )
    _log(f"\n完成（{method}）。OneNote 后台 OCR 后，Ctrl+E 可搜到页面文字。")
    return 0


def cmd_all(a):
    a.dry_run = False
    rc = cmd_split(a)
    if rc:
        return rc
    out = Path(a.out).resolve() if a.out else Path(a.pdf).resolve().parent / f"{Path(a.pdf).stem}_章节"
    a.dir = str(out)
    return cmd_import(a)


def _add_split_args(s):
    s.add_argument("--level", type=int, default=1, choices=[1, 2],
                   help="1=按章（默认），2=按章+节")
    s.add_argument("--out", default="", help="输出目录，默认 <PDF名>_章节")
    s.add_argument("--every", type=int, default=30, help="识别失败时的兜底切分页数")
    s.add_argument("--toc-scan", type=int, default=30, help="扫描前 N 页找目录")
    s.add_argument("--ocr-workers", type=int, default=1, help="OCR 并行线程数，1 = 单线程（默认）")
    s.add_argument("--ocr-dpi", type=int, default=130,
                   help="OCR 渲染分辨率，默认 130；调低更快但小字可能认不出")
    s.add_argument("--no-ocr-cache", action="store_true",
                   help="不复用 OCR 缓存（默认会缓存每页识别结果，第二次识别秒过）")
    s.add_argument("--no-ocr", action="store_true", help="禁用 OCR 识别")
    s.add_argument("--no-front", action="store_true",
                   help="不单独产出前置页（封面/版权/目录）")


def cmd_web(a):
    """启动本机 Web 服务，浏览器里用图形界面（真跑切分与导入）。"""
    from . import webui
    webui.serve(host=a.host, port=a.port, open_browser=not a.no_browser)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="pdf2onenote",
        description="把整本 PDF 按章节切开，再一键导入 OneNote",
    )
    p.add_argument("--version", action="version", version=f"pdf2onenote {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("split", help="按章节拆分 PDF")
    s.add_argument("pdf")
    _add_split_args(s)
    s.add_argument("--dry-run", action="store_true", help="只打印结果，不切文件")
    s.set_defaults(func=cmd_split)

    i = sub.add_parser("import", help="把一个目录里的 PDF 导入 OneNote")
    i.add_argument("dir")
    i.add_argument("--dpi", type=int, default=150)
    i.add_argument("--color", action="store_true", help="彩色（默认灰度）")
    i.add_argument("--delay", type=float, default=0.6, help="每张图粘贴后等待秒数")
    i.add_argument("--section", default="PDF 导入", help="COM 通道下的目标分区名（不存在会新建）")
    i.add_argument("--notebook", default="", help="COM 通道下的笔记本名（模糊匹配）")
    i.add_argument("--ui", action="store_true", help="强制走界面驱动通道")
    i.add_argument("--onenote-exe", default="", help="ONENOTE.EXE 路径")
    i.add_argument("--limit", type=int, default=0, help="只导入前 N 个文件")
    i.add_argument("--dry-run", action="store_true", help="只列文件，不导入")
    i.add_argument("--yes", action="store_true", help="跳过确认")
    i.set_defaults(func=cmd_import)

    al = sub.add_parser("all", help="拆分 + 导入一条龙")
    al.add_argument("pdf")
    _add_split_args(al)
    al.add_argument("--dpi", type=int, default=150)
    al.add_argument("--color", action="store_true")
    al.add_argument("--delay", type=float, default=0.6)
    al.add_argument("--section", default="PDF 导入")
    al.add_argument("--notebook", default="")
    al.add_argument("--ui", action="store_true")
    al.add_argument("--onenote-exe", default="")
    al.add_argument("--limit", type=int, default=0, help="只导入前 N 个文件")
    al.add_argument("--yes", action="store_true")
    al.set_defaults(func=cmd_all)

    w = sub.add_parser("web", help="启动本机图形界面（浏览器里拖 PDF 就切分 / 导入）")
    w.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    w.set_defaults(func=cmd_web)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        _log("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
