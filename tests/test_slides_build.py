# -*- coding: utf-8 -*-
"""``primer.slides build``：固定页的逐页清单、发射、编译与复核。

夹具沿用 ``test_slides_outline`` 里那份合成的"小 deck"（三个源文件、四个章、23 页、备份 2 页；
``min_per_chapter=3`` 把默认预算抬到每章 3 页，于是正文 12 帧）。除末尾那个真跑排版引擎的
集成测试外，其余都走 ``--tex-only``：到 ``.tex`` 为止，不装 TeX 也跑得完。

这一层的判据是"页与帧对得上"：骨架说多少帧就排出多少帧，空 ``picks`` 在页面上留一个明确
标出的待选记号而不是报错，句子一律经成书同一个行内渲染器，缺字与页数不符是硬失败。**页面上
没有书的页码**：帧脚与备份表的出处写节号（``§7.4``），目录页也不列页码。
"""

from __future__ import annotations

import hashlib
import re
import shutil

import pytest
import yaml

from test_slides_outline import book, config  # noqa: F401  夹具与参数工厂

from primer.book import tables
from primer.slides import beamer
from primer.slides import build as build_mod
from primer.slides.__main__ import build_parser, main
from primer.slides.outline import run as outline_run
from primer.slides.plan import (
    PAGE_LABELS,
    PLACEHOLDER,
    frame_slots,
    page_entries,
    read_page_entries,
)
from primer.slides.theme import default_theme, read_theme
from primer.slides.validate import validate_outline

DECK_REL = ("_primer", "slides", "review-seminar")
OUTLINE_NAME = "outline.yaml"
CANDIDATES_NAME = "candidates.md"

XELATEX = shutil.which("xelatex")
PDFINFO = shutil.which("pdfinfo")
needs_engine = pytest.mark.skipif(
    XELATEX is None or PDFINFO is None,
    reason="xelatex and the poppler tools are needed to compile the deck",
)

# 这份夹具的帧数：开场 2 + 目录 1 + 正文 12 + 横向 3 + 讨论 3 + 备份 2。
TOTAL_FRAMES = 23
FIXED_FRAMES = TOTAL_FRAMES - 12


def deck_dir(root):
    return root.joinpath(*DECK_REL)


def outline_path(root):
    return deck_dir(root) / OUTLINE_NAME


def candidates_path(root):
    return deck_dir(root) / CANDIDATES_NAME


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def dump(path, document):
    path.write_text(
        yaml.safe_dump(
            document, allow_unicode=True, sort_keys=False, default_flow_style=False, width=10**6
        ),
        encoding="utf-8",
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_text(root, extra=()):
    """跑一次 ``build --tex-only``，返回 ``slides.tex`` 的文本。"""
    code = main(
        [
            "build",
            str(outline_path(root)),
            "--project-root",
            str(root),
            "--tex-only",
            *extra,
        ]
    )
    assert code == 0
    return (deck_dir(root) / "slides.tex").read_text(encoding="utf-8")


def frame_picks(document, chapter_index, frame_index, values):
    """把一串 pick 填进某一章的某一帧（正文一帧一页，帧自带 picks）。"""
    document["chapters"][chapter_index]["frames"][frame_index]["picks"] = list(values)
    return document


def fill_all_frames(document, root):
    """把每一章的候选句均衡摊进它自己的各帧——正文不再是章一级的一串 picks。"""
    for chapter in document["chapters"]:
        ids = [
            line.split("|")[1].strip()
            for line in candidates_path(root).read_text(encoding="utf-8").splitlines()
            if line.startswith(f"| s{chapter['chapter']}-")
        ]
        frames = chapter["frames"]
        if not frames:
            continue
        picks = frame_slots(len(ids), len(frames))
        offset = 0
        for frame, count in zip(frames, picks):
            frame["picks"] = ids[offset : offset + count]
            offset += count
    return document


def sentence_of(root, contains):
    """在候选表里找一句含某串的行，返回（id，句子原文，指针）。"""
    for line in candidates_path(root).read_text(encoding="utf-8").splitlines():
        if contains in line and line.startswith("| s"):
            cells = line.split("|")
            return cells[1].strip(), cells[7].strip(), cells[6].strip()
    raise AssertionError(f"no candidate containing {contains!r}")


# ---------------------------------------------------------------- 固定页清单


def test_outline_materialises_the_fixed_pages_as_individual_entries(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    pages = load(outline_path(root))["pages"]

    # 2 开场 + 1 目录 + 3 横向 + 3 讨论 + backup 2
    assert [entry["kind"] for entry in pages] == [
        "opening", "opening",
        "navigation",
        "cross_cutting", "cross_cutting", "cross_cutting",
        "discussion", "discussion", "discussion",
        "backup", "backup",
    ]
    assert [entry["label"] for entry in pages[:3]] == list(PAGE_LABELS["opening"]) + ["目录"]
    # 每一页都是完整的一条：人填的三样与工具算的标记
    for entry in pages:
        assert set(entry) >= {"kind", "label", "picks", "figure", "notes"}
        assert entry["picks"] == [] and entry["figure"] is None and entry["notes"] == ""
    # 目录与备份的正文由工具算出，标 auto；开场、横向、讨论要人填
    auto = {entry["kind"]: entry.get("auto", False) for entry in pages}
    assert auto["navigation"] is True and auto["backup"] is True
    assert auto["opening"] is False and auto["cross_cutting"] is False
    # 页记录与常量生成的清单逐条一致
    assert pages == [dict(entry.as_dict()) for entry in page_entries(2)]


def test_read_page_entries_still_accepts_the_old_count_only_form(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"] = [
        {"kind": "opening", "label": "开场", "count": 2},
        {"kind": "navigation", "label": "目录", "count": 1},
        {"kind": "cross_cutting", "label": "横向", "count": 3},
        {"kind": "discussion", "label": "讨论", "count": 3},
        {"kind": "backup", "label": "备份", "count": 2},
    ]

    entries = read_page_entries(document)

    assert len(entries) == 11
    assert entries[0].label == "封面" and entries[2].auto is True


def test_check_flags_a_fixed_page_pick_that_is_not_a_candidate(book, capsys):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"][1]["picks"] = ["s9-99p01"]
    dump(outline_path(root), document)

    code = main(["check", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[pick-missing]" in out
    assert "主旨" in out and "s9-99p01" in out


def test_check_flags_a_pages_block_that_does_not_match_the_control_model(book, capsys):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"].pop()  # 少一页备份
    dump(outline_path(root), document)

    code = main(["check", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[pages-count]" in out
    assert "10 vs 11" in out


def test_validate_reports_an_unknown_fixed_page_kind():
    outline = {
        "version": 2,
        "deck": {"backup": 0, "capacity_per_page": 176,
                 "min_per_chapter": 0, "max_per_chapter": 9},
        "pages": [{"kind": "explosion", "label": "?", "picks": []}],
        "chapters": [],
    }

    findings = validate_outline(outline, candidates={})

    assert any(finding.code == "pages-kind" and finding.fatal for finding in findings)


def test_merge_keeps_page_and_chapter_figure_and_notes(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"][1]["picks"] = ["s1-0p01"]
    document["pages"][1]["notes"] = "开场定调"
    document["pages"][9]["figure"] = "1-1"
    document["chapters"][0]["figure"] = "1-1"
    document["chapters"][0]["notes"] = "别展开"
    dump(outline_path(root), document)

    outline_run(root, config(), spec_path=spec, merge=True)
    merged = load(outline_path(root))

    assert merged["pages"][1]["picks"] == ["s1-0p01"]
    assert merged["pages"][1]["notes"] == "开场定调"
    assert merged["pages"][9]["figure"] == "1-1"
    assert merged["chapters"][0]["figure"] == "1-1"
    assert merged["chapters"][0]["notes"] == "别展开"


# ---------------------------------------------------------------- 过闸


def test_build_refuses_to_write_when_the_outline_has_an_error(book, capsys):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(document, 0, 0, ["s1-99p01"])
    dump(outline_path(root), document)
    before = sorted(path.name for path in deck_dir(root).iterdir())

    code = main(["build", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[pick-missing]" in out
    assert "不生成：" in out
    # 盘上一个字节没多、没少、没改
    assert sorted(path.name for path in deck_dir(root).iterdir()) == before
    assert not (deck_dir(root) / "slides.tex").exists()


def test_build_refuses_when_the_deck_spec_has_moved_on(book, capsys):
    """规格变了（章与节可能数得不一样）就是 error，挡住生成。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("chapter_start: 3", "chapter_start: 4"),
        encoding="utf-8",
    )

    code = main(["build", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[stale-outline]" in out
    assert "spec_sha256" in out
    assert not (deck_dir(root) / "slides.tex").exists()


def test_editing_a_source_only_warns_and_still_builds(book, capsys):
    """源文件内容变了只是 warning（节号仍有效），报告提醒重读 picks，但仍生成。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    source = root / "成果文件" / "第一篇_甲篇.md"
    source.write_text(
        source.read_text(encoding="utf-8").replace("第二句是别的话。", "第二句换了个说法。"),
        encoding="utf-8",
    )

    text = build_text(root)
    out = capsys.readouterr().out

    assert "[stale-outline]" in out
    assert "sha256" in out
    assert r"\begin{frame}" in text


def test_the_skeleton_still_says_version_2_after_a_merge(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    outline_run(root, config(), spec_path=spec, merge=True)

    assert load(outline_path(root))["version"] == 2


# ---------------------------------------------------------------- 发射


def test_build_emits_one_frame_per_page_and_per_chapter_slide(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    # 11 个固定页 + 12 个正文页（预算 3+3+3+3）
    assert text.count(r"\begin{frame}") == TOTAL_FRAMES
    assert text.count(r"\begin{frame}") == text.count(r"\end{frame}")
    # 正文帧的标题与页脚由 \PageTop 画：标题是章名，页脚左是出处（节号，没有页码）、右是页序
    assert r"\PageTop{第一章　第一章标题}{详见 §1.1}{4/23}" in text
    assert r"\PageTop{附录 A　附录标题}{详见 §A.1}{13/23}" in text
    # 空 picks 打成待选记号：正文 12 帧 × 4 + 横向讨论 6 帧 × 4 + 主旨 1 + 备份 2
    assert text.count(PLACEHOLDER) == 12 * 4 + 6 * 4 + 1 + 2


def test_no_book_page_number_reaches_the_slides(book):
    """页面上没有书的页码：帧脚只有节号，"详见 §1.1" 后面不跟 "，p.5"。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert "详见 §1.1，p." not in text
    assert re.search(r"详见 §[0-9A-Za-z.]+，", text) is None
    # 页码列与结构图的页轴都不在了
    assert "书页" not in text and "印刷页" not in text
    assert r"\MapBand" not in text and r"\MapAxis" not in text and r"\MapGap" not in text
    assert r"\tmPageW" not in text and r"\tmAxisY" not in text


def test_a_content_frame_carries_the_theme_and_the_page_wiring(book):
    """一帧的版式：篇色、进度条区间、标题带（标题／指针／页序）、四条正文条目。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    item_line = r"\BodyItem[thmmuted]{{\color{thmmuted}" + PLACEHOLDER + "}}"
    frame = "\n".join(
        [
            r"\begin{frame}[plain,t]",
            r"  \SetVolume{V1}",
            r"  \SetPageBar{7}{9}",
            r"  \PageTop{第二章　第二章标题}{详见 §2.1}{7/23}",
            r"  \vbox to \dimexpr\paperheight-\tmTmpA-\tmBodyGuard-\tmBodySlack\relax{%",
            r"  \vfil",
            r"  \noindent",
            r"  \begin{minipage}[t]{1.0\textwidth}\vspace{0pt}",
            *("  " + item_line for _ in range(4)),
            r"  \end{minipage}",
            r"  \par",
            r"  \vfil",
            r"  }%",
            r"  \vspace*{\tmBodyGuard}",
            r"\end{frame}",
        ]
    )
    # 第二章没有插图，所以它是整宽的要点帧；篇色取它所属的那一篇（第一章与第二章同属 V1）
    assert frame in text
    # 进度条只在内容页、横向页与讨论页画（导言区里那一条定义不算）
    assert text.count(r"\SetPageBar{") == 12 + 3 + 3


def test_the_title_rule_and_the_type_scale_come_from_the_theme(book):
    """导言区里的画布、六档字号与配色 token 全部取自 theme 块。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert r"\geometry{paperwidth=960 bp,paperheight=540 bp}" in text
    assert r"\newlength{\tmMargin}\setlength{\tmMargin}{0.05\paperwidth}" in text
    assert r"\newcommand{\FzTitle}{\fontsize{28}{34}\selectfont}" in text
    assert r"\newcommand{\FzBody}{\fontsize{22}{30}\selectfont}" in text
    assert r"\newcommand{\FzTabHead}{\fontsize{18}{24}\selectfont}" in text
    assert r"\newcommand{\FzTab}{\fontsize{16}{24}\selectfont}" in text
    assert r"\newcommand{\FzCap}{\fontsize{18}{24}\selectfont}" in text
    assert r"\newcommand{\FzFoot}{\fontsize{14}{17}\selectfont}" in text
    assert r"\definecolor{thmink}{HTML}{1a1a1a}" in text
    assert r"\definecolor{thmmuted}{HTML}{6b7280}" in text
    assert r"\definecolor{thmhair}{HTML}{d1d5db}" in text
    assert r"\definecolor{thmaccent}{HTML}{1f4e79}" in text
    assert r"\definecolor{thmV2}{HTML}{2f6f9f}" in text
    assert r"\definecolor{thmVA}{HTML}{9aa5b1}" in text
    # 标题下那道短横横贯版心、发丝线
    assert r"\def\tmTitleRuleLen{\textwidth}" in text


def test_a_hand_edited_theme_reaches_the_preamble(book):
    """theme 块是数据：改了 YAML，导言区跟着变。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["theme"]["canvas"]["width_bp"] = 1024
    document["theme"]["canvas"]["height_bp"] = 576
    document["theme"]["type"]["body"]["size_pt"] = 24
    document["theme"]["palette"]["accent"] = "#b03030"
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\geometry{paperwidth=1024 bp,paperheight=576 bp}" in text
    assert r"\newcommand{\FzBody}{\fontsize{24}{30}\selectfont}" in text
    assert r"\definecolor{thmaccent}{HTML}{b03030}" in text
    assert r"\color{thmaccent}\rule{\tmCoverRuleLen}{\tmCoverRuleW}" in text


def test_a_hand_edited_theme_font_reaches_the_preamble(book):
    """theme.fonts 是数据：改了某一族的字体名，导言区那一条跟着变。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["theme"]["fonts"]["cjk_sans"] = "PingFang SC"
    document["theme"]["fonts"]["main"] = "Arial"
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\setCJKsansfont[AutoFakeBold=0]{PingFang SC}" in text
    assert r"\setmainfont{Arial}" in text


def test_the_body_is_justified_and_the_structure_text_stays_left(book):
    r"""正文要点两端对齐（\justifying），标题与表头仍左对齐、不拉伸。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert r"\usepackage{ragged2e}" in text
    assert (
        r"\parbox[t]{\dimexpr\linewidth-4.7mm\relax}{\justifying\FzBody\strut#2}"
        in text
    )
    assert r"{\raggedright\FzTitle\sffamily\color{thmtitlefg}#1}" in text


def test_the_content_frames_get_a_footer_guard_and_vertical_centering(book):
    r"""正文页、主旨页、目录页：内容装在版心高的 \vbox 里、两个 \vfil 平分余量，收尾护栏。

    盒高 ``\paperheight-\tmTmpA-\tmBodyGuard`` 是实测出来的：只用 \vfill 的话，它与
    beamer 帧尾的 1fill 同阶、空白三分，正文落到整页三分之一处（下净空约是上净空的两倍）。
    """
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    guard = r"\vspace*{\tmBodyGuard}"
    box = r"\vbox to \dimexpr\paperheight-\tmTmpA-\tmBodyGuard-\tmBodySlack\relax{%"
    # 正文页 12 + 主旨 1（横向/讨论页同正文版式）
    assert text.count(guard) >= 12 + 1
    assert text.count(box) >= 12 + 1
    # 主旨页不再用 \begin{center} 把句子缩在中间，改成左对齐的整宽段落
    assert r"\begin{center}" not in text
    assert r"\noindent{\FzBody\justifying " in text


def test_a_points_frame_puts_the_body_and_the_figure_in_one_row():
    r"""有图的一页：一行两块 [t] minipage（0.63 + 0.36），图列 \centering、图注仍左对齐。"""
    frame = beamer.Frame(
        kind="points",
        title="题",
        points=("甲。", "乙。"),
        figure=beamer.FigureRef("1-1", "题注", "figs/a.png"),
        figure_label="图 1-1　题注",
    )

    lines = beamer._points_frame(frame, context())

    assert r"\begin{minipage}[t]{0.63\textwidth}\vspace{0pt}" in lines
    assert r"\begin{minipage}[t]{0.36\textwidth}\vspace{0pt}\centering" in lines
    assert r"\end{minipage}\hfill%" in lines
    # 图注那一行仍 \raggedright，不被整栏的 \centering 拉成居中
    assert (
        r"\begin{minipage}{\linewidth}\raggedright\FzSecond\color{thmmuted}图 1-1　题注\end{minipage}\par"
        in lines
    )
    assert r"\vbox to \dimexpr\paperheight-\tmTmpA-\tmBodyGuard-\tmBodySlack\relax{%" in lines
    assert lines[-3:] == [r"\vfil", r"}%", r"\vspace*{\tmBodyGuard}"]


def test_the_contents_page_lists_the_parts_and_the_chapters_without_pages(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    # 目录一页：篇／章一行一条，行首一枚篇色小块；表头三列，没有页码列
    assert r"\rowcolor{volhead}\Hd{章号} & \Hd{篇 / 章} & \Hd{正片页}" in text
    assert r"\RowCap{V1}第一章 & 第一章标题 & 3" in text
    assert r"\RowCap{V2}第三章 & 第三章标题 & 3" in text
    assert r"\RowCap{VA}附录 A & 附录标题 & 3" in text
    # 篇行：篇名取自 deck.yaml，章号那一格空着（篇色小块仍在）
    assert r"\RowCap{V2} & 第二篇　乙篇 & " in text
    assert r"\toprule" not in text and r"\midrule" not in text and r"\bottomrule" not in text
    assert r">{\raggedleft\arraybackslash}p{" in text
    # 目录页的帧标题是「目录」，不是旧的三张导航页
    assert r"\PageTop{目录}" in text
    for gone in ("结构图", "阅读路径", "章节索引"):
        assert gone not in text


def test_the_contents_columns_are_sized_to_their_content(book):
    """目录表的列宽：数字列收在内容上，余量全给"篇 / 章"——一章一行，版心不空。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    spec_obj = build_mod.resolve_spec(root, "review-seminar", spec)
    rows, volumes = build_mod._toc_rows(document["chapters"], spec_obj)
    theme = read_theme(document)[0]
    unit_mm = 0.5 * theme.level("table_body").size * 25.4 / 72

    assert rows[0] == ("章号", "篇 / 章", "正片页")
    assert rows[1] == ("", "第一篇　甲篇", "")
    assert rows[2] == ("第一章", "第一章标题", "3")
    assert volumes[1] == "V1"

    fractions = build_mod._toc_fractions(rows, theme)
    gap_mm = build_mod.TABLE_GAP_PT * (len(rows[0]) - 1) / len(rows[0]) * 25.4 / 72
    widths = [fraction * theme.text_width_mm - gap_mm for fraction in fractions]

    # 列宽加起来正好是版心宽（版心减去列间距），各列不越界也不留缝
    assert sum(fractions) == pytest.approx(1.0)
    assert sum(widths) == pytest.approx(theme.text_width_mm - len(rows[0]) * gap_mm)
    # "篇 / 章"那一栏最宽，且放得下最长的那一格
    assert widths[1] == max(widths)
    widest = max(tables.natural_width(str(row[1])) for row in rows) * unit_mm
    assert widths[1] >= widest
    # 数字列收在内容上：正片页那一列不到"篇 / 章"栏的三分之一
    assert widths[2] < widths[1] / 3


def test_the_lookup_geometry_measures_the_floor_type_and_the_frame_body():
    """备份查找表的三列宽与每帧行数：字号取主题下限，行数按它的行距量。"""
    theme = default_theme()
    columns = beamer.lookup_columns(["第八章"], ["详见 §8.6"], theme)

    assert beamer.lookup_level_command(theme) == "FzFoot"
    # 正文区 163.96 mm ÷ 页脚行距 17 pt（= 6.00 mm）= 27 行
    assert columns.lines_per_frame == 27
    assert columns.label_mm + columns.pointer_mm + columns.gist_mm < theme.text_width_mm
    assert columns.gist_units == int(columns.gist_mm / (theme.level("footer").size * 25.4 / 72))


def test_picks_go_through_the_book_inline_renderer(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    identifier, sentence, _ = sentence_of(root, "第二段有粗体")
    document = load(outline_path(root))
    frame_picks(document, 0, 0, [identifier])
    dump(outline_path(root), document)

    text = build_text(root)

    # 原文是 `**第二段有粗体**，并且提到了 2024 年。`，成书的 render_inline 把它变成 \textbf；
    # 行内渲染之后再过 prose.normalize_pick_text：数字与单位（年）之间的空格去掉。
    assert r"\BodyItem{\textbf{第二段有粗体}，并且提到了 2024年。}" in text
    assert sentence == "**第二段有粗体**，并且提到了 2024 年。"


def test_free_text_and_derived_picks_reach_the_points_and_the_lookup(book):
    """自由文本与提炼句和候选句同一条渲染路径：进要点页，也进备份出处查找表。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    identifier, _, _ = sentence_of(root, "第二段有粗体")
    document = load(outline_path(root))
    frame_picks(
        document,
        0,
        0,
        ["手写的一句自由文本。", {"text": "从候选提炼的一句。", "derived_from": [identifier]}],
    )
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\BodyItem{手写的一句自由文本。}" in text
    assert r"\BodyItem{从候选提炼的一句。}" in text
    # 备份出处查找表里也各有一条（要点页 + 查找表 = 至少出现两次）
    assert text.count("手写的一句自由文本。") >= 2
    assert text.count("从候选提炼的一句。") >= 2


def test_a_text_pick_with_no_source_leaves_the_lookup_pointer_blank(book):
    """自由文本没有书的出处，来源表的指针留空——不硬塞一个假出处。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(document, 0, 0, ["手写的一句自由文本。"])
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\makebox[" in text  # 查找表确实发出来了
    lookup_line = next(
        line
        for line in text.splitlines()
        if "手写的一句自由文本。" in line and r"\RowCap" in line
    )
    assert "详见" not in lookup_line


def test_free_text_on_a_fixed_page_reaches_the_points(book):
    """固定页（横向／讨论）上手写的句子同样照原样进要点。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"][3]["picks"] = ["科学、工程与战略研判"]
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\BodyItem{科学、工程与战略研判}" in text


def test_straight_quotes_in_a_pick_become_chinese_quotes(book):
    """源句子里的 ASCII 直引号在幻灯片上按成书同一套规整成中文弯引号。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(document, 0, 0, ['宜居性从"条件判据"走向"演化路径"。'])
    dump(outline_path(root), document)

    text = build_text(root)

    assert '"' not in text
    assert "宜居性从“条件判据”走向“演化路径”。" in text


def test_an_odd_straight_quote_is_left_alone_and_reported_by_check(book, capsys):
    """奇数个直引号的原样保留（不猜哪一个是收尾），check 报一条警告并点出帧与片段。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(document, 0, 0, ['他只说了"开始。'])
    dump(outline_path(root), document)

    code = main(["check", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert "[quotes-unpaired]" in out
    assert "帧 第一章 第 1 页" in out
    assert "他只说了" in out
    # 只是警告：不挡生成，页面上那一句原样保留
    text = build_text(root)
    assert '他只说了"开始。' in text
    assert "他只说了“开始。" not in text


def test_a_link_keeps_its_label_and_loses_the_url_footnote(book, capsys):
    """幻灯片上链接只留标签，不发 URL 脚注；丢弃的目标进讲者备注与一条提示。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(
        document, 0, 0, ["见[深空探测学报](https://journal.hep.com.cn/jdse/EN/PDF/x)。"]
    )
    dump(outline_path(root), document)

    text = build_text(root)
    out = capsys.readouterr().out

    assert r"\footnote" not in text
    assert "见深空探测学报。" in text  # 只剩标签文字
    assert r"\note{" in text and "链接目标" in text and r"\texttt{" in text
    assert "[links-dropped]" in out
    assert "https://journal.hep.com.cn/jdse/EN/PDF/x" in out


def test_log_findings_lock_the_log_line_to_the_page_it_belongs_to(tmp_path):
    """一处映射：``at line N`` 在一帧即一页的 beamer 里归给消息**之后**那个出页标记。"""
    log = tmp_path / "slides.log"
    log.write_text(
        "[1\n\n]\nOverfull \\vbox (160.43008pt too high) detected at line 9\n []\n\n[2\n\n]\n",
        encoding="utf-8",
    )

    overfull = next(
        item for item in build_mod.read_log_findings(log) if item.code == "overfull-vbox"
    )

    assert "page 2" in overfull.location
    assert "line(s) 9" in overfull.location


def test_build_emits_one_frame_per_frame_entry_with_its_own_picks(book):
    """正文一帧一页：每一帧照骨架 frames 的那一条排，帧的 picks 进它自己那一页。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    ids = [
        line.split("|")[1].strip()
        for line in candidates_path(root).read_text(encoding="utf-8").splitlines()
        if line.startswith("| s1-")
    ]
    assert len(ids) == 4
    # 第一章预算 3 页：2+1+1，四条各进它自己那一帧
    frame_picks(document, 0, 0, ids[:2])
    frame_picks(document, 0, 1, ids[2:3])
    frame_picks(document, 0, 2, ids[3:])
    dump(outline_path(root), document)

    text = build_text(root)
    bodies = [
        body
        for body in re.findall(r"\\begin\{frame\}(.*?)\\end\{frame\}", text, re.S)
        if r"\PageTop{第一章　第一章标题}" in body
    ]

    assert [body.count(r"\BodyItem") for body in bodies] == [2, 1, 1]
    assert all(PLACEHOLDER not in body for body in bodies)


def test_a_frame_marker_is_dropped_where_its_picks_went_to_another_frame(book):
    """一帧一句：句子落在第几帧，待选记号就少几个，其余帧照默认 4 个排。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    identifier, _, _ = sentence_of(root, "第二段有粗体")
    document = load(outline_path(root))
    frame_picks(document, 0, 0, [identifier])
    dump(outline_path(root), document)

    text = build_text(root)

    first = text.split(r"\PageTop{第一章　第一章标题}", 1)[1].split(r"\end{frame}", 1)[0]
    assert first.count(r"\BodyItem") == 1
    assert PLACEHOLDER not in first


def test_notes_reach_the_note_command_and_empty_notes_emit_nothing(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["chapters"][0]["notes"] = "只讲一句"
    document["pages"][1]["notes"] = "开场定调"
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\note{只讲一句}" in text
    assert r"\note{开场定调}" in text
    # 一章的备注只发在第一帧上，不在该章每一帧重复
    assert text.count(r"\note{只讲一句}") == 1


def test_a_build_without_a_report_produces_placeholder_backup_pages(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert text.count(r"\PageTop{备份 1}") == 1
    assert text.count(r"\PageTop{备份 2}") == 1
    assert r"\PageTop{备份 3}" not in text
    # 一条要点也没有：备份页打待选记号，不留一张空表
    backup = text.split(r"\PageTop{备份 1}", 1)[1].split(r"\end{frame}", 1)[0]
    assert PLACEHOLDER in backup and r"\RowCap" not in backup


def test_backup_pages_are_a_lookup_table_not_a_second_copy_of_the_deck(book):
    """备份页的职责是回答"这条要点出自哪一节"：一条一行，整句不重排。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    identifier, _, _ = sentence_of(root, "第二段有粗体")
    document = load(outline_path(root))
    frame_picks(document, 0, 0, [identifier])
    dump(outline_path(root), document)

    text = build_text(root)

    backup = text.split(r"\PageTop{备份 1}", 1)[1].split(r"\end{frame}", 1)[0]
    assert r"\RowCap{V1}\makebox" in backup and "{第一章}" in backup
    # 出处写节号（章首正文写章标签），没有页码
    assert "详见 第一章" in backup
    assert "第二段有粗体" in backup
    # 句子被截到 16 字，整句（含粗体）与要点版式都不在备份页上
    assert r"\textbf{第二段有粗体}，并且提到了 2024 年。" not in backup
    assert r"\BodyItem" not in backup


def test_the_backup_frames_do_not_grow_with_the_number_of_sentences(book):
    """预留几帧就是几帧：圈满候选也不会把骨架撑长。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = fill_all_frames(load(outline_path(root)), root)
    assert all(
        pick for chapter in document["chapters"] for frame in chapter["frames"] for pick in frame["picks"]
    )
    dump(outline_path(root), document)

    text = build_text(root)

    assert text.count(r"\begin{frame}") == TOTAL_FRAMES
    assert len(re.findall(r"\\PageTop\{备份 \d+\}", text)) == 2


def test_slides_tex_is_byte_identical_on_a_second_run(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    build_text(root)
    before = digest(deck_dir(root) / "slides.tex")

    build_text(root)

    assert digest(deck_dir(root) / "slides.tex") == before


def test_build_writes_nothing_outside_the_deck_directory(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    outside = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(deck_dir(root))
    }

    build_text(root)
    makefile = (deck_dir(root) / "Makefile").read_text(encoding="utf-8")

    assert {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(deck_dir(root))
    } == outside
    # Makefile 走成书同一套编译计划，只是换了署名与目录
    assert "JOB  = slides" in makefile
    assert "由 primer-slides 生成" in makefile
    assert "由 primer-book 生成" not in makefile
    assert "xdvipdfmx" in makefile


def test_the_deck_takes_its_sans_fonts_from_the_theme_and_the_title_from_the_spec(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    # 字体取自幻灯片自己的无衬线栈（theme.fonts）
    fonts = default_theme().fonts
    assert r"\setmainfont{" + fonts["main"] + "}" in text
    assert r"\setsansfont{" + fonts["sans"] + "}" in text
    assert r"\setmonofont{" + fonts["mono"] + "}" in text
    assert (
        r"\setCJKmainfont[AutoFakeBold=2.5,AutoFakeSlant=0.15]{" + fonts["cjk_main"] + "}"
        in text
    )
    assert r"\setCJKsansfont[AutoFakeBold=0]{" + fonts["cjk_sans"] + "}" in text
    assert r"\setCJKmonofont[AutoFakeBold=2.5]{" + fonts["cjk_mono"] + "}" in text
    assert r"\renewcommand{\familydefault}{\rmdefault}" in text
    assert r"\usefonttheme{serif}" not in text
    assert "Songti SC" not in text and "Times New Roman" not in text
    # 书目取自 deck.yaml 的 book 段；图形搜索路径取 source_root
    assert r"\subtitle{（测试）}" in text
    assert "合成书" in text
    assert r"\graphicspath{{成果文件/}}" in text


def test_the_title_page_carries_the_presenter_only_when_the_human_filled_it(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    assert "张三" not in build_text(root)

    document = load(outline_path(root))
    document["deck"]["presenter"] = "张三"
    document["deck"]["occasion"] = "评审会"
    dump(outline_path(root), document)

    text = build_text(root)
    assert "张三　评审会" in text


def test_the_cover_carries_the_first_pick_as_a_subtitle_line(book):
    """pages[0].picks[:1] 渲成副题行：书目副题之下、正文字号、灰阶、左对齐。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"][0]["picks"] = ["科学、工程与战略研判"]
    dump(outline_path(root), document)

    text = build_text(root)

    subtitle = r"{\FzBody\color{thmcoversubfg}（测试）}\par"
    subline = r"{\FzBody\color{thmmuted}科学、工程与战略研判}\par"
    assert subline in text
    assert subtitle in text
    assert text.index(subtitle) < text.index(subline)
    assert r"\vspace{" + f"{beamer.COVER_SUBLINE_GAP_MM:g}" + r"mm}" in text


def test_a_cover_without_a_pick_emits_no_subtitle_line(book):
    """pages[0].picks 空着：封面与从前逐字相同，不发那一行。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert r"\color{thmmuted}科学" not in text
    cover = text.split(r"\begin{frame}[plain,t]", 1)[1].split(r"\end{frame}", 1)[0]
    assert cover.count(r"\FzBody") == 1


def test_the_cover_band_is_measured_so_the_hairline_never_crosses_the_block(book):
    """色带高按标题块实测：发丝线不再穿字（副题行加进来也不怕）。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert r"\sbox{\tmCoverBoxS}{%" in text
    assert (
        r"\setlength{\tmCoverH}{\dimexpr\tmCoverTop+\ht\tmCoverBoxS+\dp\tmCoverBoxS+\tmCoverClear\relax}"
        in text
    )
    assert r"\ifdim\tmCoverH<\tmCoverMinH\relax\setlength{\tmCoverH}{\tmCoverMinH}\fi" in text
    assert r"\def\tmCoverH{46mm}" not in text


def test_the_cover_signature_sits_centered_near_the_page_bottom(book):
    """署名在页面下部居中（anchor=south + \tmCoverFootY），页序仍在右下。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["deck"]["presenter"] = "张三"
    document["deck"]["occasion"] = "评审会"
    dump(outline_path(root), document)

    text = build_text(root)

    assert (
        r"\node[anchor=south,inner sep=0pt] at ([yshift=\tmCoverFootY]current page.south)"
        in text
    )
    assert r"\begin{minipage}{\textwidth}\centering" in text
    assert "张三　评审会" in text
    assert r"current page.south east" in text


def test_a_lead_enumerator_in_a_pick_is_stripped_on_the_slide(book):
    """行首原文编号在出页面之前剥掉（形式由排版系统定），封面副题走同一条清洗。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    frame_picks(document, 0, 0, ["（一）我国水平"])
    document["pages"][0]["picks"] = ["（三）科学、工程与战略研判"]
    dump(outline_path(root), document)

    text = build_text(root)

    assert r"\BodyItem{我国水平}" in text
    assert r"\BodyItem{（一）" not in text
    assert r"{\FzBody\color{thmmuted}科学、工程与战略研判}\par" in text
    assert "（三）科学" not in text


# ---------------------------------------------------------------- 插图


def test_a_chapter_takes_its_figures_from_the_source_markdown(book):
    """插图由源 markdown 按章重编：第一章的两帧配它的两幅图，图注取自源题注。"""
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    text = build_text(root)

    assert r"\adjustbox{max width=\linewidth,max height=" in text
    assert r"}{\includegraphics[width=\linewidth]{图/one.png}}" in text
    assert r"}{\includegraphics[width=\linewidth]{图/two.png}}" in text
    # 图注走题注层（18 pt / 页脚灰）
    assert r"\FzSecond\color{thmmuted}图 1-1　示例图" in text
    assert r"\FzSecond\color{thmmuted}图 1-2　第二幅示例图" in text


def test_a_chapter_can_turn_its_figure_off_or_pin_one_number(book):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["chapters"][0]["figure"] = ""
    dump(outline_path(root), document)
    assert "one.png" not in build_text(root)

    # 钉一个编号：第二章（自己一幅图也没有）可以整章用第一章的那一幅——编号是全 deck 的
    document = load(outline_path(root))
    document["chapters"][1]["figure"] = "1-2"
    dump(outline_path(root), document)
    text = build_text(root)
    chapter_two = text.split(r"\PageTop{第二章　第二章标题}", 1)[1].split(r"\end{frame}", 1)[0]
    assert r"}{\includegraphics[width=\linewidth]{图/two.png}}" in chapter_two


def test_a_figure_number_that_is_not_in_the_deck_is_a_warning(book, capsys):
    root, spec = book
    outline_run(root, config(), spec_path=spec)
    document = load(outline_path(root))
    document["pages"][3]["figure"] = "9-9"
    dump(outline_path(root), document)

    text = build_text(root)
    out = capsys.readouterr().out

    assert "[figure-missing]" in out
    assert "9-9" in out


# ---------------------------------------------------------------- 表格


def context(**overrides):
    values = dict(
        title="甲",
        subtitle="",
        presenter="",
        occasion="",
        institute="",
        graphics_path="成果文件/",
    )
    values.update(overrides)
    return beamer.DeckContext(**values)


def test_a_wide_table_wraps_instead_of_being_rotated():
    """beamer 里没有可转的页：成书的横向档翻成「按版心宽换行排」，并如实上报。"""
    wide = "深空探测专用技术产业链调研" * 2
    frame = beamer.Frame(
        kind="table",
        title="目录",
        rows=tuple(
            [("章号", "篇 / 章", "页码", "印刷页", "正片页", "备注")]
            + [(f"附录 A　{wide}", wide, wide, wide, wide, wide)]
        ),
    )

    document, findings, _, notes = beamer.render_document([frame], context())

    assert r"\begin{tabular}{@{}>{\raggedright\arraybackslash}p{\dimexpr " in document
    assert "allowframebreaks" not in document
    assert any("横向档在 beamer 无页可转" in note for note in notes)
    assert [item.code for item in findings] == ["table-layout"]
    assert findings[0].severity == "info"


def test_a_table_too_tall_even_wrapped_is_scaled():
    # 一帧正文可用高度约 164 mm（960 × 540 bp 画布）：30 行 16 pt 的表排不下
    rows = [("序", "条目")] + [(str(index), f"第 {index} 条") for index in range(1, 30)]
    frame = beamer.Frame(kind="table", title="长表", rows=tuple(rows))

    document, findings, _, notes = beamer.render_document([frame], context())

    assert r"\begin{adjustbox}{max width=\textwidth,max height=" in document
    assert "allowframebreaks" not in document
    assert any("缩放" in note for note in notes)
    assert findings and findings[0].severity == "info"


def test_a_table_that_cannot_be_read_even_scaled_breaks_across_frames():
    rows = [("序", "条目")] + [(str(index), f"第 {index} 条") for index in range(1, 1201)]
    frame = beamer.Frame(kind="table", title="超长表", rows=tuple(rows))

    document, findings, _, notes = beamer.render_document([frame], context())

    assert r"\begin{frame}[allowframebreaks]" in document
    assert r"\PageTop{超长表}" in document
    assert any("allowframebreaks" in note for note in notes)
    assert [item.code for item in findings] == ["table-layout"]


# ---------------------------------------------------------------- 复核


def test_missing_characters_come_back_as_hard_errors(tmp_path):
    log = tmp_path / "slides.log"
    log.write_text(
        "! Missing character: There is no ① in font nullfont!\n"
        "Overfull \\hbox (12.0pt too wide) in paragraph at lines 10--12\n",
        encoding="utf-8",
    )

    findings = build_mod.read_log_findings(log)
    counts, places = build_mod.log_summary(findings)

    assert counts == {"missing-character": 1, "overfull-hbox": 1}
    assert {item.code: item.severity for item in findings} == {
        "missing-character": "error",
        "overfull-hbox": "warning",
    }
    assert places["overfull-hbox"] == ["page 0, line(s) 10--12"]


def test_latex_errors_in_the_log_are_hard_errors(tmp_path):
    """PDF 出得来不代表排对了：日志里的 ``!`` 行与缺字同级。"""
    log = tmp_path / "slides.log"
    log.write_text(
        "! Undefined control sequence.\n"
        "<argument> \\tmTitleRuleLen\n"
        "! Undefined control sequence.\n"
        "! Missing character: There is no ① in font nullfont!\n"
        "Overfull \\hbox (12.0pt too wide) in paragraph at lines 10--12\n",
        encoding="utf-8",
    )

    findings = build_mod.latex_error_findings(log)

    # 同一条错误只报一次；缺字交给专门的判据，不在这里重复
    assert [(item.code, item.message) for item in findings] == [
        ("latex-error", "Undefined control sequence.")
    ]
    assert findings[0].fatal

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    class FakePdf:
        @staticmethod
        def page_count(path):
            return 1

        @staticmethod
        def page_texts(path):
            return ["页脚 1/1"]

    original = build_mod.pdf_tools
    build_mod.pdf_tools = FakePdf
    try:
        _, _, gate = build_mod.gate_build(
            pdf, expected=1, slide_count=1, log_findings=findings, root=tmp_path
        )
    finally:
        build_mod.pdf_tools = original

    assert any(item.code == "latex-error" and item.fatal for item in gate)


def test_frame_numbers_only_accept_the_footer_shape():
    assert build_mod.frame_numbers(["页脚 1/3", "页脚 2/3", "页脚 3/3"]) == [1, 2, 3]
    # 分母不是总页数、或分子不是该页页序的，都不算
    assert build_mod.frame_numbers(["页脚 2/4", "页脚 2/2"]) == [2]
    assert build_mod.frame_numbers(["没有页脚"]) == []


def test_gate_reports_a_page_count_that_does_not_match_the_frames(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    class FakePdf:
        @staticmethod
        def page_count(path):
            return 24

        @staticmethod
        def page_texts(path):
            return [f"页脚 {index}/24" for index in range(1, 25)]

    original = build_mod.pdf_tools
    build_mod.pdf_tools = FakePdf
    try:
        pages, numbers, findings = build_mod.gate_build(
            pdf, expected=TOTAL_FRAMES, slide_count=TOTAL_FRAMES, log_findings=(), root=tmp_path
        )
    finally:
        build_mod.pdf_tools = original

    assert pages == 24 and len(numbers) == 24
    assert any(item.code == "deck-pages" and item.fatal for item in findings)
    # 备份帧不再随句子多长而长，页数闸门里也就没有"deck 长了"这一说
    assert all(item.code != "deck-slides" for item in findings)


def test_a_gist_drops_the_source_numbering_before_it_measures_the_width():
    """摘要列已经点了出处，行首的原文编号是形式：先剥再截字，宽度与印出来的字一致。"""
    sentence = "（一）火星曾经宜居吗？——回答：是，而且是长时期的、全球性的。"

    assert build_mod.gist_text(sentence, 0) == sentence[3:]
    assert build_mod.gist_text(sentence, 12) == "火星曾经宜居吗？——回…"
    assert build_mod.gist_text("（1） 编号在括号里。", 0) == "编号在括号里。"


def test_the_gist_ladder_shortens_the_summary_until_the_lines_fit():
    """摘要阶梯：16 → 12 → 10 → 8 字，取第一个装得下的；一条一行，行数由条目数定。"""
    sentences = ["甲" * 24, "乙" * 24]

    tight = build_mod.fit_backup(sentences, frames=1, lines_per_frame=2, gist_units=12)
    assert tight.gist_chars == 12
    assert tight.lines_needed == 2
    assert tight.gist_units_needed == 12
    assert tight.fits

    roomy = build_mod.fit_backup(sentences, frames=1, lines_per_frame=8, gist_units=16)
    assert roomy.gist_chars == build_mod.BACKUP_GIST_LADDER[0] == 16
    assert roomy.lines_needed == 2 and roomy.fits


def test_a_reserve_too_small_for_even_the_shortest_gist_is_reported_with_numbers():
    sentences = ["一条要点。" * 4] * 30

    fit = build_mod.fit_backup(sentences, frames=1, lines_per_frame=27, gist_units=45)

    assert not fit.fits
    assert fit.gist_chars == build_mod.BACKUP_GIST_LADDER[-1] == 8
    assert fit.capacity_lines == 27 and fit.lines_needed == 30
    assert fit.required_frames == 2
    message = build_mod._backup_reserve_message(fit)
    assert "预留 1 帧、每帧 27 行（共 27 行）" in message
    assert "装不下正片用到的 30 条要点" in message
    assert "最满一帧要 30 行" in message
    assert "--backup 加到 2 帧以上" in message


def test_a_gist_column_too_narrow_for_even_the_shortest_gist_says_so():
    """摘要列本身太窄（版心／字号下限太紧）是另一回事：加帧救不了，报的也不是加帧。"""
    fit = build_mod.fit_backup(["甲" * 24], frames=2, lines_per_frame=27, gist_units=3)

    assert not fit.fits and fit.width_fits is False
    message = build_mod._backup_reserve_message(fit)
    assert "摘要列一行只有 3 个 em" in message
    assert "最短的 8 字摘要也要 8 个 em" in message
    assert "--backup" not in message


def test_a_reserve_too_small_for_the_points_blocks_the_build(book, capsys):
    """预留装不下正片要点是 error：报告照打、盘上一个字节不写，不靠添帧圆场。"""
    root, spec = book
    outline_run(root, config(backup=1), spec_path=spec)
    document = load(outline_path(root))
    # 画布压低（同一套骨架、同一份主题，只把纸面调小）：一帧的可用行数掉下来，
    # 预留 1 帧，圈满各章的候选句就装不下了
    document["theme"]["canvas"]["height_bp"] = 220
    fill_all_frames(document, root)
    dump(outline_path(root), document)
    before = sorted(path.name for path in deck_dir(root).iterdir())

    code = main(["build", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[deck-slides]" in out
    assert "备份页预留 1 帧" in out and "把 --backup 加到" in out
    assert "不生成：" in out
    assert sorted(path.name for path in deck_dir(root).iterdir()) == before
    assert not (deck_dir(root) / "slides.tex").exists()


# ---------------------------------------------------------------- 断掉对成书的依赖


def test_the_whole_chain_runs_without_any_built_book(book):
    """outline → check → build（--tex-only）全程不需要 ``_primer/book/`` 存在一步。"""
    root, spec = book
    assert not (root / "_primer" / "book").exists()

    assert main(["outline", "--project-root", str(root), "--spec", str(spec)]) == 0
    assert main(["check", str(outline_path(root)), "--project-root", str(root)]) == 0
    text = build_text(root)

    assert r"\begin{frame}" in text
    # 结构确实来自源 markdown：四章都在，且没有成书产物被读过的痕迹
    assert r"\PageTop{附录 A　附录标题}" in text
    assert not (root / "_primer" / "book").exists()


def test_build_help_states_the_gate():
    parser = build_parser()
    build = parser._subparsers._group_actions[0].choices["build"].format_help()
    flat = " ".join(build.split())

    assert "Errors in the outline block generation before anything is written" in flat
    assert "Missing character is a hard failure" in flat
    assert "Overfull boxes are reported with their page numbers" in flat
    assert "never writes prose of its own" in flat


# ---------------------------------------------------------------- 端到端（要排版引擎）


@needs_engine
def test_the_compiled_deck_has_the_page_count_the_frames_ask_for(book, capsys):
    root, spec = book
    outline_run(root, config(), spec_path=spec)

    code = main(["build", str(outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert f"页数: PDF {TOTAL_FRAMES} 页 vs 排出 {TOTAL_FRAMES} 帧" in out
    assert "没有发现。" in out or "missing-character 0" in out
    pdf = deck_dir(root) / "slides.pdf"
    assert pdf.is_file()
    assert build_mod.pdf_tools.page_count(pdf) == TOTAL_FRAMES
    # 复核读的是文本层里的帧号：每一帧（含封面）右下角都有 n/N
    assert f"帧号: 文本层读到 {TOTAL_FRAMES} 个自洽的帧号" in out
    # 画布是 PowerPoint 的 16:9：960 × 540 bp，PDF 页数就是帧数
    assert build_mod.pdf_tools.page_count(pdf) == TOTAL_FRAMES
