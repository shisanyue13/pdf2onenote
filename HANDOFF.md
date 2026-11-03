# pdf2onenote · 交接与检验说明

> 项目交接说明：这份代码做了什么、改过什么、有哪些已知限制，方便接手的人快速判断能不能用、哪里要小心。

---

## 1. 这是什么

把整本 PDF 按章节切开（一章/一节一个 PDF），再一键导入 OneNote——每章一个页面，可手写批注、可被 OneNote OCR 搜索。

难点在于**章节目录的识别**：教材有扫描件（无文本层）、电子版（书签和印刷页码对不上）、文件汇编（无书签）等各种情况。核心工作在 `pdf2onenote/toc.py`（922 行）。

---

## 2. 包里有什么

```
handoff/
├─ pdf2onenote/                 项目根（可直接 git init / 上传）
│  ├─ pdf2onenote/
│  │  ├─ toc.py        (1024行) 章节识别：四级降级 + 目录行解析（核心）
│  │  ├─ splitter.py   ( 123行) 按条目切 PDF + 文件命名（重名自动加后缀）
│  │  ├─ onenote.py    ( 306行) OneNote 导入（COM / UI 两条通道，仅 Windows）
│  │  ├─ cli.py        ( 177行) 命令行：split / import / all
│  │  ├─ __init__.py / __main__.py
│  ├─ README.md                  能力说明 + 已知限制（含 10 类已修边界）
│  ├─ requirements.txt           核心依赖（pymupdf, pypdf, pywin32, pywinauto, Pillow）
│  ├─ requirements-ocr.txt       可选 OCR 依赖（rapidocr-onnxruntime）
│  ├─ LICENSE (MIT) / .gitignore / install.bat / run.bat
├─ selfcheck.py                 自检脚本（20 项，合成数据，无需外部 PDF）
└─ HANDOFF.md                   本文件
```

不含 `.git`、`__pycache__`、`*.pyc`、任何教材 PDF。

---

## 3. 最快上手：跑自检

```bash
cd handoff
python selfcheck.py        # 或 python handoff/selfcheck.py
```

预期输出：`通过 20 / 失败 0 / 共 20`，退出码 0。依赖仅 `pymupdf`。

20 项分别验证：模块导入、关键符号存在、孤立小簇修复、中文第X章路由、文件名编号、拆分末页、文本层判定、文本通道端到端+页码偏移、双栏 `Chapter N`、噪声行不劫持章层、首章补页不吃附页、重名去重+前置页编号、`all` 子命令、`--level 2`、书签通道、行尾年份/中文章标记、`split_pdf` 异常起始页、`_log` 关键字参数、书签三层（Part > 章 > 节）、文件名不重复编号。

---

## 4. 架构：四级降级（`toc.build_plan`）

按顺序尝试，前一级成功（≥2 条目）即返回：

| 级别 | 策略 | 触发条件 |
|---|---|---|
| 1 | **书签** `plan_from_outline` | PDF 有 outline 且像章节结构 |
| 2 | **文本层目录页** `plan_from_text` | 有文本层，在前 `--toc-scan`(默认30) 页找到目录 |
| 3 | **OCR 目录页** `plan_from_ocr` | 扫描件；需 `rapidocr-onnxruntime`，渲染+OCR 前 30 页找目录 |
| 4 | **固定页数** `plan_from_pages` | 兜底，每 `--every`(默认30) 页切一刀 |

文本通道/OCR 通道的核心是 `parse_toc_rows(rows, max_level, max_page=None)`：输入 `[(x, text)]` 目录行，输出 `(有页码条目, 无页码条目)`，条目是 `(level, title, printed)`。它靠**两条互补规则**判层级：

- **多缩进层级**（章比节更靠左）：取最左「含章样条目」的簇为章；右栏章号不重叠则视为双栏续章；整簇都是书末附页（索引/参考文献）的也保留。
- **单缩进层级**（整本左对齐）：只保留章样条目（`第X章`/`Chapter N`/`附录`/`索引`…）与书末附页，`N.M` 小节和重复小节被剔除。

补充：

- 聚类返回的是**成员下标**（`_cluster_groups`），`parse_toc_rows` 按成员取条目——不再按「距均值中心 < gap」取，否则合并后的条目会落在 gap 外凭空消失。
- 孤立簇豁免要求 **≥2 条**章样条目，单条噪声行不会劫持章层。
- `max_level=2` 时把小节收成 `level=2`；`max_page` 用于把「行尾年份」这类假页码挡掉。
- 第一个条目不是第 1 页时，`build_plan` 会在最前面补 `00_前置页（封面·版权·目录）`（`front=True`，CLI `--no-front` 可关）。

---

## 5. 已修复的边界情况（README 同步维护，共 10 类）

| # | 边界 | 例子 | 处理方式 |
|---|---|---|---|
| 1 | 章号独占一行 | `CHAPTER 1` / `第一章` 单独一行 | 把远右独立页码框抓回，章号不当页码抠掉 |
| 2 | 章标题折行 | `Chapter 12` / `Chapter 13` 的长标题折成两行 | `_merge_head_lines` 贪心吞折行标题 |
| 3 | 纯缩进分级目录 | 章 x=148.5 / 节 x=163.4（章号复用） | 取最左含章簇；剔除每章重复的小节 |
| 4 | 罗马 Part + 阿拉伯章 | Part x=65 / Chapter x=88.3 | 有阿拉伯章时丢弃纯罗马 Part 簇 |
| 5 | 双栏目录 | 章 1–4 在左栏 / 5–8 在右栏 | 右栏章号不重叠 → 视为续章保留 |
| 6 | 双栏 + 孤立小簇吞章 | **章 11/12 挤在 x≈52.9 的 2 条小簇** | `_cluster_levels` 加 `titles` 参数：含 **≥2 条**章样条目的孤立小簇不并入噪声簇 |
| 7 | 中文第X章 + OCR 缩进抖动 | **扫描件章标题 x 在 42~82 双峰跳变** | 新增 `RE_CN_CHAPTER`；候选含 ≥3 个「第X章」→ `explicit_cn=True`，跳过缩进聚类走显式标记规则 |
| 8 | 超长英文章标题 | 82 字符的英文标题 | `_title_ok` 上限放宽至 120(ASCII)/50(中文) |
| 9 | 双栏目录里写 `Chapter N` | 两栏都是 `Chapter 3 …` / `Chapter 7 …` | 章号正则加词边界，`Chapter` 的 `C` / `Index` 的 `I` 不再被当罗马数字（否则两栏都算成 `{C}`，右栏整栏丢弃） |
| 10 | 页脚页码居中 | 中文教材常见页码居中 | 角落页码投票采不到时，偏移兜底改为「目录末页 + 1」，不再整体错一页 |

另 `splitter.py` 四处：英文书无「第X章」时按出现顺序编号；附录/索引/答案排到 91 起；修掉 `ends = starts[1:] + [total+1]` 之前会吃掉全书最后一页的 off-by-one；重名自动加 `_2` 后缀、起始页不递增的条目直接跳过。

**关键不变量**：修复 6、7 只影响特定排版形态；早期用十本英文教材做过修复前后对照，**完全一致**（无回归；该批数据见 §7）。

---

## 6. 验证证据

### 6.1 英文教材对照（书签通道 vs 文本通道）

早期用十本带完整书签的英文教材做过通道对照：文本通道比书签通道多出来的条目都落在书末附页
（答案区、参考文献、索引），**没有一本丢真章**。该批数据未随本仓库发布（见 §7）。

### 6.2 扫描件实测（381 页，无文本层）

修复前 OCR 通道只切出 4 条（缺 3–7 章）；修复后 11 条：8 章 + 附录 + 部分习题答案 + 索引，
章起始页与书内目录一致。页码偏移 19（页脚角标投票）；首章原书无页码，按印刷页 1 补。

### 6.3 真实教材 + OneNote 桌面端（可复现）

对象：一本 417 页的免费英文教材（CC-BY-NC-ND 许可，有文本层、104 条书签、
`Part > 章 > 节` 三层书签结构）。

| 项目 | 结果 |
|---|---|
| `--level 1` 与书签逐条比对 | 14 条 = 12 章 + References + Index，**14/14 命中，0 页码不符，0 漏识** |
| 切分产物 | 15 个文件（含 `00_前置页`），**页数合计 417/417**，无重叠无空洞 |
| `--level 2` | 94 条 / 94 个文件，**417/417 页，0 空文件**，7.4 s |
| OneNote 实机导入（6 页一章） | UI 驱动通道成功；UIA 读回该页：标题 = 文件名，正文 **6 个内嵌图片** |

这本书的书签是 `Part > 章 > 节` 三层，逼出了 4 个旧逻辑没有覆盖的问题（已修，见 §11 第 21–25 条）。
另外实测环境的 OneNote COM 接口不可用（`Dispatch` 成功但 `GetHierarchy` 不存在，早绑定 makepy 也失败），
导入实际走的是 UI 驱动通道——这也验证了 README 里「COM 坏了会落到 UI 通道」的说法。

---

## 7. 已知限制（请重点质疑这些）

1. **无名章**：首章是 `Introduction`/`Preface` 这类无 `Chapter N` 字样的书，文本通道可能不单独成章。这类书通常有完整书签，会走书签通道。
2. **书末附属页漏识**：部分教材的附录、Solutions、References 在文本通道未识别（只影响书末附属页，正文 34 章全对）。本版修掉了其中一条可定位的机制（「只留阿拉伯章」规则会把独立缩进的附页簇整簇删掉），但**不保证**该现象全部消失。
3. **OCR 慢**：CPU 上约 5–10 秒/页，整本定位要扫前 30 页，耗时以十分钟计；未装 OCR 则退化为固定页数。
4. **`explicit_cn` 阈值取 ≥3**：只有 1–2 个「第X章」的中文书不会触发显式路由，仍走缩进聚类——若同时有 OCR 抖动，可能丢章（罕见书的取舍）。
5. **导入链路已做一次实机验证（UI 通道），COM 通道仍未实测**：本版在一台 OneNote 桌面上跑通了 UI 驱动通道（6 页一章，页面标题与图片数都读回确认过）。该环境 COM 接口不可用（`Dispatch` 成功但方法不存在、makepy 失败），所以 CDATA 转义、COM 失败回退 UI、`UpdatePageContent` 整页重发这三处**仍未经实机验证**。UI 通道依赖前台窗口与时间等待（建页后等 1.5 s、粘标题后 UIA 读回校验并重试），运行期间别碰键鼠。
6. **OCR 通道整本复跑曾出现进程挂起**（CPU 占用异常低），停止后用「真实 OCR 行 + 生产装配代码」验证。挂起原因未定位，疑似环境/onnxruntime 线程问题，非解析逻辑。
7. **同页的节不单独成文件**：`§1.1` 若和章标题同页，切出来会是 0 页文件，所以它在 `--level 2` 下不会被单列（内容含在章文件里）。
8. **早期数据未复现**：§6 的对照数据来自更早的环境，未在本版本复现。

---

## 8. 检验清单（已逐项完成）

- [x] 跑 `selfcheck.py`，**17/17 通过、退出码 0**（原 7 项 + 10 项回归）。
- [x] 审 `parse_toc_rows` 多簇/单簇判据：结构上互斥（`if ... return`），无同时命中；但多簇分支原有 3 个洞（见 §11）已修。
- [x] 审 `_cluster_levels` 的 `kept`：**能**被单条噪声行突破，已构造反例并修复（豁免要求 ≥2 条章样条目 + 按成员取条目）。
- [x] 审 `RE_CN_CHAPTER`：会误命中 `第三章的证明见…`，已加 `(?!\s*的)`。
- [x] 审 `build_names`：中文优先取「第X章」、英文按顺序编号；标题带数字/年份不再被当页码；重名自动加后缀。
- [ ] 用真实 PDF 跑 `split --dry-run --level 1` 与书签对照（需要你手上的书）。
- [x] `requirements.txt` 与 README 一致：OCR 依赖已拆到 `requirements-ocr.txt`。
- [x] 无敏感信息（无 token/密钥/硬编码个人路径）。

---

## 9. 运行方式

```bash
# 依赖
pip install -r requirements.txt
# 扫描件识别需要（可选，约 100MB 含模型）
pip install -r requirements-ocr.txt

# 主入口（必须让包可导入）
cd pdf2onenote            # 项目根
export PYTHONPATH=$PWD    # Windows: set PYTHONPATH=%CD%
python -m pdf2onenote split --dry-run --level 1 "书.pdf"   # 只看章节，不切文件
python -m pdf2onenote split --level 1 "书.pdf"             # 切分
python -m pdf2onenote split --level 2 --no-front "书.pdf"   # 按节、不要前置页文件
python -m pdf2onenote import "..._章节"                     # 导入 OneNote（Windows）
python -m pdf2onenote all "书.pdf"                         # 一条龙
```

`split` 常用参数：`--level 1|2`（章 / 章+节）、`--toc-scan N`（扫描前 N 页找目录）、`--no-ocr`、`--every N`（兜底页数）、`--no-front`（不产出 `00_前置页`）、`--dry-run`。

> 注意：`--level 2` 现在在文本层 / OCR 通道也生效（原实现只对书签通道有效）。

---

## 10. 提交状态

- 仓库：`github.com/shisanyue13/pdf2onenote`，分支 `main`。
- 提交作者：`shisanyue13 <330418132+shisanyue13@users.noreply.github.com>`。
- 提交前请先跑 `python selfcheck.py`（应 `20 / 20` 通过）。

---

## 11. 已修复问题清单

下表是「问题 → 修法 → 是否被 selfcheck 覆盖」。

| # | 问题（原包） | 严重度 | 修法 | 覆盖 |
|---|---|---|---|---|
| 1 | `all` 子命令必崩（`a.limit` 不存在，`run.bat` 100% 崩） | P0 | `all` 补 `--limit`；`cmd_import` 统一 `getattr` | ✅ 第 13 项 |
| 2 | `_numbers()` 把 `Chapter` 的 `C` / `Index` 的 `I` 当罗马数字 → 双栏 `Chapter N` 右栏整栏丢失 | P1 | 章号正则加 `(?![A-Za-z0-9])` | ✅ 第 9 项 |
| 3 | 页码偏移兜底 off-by-one（页脚页码居中时整本早一页） | P1 | 两处改为 `toc_pages[-1] + 1` | ✅ 第 8 项 |
| 4 | 书签通道 `--level 1` 可能整条丢弃（门槛 3 > build_plan 的 2）；顶层附录/索引被当封面剔除 | P1 | 门槛改 ≥2；顶层叶子只按 `STOP_WORDS/FRONT_MATTER` 过滤 | ✅ 第 15 项 |
| 5 | `build_names` 重名 → `split_pdf` 静默覆盖 | P1 | 重名加 `_2` 后缀 | ✅ 第 12 项 |
| 6 | 首章补页可能把「索引」补成首章，压掉正文十几页 | P1 | 补首章要求 `RE_CHAPTER` 且非 `RE_BACK` | ✅ 第 11 项 |
| 7 | `--level 2` 在文本/OCR 通道完全无效（参数从未使用） | P2 | `parse_toc_rows` 按 `max_level` 收 `level=2` 小节 | ✅ 第 14 项 |
| 8 | `_cluster_levels` 孤立簇豁免无下界 → 单条噪声行劫持章层、整本退回兜底 | P2 | 豁免要求 ≥2 条章样条目 | ✅ 第 10 项 |
| 9 | 噪声簇「合并」后按均值中心取成员 → 被并条目实际消失 | P2 | 新增 `_cluster_groups` 返回成员下标，`parse_toc_rows` 按成员取 | ✅ 第 10 项 |
| 10 | 多簇分支「只留阿拉伯章」把独立缩进的附页簇整簇删掉（§7.2 的一条机制） | P2 | 新增 `_only_back()` 豁免书末附页簇 | 间接（合成用例） |
| 11 | 书签通道同页去重丢首节 | P2 | 保留章、丢同页节（切出来必是 0 页），文档写明 | ✅ README |
| 12 | 行尾年份被当页码（`Chapter 4: The 2024` 整条丢） | P2 | `parse_toc_rows(max_page=total)`，页码越界即视为标题 | ✅ 第 16 项 |
| 13 | 前置页（封面/版权/目录）不产出任何文件 | P2 | `build_plan` 注入 `00_前置页`，`--no-front` 可关 | ✅ 第 12 项 |
| 14 | `import` 按字典序，`01-1_` 排在 `01_` 前面 | P2 | `name_order_key()` 按「章号, 节号」排序 | ✅ 手工验证 |
| 15 | `split_pdf` 对重复/乱序起始页会产出垃圾文件 | P2 | 跳过起始页不递增的条目 | ✅ 第 17 项 |
| 16 | `RE_CN_CHAPTER` 误命中 `第三章的证明见…` | P2 | 加 `(?!\s*的)` | ✅ 第 16 项 |
| 17 | `classify()` 死代码；README 的「四条规则」与实现不符 | 文档 | 删除死代码，README 按实现重写 | — |
| 18 | `onenote.py`：`]]>` 破坏 CDATA；COM 中途失败不回退；分块 `UpdatePageContent` 语义不明 | P2 | CDATA 拆分；COM 异常回退 UI；每页末尾重发整页 | ⚠️ 需实机 |
| 19 | `rapidocr-onnxruntime` 是硬依赖，装不上连核心功能都装不了 | P2 | 拆到 `requirements-ocr.txt`，`install.bat` 失败仅告警 | — |
| 20 | `_pick_toc_pages` 段尾硬限 `best+10`，超长目录被截断 | 低 | 放宽到 40 页 | — |
| 21 | 书签是 `Part > 章 > 节` 三层时，`--level 1` 只切出 2 个大文件（章不在顶层） | P1 | 顶层过半是 `Part/篇` 就把章层下移到 depth 1，Part 层不单独成文件 | ✅ 第 19 项 |
| 22 | 每章重复的 `Exercises`/`Summary`/`Key Terms` 被当成章 | P2 | `STOP_SUBTITLES` + 「同标题出现 ≥3 次」过滤（书末唯一的 References/Index 保留） | ✅ 第 19 项 |
| 23 | 文件名重复编号 `01_1 Introduction` | 低 | 标题自带章号与文件号相同时去掉 | ✅ 第 20 项 |
| 24 | **实机导入直接崩**：`log(..., flush=True)` 撞上 `_log(msg)` 的签名 → `TypeError`，一页都建不出来 | **P0（真机）** | `_log(msg="", **_kw)` | ✅ 第 18 项 |
| 25 | 实机导入页面标题变「未命名」：新版 OneNote 建页异步，标题粘贴被丢；剪贴板 `OpenClipboard` 偶发「拒绝访问」 | P1（真机） | 建页后等 1.5 s + 粘完用 UIA 读回校验并重试（最多 3 次）+ 剪贴板重试 | ⚠️ 实机验证过，未自动化 |
