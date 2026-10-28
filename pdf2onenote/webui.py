# -*- coding: utf-8 -*-
"""本机 Web 服务：给 app.html 提供真的切分 / 预览 / 导入能力。

设计要点：
  * 只用标准库（http.server），不引入新依赖；
  * 只监听 127.0.0.1，上传的 PDF 存临时目录，进程退出后由系统清理；
  * 耗时的动作（识别 / 切分 / 导入）都放到后台线程，前端轮询 /api/job 拿进度；
  * 缩略图按需渲染并缓存，避免每次都重新打开 PDF。

启动：
    python -m pdf2onenote web            # 默认 http://127.0.0.1:8765
    python -m pdf2onenote web --port 9000 --no-browser
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pymupdf

from . import toc, splitter

MAX_UPLOAD = 512 * 1024 * 1024          # 512MB，够放任何教材
ROOT = Path(__file__).resolve().parent.parent   # 仓库根（app.html 在这里）
WORK = Path(tempfile.gettempdir()) / "pdf2onenote-web"

_MODES = {"l1", "l2", "fixed"}


# ---------------------------------------------------------------- 状态
class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.uploads: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.thumbs: dict[tuple, bytes] = {}

    # ---- 上传
    def new_upload(self, name: str, data: bytes) -> dict:
        uid = uuid.uuid4().hex[:12]
        d = WORK / uid
        d.mkdir(parents=True, exist_ok=True)
        path = d / "source.pdf"
        path.write_bytes(data)
        doc = pymupdf.open(path)
        pages = doc.page_count
        doc.close()
        rec = {"id": uid, "name": name or "未命名.pdf", "size": len(data),
               "pages": pages, "path": path, "dir": d, "runs": {}, "created": time.time()}
        with self.lock:
            self.uploads[uid] = rec
        return rec

    def get(self, uid: str) -> dict:
        rec = self.uploads.get(uid or "")
        if not rec:
            raise KeyError("上传已失效，请重新拖入 PDF")
        return rec

    # ---- 任务
    def new_job(self, kind: str) -> dict:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        job = {"id": jid, "kind": kind, "stage": "排队中…", "pct": 0,
               "logs": [], "done": False, "error": None, "result": None,
               "started": now, "last": now}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._watchdog, args=(job,), daemon=True).start()
        return job

    def _watchdog(self, job: dict, limit: float = 300.0):
        """OCR 偶尔会卡死（onnxruntime 线程问题）：长时间没进展就把任务判失败，
        免得前端一直转圈。"""
        while True:
            time.sleep(5)
            with self.lock:
                if job["done"]:
                    return
                last = job.get("last", job["started"])
            if time.time() - last > limit:
                self.fail(job, TimeoutError(
                    f"任务 {int(limit)} 秒没有任何进展，已中止。OCR 可能是卡住了："
                    f"把「OCR 线程」设为 1，或把「目录扫描页数」调小再试。"))
                return

    def log(self, job: dict, msg: str, step: int | None = None, stage: str | None = None):
        line = str(msg).rstrip()
        if not line:
            return
        with self.lock:
            job["last"] = time.time()
            job["logs"].append(line)
            if len(job["logs"]) > 400:
                job["logs"] = job["logs"][-400:]
            if step is not None:
                job["pct"] = max(job["pct"], min(97, step))
            if stage:
                job["stage"] = stage

    def finish(self, job: dict, result=None):
        with self.lock:
            job["pct"] = 100
            job["done"] = True
            job["result"] = result
            job["stage"] = "完成"

    def fail(self, job: dict, exc: BaseException):
        with self.lock:
            job["done"] = True
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["stage"] = "失败"


STORE = Store()


# ---------------------------------------------------------------- 业务动作
def _run_plan(rec: dict, mode: str, job: dict, opts: dict | None = None) -> None:
    """识别章节；结果按 mode 缓存在 rec['runs'][mode]。"""
    opts = opts or {}
    toc_scan = max(5, min(200, int(opts.get("toc_scan") or 30)))
    workers = max(1, min(8, int(opts.get("workers") or 1)))
    ocr_dpi = max(72, min(300, int(opts.get("ocr_dpi") or 130)))
    use_ocr = opts.get("ocr", True) is not False
    level = 2 if mode == "l2" else 1
    STORE.log(job, f"《{rec['name']}》 {rec['pages']} 页，开始识别…", 6, "识别章节")
    if mode == "fixed":
        every = 30
        entries = toc.plan_from_pages(rec["pages"], every)
        strategy = f"固定页数（每 {every} 页一刀）"
        STORE.log(job, f"固定页数切分：每 {every} 页一段", 60)
    else:
        STORE.log(job, f"目录扫描范围：前 {toc_scan} 页；OCR {ocr_dpi}dpi；线程 {workers}", 7)
        entries, strategy = toc.build_plan(
            str(rec["path"]), max_level=level, ocr=use_ocr, every=30, toc_scan=toc_scan,
            workers=workers, front=True, ocr_dpi=ocr_dpi,
            ocr_cache=opts.get("ocr_cache", True) is not False,
            log=lambda m: STORE.log(job, m, job["pct"] + 2),
            progress=lambda done, total: STORE.log(
                job, f"OCR 目录页 {done}/{total}",
                8 + int(58 * done / max(1, total)), "OCR 识别目录页"),
        )
    STORE.log(job, f"识别方式：{strategy}，得到 {len(entries)} 个条目", 72)
    if not entries:
        raise RuntimeError("没有识别出任何章节（这份 PDF 可能全是图片且没有目录）")
    rec["runs"][mode] = {"entries": entries, "strategy": strategy, "level": level}
    STORE.finish(job, {"strategy": strategy, "count": len(entries),
                       "entries": [{"level": e.level, "title": e.title, "page": e.page,
                                    "front": getattr(e, "front", False)} for e in entries]})


def _run_split(rec: dict, mode: str, job: dict) -> None:
    info = rec["runs"].get(mode)
    if not info:
        raise RuntimeError("还没有识别结果，请先识别")
    entries = info["entries"]
    out = rec["dir"] / f"split-{mode}"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    STORE.log(job, f"开始切分 {len(entries)} 个条目 → {out.name}/", 8, "切分中")
    total = max(1, len(entries))
    done = {"n": 0}

    def hook(msg):
        done["n"] += 1
        STORE.log(job, msg, 8 + int(88 * done["n"] / total))

    files = splitter.split_pdf(str(rec["path"]), entries, out, log=hook)
    items = []
    for f in files:
        d = pymupdf.open(f)
        pages = d.page_count
        d.close()
        m = re.match(r"^(\d{1,3})(?:-(\d{1,3}))?", f.stem)
        items.append({"name": f.name, "title": f.stem, "pages": pages,
                      "chapter": int(m.group(1)) if m else 999,
                      "section": int(m.group(2)) if (m and m.group(2)) else -1})
    rec["runs"][mode].update({"outdir": out, "files": files, "items": items})
    STORE.log(job, f"切分完成：{len(files)} 个文件", 99)
    STORE.finish(job, {"dir": str(out), "files": items,
                       "strategy": info["strategy"], "count": len(items)})


def _run_import(rec: dict, mode: str, names: list[str] | None, opts: dict, job: dict) -> None:
    from . import onenote

    info = rec["runs"].get(mode)
    if not info or not info.get("files"):
        raise RuntimeError("还没有切分结果，请先切分")
    all_files = info["files"]
    if names:
        wanted = set(names)
        picked = [f for f in all_files if f.name in wanted]
    else:
        picked = list(all_files)
    if not picked:
        raise RuntimeError("没有选中的切分文件")
    limit = int(opts.get("limit") or 0)
    if limit > 0:
        picked = picked[:limit]
    STORE.log(job, f"准备导入 {len(picked)} 个文件到 OneNote…", 6, "导入 OneNote")
    total = max(1, len(picked))
    seen = {"n": 0}

    def hook(msg, **_kw):
        s = str(msg).strip()
        if s.startswith("[页]") or s.startswith("[新建页]") or s.startswith("[新建页]"):
            seen["n"] += 1
        STORE.log(job, s, 6 + int(90 * min(seen["n"], total) / total))

    method = onenote.import_files(
        picked,
        dpi=int(opts.get("dpi") or 150),
        color=bool(opts.get("color")),
        delay=float(opts.get("delay") or 0.6),
        section=opts.get("section") or "PDF 导入",
        notebook=opts.get("notebook") or "",
        prefer_com=not bool(opts.get("ui")),
        onenote_exe=opts.get("onenote_exe") or None,
        log=hook,
    )
    STORE.finish(job, {"method": method, "count": len(picked)})


# ---------------------------------------------------------------- 缩略图
def _thumb(rec: dict, mode: str, idx: int, width: int) -> bytes:
    key = (rec["id"], mode, idx, width)
    cached = STORE.thumbs.get(key)
    if cached:
        return cached
    info = rec["runs"].get(mode) or {}
    files = info.get("files") or []
    if not files:
        raise KeyError("还没有切分结果")
    if idx < 0 or idx >= len(files):
        raise KeyError("序号越界")
    doc = pymupdf.open(files[idx])
    try:
        page = doc[0]
        zoom = width / max(1.0, page.rect.width)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
        png = pix.tobytes("png")
    finally:
        doc.close()
    STORE.thumbs[key] = png
    return png


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "pdf2onenote-web/0.2"

    # ---------- 基础
    def log_message(self, fmt, *args):        # 静默：命令行只留关键信息
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code, msg):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_UPLOAD:
            raise ValueError("文件太大（上限 512MB）")
        chunks, left = [], n
        while left > 0:
            b = self.rfile.read(min(1 << 20, left))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)

    def _q(self):
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    # ---------- 静态 / 页面
    def do_GET(self):
        path, q = self._q()
        try:
            if path in ("/", "/index.html", "/app.html"):
                html = ROOT / "app.html"
                if not html.exists():
                    return self._err(500, "找不到 app.html")
                return self._send(200, html.read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/images/"):
                f = (ROOT / path.lstrip("/")).resolve()
                if ROOT not in f.parents or not f.exists():
                    return self._err(404, "not found")
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype)
            if path == "/api/info":
                try:
                    from . import onenote
                    com = onenote.com_available()
                except Exception:
                    com = False
                return self._json({"ok": True, "version": _version(), "com": com,
                                   "onenote_host": os.name == "nt",
                                   "workdir": str(WORK)})
            if path == "/api/state":
                rec = STORE.get(q.get("id", ""))
                runs = {}
                for k, v in rec["runs"].items():
                    if v.get("items"):
                        runs[k] = {"items": v["items"], "strategy": v["strategy"],
                                   "count": len(v["items"])}
                return self._json({"ok": True, "id": rec["id"], "name": rec["name"],
                                   "pages": rec["pages"], "size": rec["size"],
                                   "runs": runs})
            if path == "/api/job":
                job = STORE.jobs.get(q.get("id", ""))
                if not job:
                    return self._err(404, "任务不存在")
                with STORE.lock:
                    snap = {k: job[k] for k in ("id", "kind", "stage", "pct", "done",
                                                "error", "result")}
                    snap["logs"] = job["logs"][-12:]
                return self._json({"ok": True, "job": snap})
            if path == "/api/thumb":
                rec = STORE.get(q.get("id", ""))
                png = _thumb(rec, q.get("mode", "l1"), int(q.get("idx", "0")),
                             int(q.get("w", "240")))
                return self._send(200, png, "image/png")
            return self._err(404, "not found")
        except KeyError as e:
            self._err(404, e.args[0] if e.args else "not found")
        except Exception as e:                     # noqa: BLE001
            self._err(500, f"{type(e).__name__}: {e}")

    # ---------- 动作
    def do_POST(self):
        path, q = self._q()
        try:
            if path == "/api/upload":
                data = self._body()
                if not data.startswith(b"%PDF-"):
                    return self._err(400, "这不是 PDF 文件（缺少 %PDF- 头）")
                rec = STORE.new_upload(q.get("name", ""), data)
                return self._json({"ok": True, "id": rec["id"], "name": rec["name"],
                                   "pages": rec["pages"], "size": rec["size"]})
            body = json.loads(self._body() or b"{}")
            rec = STORE.get(body.get("id", ""))
            mode = body.get("mode") or "l1"
            if mode not in _MODES:
                return self._err(400, f"未知模式：{mode}")

            if path == "/api/plan":
                job = STORE.new_job("plan")
                opts = body.get("opts") or {}
                threading.Thread(target=_guard_plan, args=(rec, mode, job, opts),
                                 daemon=True).start()
                return self._json({"ok": True, "job": job["id"]})
            if path == "/api/split":
                job = STORE.new_job("split")
                threading.Thread(target=_guard, args=(rec, mode, job, _run_split),
                                 daemon=True).start()
                return self._json({"ok": True, "job": job["id"]})
            if path == "/api/import":
                opts = body.get("opts") or {}
                names = body.get("names")
                job = STORE.new_job("import")
                threading.Thread(target=_guard_import,
                                 args=(rec, mode, names, opts, job), daemon=True).start()
                return self._json({"ok": True, "job": job["id"]})
            return self._err(404, "not found")
        except KeyError as e:
            self._err(404, e.args[0] if e.args else "not found")
        except Exception as e:                     # noqa: BLE001
            self._err(500, f"{type(e).__name__}: {e}")


def _guard(rec, mode, job, fn):
    try:
        fn(rec, mode, job)
    except BaseException as e:                     # noqa: BLE001
        STORE.fail(job, e)


def _guard_plan(rec, mode, job, opts):
    try:
        _run_plan(rec, mode, job, opts)
    except BaseException as e:                     # noqa: BLE001
        STORE.fail(job, e)


def _guard_import(rec, mode, names, opts, job):
    try:
        _run_import(rec, mode, names, opts, job)
    except BaseException as e:                     # noqa: BLE001
        STORE.fail(job, e)


def _version() -> str:
    from . import __version__
    return __version__


# ---------------------------------------------------------------- 启动
def serve(host="127.0.0.1", port=8765, open_browser=True) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"pdf2onenote 图形界面已启动：{url}")
    print(f"（工作目录：{WORK}　按 Ctrl+C 结束）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
