# -*- coding: utf-8 -*-
"""参考文献条目 → biblatex 友好的 ``.bib``，另出一份校核报告。

源条目是没有字段分隔的自由文本（``[n] 作者. 标题. 出处, 年.``），切分必然不完美。
本模块的原则是：**宁可留空并标记，也不编造字段**。凡是靠猜填进去的东西，都会在校核报告
里连同原始文本一起列出；切不出来就干脆不写这个字段。

cite key 方案（确定性、可复现）：

    key = refNNN     NNN 是文献表里的编号，三位补零（如 ref014）

编号就是书稿正文引用的编号（``[n]``），因此 key 与书稿一一对应，书目重排也不会变。
同一批条目里编号重复时（一个工作区并放多份清单才会出现），后出现的按顺序追加
``-2``、``-3``…，保证唯一。每个条目前另有一行 ``% [n] 原始题录`` 注释，便于人工对照。

字段取值约定：

* ``author``：``姓 缩写`` 形式的姓名列表按英文惯例反转为 ``缩写 姓``，以 ``and`` 连接；
  以 ``et al.``/``等`` 结尾的写成 BibTeX 的 ``and others``。不符合这个形式的作者段
  （机构名、团队名、写法异常）**原样保留**并加双层花括号，让 biblatex 当作一个名字整体
  输出，同时记 ``author-literal`` 待人工确认——绝不硬拆。
* ``title``：按 ``. `` 切出的第二段；切不出时退化为整条并记 ``title-unsplit``。
* ``journal``/``booktitle``/``publisher``/``institution``/``location``：按出处段的形态判定
  条目类型后落到对应字段，判定不出类型时退回 ``@misc`` 并记 ``type-default``。
* ``volume``/``number``/``pages``：只在出处段尾部形如 ``期刊 卷(期), 页`` 时才写。
* ``year``：优先取出处段里的年份；出处段里年份不唯一时取最后一个并记 ``year-ambiguous``。
* ``eprint``/``archivePrefix``：条目已知 arXiv 号时一律写。
* 字段值里的 LaTeX 特殊字符（``& % $ # _ { } \\ ~ ^``）按纯文本转义，不改变字符本身。

**本模块产物不接入出书流程**：书稿目前把参考文献当手写编号的 ``\\chapter*`` 列表渲染，
这里只是另存一份 bib，供将来换 biblatex/biber 时用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .entries import RefEntry

YEAR = re.compile(r"(?:1[6-9]\d{2}|20\d{2})")
SEGMENT = re.compile(r"\.\s+")
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# 出处段尾部的时间戳：``, 2003`` / ``2014–2017`` / ``2024–2025`` / ``, 2026-04`` / ``，2016年12月``。
TRAILING_DATE = re.compile(
    r"[\s,，;；.]*"
    r"(?:1[6-9]\d{2}|20\d{2})"
    r"(?:"
    r"\s*(?:[-–—/]\s*\d{1,4}){1,2}"
    r"|\s*年\s*\d{1,2}\s*月?"
    r"|\s*月\s*\d{1,2}\s*日?"
    r"|\s*年\s*[^\s，。,；;）)]{0,8}"
    r")?"
    r"\s*[.。]?$"
)

# 出处段尾部形如 ``期刊 卷(期), 页`` 的引文结构；页码/文章号可以带字母、可达 16 字符
# （``e2019JE006272``、``eaax7445``、``1242777``）。
_PAGE_TOKEN = r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{1,16}"
JOURNAL = re.compile(
    r"^(?P<journal>.+?)[,\s]\s*(?P<volume>\d{1,5})"
    r"\s*(?:\((?P<number>\d{1,4})\))?"
    r"(?:\s*,\s*(?P<pages>" + _PAGE_TOKEN + r"(?:\s*[–—-]\s*" + _PAGE_TOKEN + r")?))?$"
)

THESIS = re.compile(r"\b(?:ph\.?\s?d\.?\s+thesis|doctoral\s+thesis|dissertation|thesis)\b", re.I)
THESIS_TYPE = re.compile(
    r"^(?P<type>ph\.?\s?d\.?\s+thesis|doctoral\s+thesis|dissertation|thesis)\s*,?\s*", re.I
)
CONFERENCE = re.compile(
    r"\b(?:proceedings|conference|symposium|workshop|meeting|conference|"
    r"lunar and planetary|\bAIAA\b|\bIEEE\b|\bIAC\b|\bSPIE\b)\b",
    re.I,
)
REPORT = re.compile(
    r"\b(?:NASA|ESA|JPL|CBO|OECD|ISECG|EIROforum|National Academies|National Academy|"
    r"National Research Council|Congressional Budget Office|UK Space Agency|"
    r"Space Technology Mission Directorate|Office of Technology|"
    r"Technical Memorandum|Technical Report|white paper|fact ?sheet|roadmap)\b"
    r"|白皮书|公告|通知|规划|官网",
    re.I,
)
REPORT_TYPE = re.compile(
    r"(white\s+paper|technical\s+memorandum|technical\s+report|fact ?sheet|"
    r"final report|position paper|roadmap|白皮书|公告|通知|规划)",
    re.I,
)
BOOK = re.compile(
    r"\b(?:press|publisher|university of|springer|cambridge university|"
    r"oxford university|wiley|kluwer|elsevier)\b",
    re.I,
)

# LaTeX 纯文本转义；源题录里没有现成的 LaTeX 命令，因此一律按字面转义。
_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# 姓名分子：末段是缩写、前面都是姓（可带介词与连字符），只在完全吻合时才硬拆。
_INITIALS = re.compile(r"^(?:[^\W\d_]{1,3}\.(?:\s*[-–]\s*[^\W\d_]{1,3}\.)?)+[^\W\d_]{0,3}$")
_SURNAME_OK = re.compile(r"^[^\W\d_][\w'’\-]*$")
_ET_AL = re.compile(r"[,\s]*(?:et\s+al\.?|and\s+others|others|等)\s*[.,，。]?$", re.I)

FIELD_ORDER = (
    "author",
    "title",
    "journal",
    "booktitle",
    "publisher",
    "institution",
    "location",
    "type",
    "year",
    "volume",
    "number",
    "pages",
    "note",
    "doi",
    "eprint",
    "archivePrefix",
)
_FIELD_WIDTH = 13


@dataclass
class BibRecord:
    """一条条目的 bib 记录。

    ``fields`` 按 :data:`FIELD_ORDER` 排好序，值是**写进 .bib 的字面值**（LaTeX 特殊字符
    已转义），因此 :func:`render_bib` 只是把 ``key = {value}`` 逐行排出，不再做转换。
    原始题录仍可从 ``entry.raw`` 取到。
    """

    entry: RefEntry
    key: str
    kind: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    caveats: list[tuple[str, str]] = field(default_factory=list)

    @property
    def uncertain(self) -> bool:
        """是否存在靠猜填的字段（有 warnings 即为"解析不确定"）。"""
        return bool(self.warnings)

    @property
    def number(self) -> int:
        return self.entry.number

    def get(self, name: str) -> Optional[str]:
        for key, value in self.fields:
            if key == name:
                return value
        return None

    def warning_codes(self) -> list[str]:
        return [code for code, _ in self.warnings]

    def caveat_codes(self) -> list[str]:
        return [code for code, _ in self.caveats]


def cite_key(number: int) -> str:
    """编号 → cite key（``refNNN``，三位补零）。"""
    return f"ref{number:03d}"


def build_records(entries: Sequence[RefEntry]) -> list[BibRecord]:
    """把条目转成 bib 记录；cite key 按 :func:`cite_key` 生成，重号时追加后缀。"""
    seen: dict[str, int] = {}
    records: list[BibRecord] = []
    for entry in entries:
        base = cite_key(entry.number)
        count = seen.get(base, 0)
        seen[base] = count + 1
        key = base if count == 0 else f"{base}-{count + 1}"
        record = _record(entry, key)
        if count:
            record.warnings.insert(
                0,
                (
                    "key-duplicate-number",
                    f"编号 {entry.number} 在本批条目里已出现过，cite key 追加后缀得到 {key}",
                ),
            )
        records.append(record)
    return records


def _record(entry: RefEntry, key: str) -> BibRecord:
    warnings: list[tuple[str, str]] = []
    caveats: list[tuple[str, str]] = []
    raw = entry.raw.strip()
    if CJK.search(raw):
        warnings.append(
            ("chinese", "条目含中文，``作者. 标题. 出处`` 的切分沿用英文习惯，中文题录可能切错")
        )

    authors = entry.authors.strip(" .,;")
    title = entry.title.strip()
    venue = entry.venue_year.strip()
    segments = [part.strip(" .") for part in SEGMENT.split(raw) if part.strip()]

    if not authors or authors == title or authors == raw:
        warnings.append(("author-missing", "切不出作者段（``. `` 分段的第一段为空、或与标题相同），author 字段留空"))
        authors = ""
    if not title or title == raw:
        warnings.append(("title-unsplit", "标题切不出来（``. `` 分段失败），title 写成整条题录"))
        title = raw
    elif title == authors:
        warnings.append(("title-unsplit", "标题与作者段相同，说明 ``. `` 分段失败"))
    if "（" in title:
        warnings.append(("title-annotation", "标题里含全角括号，多半是编者夹注而非标题正文，未做剥离"))
    if len(title) > 200:
        warnings.append(("title-long", f"标题段长 {len(title)} 字符，疑似把编者夹注或出处吞了进去"))
    if _title_looks_cut(title, segments):
        warnings.append(
            (
                "title-truncated",
                "标题段以 ``: I.``/单字母等收尾，说明 ``. `` 分段把标题从中间切断，"
                "真实标题应是本段与紧随其后的那一段拼接",
            )
        )
    dated = [part for part in segments if TRAILING_DATE.search(part)]
    if len(dated) > 1:
        warnings.append(
            (
                "multi-citation",
                f"``. `` 分段里有 {len(dated)} 段以年份收尾，这条题录很可能塞了两条以上文献",
            )
        )

    year = _publication_year(entry, venue, warnings)
    kind, venue_fields, venue_warnings = _venue_fields(entry, venue, raw)
    warnings.extend(venue_warnings)
    if kind in ("article", "inproceedings") and "pages" not in dict(venue_fields):
        caveats.append(("pages-missing", "出处段尾部没有页码，pages 字段留空"))
    if entry.arxiv:
        venue_fields.extend([("eprint", entry.arxiv), ("archivePrefix", "arXiv")])
    if entry.doi:
        venue_fields.append(("doi", entry.doi))

    fields: list[tuple[str, str]] = []
    if authors:
        rendered, author_warnings, author_caveats = _render_authors(authors)
        fields.append(("author", rendered))
        warnings.extend(author_warnings)
        caveats.extend(author_caveats)
    fields.append(("title", _escape(title)))
    ordered = dict(venue_fields)
    if year:
        ordered["year"] = year
    for name in FIELD_ORDER:
        if name in ("author", "title") or name not in ordered:
            continue
        fields.append((name, _escape(ordered[name])))
    return BibRecord(entry=entry, key=key, kind=kind, fields=fields, warnings=warnings, caveats=caveats)


def _title_looks_cut(title: str, segments: Sequence[str]) -> bool:
    """标题段是否被 ``. `` 从中间切断：以 ``: I.`` / 单字母 / 冒号收尾，后面还有段。"""
    if len(segments) < 3:
        return False
    words = title.split()
    if not words:
        return False
    tail = words[-1].rstrip(".")
    if re.fullmatch(r"[IVXLCDM]+", tail) or re.fullmatch(r"[A-Z]", tail):
        return True
    return title.rstrip().endswith((":", "：", ";", "；", ",", "，", "、"))


def _publication_year(entry: RefEntry, venue: str, warnings: list[tuple[str, str]]) -> str:
    """取出版年：优先出处段，其次整条；出处段年份不唯一时取最后一个并记警告。"""
    venue_years = [match.group(0) for match in YEAR.finditer(venue)]
    raw_years = [match.group(0) for match in YEAR.finditer(entry.raw)]
    if not raw_years:
        warnings.append(("no-year", "整条题录里找不到四位年份，year 字段留空"))
        return ""
    if len(venue_years) == 1:
        return venue_years[0]
    warnings.append(
        (
            "year-ambiguous",
            "出处段里"
            + ("没有年份" if not venue_years else f"有 {len(venue_years)} 个年份")
            + f"，改取整条题录里最后一个年份 {raw_years[-1]}",
        )
    )
    return raw_years[-1]


def _venue_fields(
    entry: RefEntry, venue: str, raw: str
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """判定条目类型并切出处段；返回 ``(类型, 字段, 警告)``。"""
    warnings: list[tuple[str, str]] = []
    fields: list[tuple[str, str]] = []
    if not venue:
        warnings.append(("venue-missing", "没有出处段（``. `` 之后直接结束），出处相关字段一律留空"))
        return ("online" if entry.arxiv else "misc", fields, warnings)

    clean = TRAILING_DATE.sub("", venue).strip(" ,;.;，。")
    if not clean:
        warnings.append(("venue-missing", "出处段只有年份，没有期刊/出版者信息，出处相关字段留空"))
        return ("online" if entry.arxiv else "misc", fields, warnings)

    location, publisher = _split_place(clean)

    thesis = THESIS_TYPE.match(clean) or (THESIS.search(clean) and not JOURNAL.match(clean))
    if thesis:
        kind = "thesis"
        match = THESIS_TYPE.match(clean)
        if match:
            fields.append(("type", match.group("type")))
            institution = clean[match.end() :].strip(" ,")
        else:
            institution = clean
        if institution:
            fields.append(("institution", institution))
        return (kind, fields, warnings)

    if CONFERENCE.search(clean):
        fields.append(("booktitle", clean))
        return ("inproceedings", fields, warnings)

    if REPORT.search(clean) or REPORT.search(entry.authors) or REPORT.search(raw):
        report_type = REPORT_TYPE.search(clean)
        if report_type:
            fields.append(("type", report_type.group(0)))
        institution = publisher if publisher else clean
        fields.append(("institution", institution))
        if location:
            fields.append(("location", location))
        if not REPORT.search(clean) or len(institution) > 80:
            warnings.append(
                (
                    "institution-assumed",
                    f"出处段本身不含机构名（机构词来自作者段或正文），institution 直接照抄 "
                    f"``{institution}``，可能把副题或地点也抄了进去，请核对",
                )
            )
        return ("report", fields, warnings)

    if BOOK.search(clean):
        fields.append(("publisher", publisher if publisher else clean))
        if location:
            fields.append(("location", location))
        return ("book", fields, warnings)

    journal = JOURNAL.match(clean)
    if journal:
        fields.append(("journal", journal.group("journal").strip(" ,")))
        fields.append(("volume", journal.group("volume")))
        if journal.group("number"):
            fields.append(("number", journal.group("number")))
        if journal.group("pages"):
            fields.append(("pages", journal.group("pages")))
        return ("article", fields, warnings)

    if _is_arxiv_venue(clean):
        return ("online", fields, warnings)

    if _looks_like_journal_name(clean):
        fields.append(("journal", clean))
        return ("article", fields, warnings)

    warnings.append(
        (
            "type-default",
            f"出处段 ``{clean}`` 判不出期刊/会议/报告/图书形态（也未见 arXiv 号），退回 @misc",
        )
    )
    fields.append(("note", clean))
    return ("misc", fields, warnings)


def _is_arxiv_venue(text: str) -> bool:
    """出处段本身就是 arXiv 号（``arXiv:2001.06683``）时按 @online 处理。"""
    return bool(re.match(r"^arxiv[:.\s]*\d{4}\.\d{4,5}$", text.strip(), re.I))


def _looks_like_journal_name(text: str) -> bool:
    """判断出处段是否只是刊名（``Nature``）而没有卷期页。

    只用形态：不超过 6 个词、无数字、无斜杠、无中文、不是单个全大写词——
    最后一条把 ``CERN`` 这类机构名挡在外面（刊名不会通篇大写且只有一个词）。
    """
    if len(text) > 60 or "/" in text or any(char.isdigit() for char in text):
        return False
    if CJK.search(text):
        return False
    words = text.split()
    if not words or len(words) > 6:
        return False
    if len(words) == 1 and words[0].isupper():
        return False
    return True


def _split_place(text: str) -> tuple[str, str]:
    """``Washington, DC: The National Academies Press`` → ``("Washington, DC", "…Press")``。"""
    head, sep, tail = text.partition(": ")
    if sep and tail.strip() and len(head) <= 40 and "://" not in text:
        return head.strip(), tail.strip()
    return "", ""


def _render_authors(
    authors: str,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """作者段 → BibTeX 姓名列表；转换不了就原样加双层花括号并记警告。"""
    warnings: list[tuple[str, str]] = []
    caveats: list[tuple[str, str]] = []
    body = authors.strip()
    # entries.split_citation 会剥掉段尾的 ``.``，这里补回去再判姓名（末位缩写没有点就认不出）。
    if body and not body.endswith("."):
        body += "."
    open_list = False
    match = _ET_AL.search(body)
    if match and match.start() > 0:
        body = body[: match.start()].strip(" ,")
        open_list = True
    names = [part.strip() for part in body.split(", ") if part.strip()] if body else []
    converted = [_invert_name(name) for name in names] if names else []
    if not converted or any(name is None for name in converted):
        warnings.append(
            (
                "author-literal",
                f"作者段 ``{authors}`` 不是可机械转换的姓名列表（机构名、团队名或写法异常），"
                "原样保留并加双层花括号，biblatex 会整体输出、不参与按姓氏排序；"
                "若这条题录本来就没有作者（如中文论文），这里可能是标题被切成了作者段",
            )
        )
        return ("{" + _escape(authors) + "}", warnings, caveats)
    if open_list:
        caveats.append(
            ("author-etal", "作者段以 et al./等 结尾，只能写成 ``and others``（渲染为 et al.），真实作者名单未收全")
        )
    rendered = [_escape(name) for name in converted if name is not None]
    if open_list:
        rendered.append("others")
    return (" and ".join(rendered), warnings, caveats)


def _invert_name(part: str) -> Optional[str]:
    """``Granvik M.`` → ``M. Granvik``；不认识的形式返回 ``None``。"""
    words = part.split()
    if len(words) < 2:
        return None
    tail = words[-1]
    if not _INITIALS.match(tail):
        return None
    surname = " ".join(words[:-1])
    if CJK.search(surname) or not all(_SURNAME_OK.match(word) for word in surname.split()):
        return None
    return f"{tail} {surname}"


def _escape(value: str) -> str:
    """字段值里的 LaTeX 特殊字符按纯文本转义（字符本身不丢）。"""
    out = []
    for char in value:
        out.append(_LATEX_ESCAPES.get(char, char))
    return "".join(out)


def _field_line(name: str, value: str) -> str:
    return f"  {name.ljust(_FIELD_WIDTH)} = {{{value}}},"


def render_bib(records: Sequence[BibRecord], *, source_note: str = "") -> str:
    """生成 ``.bib`` 全文。"""
    lines = [
        "% 参考文献库 → biblatex/biber",
        "% 由 primer.references 从参考文献 markdown 生成；字段解析是尽力而为，",
        "% 凡是靠猜填的字段都在 references-bib-review.md 里逐条列出。",
        "% cite key：refNNN（NNN = 文献表编号，三位补零），与书稿正文 [n] 一一对应。",
    ]
    if source_note:
        lines.append(f"% 源：{source_note}")
    lines.append("")
    for record in records:
        lines.append(f"% [{record.number}] {record.entry.raw}")
        lines.append(f"@{record.kind}{{{record.key},")
        for name, value in record.fields:
            lines.append(_field_line(name, value))
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_review(
    records: Sequence[BibRecord], source_note: str = ""
) -> str:
    """生成校核报告：统计、按 flag 汇总、逐条列出不确定条目与当时的假定。"""
    flagged = [record for record in records if record.warnings]
    warning_counts: dict[str, list[int]] = {}
    caveat_counts: dict[str, list[int]] = {}
    for record in records:
        for code, _ in record.warnings:
            warning_counts.setdefault(code, []).append(record.number)
        for code, _ in record.caveats:
            caveat_counts.setdefault(code, []).append(record.number)

    lines = [
        "# 参考文献 .bib 校核报告",
        "",
        f"> 源：{source_note or '(未注明)'}，共 {len(records)} 条",
        "> 生成工具：python -m primer.references audit",
        "> cite key：``refNNN``（NNN = 文献表编号，三位补零；编号重复时追加 ``-2``、``-3``…）",
        "> 本报告只说明解析的不确定之处；条目本身没有问题。",
        "",
        "## 统计",
        "",
        f"- 条目总数：{len(records)}",
        f"- **解析不确定（有靠猜的字段）：{len(flagged)} 条**",
    ]
    field_hits: dict[str, int] = {}
    for record in records:
        for name, _ in record.fields:
            field_hits[name] = field_hits.get(name, 0) + 1
    lines.append(
        "- 字段出现次数："
        + "，".join(f"{name}={field_hits.get(name, 0)}" for name in FIELD_ORDER)
    )
    lines.append("")

    if warning_counts:
        lines += ["## 不确定项汇总", "", "| flag | 条数 | 含义 |", "| --- | --- | --- |"]
        for code in sorted(warning_counts):
            text = _WARNING_TEXT.get(code, code)
            lines.append(f"| `{code}` | {len(warning_counts[code])} | {text} |")
        lines.append("")
    if caveat_counts:
        lines += [
            "## 信息不完整但未作猜测（不进上节计数）",
            "",
            "| flag | 条数 | 含义 |",
            "| --- | --- | --- |",
        ]
        for code in sorted(caveat_counts):
            text = _CAVEAT_TEXT.get(code, code)
            numbers = ", ".join(str(number) for number in caveat_counts[code])
            lines.append(f"| `{code}` | {len(caveat_counts[code])} | {text}（条目：{numbers}） |")
        lines.append("")

    lines += ["## 逐条：解析不确定的条目", ""]
    if not flagged:
        lines.append("- 无")
    for record in flagged:
        entry = record.entry
        lines.append(f"### [{entry.number}] {record.key} — {entry.class_letter or '-'}")
        lines.append("")
        lines.append(f"- 位置：``{entry.source}:{entry.line}``")
        lines.append(f"- 原始：``{entry.raw}``")
        lines.append(f"- flags：{', '.join(record.warning_codes())}")
        for code, note in record.warnings:
            lines.append(f"- 假定（`{code}`）：{note}")
        lines.append("- 已写字段：")
        lines.append("")
        lines.append("  ```")
        lines.append(f"  @{record.kind}{{{record.key},")
        for name, value in record.fields:
            lines.append("  " + _field_line(name, value).strip())
        lines.append("  }")
        lines.append("  ```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_WARNING_TEXT = {
    "author-missing": "切不出作者段，author 留空",
    "author-literal": "作者段无法机械转换，原样加双层花括号",
    "title-unsplit": "标题与作者段切不开",
    "title-annotation": "标题里含编者夹注（全角括号）",
    "title-long": "标题段过长，疑含夹注或出处",
    "title-truncated": "标题被 ``. `` 从中间切断（以 ``: I.`` 等收尾）",
    "no-year": "整条题录无年份",
    "year-ambiguous": "出处段年份不唯一，取整条最后的年份",
    "multi-citation": "一条题录里塞了多条文献",
    "type-default": "判不出条目类型，退回 @misc",
    "institution-assumed": "机构名照抄出处段，未从机构词确认",
    "venue-missing": "没有出处段",
    "chinese": "中文题录，分段规则按英文习惯",
    "key-duplicate-number": "编号重复，cite key 追加后缀",
}

_CAVEAT_TEXT = {
    "author-etal": "作者段以 et al./等 结尾，写成 and others",
    "pages-missing": "出处段无页码",
}


def write_bib(path: Path, records: Sequence[BibRecord], *, source_note: str = "") -> None:
    """写出 ``.bib``。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_bib(records, source_note=source_note), encoding="utf-8")


def write_review(
    path: Path, records: Sequence[BibRecord], *, source_note: str = ""
) -> None:
    """写出校核报告。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review(records, source_note=source_note), encoding="utf-8")
