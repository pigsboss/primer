# -*- coding: utf-8 -*-
"""primer-imgtriage —— H1 差异分诊与交付门禁。

**用途**：在 kimi code 建模工作流的"渲染预览之后、交付说明之前"插入一环——把 T2 的
**差异对照表**（:func:`primer.imgcmp.feat.compare_tables` 的 ``diffs[]``）逐条送进
调用方给的**规则文件**，判出**三档**并输出《差异分诊表》与《异议单》；再用 ``gate``
把"交付说明缺《差异分诊表》"这件事变成**非零退出**的门禁。

**三档与处置边界**（判据来源：H1 需求书 §三.2）：

- **允许差**（``allow``）：差异命中调用方给的《允许差清单》条目 → 维持设计，登记分诊表，
  挂**清单条目号**（``list_item_id``）。
- **执行自由度内差异**（``free``）：仅命中**声明的自由度范围**规则（观感/工艺/未冻结细节，
  规则里明确列出允许的字段与范围）→ 可自行迭代，迭代记录入表，挂**自由度范围**（``scope``）。
- **升级**（``escalate``）：其余一律升级。清单外差异**不得自我豁免**——默认档位由
  ``escalation.default`` 给出（示例文件与判据都设为 ``escalate``）；触及构型/几何判据的
  差异由规则里的**显式构型级声明**（``escalate[]``）拦下，**优先于** allow/free 判定。

**匹配口径**：每条差异先合成一个**主题串**（``subject``）——

    kind=<差异类> ref=<基准值> ours=<我方值> delta=<差> shape=<基准形状类>-><我方形状类> | <特征证据>

外加**配对级**主题串（底色/背景）：``kind=background_kind ref_kind=<白/黑> ours_kind=<白/黑>``。
规则的选择器全部作用在主题串与差异字段上：``kind``（差异类，可给列表）、``pattern``
（对主题串做不区分大小写的正则搜索，可给列表）、``ref_shape``/``ours_shape``（形状类）、
``ref_min/ref_max/ours_min/ours_max/delta_min/delta_max/abs_delta_min/abs_delta_max``
（数值区间，字段非数值则该选择器判不命中）、``top_band_equal``（开口是否在筒顶带内，
用于把"拆面剖口"与"筒顶开口数"分开）、``when``（仅在指定配对上生效，见下）。

**保守性约束（工具内零任务数值）**：规则文件由总体提供；工具不内嵌任何任务参数、
不预设任何差异类属于哪一档。规则文件里一条 allow/free/escalate 规则**必须至少给一个
选择器**（否则整份规则被拒——防止"通配豁免"），allow 必须给 ``list_item_id``，
free 必须给 ``scope``，escalate 必须给 ``judgement``。

**规则文件结构**（英文键；示例见 ``triage_rules.example.yaml``）：

.. code-block:: yaml

    version: 1
    allow:
      - list_item_id: <清单条目号>
        note: <为什么允许>
        disposition: <处置（可选）>
        kind: <差异类｜列表>
        pattern: <正则｜列表>
        ours_max: <数值>
        when: {ref_path_contains: <子串>, ours_kind: black}   # 可选，仅对指定配对生效
    free:
      - scope: <自由度范围名>
        note: <允许的范围>
        ...
    escalate:
      - judgement: <判据号>
        note: <差异描述>
        advice: <建议方案>
        ...
    escalation:
      default: escalate                  # 清单外默认档位（判据要求 escalate）
      template:
        title: 异议单
        fields: [差异描述, 特征证据, 建议方案]

**产物**（``--out DIR``）：``差异分诊表.md``（字段固定为 差异｜特征证据｜分诊档位｜依据｜处置）、
``triage.json``（英文键）、``escalations/ESC-NN-<kind>.md``（逐条升级的《异议单》）。

**CLI**：``triage``（跑一对比对）／``gate``（查交付说明是否附了《差异分诊表》）／``--selftest``。

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import os
import re

import yaml

from .common import (
    ImgCmpError,
    ensure_dir,
    make_parser,
    read_json,
    report,
    run_main,
    selftest_report,
    sha256_16,
    write_json,
)
from .feat import build_table, compare_tables

TOOL = "primer.imgcmp.triage"

TIER_ALLOW = "allow"
TIER_FREE = "free"
TIER_ESCALATE = "escalate"
TIERS = (TIER_ALLOW, TIER_FREE, TIER_ESCALATE)
TIER_ZH = {TIER_ALLOW: "允许差", TIER_FREE: "执行自由度内差异", TIER_ESCALATE: "升级"}

# 差异类的通用中文名（工具只做词条翻译，不判档位——档位一律由规则文件决定）。
KIND_ZH = {
    "count": "组件数",
    "height_fraction": "高度分数",
    "area_fraction": "面积占比",
    "anchor_ratio": "锚件比率",
    "shape_class": "形状类",
    "hole_count": "开口数",
    "contact": "相接件数",
    "merged": "并件（基准 1 件 ← 我方多件）",
    "split": "拆件（基准多件 → 我方 1 件）",
    "removed": "基准独有件（我方缺失）",
    "added": "我方独有件（基准无）",
    "background_kind": "底色／背景",
}

DEFAULT_DISPOSITION = {
    TIER_ALLOW: "维持设计，登记分诊表（挂清单条目号）",
    TIER_FREE: "自行迭代，迭代记录入分诊表",
    TIER_ESCALATE: "生成异议单，禁止自行维持（待总体裁决）",
}

SECTION_NAME = "差异分诊表"
SECTION_COLUMNS = ("差异", "特征证据", "分诊档位", "依据", "处置")
DEFAULT_NOTE_FIELDS = ("差异描述", "特征证据", "建议方案")

SELECTOR_KEYS = (
    "kind", "pattern", "ref_shape", "ours_shape", "top_band_equal", "when",
    "ref_min", "ref_max", "ours_min", "ours_max",
    "delta_min", "delta_max", "abs_delta_min", "abs_delta_max",
)
NUMERIC_KEYS = ("ref_min", "ref_max", "ours_min", "ours_max",
                "delta_min", "delta_max", "abs_delta_min", "abs_delta_max")
WHEN_KEYS = ("ref_path_contains", "ours_path_contains", "ref_kind", "ours_kind")


# ---------------------------------------------------------------- 规则文件

def load_rules(path: str) -> dict:
    """读规则文件并校验；任何缺口都抛 :class:`ImgCmpError`（禁止静默放行）。"""
    if not os.path.isfile(path):
        raise ImgCmpError("rules file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ImgCmpError("cannot parse rules yaml %s: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise ImgCmpError("rules file must be a mapping, got %s"
                          % type(raw).__name__)
    unknown = sorted(set(raw) - {"version", "allow", "free", "escalate", "escalation"})
    if unknown:
        raise ImgCmpError("unknown rules key(s): %s (known: version, allow, free, "
                          "escalate, escalation)" % ", ".join(unknown))

    ruleset = {
        "version": raw.get("version", 1),
        "path": os.path.abspath(path),
        "allow": _load_rule_list(raw.get("allow"), TIER_ALLOW, path),
        "free": _load_rule_list(raw.get("free"), TIER_FREE, path),
        "escalate": _load_rule_list(raw.get("escalate"), TIER_ESCALATE, path),
        "escalation": _load_escalation(raw.get("escalation"), path),
    }
    return ruleset


def _load_escalation(raw, path) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ImgCmpError("escalation must be a mapping, got %s" % type(raw).__name__)
    default = str(raw.get("default", TIER_ESCALATE)).strip()
    if default not in TIERS:
        raise ImgCmpError("escalation.default must be one of %s, got %r"
                          % ("|".join(TIERS), default))
    tmpl = raw.get("template") or {}
    if not isinstance(tmpl, dict):
        raise ImgCmpError("escalation.template must be a mapping")
    fields = tmpl.get("fields") or list(DEFAULT_NOTE_FIELDS)
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ImgCmpError("escalation.template.fields must be a list of strings")
    return {
        "default": default,
        "template": {"title": str(tmpl.get("title", "异议单")), "fields": list(fields)},
    }


def _load_rule_list(raw, tier, path) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ImgCmpError("rules '%s' must be a list, got %s" % (tier, type(raw).__name__))
    out = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ImgCmpError("rules '%s[%d]' must be a mapping, got %s"
                              % (tier, idx, type(item).__name__))
        rule = dict(item)
        rule["tier"] = tier
        rule["_index"] = idx
        present = [k for k in SELECTOR_KEYS if rule.get(k) is not None]
        if not present:
            raise ImgCmpError(
                "rules '%s[%d]' declares no selector (%s); a wildcard rule could exempt a "
                "difference wholesale — add at least one selector"
                % (tier, idx, "/".join(SELECTOR_KEYS)))
        if tier == TIER_ALLOW and not rule.get("list_item_id"):
            raise ImgCmpError("rules 'allow[%d]' must carry 'list_item_id' "
                              "(the allowance-list entry number)" % idx)
        if tier == TIER_FREE and not rule.get("scope"):
            raise ImgCmpError("rules 'free[%d]' must carry 'scope' "
                              "(the declared freedom-of-execution scope)" % idx)
        if tier == TIER_ESCALATE and not rule.get("judgement"):
            raise ImgCmpError("rules 'escalate[%d]' must carry 'judgement' "
                              "(the construction/geometry criterion id)" % idx)
        for key in NUMERIC_KEYS:
            if key in rule and rule[key] is not None:
                try:
                    rule[key] = float(rule[key])
                except (TypeError, ValueError) as exc:
                    raise ImgCmpError("rules '%s[%d].%s' must be a number, got %r"
                                      % (tier, idx, key, rule[key])) from exc
        when = rule.get("when")
        if when is not None:
            if not isinstance(when, dict):
                raise ImgCmpError("rules '%s[%d].when' must be a mapping" % (tier, idx))
            bad = sorted(set(when) - set(WHEN_KEYS))
            if bad:
                raise ImgCmpError("rules '%s[%d].when' unknown key(s): %s (known: %s)"
                                  % (tier, idx, ", ".join(bad), ", ".join(WHEN_KEYS)))
        rule["_patterns"] = _as_patterns(rule.get("pattern"), tier, idx, path)
        out.append(rule)
    return out


def _as_patterns(value, tier, idx, path) -> list:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if not isinstance(item, str):
            raise ImgCmpError("rules '%s[%d].pattern' in %s must be a string or a list of "
                              "strings" % (tier, idx, path))
        try:
            out.append(re.compile(item, re.IGNORECASE | re.DOTALL))
        except re.error as exc:
            raise ImgCmpError("rules '%s[%d].pattern' in %s is not a valid regexp: %s"
                              % (tier, idx, path, exc)) from exc
    return out


def rules_sha16(ruleset: dict) -> str:
    return sha256_16(ruleset["path"]) if os.path.isfile(ruleset["path"]) else ""


# ---------------------------------------------------------------- 主题串与行

def infer_kind(table: dict) -> str:
    """取组件表的底别；缺失时按 ``components[]`` 的 kind 推断，再缺失给空串。"""
    kind = table.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return ""


def _shapes(evidence: str, refs: list, ourss: list) -> tuple:
    ref_ids = [int(v) for v in re.findall(r"ref#(\d+)", evidence)]
    our_ids = [int(v) for v in re.findall(r"ours#(\d+)", evidence)]
    m = re.search(r"基准 1 件 ← 我方 \d+ 件（#\[([\d, ]+)\]）", evidence)
    if m:
        our_ids += [int(v) for v in re.findall(r"\d+", m.group(1))]
    m = re.search(r"基准 \d+ 件 → 我方 1 件（#\[([\d, ]+)\]）", evidence)
    if m:
        ref_ids += [int(v) for v in re.findall(r"\d+", m.group(1))]
    return _shape_of(refs, ref_ids), _shape_of(ourss, our_ids)


def _shape_of(comps: list, ids: list) -> list:
    out = []
    for i in sorted(set(ids)):
        if 0 <= i < len(comps):
            shape = comps[i].get("shape_class")
            if shape and shape not in out:
                out.append(shape)
    return out


def _top_band(evidence: str):
    m = re.search(r"顶带内\s*(\d+)→(\d+)", evidence)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def build_rows(diff: dict, ref_table: dict, ours_table: dict) -> list:
    """差异对照表 → 分诊行（含一条**配对级**底色/背景行）。"""
    refs = [c for c in (ref_table.get("components") or [])
            if c.get("kind", "component") == "component"]
    ourss = [c for c in (ours_table.get("components") or [])
             if c.get("kind", "component") == "component"]

    rows = []
    rk, ok_ = infer_kind(ref_table), infer_kind(ours_table)
    if rk and ok_ and rk != ok_:
        rows.append({
            "kind": "background_kind",
            "ref": rk, "ours": ok_, "delta": None,
            "ref_shapes": [], "ours_shapes": [],
            "top_band_ref": None, "top_band_ours": None,
            "evidence": "底色／背景 基准 %s（图 %s）／我方 %s（图 %s）"
                        % (rk, os.path.basename(str(ref_table.get("image", "?"))),
                           ok_, os.path.basename(str(ours_table.get("image", "?")))),
            "source": "pair",
        })

    for item in diff.get("diffs") or []:
        evidence = str(item.get("evidence", ""))
        ref_shapes, ours_shapes = _shapes(evidence, refs, ourss)
        tb_ref, tb_ours = _top_band(evidence)
        rows.append({
            "kind": item.get("kind"),
            "ref": item.get("ref"), "ours": item.get("ours"), "delta": item.get("delta"),
            "ref_shapes": ref_shapes, "ours_shapes": ours_shapes,
            "top_band_ref": tb_ref, "top_band_ours": tb_ours,
            "evidence": evidence,
            "source": "diff",
            "rank": item.get("rank"),
        })

    for n, row in enumerate(rows, 1):
        row["row_id"] = "R%02d" % n
        row["subject"] = _subject(row)
    return rows


def _subject(row: dict) -> str:
    if row["source"] == "pair":
        prefix = ("kind=%s ref_kind=%s ours_kind=%s"
                  % (row["kind"], row["ref"], row["ours"]))
    else:
        prefix = ("kind=%s ref=%s ours=%s delta=%s shape=%s->%s"
                  % (row["kind"], row["ref"], row["ours"], row["delta"],
                     "/".join(row["ref_shapes"]) or "-",
                     "/".join(row["ours_shapes"]) or "-"))
    return "%s | %s" % (prefix, row["evidence"])


# ---------------------------------------------------------------- 规则匹配

def _match(row: dict, rule: dict, ref_table: dict, ours_table: dict) -> bool:
    if not _match_when(rule.get("when"), ref_table, ours_table):
        return False
    kinds = rule.get("kind")
    if kinds is not None:
        wanted = kinds if isinstance(kinds, list) else [kinds]
        if row["kind"] not in wanted:
            return False
    for regex in rule["_patterns"]:
        if not regex.search(row["subject"]):
            return False
    for key, field in (("ref_shape", "ref_shapes"), ("ours_shape", "ours_shapes")):
        want = rule.get(key)
        if want is None:
            continue
        wanted = want if isinstance(want, list) else [want]
        if not set(wanted) & set(row[field]):
            return False
    if rule.get("top_band_equal") is not None:
        equal = (row["top_band_ref"] is not None
                 and row["top_band_ref"] == row["top_band_ours"])
        if bool(rule["top_band_equal"]) != bool(equal):
            return False
    for key in NUMERIC_KEYS:
        want = rule.get(key)
        if want is None:
            continue
        value = _numeric(row, key)
        if value is None:
            return False
        if key.endswith("_min") and not value >= want:
            return False
        if key.endswith("_max") and not value <= want:
            return False
    return True


def _numeric(row: dict, key: str):
    if key.startswith("abs_delta"):
        field, fn = "delta", abs
    elif key.startswith("ref_"):
        field, fn = "ref", float
    elif key.startswith("ours_"):
        field, fn = "ours", float
    else:
        field, fn = "delta", float
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return fn(value)


def _match_when(when, ref_table: dict, ours_table: dict) -> bool:
    if not when:
        return True
    pairs = (("ref_path_contains", ref_table), ("ours_path_contains", ours_table))
    for key, table in pairs:
        if key in when:
            if str(when[key]) not in str(table.get("image", "")):
                return False
    if "ref_kind" in when and infer_kind(ref_table) != str(when["ref_kind"]):
        return False
    if "ours_kind" in when and infer_kind(ours_table) != str(when["ours_kind"]):
        return False
    return True


def decide(row: dict, ruleset: dict, ref_table: dict, ours_table: dict) -> dict:
    """单行判档：**显式构型级声明 → allow → free → 默认档位**（保守优先）。"""
    for rule in ruleset["escalate"]:
        if _match(row, rule, ref_table, ours_table):
            return {"tier": TIER_ESCALATE, "rule": rule, "hit": "explicit"}
    for rule in ruleset["allow"]:
        if _match(row, rule, ref_table, ours_table):
            return {"tier": TIER_ALLOW, "rule": rule, "hit": "rule"}
    for rule in ruleset["free"]:
        if _match(row, rule, ref_table, ours_table):
            return {"tier": TIER_FREE, "rule": rule, "hit": "rule"}
    return {"tier": ruleset["escalation"]["default"], "rule": None, "hit": "default"}


# ---------------------------------------------------------------- 分诊主流程

def run_triage(ref_spec: str, ours_spec: str, rules_path: str, out_dir: str,
               json_path: str | None = None, ref_kind: str = "auto", ours_kind: str = "auto",
               ref_roi=None, ours_roi=None) -> dict:
    ruleset = load_rules(rules_path)
    ref_table = load_table(ref_spec, kind=ref_kind, roi=ref_roi)
    ours_table = load_table(ours_spec, kind=ours_kind, roi=ours_roi)
    diff = compare_tables(ref_table, ours_table)
    result = triage_pair(diff, ref_table, ours_table, ruleset)

    ensure_dir(out_dir)
    table_md = os.path.join(out_dir, "%s.md" % SECTION_NAME)
    with open(table_md, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    result["out"] = {"dir": os.path.abspath(out_dir), "table": os.path.abspath(table_md)}
    _write_escalations(result, out_dir, ruleset)
    target = json_path or os.path.join(out_dir, "triage.json")
    result["out"]["json"] = os.path.abspath(target)
    write_json(target, result)
    return result


def load_table(spec: str, kind: str = "auto", roi=None) -> dict:
    """``*.json`` 当组件特征表直接读；其余当图，走 ``feat.build_table`` 建表。"""
    if not os.path.isfile(spec):
        raise ImgCmpError("input not found: %s" % spec)
    if spec.lower().endswith(".json"):
        try:
            table = read_json(spec)
        except (OSError, ValueError) as exc:
            raise ImgCmpError("cannot parse table json %s: %s" % (spec, exc)) from exc
        if not isinstance(table, dict) or not isinstance(table.get("components"), list):
            raise ImgCmpError("table %s must be a feature-table JSON with a 'components' "
                              "list" % spec)
        return table
    if kind not in ("auto", "white", "black"):
        raise ImgCmpError("--kind must be white|black|auto, got: %r" % (kind,))
    return build_table(spec, kind=kind, roi=roi)


def triage_pair(diff: dict, ref_table: dict, ours_table: dict, ruleset: dict) -> dict:
    """差异对照表 × 规则 → 分诊结果（纯函数，供 CLI 与 selftest 共用）。"""
    rows = build_rows(diff, ref_table, ours_table)
    result_rows = []
    counts = {tier: 0 for tier in TIERS}
    escalations = []
    for row in rows:
        verdict = decide(row, ruleset, ref_table, ours_table)
        tier = verdict["tier"]
        counts[tier] += 1
        rule = verdict["rule"] or {}
        entry = {
            "row_id": row["row_id"],
            "kind": row["kind"],
            "subject": row["subject"],
            "ref": row["ref"], "ours": row["ours"], "delta": row["delta"],
            "ref_shapes": row["ref_shapes"], "ours_shapes": row["ours_shapes"],
            "rank": row.get("rank"),
            "evidence": row["evidence"],
            "tier": tier,
            "tier_zh": TIER_ZH[tier],
            "hit": verdict["hit"],
            "rule_index": rule.get("_index"),
        }
        if verdict["hit"] == "default":
            entry["basis"] = {"default": "清单外差异（默认档位 %s）" % TIER_ZH[tier]}
        elif tier == TIER_ALLOW:
            entry["basis"] = {"list_item_id": rule["list_item_id"]}
        elif tier == TIER_FREE:
            entry["basis"] = {"scope": rule["scope"]}
        else:
            entry["basis"] = {"judgement": rule["judgement"]}
        entry["note"] = rule.get("note", "")
        entry["disposition"] = rule.get("disposition") or DEFAULT_DISPOSITION[tier]
        if tier == TIER_ESCALATE:
            entry["escalation_id"] = "ESC-%02d" % (len(escalations) + 1)
            entry["basis"]["escalation_id"] = entry["escalation_id"]
            escalations.append({
                "escalation_id": entry["escalation_id"],
                "row_id": row["row_id"],
                "kind": row["kind"],
                "subject": row["subject"],
                "evidence": row["evidence"],
                "tier_zh": TIER_ZH[tier],
                "judgement": rule.get("judgement") if verdict["hit"] == "explicit" else None,
                "note": rule.get("note", ""),
                "advice": rule.get("advice", ""),
            })
        result_rows.append(entry)

    return {
        "tool": TOOL,
        "mode": "triage",
        "rules": {"path": ruleset["path"], "sha256_16": rules_sha16(ruleset),
                  "version": ruleset["version"],
                  "counts": {t: len(ruleset[t]) for t in TIERS},
                  "escalation_default": ruleset["escalation"]["default"]},
        "ref": {"table": ref_table.get("image"), "kind": infer_kind(ref_table),
                "component_count": len([c for c in (ref_table.get("components") or [])
                                        if c.get("kind", "component") == "component"])},
        "ours": {"table": ours_table.get("image"), "kind": infer_kind(ours_table),
                 "component_count": len([c for c in (ours_table.get("components") or [])
                                         if c.get("kind", "component") == "component"])},
        "rows": result_rows,
        "counts": counts,
        "escalations": escalations,
        "diff_counts": {"major": diff.get("summary", {}).get("major"),
                        "minor": diff.get("summary", {}).get("minor")},
        "method": {
            "subjects": "one row per T2a diff plus one pair-level row for a background "
                        "(white/black) mismatch; the subject string carries kind, values, "
                        "the paired shape classes and the raw evidence text",
            "priority": "explicit escalate[] declaration > allow[] > free[] > default tier "
                        "(touching a construction/geometry criterion must never be absorbed "
                        "by an allowance or a freedom scope)",
            "default": "escalation.default from the rules file; the criterion requires "
                       "'escalate' so that a difference outside the allowance list is never "
                       "self-exempted",
        },
    }


# ---------------------------------------------------------------- 产物渲染

def _cell(text) -> str:
    return str(text).replace("|", r"\|").replace("\n", " ")


def render_markdown(result: dict) -> str:
    lines = [
        "# %s" % SECTION_NAME,
        "",
        "- 工具：`%s`（H1 差异分诊与交付门禁）" % TOOL,
        "- 基准：`%s`（底色 %s，组件 %d）"
        % (result["ref"]["table"], result["ref"]["kind"], result["ref"]["component_count"]),
        "- 我方：`%s`（底色 %s，组件 %d）"
        % (result["ours"]["table"], result["ours"]["kind"], result["ours"]["component_count"]),
        "- 规则：`%s`（sha256-16 `%s`；allow %d／free %d／escalate %d；默认档位 %s）"
        % (result["rules"]["path"], result["rules"]["sha256_16"],
           result["rules"]["counts"]["allow"], result["rules"]["counts"]["free"],
           result["rules"]["counts"]["escalate"], result["rules"]["escalation_default"]),
        "- 三档计数：允许差 %d／执行自由度内差异 %d／**升级 %d**"
        % (result["counts"]["allow"], result["counts"]["free"], result["counts"]["escalate"]),
        "",
        "| 差异 | 特征证据 | 分诊档位 | 依据 | 处置 |",
        "|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        label = "%s（%s）" % (KIND_ZH.get(row["kind"], row["kind"]), row["kind"])
        if row["ref_shapes"] or row["ours_shapes"]:
            label += " %s→%s" % ("/".join(row["ref_shapes"]) or "—",
                                 "/".join(row["ours_shapes"]) or "—")
        lines.append("| %s | %s | %s | %s | %s |"
                     % (_cell(label), _cell(row["evidence"]), _cell(row["tier_zh"]),
                        _cell(_basis_text(row)), _cell(row["disposition"])))
    lines.append("")
    if result["escalations"]:
        lines.append("## 升级项与异议单")
        lines.append("")
        for esc in result["escalations"]:
            lines.append("- **%s**（%s）：%s → `escalations/%s-%s.md`"
                         % (esc["escalation_id"], esc["kind"],
                            esc["note"] or esc["evidence"][:60],
                            esc["escalation_id"], esc["kind"]))
        lines.append("")
    lines.append("## 处置边界")
    lines.append("")
    lines.append("- **允许差**：命中《允许差清单》条目 → 维持设计、登记本表（挂清单条目号）。")
    lines.append("- **执行自由度内差异**：仅命中声明的自由度范围（观感/工艺/未冻结细节）"
                 "→ 可自行迭代，迭代记录入本表（挂自由度范围）。")
    lines.append("- **升级**：其余一律升级 → 逐条出《异议单》，**禁止自行推理后维持**；"
                 "清单外差异不得自我豁免。")
    lines.append("")
    return "\n".join(lines)


def _basis_text(row: dict) -> str:
    basis = row["basis"]
    if "list_item_id" in basis:
        text = "清单条目 %s" % basis["list_item_id"]
    elif "scope" in basis:
        text = "自由度范围 %s" % basis["scope"]
    elif "judgement" in basis:
        text = "判据号 %s" % basis["judgement"]
    else:
        text = "默认档位（清单外差异）"
    if basis.get("escalation_id"):
        text += "／异议单 %s" % basis["escalation_id"]
    if row.get("note"):
        text += "：%s" % row["note"]
    return text


def _write_escalations(result: dict, out_dir: str, ruleset: dict) -> None:
    tmpl = ruleset["escalation"]["template"]
    target_dir = os.path.join(out_dir, "escalations")
    if not result["escalations"]:
        return
    ensure_dir(target_dir)
    for esc in result["escalations"]:
        path = os.path.join(target_dir, "%s-%s.md" % (esc["escalation_id"], esc["kind"]))
        esc["file"] = os.path.abspath(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_render_escalation(esc, tmpl))
    result["out"]["escalations"] = os.path.abspath(target_dir)


def _render_escalation(esc: dict, tmpl: dict) -> str:
    fields = tmpl["fields"]
    body = {
        "差异描述": "%s：%s" % (KIND_ZH.get(esc["kind"], esc["kind"]),
                              esc["note"] or esc["evidence"]),
        "特征证据": esc["evidence"],
        "建议方案": esc["advice"] or "（规则文件未给建议方案：请执行方在提交前补写）",
    }
    lines = [
        "# %s %s" % (tmpl["title"], esc["escalation_id"]),
        "",
        "- 分诊档位：%s" % esc["tier_zh"],
        "- 差异类：%s（%s）" % (KIND_ZH.get(esc["kind"], esc["kind"]), esc["kind"]),
        "- 依据：%s" % ("判据号 %s" % esc["judgement"] if esc["judgement"]
                       else "默认档位（清单外差异，无匹配规则）"),
        "- 主题：`%s`" % esc["subject"],
        "",
    ]
    for field in fields:
        lines.append("## %s" % field)
        lines.append("")
        lines.append(body.get(field, "（待填）"))
        lines.append("")
    lines.append("> 处置：升级项**禁止自行推理后维持**；本单随交付说明一并提交，"
                 "由总体裁决后补入《允许差清单》或返修指令。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 交付说明模板与门禁

def section_template(ruleset: dict | None = None) -> str:
    """交付说明里《差异分诊表》章节的骨架（A-H1-2 字段固定）。"""
    fields = (ruleset or {}).get("escalation", {}).get("template", {}).get("fields",
                                                                         list(DEFAULT_NOTE_FIELDS))
    return "\n".join([
        "## %s" % SECTION_NAME,
        "",
        "> 强制章节（A-H1-2）：字段固定为 %s；交付物未附本表即视为未交付。"
        % "｜".join(SECTION_COLUMNS),
        "> 三档：允许差（挂清单条目号）／执行自由度内差异（挂自由度范围）／升级（出《异议单》，"
        "禁止自行维持）。",
        "> 异议单字段：%s（落 `escalations/`）。" % "＋".join(fields),
        "",
        "| %s |" % " | ".join(SECTION_COLUMNS),
        "|%s|" % "|".join(["---"] * len(SECTION_COLUMNS)),
        "| %s |" % " | ".join(["（差异描述）", "（特征表证据）",
                               "允许差／执行自由度内差异／升级",
                               "（清单条目号／判据号／异议单号）", "（处置）"]),
        "",
    ])


def check_note(path: str, section: str = SECTION_NAME) -> dict:
    """查交付说明是否带齐《差异分诊表》章节与固定字段。不通过抛 :class:`ImgCmpError`。"""
    if not os.path.isfile(path):
        raise ImgCmpError("delivery note not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise ImgCmpError("cannot read delivery note %s: %s" % (path, exc)) from exc

    start = level = None
    for idx, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        if m and section in m.group(2):
            start, level = idx, len(m.group(1))
            break
    if start is None:
        raise ImgCmpError("delivery note is missing the required section '%s': %s "
                          "(H1 gate: a delivery without the triage table counts as not "
                          "delivered)" % (section, path))

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s*(.+?)\s*$", lines[idx])
        if m and len(m.group(1)) <= level:
            end = idx
            break

    body = lines[start + 1:end]
    table_lines = [ln for ln in body if ln.strip().startswith("|")]
    missing = []
    for column in SECTION_COLUMNS:
        if not any(column in ln for ln in table_lines):
            missing.append(column)
    if missing:
        raise ImgCmpError("section '%s' in %s has no table row carrying the required "
                          "field(s): %s (expected columns: %s)"
                          % (section, path, ", ".join(missing), " | ".join(SECTION_COLUMNS)))
    data_rows = [ln for ln in table_lines
                 if not re.match(r"^\|[\s\-:|]+\|$", ln.strip())]
    return {"note": os.path.abspath(path), "section": section,
            "line": start + 1, "level": level, "columns": list(SECTION_COLUMNS),
            "rows": max(0, len(data_rows) - 1)}


# ---------------------------------------------------------------- 打印

def print_triage(result: dict) -> None:
    rows = [
        ("基准表", "%s（底色 %s，组件 %d）" % (result["ref"]["table"], result["ref"]["kind"],
                                            result["ref"]["component_count"])),
        ("我方表", "%s（底色 %s，组件 %d）" % (result["ours"]["table"], result["ours"]["kind"],
                                            result["ours"]["component_count"])),
        ("规则文件", "%s（sha256-16 %s）" % (result["rules"]["path"],
                                           result["rules"]["sha256_16"])),
        ("分诊行数", "%d（T2a 差异 %s 条 major／%s 条 minor ＋ 配对级行）"
                     % (len(result["rows"]), result["diff_counts"]["major"],
                        result["diff_counts"]["minor"])),
        ("三档计数", "允许差 %d／执行自由度内差异 %d／升级 %d"
                     % (result["counts"]["allow"], result["counts"]["free"],
                        result["counts"]["escalate"])),
    ]
    notes = []
    for row in result["rows"]:
        mark = {"allow": "允许", "free": "自由度", "escalate": "升级"}[row["tier"]]
        notes.append("[%s] %s %s %s｜依据 %s"
                     % (mark, row["row_id"], KIND_ZH.get(row["kind"], row["kind"]),
                        str(row["evidence"])[:80], _basis_text(row)[:80]))
    out = result.get("out", {})
    for key, label in (("table", "分诊表"), ("json", "JSON"), ("escalations", "异议单目录")):
        if out.get(key):
            notes.append("%s：%s" % (label, out[key]))
    report("差异分诊（H1）", rows, notes)


def print_gate(info: dict) -> None:
    report("交付门禁（H1）", [
        ("交付说明", info["note"]),
        ("强制章节", "%s（第 %d 行，%s 级标题）" % (info["section"], info["line"],
                                                 "#" * info["level"])),
        ("固定字段", "｜".join(info["columns"])),
        ("数据行数", "%d" % info["rows"]),
    ], ["门禁通过：交付物已附《差异分诊表》（A-H1-3）。"])


# ---------------------------------------------------------------- selftest

def _synth_table(image, kind, components):
    total = max([c["bbox_xyxy"][3] for c in components] or [1]) - \
        min([c["bbox_xyxy"][1] for c in components] or [0])
    return {"tool": "synthetic", "image": image, "kind": kind,
            "image_size": [64, 64], "roi": [0, 0, 64, 64], "min_area": 1,
            "total_payload_height_px": total, "annotation_count": 0,
            "components": components}


def _synth_component(bbox, px, shape, offset_y=0.0, height_fraction=0.5,
                     area_fraction=0.2, contacts=None):
    x0, y0, x1, y1 = bbox
    return {"label": "synth", "kind": "component", "bbox_xyxy": list(bbox),
            "bbox_wh": [x1 - x0, y1 - y0], "pixel_count": px,
            "centroid": [(x0 + x1) / 2.0, (y0 + y1) / 2.0],
            "height_fraction": height_fraction, "area_fraction": area_fraction,
            "anchor_ratio": None, "shape_class": shape, "shape_descriptors": {},
            "holes": {"count": 0}, "openings": {"count": 0, "top_band_count": 0, "items": []},
            "contacts": contacts or []}


def _synth_case():
    """一组合成表：一条 allow、一条 free、一条显式升级、一条默认升级。"""
    ref = _synth_table("synth_ref.png", "white", [
        _synth_component((0, 0, 200, 40), 4000, "rect", height_fraction=0.2,
                         area_fraction=0.3),
        _synth_component((0, 60, 200, 70), 900, "circle", height_fraction=0.2,
                         area_fraction=0.2),
    ])
    ours = _synth_table("synth_ours.png", "black", [
        _synth_component((0, 0, 500, 40), 9000, "rod", height_fraction=1.0,
                         area_fraction=0.5),
        _synth_component((0, 60, 200, 70), 900, "circle", height_fraction=0.2,
                         area_fraction=0.2),
        _synth_component((400, 20, 420, 30), 120, "other", height_fraction=0.05,
                         area_fraction=0.01),
    ])
    rules = {
        "version": 1,
        "allow": [{"list_item_id": "AL-1", "note": "star spec", "kind": "added",
                   "ours_max": 200}],
        "free": [{"scope": "shape-cosmetics", "note": "shape wording", "kind": "shape_class"}],
        "escalate": [{"judgement": "CFG-1", "note": "long rod", "advice": "replace",
                      "kind": "height_fraction", "abs_delta_min": 0.5}],
        "escalation": {"default": "escalate",
                       "template": {"title": "异议单",
                                    "fields": ["差异描述", "特征证据", "建议方案"]}},
    }
    return ref, ours, rules


def _selftest() -> int:
    import tempfile

    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        ref, ours, rules = _synth_case()
        rules_path = os.path.join(tmp, "rules.yaml")
        with open(rules_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(rules, handle, allow_unicode=True)
        ruleset = load_rules(rules_path)
        diff = compare_tables(ref, ours)
        result = triage_pair(diff, ref, ours, ruleset)
        by_kind = {}
        for row in result["rows"]:
            by_kind.setdefault(row["kind"], []).append(row)

        added = by_kind.get("added", [{}])[0]
        checks.append(("allow_tier", added.get("tier") == TIER_ALLOW
                       and added.get("basis", {}).get("list_item_id") == "AL-1",
                       "added 行 → %s，依据 %s" % (added.get("tier_zh"), added.get("basis"))))
        shape = by_kind.get("shape_class", [{}])[0]
        checks.append(("free_tier", shape.get("tier") == TIER_FREE
                       and shape.get("basis", {}).get("scope") == "shape-cosmetics",
                       "shape_class 行 → %s，依据 %s"
                       % (shape.get("tier_zh"), shape.get("basis"))))
        hf = [r for r in by_kind.get("height_fraction", []) if r["tier"] == TIER_ESCALATE]
        checks.append(("explicit_escalate", bool(hf)
                       and hf[0]["basis"].get("judgement") == "CFG-1",
                       "height_fraction 大差 → 升级，判据 %s"
                       % (hf[0]["basis"].get("judgement") if hf else None)))
        plain = by_kind.get("count", [{}])[0]
        checks.append(("default_escalate", plain.get("tier") == TIER_ESCALATE
                       and plain.get("hit") == "default",
                       "count 行（无规则）→ %s／%s"
                       % (plain.get("tier_zh"), plain.get("hit"))))
        bg = by_kind.get("background_kind", [{}])[0]
        checks.append(("pair_background_row", bg.get("kind") == "background_kind"
                       and "ref_kind=white" in bg.get("subject", ""),
                       "配对级底色行：%s" % bg.get("subject", "-")[:60]))
        checks.append(("escalations_built", len(result["escalations"]) >= 2
                       and all(e.get("escalation_id") for e in result["escalations"]),
                       "异议单 %d 张" % len(result["escalations"])))

        # 产物落盘
        out = os.path.join(tmp, "out")
        ensure_dir(out)
        table_md = os.path.join(out, "%s.md" % SECTION_NAME)
        with open(table_md, "w", encoding="utf-8") as handle:
            handle.write(render_markdown(result))
        result["out"] = {"dir": out, "table": table_md}
        _write_escalations(result, out, ruleset)
        notes = sorted(os.listdir(os.path.join(out, "escalations")))
        checks.append(("escalation_files", len(notes) == len(result["escalations"]),
                       "escalations/：%s" % ", ".join(notes)))
        text = open(table_md, encoding="utf-8").read()
        checks.append(("table_columns", all(c in text for c in SECTION_COLUMNS)
                       and text.count("|") > 10,
                       "分诊表含固定字段 %s" % "｜".join(SECTION_COLUMNS)))

        # 模板
        tmpl = section_template(ruleset)
        checks.append(("template_skeleton", all(c in tmpl for c in SECTION_COLUMNS)
                       and tmpl.startswith("## %s" % SECTION_NAME),
                       "模板首行 %r" % tmpl.splitlines()[0]))

        # 门禁：带章节 → 通过；删章节 → 阻断
        good = os.path.join(tmp, "note_good.md")
        with open(good, "w", encoding="utf-8") as handle:
            handle.write("# 交付说明\n\n" + tmpl + "\n## 其他\n\n无关内容\n")
        info = check_note(good)
        checks.append(("gate_pass", info["rows"] >= 1 and info["section"] == SECTION_NAME,
                       "门禁通过，数据行 %d" % info["rows"]))

        bad = os.path.join(tmp, "note_bad.md")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("# 交付说明\n\n## 其他\n\n删掉了分诊表章节。\n")
        blocked = False
        try:
            check_note(bad)
        except ImgCmpError as exc:
            blocked = "missing the required section" in str(exc)
        checks.append(("gate_block_missing_section", blocked,
                       "缺章节 → ImgCmpError（非零退出）"))

        no_fields = os.path.join(tmp, "note_nofields.md")
        with open(no_fields, "w", encoding="utf-8") as handle:
            handle.write("# 交付说明\n\n## %s\n\n只有标题没有表。\n" % SECTION_NAME)
        blocked = False
        try:
            check_note(no_fields)
        except ImgCmpError as exc:
            blocked = "required field" in str(exc)
        checks.append(("gate_block_missing_fields", blocked,
                       "有章节无字段表 → ImgCmpError"))

        # 规则保守性：无选择器 / allow 缺 list_item_id / 非法正则 → 拒
        rejects = 0
        for bad_rule in ({"version": 1, "allow": [{"list_item_id": "X", "note": "wild"}]},
                         {"version": 1, "allow": [{"note": "no id", "kind": "added"}]},
                         {"version": 1, "free": [{"note": "no scope", "kind": "added"}]},
                         {"version": 1, "escalate": [{"note": "no judgement",
                                                      "kind": "added"}]},
                         {"version": 1, "allow": [{"list_item_id": "X", "kind": "added",
                                                   "pattern": "([unclosed"}]},
                         {"version": 1, "escalation": {"default": "nope"}},
                         {"version": 1, "nonsense": []}):
            path = os.path.join(tmp, "bad_rules.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(bad_rule, handle, allow_unicode=True)
            try:
                load_rules(path)
            except ImgCmpError:
                rejects += 1
        checks.append(("rules_are_fail_closed", rejects == 7,
                       "7 份坏规则全部被拒（%d/7）" % rejects))

    return selftest_report(TOOL, checks)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = make_parser(
        TOOL,
        "H1 difference triage and delivery gate: map the T2a diff table onto a caller-"
        "supplied rule file and sort every difference into allowance / freedom-of-execution "
        "/ escalation (escalation is the default and cannot be self-exempted); emit the "
        "triage table and one escalation note per escalated difference; gate the delivery "
        "note so that a missing triage section is a non-zero exit.",
    )
    sub = parser.add_subparsers(dest="command")

    tri = sub.add_parser("triage", help="triage one baseline/ours pair")
    tri.add_argument("--ref", metavar="FEAT_JSON|IMAGE",
                     help="baseline feature-table JSON, or an image (a table is built first)")
    tri.add_argument("--ours", metavar="FEAT_JSON|IMAGE",
                     help="our feature-table JSON, or an image (a table is built first)")
    tri.add_argument("--rules", metavar="YAML",
                     help="rule file (allow / free / escalate / escalation)")
    tri.add_argument("--out", metavar="DIR", help="output directory")
    tri.add_argument("--json", metavar="OUT", help="JSON path (default <out>/triage.json)")
    tri.add_argument("--template", action="store_true",
                     help="print the delivery-note triage section skeleton and exit")
    tri.add_argument("--ref-kind", default="auto", metavar="KIND",
                     help="white|black|auto for --ref when it is an image (default auto)")
    tri.add_argument("--ours-kind", default="auto", metavar="KIND",
                     help="white|black|auto for --ours when it is an image (default auto)")
    tri.add_argument("--ref-roi", metavar="X,Y,W,H", help="ROI for --ref if it is an image")
    tri.add_argument("--ours-roi", metavar="X,Y,W,H", help="ROI for --ours if it is an image")
    tri.add_argument("--selftest", action="store_true", help="run the built-in self-test")

    gate = sub.add_parser("gate", help="check the delivery note for the triage section")
    gate.add_argument("--delivery-note", metavar="FILE",
                      help="delivery note (Markdown); required unless --template")
    gate.add_argument("--section", default=SECTION_NAME, metavar="NAME",
                      help="required section heading (default %s)" % SECTION_NAME)
    gate.add_argument("--template", action="store_true",
                      help="print the delivery-note triage section skeleton and exit")
    gate.add_argument("--selftest", action="store_true", help="run the built-in self-test")
    return parser


def main(argv=None) -> int:
    return run_main(TOOL, _run, argv)


def _run(argv) -> int:
    args = list(argv) if argv is not None else None
    if args is not None and args and args[0] == "--selftest":
        return _selftest()
    parser = build_parser()
    ns = parser.parse_args(args)
    if getattr(ns, "selftest", False):
        return _selftest()
    if not ns.command:
        parser.print_usage()
        raise ImgCmpError("a subcommand is required: triage | gate (or --selftest)")

    if ns.command == "triage":
        if ns.template:
            print(section_template())
            return 0
        missing = [name for name in ("ref", "ours", "rules", "out") if not getattr(ns, name)]
        if missing:
            raise ImgCmpError("missing required argument(s) for 'triage': %s"
                              % ", ".join("--" + name for name in missing))
        result = run_triage(ns.ref, ns.ours, ns.rules, ns.out, json_path=ns.json,
                            ref_kind=ns.ref_kind, ours_kind=ns.ours_kind,
                            ref_roi=ns.ref_roi, ours_roi=ns.ours_roi)
        print_triage(result)
        return 0

    # gate
    if ns.template:
        print(section_template())
        return 0
    if not ns.delivery_note:
        raise ImgCmpError("--delivery-note FILE is required for 'gate'")
    info = check_note(ns.delivery_note, ns.section)
    print_gate(info)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
