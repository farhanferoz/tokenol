# Cold-start benchmark — design

A reproducible local harness that measures `tokenol serve`'s first-run resource cost on the developer's full JSONL corpus, comparing released v0.3.2 against the unmerged `feature/persistent-history-pr1` branch. The goal is to characterize what a new user actually feels when they run `tokenol serve` for the first time, and to quantify the regression PR1 introduces by adding the DuckDB backfill.

## Goals

1. Produce side-by-side cold-start metrics (peak RSS, time-to-first-paint, total wall, CPU-seconds) for v0.3.2 and PR1 on the same corpus.
2. Run on the user's existing `~/.claude*/projects/**/*.jsonl` corpus (1802 files, ~1.9 GB at the time of writing) — full scale, no subsampling.
3. Approximate "cold disk" without root: evict each JSONL from the OS page cache via `posix_fadvise(POSIX_FADV_DONTNEED)` before each run.
4. Produce a single Markdown report committed alongside CSV samples for later inspection.

## Non-goals

- True bare-metal cold boot. We do not drop kernel slab/inode caches or reboot. Numbers approximate "warm-system first run", not "fresh OS install".
- Profiling individual Python frames. This is a black-box resource measurement, not a flamegraph. If PR1 is unacceptably slow we follow up with a separate `py-spy` pass.
- Long-term tracking, CI integration, or regression gating. The harness is dev-local; results are read once and discarded.
- Subsampled / synthetic corpora. We measure what *this* developer's corpus does, since the worst-case observation came from this same machine.
- Generalizing across machines. Results characterize one machine and one corpus; they are not a published benchmark.

## Architecture

A single Python script `_local/cold_start_bench.py` (gitignored — `_local/` is in `.gitignore`). The script orchestrates two sequential runs and a final report; no daemon, no persistent state of its own.

```
   ┌─────────────────────────────────────────┐
   │ cold_start_bench.py (entry point)       │
   └─────────────────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
   pre-run         per-run         post-run
   - wipe          - spawn serve   - SIGTERM
     ~/.tokenol    - sampler       - flush CSV
   - posix_fadvise   thread        - write
     evict every     (0.5 Hz)        summary.json
     JSONL         - poll
                    /api/snapshot
                    until 200
                    (time-to-
                    first-paint)
                  - 60s steady-
                    state sample
```

**Run order:** PR1 first, v0.3.2 second. PR1 is the heavier case (DuckDB backfill from cold); running it first prevents v0.3.2 from getting an unfair OS-page-cache advantage from the prior run. Between runs the JSONL eviction step runs again to re-cool the cache.

**Process model:**
- v0.3.2 invocation: `/home/ff235/miniconda3/bin/tokenol serve --port 8788`
- PR1 invocation: same launcher, with `PYTHONPATH=/home/ff235/dev/claude_rate_limit/.worktrees/persistent-history-pr1/src` prepended so `from tokenol.cli import app` resolves to the worktree before the installed wheel. Verified: with the shim in place, `tokenol.__file__` resolves to the worktree's `__init__.py`. This avoids disturbing the installed v0.3.2 wheel — no re-install / re-cleanup needed between runs. Neither package ships `--no-open`; the script just relies on the default (no browser), so we omit it.

**Sampling:** the harness reads `/proc/<pid>/status` (`VmRSS`, `RssAnon`, `RssFile`, `Threads`) and `/proc/<pid>/stat` (`utime + stime`) for the serve PID and any children, at 0.5 Hz. CPU% per sample is `(Δ(utime+stime) ticks / clk_tck) / Δ wall`. fd count from `len(os.listdir(f'/proc/{pid}/fd'))`. No `psutil` dependency — `/proc` reads are stdlib-only.

**Stop criteria:**
- Time-to-first-paint: poll `GET http://127.0.0.1:8788/api/snapshot?period=24h` every 1 s; record wall when the first 200 with non-empty body returns.
- Steady-state window: continue sampling for 60 s after first paint.
- Hard cap: 90 minutes per run. PR1 first-run on this corpus may be multi-hour; if the cap fires the harness records `capped: true` in the summary and proceeds.
- Shutdown: SIGTERM, then SIGKILL after 30 s grace.

## Output layout

```
_local/cold_start_<timestamp>/
  pr1/
    samples.csv         # ts_iso, t_rel_s, vmrss_kb, rssanon_kb, rssfile_kb, cpu_pct, threads, fds
    summary.json        # version, git_sha, jsonl_files, jsonl_bytes, peak_vmrss_kb, p95_vmrss_kb,
                        # time_to_first_paint_s, total_wall_s, cpu_seconds, capped, shutdown_s
    serve.log           # combined stdout+stderr from tokenol serve
  v0_3_2/
    samples.csv
    summary.json
    serve.log
  report.md             # side-by-side table + 1-paragraph interpretation
```

## Data flow

For each run:

1. **Pre-run:** record `git rev-parse HEAD` (or `pip show tokenol` for the released case) into the run's metadata. Wipe `~/.tokenol/history.duckdb` and `~/.tokenol/history.duckdb.tmp/` if they exist (PR1 only — v0.3.2 ignores `~/.tokenol/`). Walk every `~/.claude*/projects/**/*.jsonl` and call `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` on each.
2. **Spawn:** `subprocess.Popen` the serve command with `stdout=subprocess.PIPE, stderr=subprocess.STDOUT`, redirected to `serve.log`. Capture child PID.
3. **Sample loop (background thread):** every 0.5 s, read `/proc/<pid>/{status,stat}` and any child PIDs (via `/proc/<pid>/task/*/children`). Append a row to `samples.csv`. Continue until the main thread signals stop.
4. **First-paint poll (main thread):** every 1 s, `urllib.request.urlopen` the snapshot URL with a 5 s timeout. On the first 200 with body length > 100 B, record `time_to_first_paint_s` and switch to steady-state mode.
5. **Steady-state hold:** sleep 60 s, then trigger shutdown.
6. **Shutdown:** send SIGTERM, wait up to 30 s for exit, escalate to SIGKILL if needed. Record `shutdown_s` (wall between SIGTERM and exit).
7. **Summarize:** compute `peak_vmrss_kb = max(samples.vmrss)`, `p95_vmrss_kb` (stdlib `statistics.quantiles` or sorted-index — no numpy), `cpu_seconds = sum(Δutime+Δstime) / clk_tck`. Write `summary.json`.

After both runs complete, write `report.md` with a Markdown table comparing the two `summary.json` files and a short interpretation (which run won on each metric, by how much, and what the absolute numbers feel like).

## Error handling

- **Port 8788 already bound:** abort with a clear message. The harness does not pick alternate ports; the user can `lsof -i :8788` and kill the offender.
- **Serve crashes during run:** sampler thread detects exit via `os.kill(pid, 0)` raising `ProcessLookupError`; harness records `crashed: true` in summary, dumps `serve.log` tail to stderr, and proceeds to the next run.
- **First-paint timeout:** if 90-min cap fires before any 200, `time_to_first_paint_s = null` and `capped: true`. Steady-state sampling is skipped.
- **JSONL eviction failure on individual files:** log and continue. A few files failing to evict (e.g., transient permission errors) does not invalidate the run.
- **Existing `~/.tokenol/` data:** the harness moves `history.duckdb*` aside to `~/.tokenol/.bench-backup-<timestamp>/` rather than deleting outright, so an interrupted run does not silently destroy data.

## Testing

This harness is itself a one-shot tool, not production code. We do not write unit tests for it. Validation: a `--smoke` flag shortens the per-run cap to 5 minutes, the steady-state hold to 10 s, and the eviction step to a no-op. Smoke runs against the real corpus and is sufficient to confirm the spawn / sample / first-paint poll / SIGTERM loop works end-to-end before committing to the multi-hour real run.

## Pitfalls

- **`posix_fadvise` is advisory.** The kernel may keep pages resident if memory pressure is low. We accept this; it matches what most real first-runs encounter.
- **PR1 worktree imports may differ from the editable install.** The `PYTHONPATH` shim assumes `src/tokenol/` is importable as a top-level package — verified true since the worktree was a working editable install seconds ago.
- **CPU% from `/proc/<pid>/stat` is per-process, not per-core normalized.** A 4-core saturating process reports 400% CPU. Interpret accordingly in the report.
- **Children parsing:** tokenol serve is single-process today, but uvicorn workers or future subprocesses would inflate RSS if not summed. The sampler walks children to be safe.
- **Run order asymmetry:** even with eviction between runs, the kernel page cache may retain some warmth from PR1 into v0.3.2. On this 30 GiB machine with ~13 GiB in `buff/cache` at quiescence, evicting the 1.9 GB JSONL corpus should drain it cleanly, but verify the "before second run" numbers in `serve.log` look cold (parse times not implausibly fast). If they do look fast, re-run v0.3.2 standalone after a fresh eviction.
