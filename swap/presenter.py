"""Text presentation helpers rendering swap domain reports."""

from collections.abc import Mapping, Sequence
import sys
from typing import TextIO

from swap.domain import (
    PAGE,
    CgroupSwapRow,
    ConfigSectionReport,
    DebugSectionReport,
    SettingsReport,
    SwapApplyResult,
    SwapDeviceRow,
    SwapPlanReport,
    SwapStatusReport,
    ZswapSummary,
    human,
    pct,
)


def _section(title: str, out: TextIO) -> None:
    print(f"\n== {title}", file=out)


def render_config_report(report: ConfigSectionReport, out: TextIO = sys.stdout) -> None:
    """Render managed file and runtime swap/zram states."""
    if not report.etc_visible:
        print(
            f"  The host's /etc is not visible here ({report.etc_dir}): run '{report.prog} {report.verb}' on the host.",
            file=out,
        )
        return
    _section(f"files (in {report.etc_dir})", out)
    for item in report.file_items:
        print(item.format_line(), file=out)
    _section("runtime", out)
    for item in report.runtime_items:
        print(item.format_line(), file=out)


def render_settings(report: SettingsReport, out: TextIO = sys.stdout, *, header: bool = True) -> bool:
    """Render live settings comparison; return True if any setting differs."""
    if header:
        _section("live settings vs desired (from swap/ in CST)", out)
    for row in report.rows:
        extra = "" if row.state == "ok" or not row.want else f"   (desired {row.want})"
        print(f"  {row.state:<5} {row.label:<28} {row.live}{extra}", file=out)
    if report.has_exp:
        print(
            f"  exp: the cpu experiment {report.exp_name} is on and sets {report.exp_key}; apply leaves it alone."
            f" Back to the desired value: `cst cpu experiment off {report.exp_name}`, then apply",
            file=out,
        )
    return report.has_diff


def render_pool(params: Mapping[str, str], s: ZswapSummary, out: TextIO = sys.stdout) -> None:
    """Render zswap compressed pool metrics."""
    _section("zswap pool", out)
    print(
        "  parameters: " + ", ".join(f"{k}={v}" for k, v in params.items()) if params else "  zswap: not available",
        file=out,
    )
    if s["pool"] is None:
        print("  no Zswap line in /proc/meminfo", file=out)
        return
    print(f"  pool in RAM: {human(s['pool'])} of a {human(s['limit'])} limit ({pct(s['pool_of_limit'])})", file=out)
    if s["accept_below"] is not None:
        print(
            f"  after the pool was full, zswap takes new pages again below {human(s['accept_below'])}"
            " (accept_threshold_percent of the limit)",
            file=out,
        )
    ratio = "-" if s["ratio"] is None else f"{s['ratio']:.2f} : 1"
    print(f"  holds {human(s['stored'])} of memory compressed into {human(s['pool'])}: ratio {ratio}", file=out)


def render_traffic(s: ZswapSummary, out: TextIO = sys.stdout) -> None:
    """Render zswap and disk swap traffic counters since boot."""

    def fmt_pages(n: int) -> str:
        return human(n * PAGE)

    _section("swap traffic since boot (/proc/vmstat; pages of 4 KiB shown as sizes)", out)
    print(f"  into the pool    zswpout {fmt_pages(s['zswpout']):>10}", file=out)
    print(
        f"  back from pool   zswpin  {fmt_pages(s['zswpin']):>10}   {pct(s['loaded_back'])} of what went in",
        file=out,
    )
    print(
        f"  pool -> disk     zswpwb  {fmt_pages(s['zswpwb']):>10}"
        f"   {pct(s['written_back'])} of what went in was written back",
        file=out,
    )
    if s["bypassed"]:
        print(
            f"  written to disk  pswpout {fmt_pages(s['pswpout']):>10}   {fmt_pages(s['bypassed'])} of it"
            " bypassed zswap (rejected, or written while zswap was off)",
            file=out,
        )
    else:
        print(
            f"  written to disk  pswpout {fmt_pages(s['pswpout']):>10}   = zswpwb: no page bypassed zswap",
            file=out,
        )
    print(
        f"  read from disk   pswpin  {fmt_pages(s['pswpin']):>10}"
        f"   {pct(s['read_back'])} of what was written to disk was read back",
        file=out,
    )
    print("  How to read: a high 'written back' share is fine while 'read back' stays low (the disk", file=out)
    print("  holds pages nobody needs). A growing 'read back' share means pages come back from the", file=out)
    print("  NVMe instead of RAM: see swap/README.md, 'Reading the status'.", file=out)


def render_debug(report: DebugSectionReport, out: TextIO = sys.stdout) -> None:
    """Render zswap debugfs counters (or root hint when inaccessible)."""
    _section(f"zswap debug counters ({report.debug_dir})", out)
    if report.status == "permission_denied":
        print(f"  needs root{(': ' + report.hint) if report.hint else ''}", file=out)
        return
    if report.status == "unavailable":
        print("  not available (debugfs not mounted, or zswap without debug counters)", file=out)
        return
    for row in report.rows:
        print(f"  {row.name:<28} {row.value:<24} {row.help_text}".rstrip(), file=out)


def render_devices(devices: Sequence[SwapDeviceRow], out: TextIO = sys.stdout) -> None:
    """Render active swap devices from /proc/swaps."""
    _section("swap devices (/proc/swaps)", out)
    if not devices:
        print("  none active", file=out)
        return
    for entry in devices:
        print(
            f"  {entry.label:<24} {entry.swap_type:<10} used {human(entry.used_bytes):>10}"
            f" of {human(entry.size_bytes):>10}  priority {entry.priority}",
            file=out,
        )


def render_cgroups(cgroups: Sequence[CgroupSwapRow], out: TextIO = sys.stdout) -> None:
    """Render per-cgroup swap/zswap/memory.low/PSI metrics."""

    def fmt_pct_int(frac: float | None) -> str:
        return "-" if frac is None else f"{frac * 100:.0f}"

    _section("per cgroup (memory.stat; counters since the cgroup was created)", out)
    head = (
        f"  {'cgroup':<32} {'swap':>9} {'zswapped':>9} {'zswap':>9} {'->pool':>9} {'->disk':>9}"
        f" {'disk in':>9} {'wb%':>6} {'rb%':>6} {'zswap.wb':>8} {'low':>8} {'psi60':>6}"
    )
    print(head, file=out)
    for item in cgroups:
        row = item.row
        print(
            f"  {item.label:<32} {human(row['swap']):>9} {human(row['zswapped']):>9} {human(row['zswap']):>9}"
            f" {human(row['zswpout']):>9} {human(row['zswpwb']):>9} {human(row['pswpin']):>9}"
            f" {fmt_pct_int(row['written_back']):>6} {fmt_pct_int(row['read_back']):>6}"
            f" {item.writeback:>8} {item.low_display:>8} {item.psi60:>6}",
            file=out,
        )
    print("  swap = memory.swap.current (pool + disk); zswapped/zswap = held in the pool, uncompressed/", file=out)
    print("  compressed; ->pool = zswpout, ->disk = zswpwb, disk in = pswpin; wb% = zswpwb/zswpout;", file=out)
    print("  rb% = pswpin/pswpout (read back from disk); zswap.wb = memory.zswap.writeback (1 = the", file=out)
    print("  pool may write this cgroup's pages to disk); psi60 = memory pressure 'some', avg 60 s.", file=out)


def render_psi(lines: Sequence[str], out: TextIO = sys.stdout) -> None:
    """Render system memory pressure stall information."""
    _section("memory pressure (/proc/pressure/memory: % of time tasks waited for memory)", out)
    for line in lines:
        print(f"  {line}", file=out)


def render_plan(report: SwapPlanReport, out: TextIO = sys.stdout) -> int:
    """Render dry-run plan output and return 0."""
    print("Dry run: nothing is changed. ok = nothing to do; adopt = apply only renews a comment;", file=out)
    print("dedupe = apply drops a second copy of the same entry; exp = set by a cpu experiment, left alone;", file=out)
    print("write/update/mask/remove/start/restart/swapoff/DIFF = apply would change that;", file=out)
    print("later = needs a reboot; ? = could not be read; REFUSE/CONFLICT = apply stops.", file=out)
    render_config_report(report.config, out)
    render_settings(report.settings, out)
    print(file=out)
    print(f"Apply with: {report.prog} apply", file=out)
    return 0


def render_status(report: SwapStatusReport, out: TextIO = sys.stdout) -> int:
    """Render read-only status report and return 0."""
    if report.config is not None:
        render_config_report(report.config, out)
    render_settings(report.settings, out)
    if report.settings_only:
        return 0
    if report.summary is not None:
        render_pool(report.params, report.summary, out)
        render_traffic(report.summary, out)
    if report.debug is not None:
        render_debug(report.debug, out)
    render_devices(report.devices, out)
    render_cgroups(report.cgroups, out)
    render_psi(report.psi_lines, out)
    return 0


def render_apply(result: SwapApplyResult, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    """Render swap apply output/warnings/errors and return exit code."""
    for ev in result.events:
        print(ev.text, file=out if ev.stream == "out" else err)
    if result.settings is not None:
        _section("verification", out)
        render_settings(result.settings, out, header=False)
        for note in result.verify_notes:
            print(note, file=out)
        if result.swapon_out:
            out.write(result.swapon_out if result.swapon_out.endswith("\n") else result.swapon_out + "\n")
    if result.verify_error:
        print(result.verify_error, file=err)
    if result.returncode == 0:
        print(f"Done. Details: {result.prog} status", file=out)
    return result.returncode
