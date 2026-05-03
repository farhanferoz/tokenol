# Cold-start benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `_local/cold_start_bench.py` and use it to capture side-by-side cold-start resource profiles for released `tokenol` v0.3.2 vs unmerged `feature/persistent-history-pr1`, on the developer's full `~/.claude*/projects/**/*.jsonl` corpus.

**Architecture:** Single-file Python 3.13 stdlib-only harness. For each of two runs: wipe DuckDB state if applicable, evict every JSONL from the OS page cache via `posix_fadvise`, spawn `tokenol serve --port 8788` (PR1 via `PYTHONPATH` shim of the worktree), sample `/proc/<pid>/{status,stat}` at 0.5 Hz on the serve PID + descendants, poll `/api/snapshot` for time-to-first-paint, hold 60 s of steady-state, SIGTERM, write CSV + summary. After both runs, write a Markdown report comparing peak RSS, time-to-first-paint, total wall, CPU-seconds.

**Tech Stack:** Python 3.13 stdlib only (`subprocess`, `threading`, `urllib.request`, `os.posix_fadvise`, `argparse`, `json`, `csv`, `signal`, `pathlib`, `statistics`). No `psutil`, no `vmtouch`. Uses installed `/home/ff235/miniconda3/bin/tokenol` for v0.3.2 and the same launcher with `PYTHONPATH=.../persistent-history-pr1/src` for PR1.

**Spec:** `docs/superpowers/specs/2026-05-03-cold-start-bench-design.md` (commit `ff61855`).

---

## File Structure

- **Create:** `_local/cold_start_bench.py` — single-file harness, ~350 LOC. Top-level `main()` reads args, runs the two benchmarks sequentially, writes the combined report.
- **Create (at runtime):** `_local/cold_start_<UTC-timestamp>/{pr1,v0_3_2}/{samples.csv,summary.json,serve.log}` and `_local/cold_start_<UTC-timestamp>/report.md`.
- **No source changes** to `src/tokenol/` — the harness is purely external.

`_local/` is gitignored (verified in `.gitignore` line 1). The harness, all sample CSVs, and the report stay local to the developer machine.

---

## Task 1: Scaffold harness with arg parsing and constants

**Files:**
- Create: `_local/cold_start_bench.py`

- [ ] **Step 1: Create the file with imports, constants, and arg parser**

```python
#!/usr/bin/env python3
"""Cold-start benchmark for tokenol serve.

Compares released v0.3.2 against feature/persistent-history-pr1 on the full
~/.claude*/projects/**/*.jsonl corpus. See
docs/superpowers/specs/2026-05-03-cold-start-bench-design.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path("/home/ff235/dev/claude_rate_limit")
WORKTREE_SRC = REPO_ROOT / ".worktrees/persistent-history-pr1/src"
TOKENOL_BIN = Path("/home/ff235/miniconda3/bin/tokenol")
TOKENOL_DATA_DIR = Path.home() / ".tokenol"
PORT = 8788
SNAPSHOT_URL = f"http://127.0.0.1:{PORT}/api/snapshot?period=24h"
SAMPLE_INTERVAL_S = 2.0  # 0.5 Hz
FIRST_PAINT_POLL_S = 1.0
FIRST_PAINT_MIN_BODY_BYTES = 100
STEADY_HOLD_S = 60.0
RUN_CAP_S = 90 * 60  # 90 min
SHUTDOWN_GRACE_S = 30.0
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Short cap (5 min), 10 s steady, no eviction. Validates harness end-to-end.",
    )
    p.add_argument(
        "--only",
        choices=["pr1", "v0_3_2"],
        help="Run just one version instead of both.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output dir (default: _local/cold_start_<UTC-ts>/).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cap = 5 * 60 if args.smoke else RUN_CAP_S
    steady = 10.0 if args.smoke else STEADY_HOLD_S
    skip_eviction = args.smoke

    out_root = args.out or _default_out_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[bench] output → {out_root}", flush=True)

    versions = ["pr1", "v0_3_2"]
    if args.only:
        versions = [args.only]

    results = {}
    for v in versions:
        results[v] = run_one(
            version=v,
            out_dir=out_root / v,
            cap_s=cap,
            steady_s=steady,
            skip_eviction=skip_eviction,
        )

    if len(results) >= 2:
        write_report(out_root, results)
    print(f"[bench] done. {out_root}/report.md", flush=True)
    return 0


def _default_out_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "_local" / f"cold_start_{ts}"


# --- placeholders filled in by later tasks ---
def run_one(version, out_dir, cap_s, steady_s, skip_eviction):
    raise NotImplementedError


def write_report(out_root, results):
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify it parses and dispatches**

Run: `/home/ff235/miniconda3/bin/python /home/ff235/dev/claude_rate_limit/_local/cold_start_bench.py --smoke --only pr1 --out /tmp/bench-test`
Expected: `[bench] output → /tmp/bench-test` then a `NotImplementedError` traceback. Confirms argparse + dispatch wiring is sound.

- [ ] **Step 3: No commit yet (file is gitignored)**

The whole `_local/` tree is gitignored. We do not commit any of this code. Move on.

---

## Task 2: Page-cache eviction

**Files:**
- Modify: `_local/cold_start_bench.py` — add `discover_jsonls()` and `evict_page_cache()`.

- [ ] **Step 1: Add discovery helper**

Append above `run_one`:

```python
def discover_jsonls() -> list[Path]:
    """Every JSONL under ~/.claude*/projects/. Mirrors tokenol's discovery."""
    home = Path.home()
    roots = sorted(p for p in home.glob(".claude*") if (p / "projects").is_dir())
    files: list[Path] = []
    for r in roots:
        files.extend((r / "projects").rglob("*.jsonl"))
    return files
```

- [ ] **Step 2: Add eviction helper**

Append:

```python
POSIX_FADV_DONTNEED = 4  # from <fcntl.h> on Linux


def evict_page_cache(paths: list[Path]) -> tuple[int, int, int]:
    """Hint the kernel to drop each file from page cache. Returns (ok, fail, bytes)."""
    ok = fail = total_bytes = 0
    for p in paths:
        try:
            fd = os.open(str(p), os.O_RDONLY)
        except OSError:
            fail += 1
            continue
        try:
            st = os.fstat(fd)
            os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)
            total_bytes += st.st_size
            ok += 1
        except OSError:
            fail += 1
        finally:
            os.close(fd)
    return ok, fail, total_bytes
```

- [ ] **Step 3: Smoke-test from a Python REPL**

Run: `/home/ff235/miniconda3/bin/python -c "
import sys; sys.path.insert(0, '/home/ff235/dev/claude_rate_limit/_local')
from cold_start_bench import discover_jsonls, evict_page_cache
files = discover_jsonls()
print('files:', len(files))
ok, fail, b = evict_page_cache(files[:5])
print(f'evict sample: ok={ok} fail={fail} bytes={b}')
"`
Expected: `files: 1802` (or current count) and `evict sample: ok=5 fail=0 bytes=<positive>`.

---

## Task 3: Process sampler thread

**Files:**
- Modify: `_local/cold_start_bench.py` — add `Sample` dataclass and `ProcessSampler` thread class.

- [ ] **Step 1: Add Sample + sampler**

Append:

```python
@dataclass
class Sample:
    t_rel_s: float
    ts_iso: str
    vmrss_kb: int
    rss_anon_kb: int
    rss_file_kb: int
    cpu_pct: float
    threads: int
    fds: int


def _read_proc_status(pid: int) -> dict[str, int]:
    out: dict[str, int] = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith(("VmRSS:", "RssAnon:", "RssFile:", "Threads:")):
                k, v = line.split(":", 1)
                out[k] = int(v.strip().split()[0])
    return out


def _read_proc_jiffies(pid: int) -> int:
    """utime + stime in ticks."""
    with open(f"/proc/{pid}/stat") as f:
        # field 14 = utime, 15 = stime; comm field may contain spaces in parens
        raw = f.read()
    rparen = raw.rindex(")")
    fields = raw[rparen + 2 :].split()
    return int(fields[11]) + int(fields[12])


def _children(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as f:
                    out.extend(int(c) for c in f.read().split())
            except OSError:
                continue
    except OSError:
        return []
    return out


def _all_pids(root_pid: int) -> list[int]:
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        for c in _children(stack.pop()):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return list(seen)


def _fd_count(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0


class ProcessSampler(threading.Thread):
    def __init__(self, root_pid: int, interval_s: float):
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.interval = interval_s
        self.samples: list[Sample] = []
        self.alive = True
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._prev_jiffies: dict[int, int] = {}
        self._prev_t = self._t0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                os.kill(self.root_pid, 0)
            except ProcessLookupError:
                self.alive = False
                return
            pids = _all_pids(self.root_pid)
            now = time.monotonic()
            dt = max(now - self._prev_t, 1e-9)
            vmrss = anon = rssf = threads = fds = 0
            jiffies_total = 0
            cur_jiffies: dict[int, int] = {}
            for pid in pids:
                try:
                    s = _read_proc_status(pid)
                    vmrss += s.get("VmRSS:", 0)
                    anon += s.get("RssAnon:", 0)
                    rssf += s.get("RssFile:", 0)
                    threads += s.get("Threads:", 0)
                    fds += _fd_count(pid)
                    j = _read_proc_jiffies(pid)
                    cur_jiffies[pid] = j
                    jiffies_total += j
                except (FileNotFoundError, ProcessLookupError, OSError):
                    continue
            prev_total = sum(self._prev_jiffies.get(p, cur_jiffies.get(p, 0)) for p in cur_jiffies)
            cpu_pct = 100.0 * (jiffies_total - prev_total) / CLK_TCK / dt
            self._prev_jiffies = cur_jiffies
            self._prev_t = now
            self.samples.append(
                Sample(
                    t_rel_s=now - self._t0,
                    ts_iso=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    vmrss_kb=vmrss,
                    rss_anon_kb=anon,
                    rss_file_kb=rssf,
                    cpu_pct=cpu_pct,
                    threads=threads,
                    fds=fds,
                )
            )
            self._stop.wait(self.interval)
```

- [ ] **Step 2: Smoke-test sampler against the harness Python interpreter itself**

Run: `/home/ff235/miniconda3/bin/python -c "
import sys, time, os
sys.path.insert(0, '/home/ff235/dev/claude_rate_limit/_local')
from cold_start_bench import ProcessSampler
s = ProcessSampler(os.getpid(), interval_s=0.5)
s.start()
time.sleep(2.5)
s.stop(); s.join(timeout=2)
print('samples:', len(s.samples))
print('first:', s.samples[0])
print('last :', s.samples[-1])
"`
Expected: 4-5 samples; non-zero `vmrss_kb`; `cpu_pct` near 0 since the parent is sleeping.

---

## Task 4: Single-run orchestration

**Files:**
- Modify: `_local/cold_start_bench.py` — replace placeholder `run_one()` with the real implementation.

- [ ] **Step 1: Add command builder + state-wipe helper**

Append (above the placeholder `run_one`):

```python
def _build_serve_env_and_cmd(version: str) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    cmd = [str(TOKENOL_BIN), "serve", "--port", str(PORT)]
    if version == "pr1":
        env["PYTHONPATH"] = (
            f"{WORKTREE_SRC}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
        )
    return cmd, env


def _wipe_pr1_state() -> Path | None:
    """Move ~/.tokenol/history.duckdb* aside. Returns backup path, or None if nothing to wipe."""
    if not TOKENOL_DATA_DIR.exists():
        return None
    targets = list(TOKENOL_DATA_DIR.glob("history.duckdb*"))
    if not targets:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = TOKENOL_DATA_DIR / f".bench-backup-{ts}"
    backup.mkdir()
    for t in targets:
        shutil.move(str(t), str(backup / t.name))
    return backup


def _resolve_tokenol_version(env: dict[str, str]) -> str:
    """Ask the chosen interpreter what tokenol.__version__ resolves to."""
    proc = subprocess.run(
        [
            "/home/ff235/miniconda3/bin/python",
            "-c",
            "import tokenol; print(tokenol.__version__); print(tokenol.__file__)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()
```

- [ ] **Step 2: Replace placeholder `run_one` with the orchestrator**

Replace the placeholder with:

```python
def run_one(
    version: str,
    out_dir: Path,
    cap_s: float,
    steady_s: float,
    skip_eviction: bool,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "serve.log"
    samples_path = out_dir / "samples.csv"
    summary_path = out_dir / "summary.json"

    cmd, env = _build_serve_env_and_cmd(version)
    resolved = _resolve_tokenol_version(env)

    backup_dir = _wipe_pr1_state() if version == "pr1" else None

    jsonls = discover_jsonls()
    if skip_eviction:
        evict_ok = evict_fail = evict_bytes = 0
        print(f"[{version}] eviction skipped (smoke)", flush=True)
    else:
        print(f"[{version}] evicting {len(jsonls)} JSONL files from page cache…", flush=True)
        evict_ok, evict_fail, evict_bytes = evict_page_cache(jsonls)
        print(f"[{version}] evict ok={evict_ok} fail={evict_fail} bytes={evict_bytes:,}", flush=True)

    print(f"[{version}] tokenol resolves to:\n{resolved}", flush=True)
    print(f"[{version}] spawning: {' '.join(cmd)}", flush=True)
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    sampler = ProcessSampler(proc.pid, SAMPLE_INTERVAL_S)
    sampler.start()
    t_start = time.monotonic()

    first_paint_s: float | None = None
    capped = False
    crashed = False
    try:
        while True:
            elapsed = time.monotonic() - t_start
            if elapsed > cap_s:
                capped = True
                break
            if proc.poll() is not None:
                crashed = True
                break
            if first_paint_s is None and _try_first_paint():
                first_paint_s = time.monotonic() - t_start
                print(f"[{version}] first paint at {first_paint_s:.1f}s", flush=True)
                # steady-state hold
                steady_end = time.monotonic() + steady_s
                while time.monotonic() < steady_end:
                    if proc.poll() is not None:
                        crashed = True
                        break
                    time.sleep(0.5)
                break
            time.sleep(FIRST_PAINT_POLL_S)
    finally:
        sampler.stop()
        sampler.join(timeout=5)
        shutdown_s = _shutdown(proc)
        log_fh.close()
        if backup_dir:
            print(f"[{version}] PR1 state moved aside → {backup_dir}", flush=True)

    total_wall_s = time.monotonic() - t_start
    summary = _summarize(
        version=version,
        resolved=resolved,
        samples=sampler.samples,
        jsonl_count=len(jsonls),
        evict_ok=evict_ok,
        evict_fail=evict_fail,
        evict_bytes=evict_bytes,
        first_paint_s=first_paint_s,
        total_wall_s=total_wall_s,
        capped=capped,
        crashed=crashed,
        shutdown_s=shutdown_s,
    )
    _write_samples_csv(samples_path, sampler.samples)
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"[{version}] summary → {summary_path}", flush=True)
    return summary


def _try_first_paint() -> bool:
    try:
        with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
            if r.status != 200:
                return False
            body = r.read()
            return len(body) > FIRST_PAINT_MIN_BODY_BYTES
    except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError):
        return False


def _shutdown(proc: subprocess.Popen) -> float:
    """SIGTERM the process group, fallback to SIGKILL after grace. Returns wall time."""
    t0 = time.monotonic()
    if proc.poll() is not None:
        return 0.0
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return time.monotonic() - t0
    try:
        proc.wait(timeout=SHUTDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)
    return time.monotonic() - t0
```

- [ ] **Step 3: Add `_write_samples_csv` and `_summarize`**

Append:

```python
def _write_samples_csv(path: Path, samples: list[Sample]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["t_rel_s", "ts_iso", "vmrss_kb", "rss_anon_kb", "rss_file_kb",
             "cpu_pct", "threads", "fds"]
        )
        for s in samples:
            w.writerow([f"{s.t_rel_s:.3f}", s.ts_iso, s.vmrss_kb, s.rss_anon_kb,
                        s.rss_file_kb, f"{s.cpu_pct:.2f}", s.threads, s.fds])


def _summarize(
    *,
    version: str,
    resolved: str,
    samples: list[Sample],
    jsonl_count: int,
    evict_ok: int,
    evict_fail: int,
    evict_bytes: int,
    first_paint_s: float | None,
    total_wall_s: float,
    capped: bool,
    crashed: bool,
    shutdown_s: float,
) -> dict:
    rss = [s.vmrss_kb for s in samples]
    cpu = [s.cpu_pct for s in samples]
    peak_vmrss_kb = max(rss) if rss else 0
    p95_vmrss_kb = int(statistics.quantiles(rss, n=20)[18]) if len(rss) >= 20 else (max(rss) if rss else 0)
    mean_cpu = round(sum(cpu) / len(cpu), 2) if cpu else 0.0
    # cpu-seconds = integrate cpu_pct over time; simple sum * dt approximation
    cpu_seconds = round(sum(c * SAMPLE_INTERVAL_S / 100.0 for c in cpu), 2)
    return {
        "version": version,
        "resolved": resolved.splitlines(),
        "jsonl_files": jsonl_count,
        "jsonl_evict_ok": evict_ok,
        "jsonl_evict_fail": evict_fail,
        "jsonl_total_bytes": evict_bytes,
        "samples": len(samples),
        "peak_vmrss_kb": peak_vmrss_kb,
        "p95_vmrss_kb": p95_vmrss_kb,
        "mean_cpu_pct": mean_cpu,
        "cpu_seconds": cpu_seconds,
        "time_to_first_paint_s": round(first_paint_s, 2) if first_paint_s is not None else None,
        "total_wall_s": round(total_wall_s, 2),
        "shutdown_s": round(shutdown_s, 2),
        "capped": capped,
        "crashed": crashed,
    }
```

- [ ] **Step 4: Smoke-test the full single-run flow**

Run (foreground; expect ~5 minutes):
`/home/ff235/miniconda3/bin/python /home/ff235/dev/claude_rate_limit/_local/cold_start_bench.py --smoke --only v0_3_2`

Expected: a `_local/cold_start_<ts>/v0_3_2/` directory containing `samples.csv` (≥10 rows), `summary.json` (`samples > 0`, `peak_vmrss_kb > 0`, `crashed: false`), and `serve.log` (uvicorn startup banner). `time_to_first_paint_s` may be non-null even on smoke (v0.3.2 paints fast); `capped: true` is fine if it didn't paint within 5 min.

---

## Task 5: Combined Markdown report

**Files:**
- Modify: `_local/cold_start_bench.py` — replace placeholder `write_report`.

- [ ] **Step 1: Implement the report writer**

Replace the placeholder with:

```python
def write_report(out_root: Path, results: dict[str, dict]) -> None:
    pr1 = results.get("pr1")
    rel = results.get("v0_3_2")
    rows = [
        ("Resolved", _fmt_resolved(pr1), _fmt_resolved(rel)),
        ("JSONL files", str(pr1["jsonl_files"]), str(rel["jsonl_files"])),
        ("Evicted bytes", f"{pr1['jsonl_total_bytes']:,}", f"{rel['jsonl_total_bytes']:,}"),
        ("Time to first paint (s)", _fmt_opt(pr1["time_to_first_paint_s"]),
         _fmt_opt(rel["time_to_first_paint_s"])),
        ("Total wall (s)", f"{pr1['total_wall_s']:.1f}", f"{rel['total_wall_s']:.1f}"),
        ("Peak VmRSS (MiB)", _kb_to_mib(pr1["peak_vmrss_kb"]),
         _kb_to_mib(rel["peak_vmrss_kb"])),
        ("p95 VmRSS (MiB)", _kb_to_mib(pr1["p95_vmrss_kb"]),
         _kb_to_mib(rel["p95_vmrss_kb"])),
        ("Mean CPU%", f"{pr1['mean_cpu_pct']:.0f}", f"{rel['mean_cpu_pct']:.0f}"),
        ("CPU-seconds", f"{pr1['cpu_seconds']:.0f}", f"{rel['cpu_seconds']:.0f}"),
        ("Capped at run cap", str(pr1["capped"]), str(rel["capped"])),
        ("Crashed", str(pr1["crashed"]), str(rel["crashed"])),
        ("Shutdown (s)", f"{pr1['shutdown_s']:.1f}", f"{rel['shutdown_s']:.1f}"),
    ]
    md = ["# tokenol cold-start: PR1 vs v0.3.2", ""]
    md.append(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}._")
    md.append("")
    md.append("| Metric | PR1 (`feature/persistent-history-pr1`) | Released v0.3.2 |")
    md.append("|---|---|---|")
    for name, a, b in rows:
        md.append(f"| {name} | {a} | {b} |")
    md.append("")
    md.append("## Interpretation")
    md.append("")
    md.append(_interpretation(pr1, rel))
    (out_root / "report.md").write_text("\n".join(md))


def _fmt_resolved(s: dict) -> str:
    return s["resolved"][1] if s["resolved"] else "?"


def _fmt_opt(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def _kb_to_mib(kb: int) -> str:
    return f"{kb / 1024:.0f}"


def _interpretation(pr1: dict, rel: dict) -> str:
    parts = []
    if pr1["time_to_first_paint_s"] and rel["time_to_first_paint_s"]:
        ratio = pr1["time_to_first_paint_s"] / rel["time_to_first_paint_s"]
        parts.append(
            f"PR1 takes **{ratio:.1f}×** as long as v0.3.2 to first paint "
            f"({pr1['time_to_first_paint_s']:.0f}s vs {rel['time_to_first_paint_s']:.0f}s)."
        )
    elif pr1["capped"]:
        parts.append(
            f"PR1 did not first-paint within the {RUN_CAP_S/60:.0f}-min cap; "
            "v0.3.2 first-painted in "
            f"{rel['time_to_first_paint_s']:.0f}s." if rel["time_to_first_paint_s"]
            else "Both runs failed to first-paint within the cap."
        )
    if pr1["peak_vmrss_kb"] and rel["peak_vmrss_kb"]:
        ratio = pr1["peak_vmrss_kb"] / rel["peak_vmrss_kb"]
        parts.append(
            f"Peak RSS: PR1 {pr1['peak_vmrss_kb']/1024:.0f} MiB vs "
            f"v0.3.2 {rel['peak_vmrss_kb']/1024:.0f} MiB ({ratio:.1f}×)."
        )
    parts.append(
        "Numbers approximate normal first-runs (page cache evicted via posix_fadvise; "
        "kernel slab/inode caches were not dropped — true bare-metal numbers would be "
        "somewhat higher)."
    )
    return " ".join(parts)
```

- [ ] **Step 2: Verify report writer with synthetic input**

Run: `/home/ff235/miniconda3/bin/python -c "
import sys, json, pathlib
sys.path.insert(0, '/home/ff235/dev/claude_rate_limit/_local')
from cold_start_bench import write_report
fake = lambda v, paint: {'version': v, 'resolved': ['0.3.2', '/p/'+v], 'jsonl_files': 1802,
    'jsonl_total_bytes': 2_000_000_000, 'samples': 100, 'peak_vmrss_kb': 1_500_000 if v=='pr1' else 500_000,
    'p95_vmrss_kb': 1_400_000 if v=='pr1' else 480_000, 'mean_cpu_pct': 80, 'cpu_seconds': 600,
    'time_to_first_paint_s': paint, 'total_wall_s': paint+60, 'shutdown_s': 0.5,
    'capped': False, 'crashed': False}
out = pathlib.Path('/tmp/bench-report-test'); out.mkdir(exist_ok=True)
write_report(out, {'pr1': fake('pr1', 600), 'v0_3_2': fake('v0_3_2', 30)})
print((out / 'report.md').read_text())
"`
Expected: a Markdown table prints to stdout with both columns populated and an interpretation paragraph saying "PR1 takes 20.0× as long as v0.3.2".

---

## Task 6: End-to-end smoke (both versions)

**Files:** none.

- [ ] **Step 1: Run smoke for both versions back-to-back**

Run: `/home/ff235/miniconda3/bin/python /home/ff235/dev/claude_rate_limit/_local/cold_start_bench.py --smoke`

Expected: ~10-min wall total. Output dir contains `pr1/`, `v0_3_2/`, and `report.md`. Confirm `report.md` has both columns populated. Confirm both `serve.log`s contain a uvicorn startup banner with no Python tracebacks. Confirm both `summary.json`s have `crashed: false`.

If anything fails, fix before Task 7.

---

## Task 7: Real run

**Files:** none.

- [ ] **Step 1: Backup + sanity check**

Confirm port 8788 is free: `ss -lntp 2>/dev/null | grep 8788 && echo "PORT BUSY — abort"`. Expected: empty output.

Confirm no `tokenol serve` is already running: `pgrep -af "tokenol serve"`. Expected: empty.

Disk headroom check (PR1 backfill writes to `~/.tokenol/`): `df -h ~ | tail -1` — expect at least 5 GiB free.

- [ ] **Step 2: Launch the real run in the background**

Run (background; multi-hour expected):
```bash
cd /home/ff235/dev/claude_rate_limit
nohup /home/ff235/miniconda3/bin/python _local/cold_start_bench.py \
    > _local/cold_start_run.log 2>&1 &
echo $! > _local/cold_start_run.pid
```

Use the Bash tool's `run_in_background=true` so the harness keeps running even if the foreground assistant pauses.

- [ ] **Step 3: Monitor**

Periodically (every 5-10 min during PR1 run, every 1-2 min during v0.3.2 run):
```bash
tail -20 /home/ff235/dev/claude_rate_limit/_local/cold_start_run.log
ps -p $(cat /home/ff235/dev/claude_rate_limit/_local/cold_start_run.pid) -o pid,etime,pcpu,rss
```

Look for `[pr1] first paint at <N>s` then `[pr1] summary →` then the same for `v0_3_2`. Final line should be `[bench] done. <out>/report.md`.

If PR1 hits the 90-min cap before first paint, the harness records `capped: true` and proceeds to v0.3.2. That outcome is itself a valid result — capture it in the report.

- [ ] **Step 4: Inspect the report**

```bash
cat /home/ff235/dev/claude_rate_limit/_local/cold_start_*/report.md
```

Verify both columns are populated, then surface the headline numbers (peak RSS, time-to-first-paint, ratio) to the user.

---

## Task 8: Update RESUME.md

**Files:**
- Modify: `RESUME.md` (gitignored — local only).

- [ ] **Step 1: Add a short "Recent shipped work" entry under the existing list**

Open `RESUME.md` and insert under "Recent shipped work":

```markdown
- **Cold-start benchmark** (2026-05-03) — `_local/cold_start_bench.py` characterizes
  PR1 vs released v0.3.2 first-run cost on the full ~/.claude*/projects corpus
  (1802 files / 1.9 GB at run time). Spec: `docs/superpowers/specs/2026-05-03-cold-start-bench-design.md`.
  Latest report: `_local/cold_start_<UTC-ts>/report.md`. Headline: <fill in: PR1 peak RSS, v0.3.2 peak RSS, time-to-first-paint ratio>.
```

Replace `<fill in: ...>` with actual numbers from the report before saving. Do not commit (RESUME.md is gitignored).

---

## Self-Review

**Spec coverage:**
- [x] Goal 1 (side-by-side metrics) — Tasks 4, 5
- [x] Goal 2 (full corpus) — Task 2 `discover_jsonls`
- [x] Goal 3 (`posix_fadvise` cold-disk approx) — Task 2
- [x] Goal 4 (Markdown report) — Task 5
- [x] Output layout `pr1/` + `v0_3_2/` + `report.md` — Tasks 4, 5
- [x] Run order PR1 → v0.3.2 — Task 1 `versions = ["pr1", "v0_3_2"]`
- [x] Sampling at 0.5 Hz — Task 1 `SAMPLE_INTERVAL_S = 2.0`
- [x] First-paint heuristic (200 + body > 100 B) — Task 4 `_try_first_paint`
- [x] Hard cap 90 min — Task 1 `RUN_CAP_S`
- [x] SIGTERM + 30 s grace + SIGKILL — Task 4 `_shutdown`
- [x] Backup `~/.tokenol/history.duckdb*` not delete — Task 4 `_wipe_pr1_state`
- [x] Crash detection — Task 4 (`proc.poll()` + sampler thread)
- [x] `--smoke` mode — Task 1 args, Task 4 honors `skip_eviction` / shorter cap

**Placeholder scan:** No TBD/TODO/handle-edge-cases left. Task 8's `<fill in: ...>` is the only intentional template, called out as "replace with actual numbers".

**Type consistency:** `Sample` dataclass fields used identically across `_write_samples_csv`, `_summarize`, and the sampler. `summary.json` schema written by `_summarize` is the same one consumed by `write_report` (verified by name in Task 5's smoke harness). `cmd, env` tuple from `_build_serve_env_and_cmd` consumed in `run_one`.

No issues; ready to execute.
