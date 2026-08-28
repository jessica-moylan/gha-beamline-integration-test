#!/usr/bin/env python3
"""Classify a beamline-profile startup run: which startup file failed, the *real*
exception (the chained one, not ophyd's lazy-init KeyError), and whether the
failure looks like the profile's, the beamline tools package's, the Python
environment's, or the CI harness/services' fault.

Stdlib only — runs on the setup-python interpreter, not the profile's pixi env.

Usage:
    classify_startup.py LOGFILE [--tla hxn] [--exit-code N] [--startup-dir PATH]
                        [--summary FILE] [--json FILE]

Reads the captured output of the ipython startup loop (the `Executing … in CI`
/ `ERROR in …:` markers), writes a Markdown report (to --summary, else
$GITHUB_STEP_SUMMARY, else stdout) and GitHub `::error`/`::notice`
annotations. Always exits 0 — it explains a failure, it never causes one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict

TS_PREFIX = re.compile(r"^(?:\S+\t)*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EXECUTING = re.compile(r"^Executing (\S+) in CI\s*$")
ERROR_IN = re.compile(r"^ERROR in (\S+):\s*$")
FILE_OK = re.compile(r"^STARTUP_FILE_OK (\S+) ([0-9.]+)\s*$")
# Standard tracebacks:  File "/path/x.py", line 12, in func
# IPython verbose ones: File ~/path/x.py:12, in func(...)
FRAME = re.compile(r'^\s*File (?:"([^"]+)", line (\d+)|(\S+?):(\d+)), in (.+)$')
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Timeout)[\w.]*)(?::\s*(.*))?$")
CHAIN = re.compile(r"^(During handling of the above exception, another exception occurred|"
                   r"The above exception was the direct cause of the following exception):")
EXIT_CODE = re.compile(r"ipython profile startup test failed with exit code (\d+)")
SEGFAULT = re.compile(r"Detected segfault \(exit 139\)|Segmentation fault")

ENV_PKGS = ("ophyd", "epics", "pyepics", "ophyd_async", "bluesky", "tiled", "nslsii",
            "databroker", "redis", "caproto", "p4p", "IPython", "traitlets", "pydantic",
            "httpx", "numpy", "pandas", "matplotlib", "PyQt5", "qt")
SERVICE_HINTS = (
    (re.compile(r"tiled\.nsls2\.bnl\.gov|api\.nsls2\.bnl\.gov|localhost:8000|127\.0\.0\.1:8000"), "Tiled / mock API (harness service)"),
    (re.compile(r"redis|:6379|:6380"), "Redis (harness service)"),
    (re.compile(r"mongo|:27017"), "MongoDB (harness service)"),
    (re.compile(r"kafka|:9092"), "Kafka (harness service)"),
    (re.compile(r"Control layer pyepics failed to send connection|Unexpected channel ID|"
                r"failed to connect|EPICS_CA_ADDR_LIST|ca\.ChannelAccessException"), "Channel Access / blackhole IOC under load (harness)"),
    (re.compile(r"NotConnectedError|pva://"), "PVA / IOC connection (harness IOC stand-ins)"),
    (re.compile(r"Name or service not known|Temporary failure in name resolution"), "DNS hijack missing an /etc/hosts entry (harness)"),
    (re.compile(r"No such file or directory: '/nsls2/|FileNotFoundError: .*'/nsls2/"), "missing /nsls2 directory (harness prepare-environment)"),
)


@dataclass
class StartupFile:
    path: str
    status: str = "ok"          # ok | failed | not-run
    seconds: float | None = None
    traceback: list[str] = field(default_factory=list)


@dataclass
class Finding:
    failed_file: str | None
    exception: str | None       # final (real) exception line
    first_exception: str | None  # what the log shows first (often a decoy)
    chained: bool
    innermost_frame: str | None
    first_profile_frame: str | None
    category: str
    reason: str
    hints: list[str]


def clean(line: str) -> str:
    return ANSI.sub("", TS_PREFIX.sub("", line.rstrip("\n")))


def parse(lines: list[str]) -> tuple[list[StartupFile], int | None, bool]:
    files: dict[str, StartupFile] = {}
    order: list[str] = []
    current: StartupFile | None = None
    collecting = False
    exit_code: int | None = None
    segfault = False
    for raw in lines:
        line = clean(raw)
        m = EXECUTING.match(line)
        if m:
            current = files.setdefault(m.group(1), StartupFile(m.group(1)))
            if m.group(1) not in order:
                order.append(m.group(1))
            collecting = False
            continue
        m = FILE_OK.match(line)
        if m and m.group(1) in files:
            files[m.group(1)].seconds = float(m.group(2))
            continue
        m = ERROR_IN.match(line)
        if m:
            current = files.setdefault(m.group(1), StartupFile(m.group(1)))
            current.status = "failed"
            collecting = True
            continue
        m = EXIT_CODE.search(line)
        if m:
            exit_code = int(m.group(1))
        if SEGFAULT.search(line):
            segfault = True
        if collecting and current is not None:
            if line.startswith("##[") or line.startswith("=== "):
                collecting = False
            else:
                current.traceback.append(line)
    return [files[p] for p in order], exit_code, segfault


def analyse(files: list[StartupFile], exit_code: int | None, segfault: bool,
            tla: str, startup_dir: str | None) -> Finding:
    failed = next((f for f in files if f.status == "failed"), None)
    tools_pkg = f"{tla}tools" if tla else None
    if failed is None:
        if segfault or exit_code == 139:
            return Finding(None, "Segmentation fault (exit 139) after every startup file executed", None,
                           False, None, None, "environment",
                           "all startup files ran without error; the crash is at interpreter exit "
                           "(typically pyepics finalize_libca/context_destroy racing ophyd threads) — "
                           "not a profile bug", ["end the ipython command with os._exit(0) on success"])
        if exit_code not in (None, 0):
            return Finding(None, f"non-zero exit {exit_code} with no failed startup file", None, False,
                           None, None, "harness", "the startup loop reported no ERROR; look at the step "
                           "itself (shell, pixi shell-hook, Xvfb) and the services logs", [])
        return Finding(None, None, None, False, None, None, "ok", "every startup file executed", [])

    tb = failed.traceback
    # Split into chained segments; the LAST segment holds the real exception.
    segments: list[list[str]] = [[]]
    for line in tb:
        if CHAIN.match(line):
            segments.append([])
        else:
            segments[-1].append(line)
    chained = len(segments) > 1
    first_exc = next((l for l in segments[0] if EXC_LINE.match(l) and not FRAME.match(l)), None)
    last = segments[-1]
    real_exc = None
    for l in reversed(last):
        if EXC_LINE.match(l) and not l.startswith(" "):
            real_exc = l
            break
    frames = [l for l in last if FRAME.match(l)]
    innermost = frames[-1] if frames else None
    prof_frames = [l for l in (l for seg in segments for l in seg if FRAME.match(l))
                   if "/startup/" in l or (startup_dir and startup_dir in l)]
    first_profile = prof_frames[-1] if prof_frames else None

    hints = []
    text = "\n".join(tb)
    for rx, hint in SERVICE_HINTS:
        if rx.search(text):
            hints.append(hint)
    if first_exc and real_exc and first_exc != real_exc and "KeyError" in first_exc \
            and "ophyd/device.py" in "\n".join(segments[0]):
        hints.insert(0, "the leading KeyError is ophyd's lazy-init path (Component.__get__) — a decoy; read the chained exception")

    path = frame_path(innermost) if innermost else ""
    cat, reason = "unknown", "no traceback frames found"
    if any(h.endswith("(harness)") or "harness" in h for h in hints):
        cat, reason = "harness", "the exception names a harness service or the CA layer: " + "; ".join(hints)
    elif "/startup/" in path or (startup_dir and startup_dir in path):
        cat, reason = "profile", "innermost frame is in the profile's startup/"
    elif tools_pkg and f"/{tools_pkg}/" in path:
        cat, reason = "tools", f"innermost frame is in the beamline tools package {tools_pkg}"
    elif "site-packages" in path:
        pkg = path.split("site-packages/", 1)[1].split("/", 1)[0]
        if pkg in ENV_PKGS:
            cat, reason = "environment", f"innermost frame is in {pkg} (installed package), no service named"
        else:
            cat, reason = "tools", f"innermost frame is in installed package {pkg}"
    elif ".github/" in path or "spoof_" in path:
        cat, reason = "harness", "innermost frame is in the harness's own scripts"
    elif "<ipython-input" in path:
        cat, reason = "harness", "innermost frame is the harness's ipython command"
    return Finding(failed.path, real_exc, first_exc, chained, innermost, first_profile, cat, reason, hints)


def frame_path(frame_line: str) -> str:
    m = FRAME.match(frame_line)
    return (m.group(1) or m.group(3) or "") if m else ""


def short(path: str) -> str:
    return path.split("/startup/", 1)[1] if "/startup/" in path else os.path.basename(path)


def render(files: list[StartupFile], finding: Finding, tla: str, exit_code: int | None) -> str:
    out = [f"## Profile startup — {tla or 'beamline'}", ""]
    badge = {"ok": "✅ OK", "profile": "🟥 PROFILE", "tools": "🟧 TOOLS PACKAGE",
             "environment": "🟨 ENVIRONMENT", "harness": "🟦 HARNESS / SERVICES", "unknown": "⬜ UNKNOWN"}[finding.category]
    out += [f"**Verdict: {badge}** — {finding.reason}", ""]
    if finding.exception:
        out += [f"**Real exception:** `{finding.exception}`"]
        if finding.chained and finding.first_exception:
            out += [f"(log shows first: `{finding.first_exception}` — chained, not the cause)"]
        out += [""]
    if finding.innermost_frame:
        out += [f"**Innermost frame:** `{finding.innermost_frame.strip()}`"]
    if finding.first_profile_frame:
        out += [f"**Nearest profile frame:** `{finding.first_profile_frame.strip()}`"]
    if finding.hints:
        out += ["", "**Hints:**"] + [f"- {h}" for h in finding.hints]
    if exit_code is not None:
        out += ["", f"Exit code: `{exit_code}`"]
    out += ["", "| # | startup file | result | seconds |", "|---|---|---|---|"]
    for i, f in enumerate(files, 1):
        mark = {"ok": "✅", "failed": "❌", "not-run": "⏭️"}[f.status]
        secs = "" if f.seconds is None else f"{f.seconds:.1f}"
        out.append(f"| {i} | `{short(f.path)}` | {mark} {f.status} | {secs} |")
    if finding.failed_file:
        idx = next(i for i, f in enumerate(files) if f.path == finding.failed_file)
        skipped = len(files) - idx - 1
        out += ["", f"{idx} file(s) loaded before the failure; {skipped} listed after it did not run."]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile")
    ap.add_argument("--tla", default=os.environ.get("BEAMLINE_ACRONYM", ""))
    ap.add_argument("--exit-code", type=int, default=None)
    ap.add_argument("--startup-dir", default=None)
    ap.add_argument("--summary", default=None, help="write Markdown here (default: $GITHUB_STEP_SUMMARY or stdout)")
    ap.add_argument("--json", default=None, help="also write the finding as JSON")
    args = ap.parse_args()

    with open(args.logfile, errors="replace") as fh:
        lines = fh.readlines()
    files, exit_code, segfault = parse(lines)
    if args.exit_code is not None:
        exit_code = args.exit_code
    finding = analyse(files, exit_code, segfault, args.tla.lower(), args.startup_dir)
    md = render(files, finding, args.tla, exit_code)

    target = args.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as fh:
            fh.write(md)
    print(md)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"files": [asdict(f) for f in files], "finding": asdict(finding),
                       "exit_code": exit_code}, fh, indent=2)
    if finding.category != "ok" and os.environ.get("GITHUB_ACTIONS"):
        where = f"file={short(finding.failed_file)}," if finding.failed_file else ""
        print(f"::error {where}title={finding.category.upper()} failure ({args.tla})::{finding.exception or finding.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
