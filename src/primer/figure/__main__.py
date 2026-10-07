# -*- coding: utf-8 -*-
"""命令行入口：``python3 -m primer.figure <command>``（装入后即 ``primer-figure``）。

四个子命令：

* ``channels`` —— 看通道：``--list``（列档，不联网）／``--probe``（逐档探活）／
  ``--explain``（说明选了哪条、依据哪一级规则）／``--set NAME``（把某档写进用户偏好文件）；
* ``gen`` —— 出图：读提示词 md 的 ``## figX-Y`` 分节，自动续号 ``<fig>-zh_v<N>.<ext>``，
  逐张落盘并追加 ``log.jsonl`` 台账；
* ``price`` —— 估算：按 1K/2K 与 4K 张数算 USD（可切 Batch 档、可折 CNY）；
* ``spec`` —— 由图稿 yaml 抽**逐字文字清单**，拼装成中文提示词 md。

语言纪律（CODING_STANDARDS v1.1 §2.2）：stdout 只出中文人读报告；stderr 的诊断、异常与
``--help`` 里的选项名一律英文。**任何路径都不打印密钥值**；代理 URL 一律掩码成
``user:***@host``；``channels --set`` 只写档名，不写 URL。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..envfile import discover_env_file, load_env_file
from . import DEFAULT_MODEL, SIZES
from . import meta, pricing, routing, runner
from .adapters import AdapterError
from .routing import RouteError

__all__ = ["build_parser", "main"]

PROG = "python3 -m primer.figure"
DEFAULT_OUT_DIR = "figures_out"


class CliError(Exception):
    """CLI 侧的用户错误（参数组合不对等）。消息英文。"""


# ---------------------------------------------------------------- 解析器

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="科研图件直出流水线：通道探活 → 出图 → 估价 → 提示词装配。")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    channels = sub.add_parser("channels", help="查看／探活／设定代理通道",
                              description="查看档案里的通道、探活、说明本次会选哪一条。")
    group = channels.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="列出档案里的档（不联网，缺省动作）")
    group.add_argument("--probe", action="store_true", help="逐档探活并写缓存")
    group.add_argument("--explain", action="store_true", help="说明选了哪条、依据哪一级规则")
    group.add_argument("--set", metavar="NAME", help="把某档写进用户偏好文件 preferred_proxy")
    channels.add_argument("--profiles", metavar="YAML", help="档案路径（缺省 ~/.config/primer/proxies.yaml）")
    channels.add_argument("--provider", default="gemini", help="provider 名（决定 <PROVIDER>_API_PROXY）")
    channels.add_argument("--proxy", metavar="X", help="显式指定档名／URL／direct（同 gen 的 --proxy）")
    channels.add_argument("--cache", metavar="JSON", help="探活缓存路径（缺省 ~/.cache/primer/channels.json）")

    gen = sub.add_parser("gen", help="按提示词 md 出图并记台账",
                         description="按提示词 md 的 ## figX-Y 分节出图，自动续号 <fig>-zh_v<N>.<ext>。")
    gen.add_argument("figs", nargs="+", metavar="FIG", help="图号（如 fig1-1），可给多个")
    gen.add_argument("--prompt", metavar="MD", required=True, help="提示词 md（## figX-Y 分节）")
    gen.add_argument("-n", type=int, default=1, metavar="N", help="每个图号连出几张（缺省 1）")
    gen.add_argument("--size", choices=list(SIZES), default=None,
                     help="档位 1K/2K/4K（1K 与 2K 同价，迭代建议 2K；缺省=模型默认）")
    gen.add_argument("--ref", metavar="PATH", help="参考图（定稿复现用；自动加“以附图为基准”前缀）")
    gen.add_argument("--ratio", action="append", default=[], metavar="R",
                     help="候选比例（如 5:4），可重复；被拒按序降级，全拒退到不带比例")
    gen.add_argument("--out", metavar="DIR", default=DEFAULT_OUT_DIR, help="输出目录（缺省 %s）" % DEFAULT_OUT_DIR)
    gen.add_argument("--model", default=DEFAULT_MODEL, help="模型名（缺省 %s）" % DEFAULT_MODEL)
    gen.add_argument("--proxy", metavar="X", help="显式通道：档名／URL／direct")
    gen.add_argument("--profiles", metavar="YAML", help="代理档案路径")
    gen.add_argument("--no-probe", action="store_true",
                     help="不按 default_order 探活（显式与环境变量给的通道照旧生效）")
    gen.add_argument("--env-file", metavar="PATH", help="显式 .env（缺省按 primer.envfile 的发现顺序）")

    price = sub.add_parser("price", help="估算出图成本（USD）",
                           description="按价目表估算：1K/2K 张数、4K 张数、参考图张数。")
    price.add_argument("--n-1k2k", type=int, default=0, metavar="N", help="1K/2K 张数（两张同价）")
    price.add_argument("--n-4k", type=int, default=0, metavar="N", help="4K 张数")
    price.add_argument("--n-ref", type=int, default=0, metavar="N", help="参考图输入张数")
    price.add_argument("--model", default=DEFAULT_MODEL, help="模型名（缺省 %s）" % DEFAULT_MODEL)
    price.add_argument("--batch", action="store_true", help="按 Batch 档（异步，一律半价）")
    price.add_argument("--cny-rate", type=float, default=pricing.CNY_RATE_DEFAULT, metavar="R",
                       help="USD→CNY 折算率（缺省 %.1f；给 0 则不折算）" % pricing.CNY_RATE_DEFAULT)

    spec = sub.add_parser("spec", help="由图稿 yaml 装配中文提示词 md",
                          description="抽 content 子树里的逐字文字清单，拼成中文提示词 md。")
    spec.add_argument("fig", metavar="FIGYAML", help="图稿 yaml（含 content 子树，或 fig: 包裹）")
    spec.add_argument("--title", metavar="T", required=True, help="中文标题")
    spec.add_argument("--layout", metavar="LAYOUTFILE", required=True, help="版式说明文件（纯文本）")
    spec.add_argument("--out", metavar="MD", required=True, help="输出提示词 md")
    return parser


# ---------------------------------------------------------------- 子命令

def _channels(args: argparse.Namespace) -> int:
    environ = None
    profiles_path = Path(args.profiles) if args.profiles else None
    cache_path = Path(args.cache) if args.cache else None
    doc = routing.load_profiles(profiles_path, environ=environ)

    if args.set:
        return _set_preference(args.set, doc)
    if args.probe:
        probes = routing.probe_all(profiles=doc, cache_path=cache_path, environ=environ)
        print("通道探活（档案：%s）" % (doc.path or "<缺省：只有直连>"))
        for probe in probes:
            print("  %-10s %-4s %-30s %s"
                  % (probe.name, "通过" if probe.ok else "未通过",
                     routing.mask_proxy_url(probe.url),
                     "%s（%.1f s）" % (probe.detail or "-", probe.seconds)))
        print("  · 结论已写入缓存：%s"
              % (cache_path or routing.default_cache_path(environ)))
        return 0
    if args.explain:
        print(routing.explain(args.provider, profiles=doc, explicit=args.proxy,
                              cache_path=cache_path, environ=environ))
        return 0
    print("代理通道（档案：%s）" % (doc.path or "<缺省：只有直连>"))
    for row in routing.list_channels(profiles=doc, cache_path=cache_path, environ=environ):
        mark = "直连" if row["direct"] else "代理"
        cached = row["cached"]
        tail = ""
        if cached is not None:
            tail = "｜缓存：%s（%s）" % ("通过" if cached["ok"] else "未通过", cached["detail"])
        print("  %-10s %-4s %-30s %s%s"
              % (row["name"], mark, row["url"], row["note"] or "-", tail))
    quick = routing.explain(args.provider, profiles=doc, explicit=args.proxy,
                            cache_path=cache_path, environ=environ,
                            allow_probe=False).splitlines()
    print("  · 不探活时的判定：%s" % "｜".join(quick[:1] + quick[2:3]))
    print("  · 要按 default_order 探活取首个通过，用 --explain；逐档探活用 --probe。")
    return 0


def _set_preference(name: str, doc: routing.ChannelProfiles) -> int:
    """把档名写进用户偏好文件（只写档名，不写 URL、不写口令）。"""
    if name != routing.DIRECT and name not in doc.profiles:
        raise RouteError("unknown proxy profile: %r (known: %s)"
                         % (name, ", ".join(sorted(doc.profiles))))
    target = routing.preference_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n", encoding="utf-8")
    print("已把偏好通道设为：%s（写入 %s）" % (name, target))
    return 0


def _load_env(args: argparse.Namespace) -> None:
    """按 primer.envfile 的发现顺序加载 .env；只报变量名，任何值不打印。"""
    explicit = Path(args.env_file) if args.env_file else None
    path = discover_env_file(explicit)
    if path is None:
        return
    if not path.is_file():
        raise CliError("env file not found: %s" % path)
    report = load_env_file(path)
    parts = ["注入 %d 项" % len(report.injected)]
    if report.injected:
        parts[0] += "：" + "、".join(report.injected)
    if report.skipped:
        parts.append("已存在跳过 %d 项" % len(report.skipped))
    if report.ignored_lines:
        parts.append("未识别 %d 行" % len(report.ignored_lines))
    print("已加载环境变量文件：%s（%s）" % (path, "；".join(parts)), file=sys.stderr)
    for warning in report.warnings:
        print("warning: %s" % warning, file=sys.stderr)


def _gen(args: argparse.Namespace) -> int:
    _load_env(args)
    if args.n < 1:
        raise CliError("-n must be >= 1, got %d" % args.n)

    def on_row(row: dict) -> None:
        sys.stdout.write(_row_line(row) + "\n")
        sys.stdout.flush()

    report = runner.run_figures(
        args.figs, args.prompt, out_dir=args.out, n=args.n, size=args.size, ref=args.ref,
        model=args.model, proxy=args.proxy, ratios=tuple(args.ratio), on_row=on_row,
        profiles_path=Path(args.profiles) if args.profiles else None, route=not args.no_probe)
    print("")
    print("出图完成：成功 %d 张，失败 %d 张" % (report.ok_count, report.failure_count))
    print("  输出目录：%s" % report.out_dir)
    print("  台账：%s" % report.log_path)
    if report.channel is not None:
        print("  通道：%s（%s，%s）" % (report.channel.name, report.channel.masked_url,
                                       report.channel.rule))
    for row in report.failures:
        print("  ✗ [%s v%d] %s" % (row["fig"], row["attempt"], row.get("error", "-")))
    return 1 if report.failure_count else 0


def _row_line(row: dict) -> str:
    if row["ok"]:
        return ("  [%s v%d] ✓ %s（%.1f s，%d B，%s%s，通道 %s）"
                % (row["fig"], row["attempt"], row["path"], row["sec"], row["bytes"],
                   row["mime"], "，ratio=%s" % row["ratio"] if row["ratio"] else "",
                   row["channel"] or "-"))
    return ("  [%s v%d] ✗ 失败：%s（通道 %s）"
            % (row["fig"], row["attempt"], row.get("error", "-"), row["channel"] or "-"))


def _price(args: argparse.Namespace) -> int:
    cny_rate = args.cny_rate if args.cny_rate else None
    result = pricing.estimate(args.n_1k2k, args.n_4k, args.n_ref, args.model,
                              batch=args.batch, cny_rate=cny_rate)
    print(pricing.format_estimate(result))
    return 0


def _spec(args: argparse.Namespace) -> int:
    doc = meta.load_figure_yaml(args.fig)
    labels = meta.collect_labels(meta.fig_dict(doc))
    layout = Path(args.layout).read_text(encoding="utf-8")
    spec = meta.assemble_spec(args.title, layout, labels)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(spec, encoding="utf-8")
    print("提示词已装配：%s" % out)
    print("  标题：%s" % args.title)
    print("  文字清单：%d 条" % len(meta.label_texts(labels)))
    for index, (path, text) in enumerate(labels, start=1):
        shown = text if len(text) <= 40 else text[:37] + "…"
        print("    %2d. %-28s %s" % (index, path, shown.replace("\n", "／")))
    print("  · 清单段只含文字本身，不含上面的 YAML 路径（路径是给人核对的）。")
    return 0


_HANDLERS = {"channels": _channels, "gen": _gen, "price": _price, "spec": _spec}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口：用户错误 → 退出码 2（消息英文，写 stderr）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(_HANDLERS[args.command](args))
    except (CliError, RouteError, pricing.PricingError, AdapterError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
