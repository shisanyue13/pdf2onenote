# -*- coding: utf-8 -*-
"""
把 PDF 批量导入 OneNote，一个 PDF -> 一个页面。

两条通道，自动选择：
  A) COM（OneNote 桌面版自动化接口）——快，能精确指定笔记本和分区
  B) UI 驱动（Ctrl+N / Ctrl+V）——COM 被禁时的退路，用当前打开的分区

很多 Office 2024 / 365 机器上 COM 会被锁（Dispatch 成功但方法调用 0x80004005、
ONENOTE.EXE 无类型库），所以 B 不是备选而是主力。
"""
from __future__ import annotations
import base64
import io
import time
import uuid
import datetime
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "http://schemas.microsoft.com/office/onenote/2013/onenote"
XS_2013 = 2


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.0000+08:00")


def _oid():
    return "{" + str(uuid.uuid4()).upper() + "}"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ============================================================ 通道 A：COM
def com_available() -> bool:
    try:
        import win32com.client
        app = win32com.client.Dispatch("OneNote.Application")
        app.GetHierarchy("", 2)
        return True
    except Exception:
        return False


def _com_call(fn, *variants):
    last = None
    for args in variants:
        try:
            r = fn(*args)
            if isinstance(r, tuple):
                vals = [x for x in r if x not in (None, "")]
                r = vals[-1] if vals else None
            return r
        except Exception as e:          # noqa
            last = e
    raise last


def _update_page(app, xml, schema=XS_2013):
    epoch = datetime.datetime(1899, 12, 30)
    _com_call(app.UpdatePageContent, (xml, epoch, schema), (xml,), (xml, 0, schema))


def _cdata(s: str) -> str:
    """CDATA 里不能出现 ]]>（文件名允许 ] 和 >），拆成两段 CDATA。"""
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _title_xml(page_id, title):
    t = _now()
    return (
        f'<one:Page xmlns:one="{NS}" ID="{page_id}" dateTime="{t}" lang="zh-CN">'
        f'<one:Title lang="zh-CN"><one:OE author="p2o" authorInitials="P2" '
        f'creationTime="{t}" lastModifiedTime="{t}" objectID="{_oid()}">'
        f"<one:T>{_cdata(title)}</one:T></one:OE></one:Title></one:Page>"
    )


def _images_xml(page_id, items):
    t = _now()
    oes = "".join(
        f'<one:OE objectID="{_oid()}" creationTime="{t}" lastModifiedTime="{t}">'
        f'<one:Image format="png" isPrintout="true" objectID="{_oid()}">'
        f'<one:Size width="{w:.1f}" height="{h:.1f}" isSetByUser="false" />'
        f"<one:Data>{b64}</one:Data></one:Image></one:OE>"
        for b64, w, h in items
    )
    return (
        f'<one:Page xmlns:one="{NS}" ID="{page_id}" dateTime="{t}" lang="zh-CN">'
        f'<one:Outline author="p2o" authorInitials="P2" creationTime="{t}" '
        f'lastModifiedTime="{t}" objectID="{_oid()}">'
        f'<one:Position x="36.0" y="86.0" z="0" />'
        f"<one:OEChildren>{oes}</one:OEChildren></one:Outline></one:Page>"
    )


def _find_section(app, notebook_name, section_name, log=print):
    xml = _com_call(app.GetHierarchy, ("", 3), ("", 3, XS_2013))
    root = ET.fromstring(xml)
    notebooks = list(root.iter(f"{{{NS}}}Notebook"))
    if not notebooks:
        raise RuntimeError("OneNote 里没有任何笔记本，先登录账号或新建一个")

    nb = None
    for n in notebooks:
        if notebook_name and notebook_name in (n.get("name") or ""):
            nb = n
            break
    nb = nb or notebooks[0]
    nb_id = nb.get("ID")

    for sec in nb.iter(f"{{{NS}}}Section"):
        if (sec.get("name") or "") == section_name:
            return nb_id, sec.get("ID"), nb.get("name"), False

    create = (
        f'<one:Notebooks xmlns:one="{NS}"><one:Notebook ID="{nb_id}">'
        f'<one:Section name="{_esc(section_name)}" /></one:Notebook></one:Notebooks>'
    )
    _com_call(app.UpdateHierarchy, (create, XS_2013), (create,))
    xml2 = _com_call(app.GetHierarchy, ("", 3), ("", 3, XS_2013))
    for n2 in ET.fromstring(xml2).iter(f"{{{NS}}}Notebook"):
        if n2.get("ID") == nb_id:
            for sec in n2.iter(f"{{{NS}}}Section"):
                if (sec.get("name") or "") == section_name:
                    return nb_id, sec.get("ID"), nb.get("name"), True
    raise RuntimeError(f"分区创建失败：{section_name}")


def com_import(files, notebook="", section="PDF 导入", dpi=150, color=False,
               chunk=5, full_resend=True, log=print):
    """COM 通道。full_resend=True 时，每页最后再发一次「整页完整内容」。

    UpdatePageContent 的分块语义（增量合并 vs 整页替换）在文档里没写清；如果它是
    整页替换，只发增量会让每 chunk 之外的图全部消失。多发一次完整内容则两种语义
    都正确，代价是每页多传一遍数据。
    """
    import win32com.client
    import pymupdf

    app = win32com.client.Dispatch("OneNote.Application")
    nb_id, sec_id, nb_name, created = _find_section(app, notebook, section, log)
    log(f"写入 {nb_name} / {section}" + ("（新建分区）" if created else ""))

    cs = None if color else pymupdf.csGRAY
    for f in files:
        doc = pymupdf.open(f)
        n = doc.page_count
        page_id = _com_call(app.CreateNewPage, (sec_id,), (sec_id, "", 0))
        _update_page(app, _title_xml(page_id, Path(f).stem))
        log(f"[页] {Path(f).stem}  {n} 页", flush=True)
        buf, all_items = [], []
        for i in range(n):
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=cs)
            item = (
                base64.b64encode(pix.tobytes("png")).decode(),
                pix.width / dpi * 72.0,
                pix.height / dpi * 72.0,
            )
            buf.append(item)
            if full_resend:
                all_items.append(item)
            if len(buf) >= chunk or i == n - 1:
                _update_page(app, _images_xml(page_id, buf))
                buf = []
        if full_resend and all_items:
            _update_page(app, _images_xml(page_id, all_items))
        doc.close()
    return "COM"


# ============================================================ 通道 B：UI 驱动
def _open_clipboard(retries: int = 20, wait: float = 0.2):
    """剪贴板经常被别的进程短暂占用（实测 OpenClipboard 会报「拒绝访问」），要重试。"""
    import win32clipboard

    last = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception as e:                  # noqa: BLE001
            last = e
            time.sleep(wait)
    raise last


def _set_text(s):
    cb = _open_clipboard()
    try:
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_UNICODETEXT, s)
    finally:
        cb.CloseClipboard()


def _set_image(png_bytes):
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    cb = _open_clipboard()
    try:
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_DIB, buf.getvalue()[14:])
    finally:
        cb.CloseClipboard()


def _page_first_line(w) -> str:
    """用 UIA 读当前页面内容的第一行（OneNote 把它当页面标题）。读不到就返回空串。"""
    best = ""
    try:
        for c in w.descendants():
            try:
                if c.element_info.control_type in ("Document", "Text", "Edit"):
                    t = c.window_text()
                    if t and "\r" in t and len(t) > len(best):
                        best = t
            except Exception:      # noqa: BLE001
                continue
    except Exception:              # noqa: BLE001
        return ""
    return (best.split("\r")[0] or "").strip()


def ui_import(files, dpi=150, color=False, delay=0.6, onenote_exe=None, log=print):
    from pywinauto import Application, keyboard
    import pymupdf

    try:
        app = Application(backend="uia").connect(title_re=".*OneNote$", timeout=3)
    except Exception:
        exe = onenote_exe or r"C:\Program Files\Microsoft Office\Root\Office16\ONENOTE.EXE"
        app = Application(backend="uia").start(exe)
        time.sleep(8)
    w = app.window(title_re=".*OneNote$")
    w.wait("exists", timeout=20)
    for fn in (lambda: w.restore(), lambda: w.set_focus()):
        try:
            fn()
            time.sleep(0.6)
        except Exception:
            pass
    log("已连接 OneNote（当前分区）")

    cs = None if color else pymupdf.csGRAY
    for f in files:
        doc = pymupdf.open(f)
        n = doc.page_count
        title = Path(f).stem
        log(f"\n[新建页] {title}  ({n} 页)", flush=True)
        keyboard.send_keys("^n")
        time.sleep(1.5)

        # 新版 OneNote 建页是异步的：太早粘标题会被丢掉，页面就停在「未命名」。
        # 粘完用 UIA 读回第一行确认；没粘上就回到页首重试（空页面时 Ctrl+Home 落在标题行）。
        for attempt in range(3):
            _set_text(title)
            keyboard.send_keys("^v")
            time.sleep(0.8)
            if _page_first_line(w) == title:
                break
            if attempt < 2:
                log(f"    标题没粘上，重试（第 {attempt + 2} 次）", flush=True)
                try:
                    keyboard.send_keys("^{HOME}")
                except Exception:      # noqa: BLE001
                    pass
                time.sleep(0.8)
        else:
            log("    警告：页面标题可能不是文件名（OneNote 没接受粘贴）", flush=True)

        keyboard.send_keys("{ENTER}")
        time.sleep(0.5)

        t0 = time.time()
        for i in range(n):
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=cs)
            _set_image(pix.tobytes("png"))
            keyboard.send_keys("^v")
            time.sleep(delay)
            if (i + 1) % 10 == 0 or i == n - 1:
                log(f"    {i + 1}/{n}  ({time.time() - t0:.0f}s)", flush=True)
        doc.close()
    return "UI 驱动"


# ============================================================ 入口
def import_files(files, dpi=150, color=False, delay=0.6, section="PDF 导入",
                 notebook="", prefer_com=True, onenote_exe=None, log=print):
    if prefer_com and com_available():
        log("检测到 OneNote COM 可用，走 COM 通道")
        try:
            return com_import(files, notebook=notebook, section=section, dpi=dpi,
                              color=color, log=log)
        except Exception as ex:                    # COM 中途坏掉 → 回退界面通道
            log(f"COM 导入失败（{type(ex).__name__}: {ex}），改走界面驱动通道")
    else:
        log("COM 不可用，走界面驱动通道（Ctrl+N / Ctrl+V）")
    return ui_import(files, dpi=dpi, color=color, delay=delay,
                     onenote_exe=onenote_exe, log=log)
