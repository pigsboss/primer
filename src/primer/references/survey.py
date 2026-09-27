# -*- coding: utf-8 -*-
"""文献调研日志来源：把 OA 下载日志里"没拿到"的条目并进图书馆索取清单。

来源是 ``参考资料/oa_download_log.csv``（列 ``topic,list,title,year,url,status,saved``），
由文献调研阶段的 OA 抓取逐条记账。一项的去留按 **同一 (topic, 标题键)** 的记账顺序判定：

* ``ok`` 且 ``saved`` 指向的文件现在仍在磁盘上——后来抓到了，整项不进清单；
* 否则以最后一条非 ``ok`` 记录为准：``fail*`` / ``no-link``（没拿到）进清单，
  ``off-topic``（调研中判定跑题）不进清单。

去重键沿用旧清单脚本（``scripts/make_library_request_list.py``）的标题键
``re.sub(r"\\W+", "", title.lower())[:60]``。与旧脚本的两处差异，都是为"能救回来的都救回来"：

1. 旧脚本只看 ``status`` 字符串就认定"已有原文"，于是把已经删掉的成功记录也当成
   "本地已有"，压掉了清单行；这里要求存档文件真的还在磁盘上；
2. 旧脚本"最后一行胜出"的实现里，一条 ``ok``（哪怕文件已不存在）会锁死该键、
   连它后面的 ``fail`` 重试也一并忽略；这里锁定条件同上（文件在磁盘上才算）。

这里只做解析与筛选，行怎么并进清单、怎么与参考文献库去重由 :mod:`primer.references.export`
负责；本模块不写任何文件。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 标题键的截断长度，与旧脚本一致（第 60 个字符之后不参与比较）。
TITLE_KEY_LIMIT = 60

STATUS_OK = "ok"
STATUS_NO_LINK = "no-link"
STATUS_FAIL = "fail"
STATUS_OFF_TOPIC = "off-topic"

SURVEY_SOURCE = "文献调研清单"

_NON_WORD = re.compile(r"\W+")


def title_key(title: str) -> str:
    """标题去重键：去掉全部非单词字符、转小写、截断到 60 字符。

    与旧清单脚本逐字一致（``\\W`` 在 Python 里把中日韩字符算作单词字符，中文标题因此
    保留原样）。不做重音折叠、不做词序归一——这两处都会改变去重结果，不在这里改。
    """
    return _NON_WORD.sub("", title.lower())[:TITLE_KEY_LIMIT]


def needs_library(status: str) -> bool:
    """该状态是否意味着"这次没拿到、要另想办法"。"""
    token = (status or "").strip()
    return token == STATUS_NO_LINK or token.startswith(STATUS_FAIL)


@dataclass(frozen=True)
class SurveyItem:
    """调研日志里一条"没拿到"的条目。``line`` 是它在日志里的行号（从 2 起，含表头）。"""

    topic: str
    listing: str
    title: str
    year: str
    url: str
    status: str
    line: int

    @property
    def source(self) -> str:
        """清单 ``source`` 列的值：与 ``参考文献库[n]`` 明显不同的来源标记。"""
        return f"{SURVEY_SOURCE}[{self.topic or self.listing or '-'}]"


def _saved_on_disk(row: dict) -> bool:
    saved = (row.get("saved") or "").strip()
    return bool(saved) and Path(saved).exists()


def read_oa_log(path: Path) -> list[SurveyItem]:
    """读 OA 下载日志，返回仍需向图书馆索取的条目（按日志出现顺序）。

    判定规则见模块文档；同一 ``(topic, 标题键)`` 只出一条。
    """
    decisions: dict[tuple[str, str], SurveyItem] = {}
    order: list[tuple[str, str]] = []
    succeeded: set[tuple[str, str]] = set()

    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            title = (row.get("title") or "").strip()
            if not title:
                continue
            key = ((row.get("topic") or "").strip(), title_key(title))
            status = (row.get("status") or "").strip()
            if status.startswith(STATUS_OK):
                if _saved_on_disk(row):
                    succeeded.add(key)
                continue
            if key not in decisions:
                order.append(key)
            decisions[key] = SurveyItem(
                topic=key[0],
                listing=(row.get("list") or "").strip(),
                title=title,
                year=(row.get("year") or "").strip(),
                url=(row.get("url") or "").strip(),
                status=status,
                line=line_no,
            )

    return [
        decisions[key]
        for key in order
        if key not in succeeded and needs_library(decisions[key].status)
    ]


def load_oa_log(path: Optional[Path]) -> list[SurveyItem]:
    """``path`` 为空时返回空表，供命令行未传 ``--oa-log`` 时直接调用。"""
    return [] if path is None else read_oa_log(Path(path))
