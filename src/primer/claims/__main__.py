# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.claims extract`` 与 ``python -m primer.claims verify``。

``extract`` 走**确定性**的前半程：书稿正文 → 引用标记 → 原文链路 → 候选段落，写出
``claims.json`` 与 ``claims.md``，**不联网、不调模型**。

``verify`` 走**后半程**：先把论断译成英文（缓存到磁盘），再在原文上分两路检索并合并
候选，然后逐对判定论断是否真被原文支撑，写出 ``verdicts.json`` 与 ``verdicts.md``，
并追加 ``ledger.jsonl``。``--dry-run`` 只报价、不发请求；``--limit`` / ``--only`` 收窄
范围；``--force`` 重做已完成的对。判定规矩见 :mod:`primer.claims.judge`。

    python3 -m primer.claims extract --project-root <工程根>
    python3 -m primer.claims verify  --project-root <工程根> --limit 5
    python3 -m primer.claims verify  --project-root <工程根> --only 291

两个子命令共用同一套输入（正文、参考文献、本地原文、转换账本），默认路径按本工程
（行星探测工程）的常规布局取；默认输入不存在时**显式**记进产物的 ``inputs`` 并打一行
stderr，命令行显式给出的输入若不存在则直接报错。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..literature.paths import output_dir_for, relative_to_root, resolve_project_root
from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
)
from .extract import DEFAULT_BODY_FILES, extract_claims, strip_citations
from .report import (
    build_payload,
    print_summary,
    print_verify_summary,
    write_claims_json,
    write_claims_md,
    write_verdicts_json,
    write_verdicts_md,
)
from .resolve import (
    DEFAULT_LITERATURE_FLAT,
    DEFAULT_LITERATURE_STATE,
    DEFAULT_LOCAL_ROOT,
    DEFAULT_REFERENCE_MD,
    DEFAULT_REFLIB_LOG,
    DEFAULT_RESCUE_CACHE,
    DEFAULT_REFERENCES_INDEX,
    MIN_TEXT_CHARS,
    STATUS_OK,
    build_resolver,
    describe_inputs,
    dump_state_summary,
)
from .retrieve import MIN_SCORE, TOP_K, SourceIndex, build_index, retrieve_passages
from .translate import MAX_BATCH_CHARS, MAX_BATCH_ITEMS, MAX_RETRIES
from .verify import Verifier, build_client

PROG = "python -m primer.claims"


def _add_input_flags(parser: argparse.ArgumentParser, *, include_tables: bool) -> None:
    """两个子命令共用的输入参数（正文、参考文献、本地原文、转换账本、输出目录）。"""
    parser.add_argument("--project-root", metavar="PATH", help="project root (read-only; default: current directory)")
    parser.add_argument("--body", action="append", metavar="MD", default=[], help="manuscript markdown (repeatable)")
    parser.add_argument("--ref", metavar="MD", help="bibliography markdown")
    parser.add_argument("--reflib-log", metavar="CSV", help="reflib_download_log.csv")
    parser.add_argument("--rescue-cache", metavar="JSON", help="arxiv_rescue_cache.json")
    parser.add_argument("--local-root", action="append", metavar="DIR", default=[], help="local PDF roots (repeatable)")
    parser.add_argument("--literature-state", metavar="JSONL", help="literature conversion ledger (state.jsonl)")
    parser.add_argument("--flat-dir", metavar="DIR", help="directory of converted flat markdown")
    parser.add_argument("--references-index", metavar="CSV", help="references index.csv (audit verdicts, optional)")
    parser.add_argument("--out", metavar="DIR", help="output directory (default: <project root>/_primer/claims)")
    parser.add_argument("--top-k", type=int, default=TOP_K, metavar="N", help="candidate passages per pair")
    parser.add_argument("--min-score", type=float, default=MIN_SCORE, metavar="X",
                        help="score floor; below it the record is marked candidates-weak")
    parser.add_argument("--min-text-chars", type=int, default=MIN_TEXT_CHARS, metavar="N",
                        help="minimum body characters for a converted file to count as usable")
    if include_tables:
        parser.add_argument("--include-tables", action="store_true",
                            help="also keep claims found in table rows (skipped by default)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Link manuscript claims to the text of the works they cite.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("extract", help="extract claim-citation pairs and resolve them to local text")
    _add_input_flags(run, include_tables=True)

    check = sub.add_parser("verify", help="translate each claim, retrieve on both queries, and judge the verdict")
    _add_input_flags(check, include_tables=False)
    check.add_argument("--limit", type=int, metavar="N", help="process at most N pairs of the selected scope")
    check.add_argument("--only", action="append", metavar="TOKEN", default=[],
                       help="restrict to a citation number (291), a claim id (c0007) or a pair key "
                            "(c0007#291); repeatable")
    check.add_argument("--dry-run", action="store_true",
                       help="print the plan and the cost estimate; send nothing")
    check.add_argument("--force", action="store_true", help="re-do pairs already in the ledger")
    check.add_argument("--base-url", default=DEFAULT_BASE_URL, metavar="URL", help="OpenAI-compatible endpoint")
    check.add_argument("--model", default=DEFAULT_MODEL, metavar="NAME", help="model name")
    check.add_argument("--key-env", default=DEFAULT_KEY_ENV, metavar="VAR",
                       help="environment variable holding the API key (never printed)")
    check.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, metavar="N",
                       help="output budget per request; the reasoning chain draws on it too")
    check.add_argument("--batch-chars", type=int, default=MAX_BATCH_CHARS, metavar="N",
                       help="character budget per translation request")
    check.add_argument("--batch-items", type=int, default=MAX_BATCH_ITEMS, metavar="N",
                       help="claims per translation request")
    check.add_argument("--max-retries", type=int, default=MAX_RETRIES, metavar="N",
                       help="retries for one LLM call (empty content counts as a failure)")
    return parser


def _pick(explicit: Optional[str], default: Path) -> Optional[Path]:
    """显式给出的路径原样采用（交给调用方校验），否则用默认路径——默认路径不存在时返回 None。"""
    if explicit:
        return Path(explicit)
    return default if default.exists() else None


def _require(path: Optional[Path], what: str) -> Path:
    if path is None or not path.exists():
        raise ValueError(f"{what} not found: {path if path else '<none>'}")
    return path


class Inputs:
    """一次运行用到的全部输入路径；两个子命令共用。"""

    def __init__(self, args: argparse.Namespace, project_root: Path):
        self.project_root = project_root
        self.body = [Path(path) for path in args.body] or [
            project_root / name for name in DEFAULT_BODY_FILES
        ]
        self.reference_md = _pick(args.ref, project_root / DEFAULT_REFERENCE_MD)
        self.reflib_log = _pick(args.reflib_log, project_root / DEFAULT_REFLIB_LOG)
        self.rescue_cache = _pick(args.rescue_cache, project_root / DEFAULT_RESCUE_CACHE)
        self.literature_state = _pick(args.literature_state, project_root / DEFAULT_LITERATURE_STATE)
        self.flat_dir = _pick(args.flat_dir, project_root / DEFAULT_LITERATURE_FLAT)
        self.audit_index = _pick(args.references_index, project_root / DEFAULT_REFERENCES_INDEX)
        if args.local_root:
            self.local_roots = [Path(root) for root in args.local_root]
        else:
            candidate = project_root / DEFAULT_LOCAL_ROOT
            self.local_roots = [candidate] if candidate.exists() else []
        self.out = Path(args.out) if args.out else output_dir_for(project_root, "claims")
        self.validate()

    def validate(self) -> None:
        for path in self.body:
            if not path.is_file():
                raise ValueError(f"body markdown not found: {path}")
        _require(self.reference_md, "bibliography markdown")
        for path, what in (
            (self.reflib_log, "reflib download log"),
            (self.rescue_cache, "arxiv rescue cache"),
            (self.literature_state, "literature ledger"),
            (self.audit_index, "references index"),
        ):
            if path is not None and not path.exists():
                raise ValueError(f"{what} not found: {path}")
        for root in self.local_roots:
            if not root.is_dir():
                raise ValueError(f"local PDF root not found: {root}")

    def describe(self) -> dict:
        """产物里的 ``inputs``：路径相对工程根，缺了哪个一眼能看见。"""
        described = describe_inputs(
            {
                "reference_md": self.reference_md,
                "reflib_log": self.reflib_log,
                "rescue_cache": self.rescue_cache,
                "literature_state": self.literature_state,
                "flat_dir": self.flat_dir,
                "references_index": self.audit_index,
            },
            self.project_root,
        )
        for index, path in enumerate(self.body):
            described[f"body[{index}]"] = {
                "path": relative_to_root(path, self.project_root),
                "exists": path.is_file(),
            }
        for index, root in enumerate(self.local_roots):
            described[f"local_root[{index}]"] = {
                "path": relative_to_root(root, self.project_root),
                "exists": root.is_dir(),
            }
        return described

    def warn_absent(self) -> None:
        for name, entry in self.describe().items():
            if not entry["exists"]:
                print(f"warning: input '{name}' is absent: {entry['path']}", file=sys.stderr)

    def resolver(self, *, min_text_chars: int):
        return build_resolver(
            self.project_root,
            reference_md=self.reference_md,
            reflib_log=self.reflib_log,
            rescue_cache=self.rescue_cache,
            local_roots=self.local_roots,
            literature_state=self.literature_state,
            flat_dir=self.flat_dir,
            audit_index=self.audit_index,
            min_text_chars=min_text_chars,
        )


def _source_language(indexes: Mapping[str, SourceIndex]) -> dict:
    """量一下可解析原文的语言：全英文的原文遇到中文论断时，词面检索注定命中很少。

    这个数字是给后半程的判断依据——"候选很弱"有多少是检索方法的锅，有多少是链路本来就断。
    """
    if not indexes:
        return {"sources": 0, "english_only": 0, "note": "no resolvable source text"}
    english_only = 0
    for index in indexes.values():
        text = index.text
        if not text:
            continue
        cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
        if cjk / len(text) < 0.05:
            english_only += 1
    return {
        "sources": len(indexes),
        "english_only": english_only,
        "note": (
            "lexical retrieval cannot bridge Chinese claims to English sources; pairs whose "
            "claim shares no token with the source end up candidates-weak/no-candidates"
        ),
    }


def _extract(args: argparse.Namespace, project_root: Path) -> int:
    inputs = Inputs(args, project_root)
    inputs.warn_absent()
    extraction = extract_claims(
        inputs.body, project_root=project_root, include_tables=args.include_tables
    )
    resolver = inputs.resolver(min_text_chars=args.min_text_chars)
    numbers = sorted({record.citation for record in extraction.claims})
    resolutions = resolver.resolve_all(numbers)

    indexes: dict[str, SourceIndex] = {}
    retrievals = {}
    for record in extraction.claims:
        resolution = resolutions[record.citation]
        if resolution.status != STATUS_OK or resolution.source is None:
            continue
        key = resolution.source.local_file
        index = indexes.get(key)
        if index is None:
            text = resolver.text_for(key, project_root / resolution.source.markdown)
            index = build_index(text)
            indexes[key] = index
        retrievals[record.id] = retrieve_passages(
            strip_citations(record.claim), index, k=args.top_k, min_score=args.min_score
        )

    payload = build_payload(
        extraction=extraction,
        resolutions=resolutions,
        retrievals=retrievals,
        inputs=inputs.describe(),
        literature=dump_state_summary(inputs.literature_state, project_root),
        source_language=_source_language(indexes),
        include_tables=args.include_tables,
        top_k=args.top_k,
    )
    write_claims_json(inputs.out, payload)
    write_claims_md(inputs.out, payload)
    print_summary(payload, inputs.out, project_root)
    return 0


def _verify(args: argparse.Namespace, project_root: Path) -> int:
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be at least 1")
    inputs = Inputs(args, project_root)
    extraction = extract_claims(inputs.body, project_root=project_root, include_tables=False)
    records = extraction.claims
    resolver = inputs.resolver(min_text_chars=args.min_text_chars)
    numbers = sorted({record.citation for record in records})

    client = build_client(
        base_url=args.base_url,
        model=args.model,
        key_env=args.key_env,
        max_tokens=args.max_tokens,
    )
    verifier = Verifier(
        project_root=project_root,
        resolver=resolver,
        client=client,
        out_dir=inputs.out,
        top_k=args.top_k,
        min_score=args.min_score,
        force=args.force,
        batch_chars=args.batch_chars,
        batch_items=args.batch_items,
        max_retries=args.max_retries,
    )
    plan = verifier.plan(records, numbers=numbers, only=args.only, limit=args.limit)
    summary = plan.as_dict(limit=args.limit, only=args.only, force=args.force)
    print(
        f"primer.claims verify plan: {summary['pairs']} pair(s), scope {summary['scope']}, "
        f"selected {summary['selected']}, already done {summary['skipped']}, to process {summary['to_process']}"
    )
    print(
        f"  judgeable in scope: {summary['judgeable_in_scope']};"
        f" blocked by corpus in scope: {summary['blocked_in_scope']}"
        f"  (whole manuscript: {summary['judgeable']} judgeable / {summary['blocked_by_corpus']} blocked,"
        f" {summary['blocked_citations']} of {summary['citations']} cited numbers have no local PDF)"
    )

    if args.dry_run:
        inputs.warn_absent()
        estimate = verifier.estimate(plan, verifier.cached_translations(plan))
        _print_estimate(estimate)
        print("dry run: nothing was sent, nothing was written")
        return 0

    if not client.configured:
        raise ValueError(
            f"no API key in the environment variable {args.key_env}; "
            "load it (set -a; . <env file>; set +a) or use --dry-run"
        )
    inputs.warn_absent()
    stats = verifier.run(plan)
    payload = verifier.render(
        plan,
        inputs=inputs.describe(),
        limit=args.limit,
        only=args.only,
        force=args.force,
        usage=stats.as_dict(),
        translation=verifier.translator.stats.as_dict(),
    )
    write_verdicts_json(inputs.out, payload)
    write_verdicts_md(inputs.out, payload)
    print_verify_summary(payload, inputs.out, project_root)
    return 0


def _print_estimate(estimate: Mapping) -> None:
    translation = estimate["translation"]
    judge = estimate["judge"]
    distribution = estimate["min_score_recheck"]
    print("  cost estimate (prompt tokens are estimates; no request is sent)")
    print(f"    pairs to process: {estimate['to_process']}"
          f" (judgeable {estimate['judgeable_to_process']},"
          f" blocked by corpus {estimate['blocked_to_process']})")
    print(f"    translation: {translation['distinct_sentences_needing_translation']} sentence(s) to translate"
          f" in {translation['batches']} request(s)"
          f" ({translation['sentences_served_from_cache']} served from cache),"
          f" ~{translation['approx_prompt_tokens']:,} prompt tokens")
    print(f"    judgement: {judge['first_pass_calls']} first-pass call(s),"
          f" up to {judge['second_pass_calls_at_most']} second-pass call(s)")
    print(f"      ~{judge['approx_prompt_tokens_first_pass']:,} prompt tokens first pass,"
          f" ~{judge['approx_prompt_tokens_second_pass_at_most']:,} second pass (upper bound)")
    print(f"    total estimated prompt tokens: ~{estimate['approx_prompt_tokens_total']:,}")
    print(f"    min-score recheck on the retrieval as it stands now: {distribution['count']} pair(s),"
          f" median {distribution['median']}, p90 {distribution['p90']}, max {distribution['max']},"
          f" at/above MIN_SCORE {distribution['at_or_above_min_score']}")
    print("      (most translations are not in hand at dry-run time, so this is the pessimistic,"
          " pre-translation view of the score distribution)")
    for note in estimate["assumptions"]:
        print(f"    assumption: {note}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口；成功返回 0，输入有问题返回 2。"""
    args = _parser().parse_args(argv)
    try:
        project_root = resolve_project_root(args.project_root or Path.cwd())
        if args.command == "extract":
            return _extract(args, project_root)
        if args.command == "verify":
            return _verify(args, project_root)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
