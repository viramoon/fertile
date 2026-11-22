#!/usr/bin/env python3
"""Summarize healthwatch JSONL evidence without extrapolating past observed samples."""

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from itertools import groupby
import json
import math
import os
from pathlib import Path
import sys
import tempfile


def timestamp(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("时间戳必须包含时区")
    return at.astimezone(timezone.utc)


def iso(at):
    return at.isoformat().replace("+00:00", "Z")


def cell(value):
    return escape(" ".join(str(value).splitlines()), quote=False).replace("|", r"\|")


def seconds(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def rows(path):
    with path.open(encoding="utf-8") as source:
        for line, raw in enumerate(source, 1):
            try:
                row = json.loads(raw)
                at = timestamp(row["timestamp"])
                if at > datetime.now(timezone.utc):
                    raise ValueError("日志时间在当前 UTC 之后")
                yield at, row
            except (AttributeError, KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"{path} 第 {line} 行有误：{exc}") from exc


@dataclass
class Sample:
    at: datetime
    interval: float
    checks: dict
    context: tuple
    prior_failures: dict = field(default_factory=dict)


def read_samples(path, start, end):
    samples = []
    previous = None
    for at, row in rows(path):
        interval = row.get("interval_seconds")
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
            raise ValueError(f"{iso(at)} 的巡检间隔无效")
        checks = row.get("checks")
        if not isinstance(checks, dict) or not checks:
            raise ValueError(f"{iso(at)} 缺少巡检项")
        for name, item in checks.items():
            if not isinstance(item, dict) or item.get("state") not in ("ok", "failed", "skipped"):
                raise ValueError(f"{iso(at)} 的 {name} 状态无效")
        if previous and previous.at >= at:
            raise ValueError(f"采样记录重复或时间倒退：{iso(at)}")
        context = tuple(row.get(k) for k in ("profile", "market_source", "release"))
        if any(v is not None and not isinstance(v, str) for v in context):
            raise ValueError(f"{iso(at)} 的运行配置标识无效")
        sample = Sample(at, interval, checks, context)
        if start is not None and at < start:
            # Keep one predecessor (including when its failures began), not the
            # entire archive before the requested report.
            carry = previous.prior_failures if previous and not gap(previous, sample) else {}
            sample.prior_failures = {k: carry.get(k, at) for k, item in checks.items()
                                     if item["state"] == "failed"}
        else:
            if previous and not samples:
                samples.append(previous)
            samples.append(sample)
            if end is not None and at > end:
                break
        previous = sample
    if previous and not samples:
        samples.append(previous)
    return samples


def gap(before, after):
    return (after.at - before.at).total_seconds() > 1.5 * before.interval


def gaps(samples, start, end):
    """Only an interval bracketed by adjacent, on-time measurements counts."""
    cursor = start
    missing = []
    for before, after in zip(samples, samples[1:]):
        left = max(start, before.at)
        right = min(end, after.at)
        if left >= right or gap(before, after):
            continue
        if cursor < left:
            missing.append((cursor, left))
        cursor = right
    if cursor < end:
        missing.append((cursor, end))
    return missing


@dataclass
class Incident:
    since: datetime
    last: datetime
    outcome: str


def outages(samples, name, start, end):
    # Retain history before the window to distinguish a new failure from one
    # already active. Never infer recovery across a missing or skipped check.
    incidents = []
    since = previous = None
    for sample in samples:
        if sample.at > end:
            break
        state = sample.checks.get(name, {}).get("state")
        if since is not None and (gap(previous, sample) or state in ("skipped", None)):
            incidents.append(Incident(since, previous.at, "观测中断，恢复未知"))
            since = None
        if state == "failed" and since is None:
            since = sample.prior_failures.get(name, sample.at)
        elif state == "ok" and since is not None:
            incidents.append(Incident(since, sample.at, "已恢复"))
            since = None
        previous = sample
    if since is not None:
        incidents.append(Incident(since, previous.at, "末次采样仍失败"))
    return [i for i in incidents if i.last >= start]


def render(samples, alerts, start, end, hours, label):
    window = [s for s in samples if start <= s.at <= end]
    missing = gaps(samples, start, end)
    duration = (end - start).total_seconds()
    uncovered = sum((right - left).total_seconds() for left, right in missing)
    observed = duration - uncovered
    known = {k for s in window for k in s.checks}
    if samples and samples[0].at < start:
        known.update(samples[0].checks)
    names = sorted(known)
    counts = {k: Counter(s.checks.get(k, {}).get("state", "missing") for s in window)
              for k in names}
    valid = any(c["ok"] + c["failed"] for c in counts.values())
    continuous = valid and not missing and all(c["missing"] == 0 for c in counts.values())
    complete = continuous and observed >= hours * 3600
    periods = sorted({s.interval for s in window})
    lines = [
        "# 健康巡检观察报告", "", f"运行环境：{cell(label)}。时间均为 UTC。", "",
        f"- 报告窗口：{iso(start)} 至 {iso(end)}。",
        f"- 窗内样本：{len(window)}。"
        + (f"首个 {iso(window[0].at)}，末个 {iso(window[-1].at)}。" if window else ""),
        f"- 配置采样周期（秒）：{', '.join(seconds(p) for p in periods) or '无样本'}。",
        f"- 相邻实测样本覆盖 {seconds(observed)} 秒，窗口共 {seconds(duration)} 秒。"
        f"缺口 {len(missing)} 段，共 {seconds(uncovered)} 秒。",
        f"- 连续 {hours:g} 小时采样门禁：{'通过' if complete else '未达到'}。"
        "已跳过的探针不计作可用。业务是否成功请查看各探针结果。",
    ]
    if hours != 24:
        enough = continuous and observed >= 86400
        lines.append(f"- 24 小时采样覆盖：{'达到' if enough else '未达到'}。")
    lines.extend(["", "## 采样来源", "",
                  "下表来自巡检样本记录的 profile、行情源及发布标识。空值表示未记录。", ""])
    if window:
        contexts = {s.context for s in window}
        if len(contexts) > 1:
            lines.append(f"窗口含 {len(contexts)} 组运行配置。各段分别列出，不能作为单版本或单一行情源的连续证据。")
            lines.append("")
        lines.extend(["| 首个样本 | 末个样本 | profile | 行情源 | release | 样本数 |",
                      "| --- | --- | --- | --- | --- | ---: |"])
        for context, group in groupby(window, key=lambda s: s.context):
            period = list(group)
            fields = " | ".join(cell(v) if v else "未记录" for v in context)
            lines.append(f"| {iso(period[0].at)} | {iso(period[-1].at)} | {fields} | {len(period)} |")
    else:
        lines.append("没有可标记来源的样本。")
    lines.extend(["", "## 各巡检项可用率", "",
        "按成功样本占实测样本（成功与失败）计算。缺测表示该轮缺少此项，"
        "跳过表示该轮巡检主动标记为 skipped。", "",
        "| 巡检项 | 成功 | 失败 | 跳过 | 缺测 | 实测可用率 | 跳过原因 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for name, tally in counts.items():
        measured = tally["ok"] + tally["failed"]
        rate = f'{100 * tally["ok"] / measured:.2f}%' if measured else "无实测"
        notes = sorted({cell(s.checks[name].get("detail", "")) for s in window
                        if s.checks.get(name, {}).get("state") == "skipped"})
        lines.append(f'| {cell(name)} | {tally["ok"]} | {tally["failed"]} | '
                     f'{tally["skipped"]} | {tally["missing"]} | {rate} | {"；".join(notes)} |')
    if not names:
        lines.append("| 无样本 | 0 | 0 | 0 | 0 | 无实测 | |")

    ages = []
    for sample in window:
        probe = sample.checks.get("market:fresh", {})
        age = probe.get("max_age_seconds")
        if age is not None and probe.get("state") != "skipped":
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise ValueError(f"{iso(sample.at)} 的行情年龄无效")
            ages.append((age, sample.at))
    lines.extend(["", "## 行情新鲜度", ""])
    if ages:
        age, at = max(ages)
        lines.append(f"最大实测行情年龄 {seconds(age)} 秒，采样时间 {iso(at)}。"
                     f"共 {len(ages)} 轮返回行情年龄。")
    else:
        lines.append("窗口内没有可用的行情年龄样本。")

    lines.extend(["", "## 断线与恢复", "",
                  "相邻失败样本归为一次可观测故障段。耗时从首次失败样本算到首次恢复样本。"
                  "缺测、跳过或采样断档会切断故障段，不能推出缺口内的恢复时刻。", ""])
    incidents = [(name, i) for name in names for i in outages(samples, name, start, end)]
    lines.append(f"窗口涉及 {len(incidents)} 个探针故障段。")
    if incidents:
        totals = Counter(name for name, _ in incidents)
        lines.append("各项故障段数：" + "；".join(f"{cell(k)} {n} 次" for k, n in sorted(totals.items())) + "。")
        lines.extend(["", "| 巡检项 | 首次观测失败 | 末次失败或首次恢复 | 结果 | 实测恢复耗时（秒） |",
                      "| --- | --- | --- | --- | ---: |"])
        for name, i in sorted(incidents, key=lambda pair: pair[1].since):
            elapsed = seconds((i.last - i.since).total_seconds()) if i.outcome == "已恢复" else "—"
            prefix = "（窗前已失败）" if i.since < start else ""
            lines.append(f"| {cell(name)} | {iso(i.since)}{prefix} | {iso(i.last)} | "
                         f"{i.outcome} | {elapsed} |")

    lines.extend(["", "## 采样缺口", "",
                  "间隔超过前一轮设定周期的 1.5 倍时，两端之间整段标为缺测。"
                  "首个或末个记录以外的时间也计入缺口。", ""])
    if missing:
        lines.extend(["| 起点 | 终点 | 秒 |", "| --- | --- | ---: |"])
        for left, right in missing:
            lines.append(f"| {iso(left)} | {iso(right)} | {seconds((right-left).total_seconds())} |")
    else:
        lines.append("窗口内没有采样断档。" if window else "没有采样记录可供判断。")

    lines.extend(["", "## 实际告警", ""])
    events = list(alerts)
    if events:
        lines.extend(["| UTC | 巡检项 | 事件 | 详情 |", "| --- | --- | --- | --- |"])
        for at, row in events:
            lines.append(f'| {iso(at)} | {cell(row["check"])} | {cell(row["event"])} | '
                         f'{cell(row.get("detail", ""))} |')
    else:
        lines.append("指定告警日志在窗口内没有记录。")
    return "\n".join(lines) + "\n", complete


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--samples", required=True, type=Path)
    cli.add_argument("--alerts", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--start", type=timestamp)
    cli.add_argument("--end", type=timestamp)
    cli.add_argument("--require-hours", type=float,
                     help="指定连续采样门限，未达到仍写报告并返回 1（默认只报告 24 小时进度）")
    cli.add_argument("--label", default="未指定")
    args = cli.parse_args()
    hours = args.require_hours if args.require_hours is not None else 24
    if not math.isfinite(hours) or hours <= 0:
        cli.error("--require-hours 必须为正有限数")
    try:
        if args.output.resolve() in (args.samples.resolve(), args.alerts.resolve()):
            raise ValueError("报告不能覆盖观测日志")
        samples = read_samples(args.samples, args.start, args.end)
        if not samples and (args.start is None or args.end is None):
            raise ValueError("零条样本，无法推断观察窗口；可指定 --start 和 --end 输出缺测报告")
        start = args.start if args.start is not None else samples[0].at
        end = args.end if args.end is not None else samples[-1].at
        if end < start:
            raise ValueError("观察终点早于起点")
        alerts = ((at, row) for at, row in rows(args.alerts) if start <= at <= end)
        report, complete = render(samples, alerts, start, end, hours, args.label)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                         delete=False) as out:
            temp = Path(out.name)
            try:
                out.write(report)
                out.flush()
                os.fsync(out.fileno())
                os.replace(temp, args.output)
            finally:
                temp.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"观察报告生成失败：{exc}", file=sys.stderr)
        return 2
    print(f"{args.output} · {hours:g}h 采样门禁：{'通过' if complete else '未达到'}")
    return int(args.require_hours is not None and not complete)


if __name__ == "__main__":
    sys.exit(main())
