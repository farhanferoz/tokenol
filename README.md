# Claude Code Usage & Efficiency Analysis Tools

This directory contains a suite of Python scripts designed to monitor, analyze, and optimize Claude Code usage, costs, and cache efficiency across multiple isolated environments.

## 🚀 Core Analysis Scripts

### 1. `claude_cost_efficiency.py` (The Main Dashboard)
The primary tool for monitoring daily budget and project performance.
- **Daily Report:** `python3 claude_cost_efficiency.py [days]`
  - Shows consolidated daily totals and a project-by-project breakdown.
  - **Metrics:** Work (Output), Context Reads, Cost, Cost/1kW (Work), Context Ratio, Cache Efficiency, and Hit Rate.
- **Session Report:** `python3 claude_cost_efficiency.py session [days]`
  - Drills down into individual conversation IDs to identify specific "expensive" chat threads.

### 2. `hourly_comprehensive_report.py` (Hourly Diagnostics)
Pinpoints the exact hour of rate limit depletion or usage spikes across all projects.
- **Usage:** `python3 hourly_comprehensive_report.py`
- **Output:** Hourly table including Cost, Work, Cache Efficiency Ratio, and Hit Rate.
- **Note:** Currently configured to analyze the most recent heavy usage day (e.g., April 17th).

### 3. `cache_hit_analysis.py` (Technical Health)
A specialized diagnostic tool for verifying technical cache performance.
- **Usage:** `python3 cache_hit_analysis.py [days]`
- **Best For:** Detecting "Cache Bashing" or verifying if native binary updates are causing systematic cache misses.

### 4. `comprehensive_report.py` (Quick 14-Day View)
A simplified, wide-table version of the daily dashboard.
- **Usage:** `python3 comprehensive_report.py [days]`

---

## 📈 Key Metrics Explained

| Metric | Definition | Goal |
| :--- | :--- | :--- |
| **Cost/1kW** | Total USD cost per 1,000 output tokens (actual work produced). | **<$0.20** |
| **Ctx Ratio** | Number of context tokens read per 1 output token. | **Lower is better** |
| **Cache Eff** | Ratio of tokens read from cache vs. tokens created (e.g., 50:1). | **>50:1** |
| **Hit Rate** | Percentage of context served from cache. | **>98%** |

---

## 🛠️ Internal Logic & Maintenance

- **Data Source:** These scripts parse the `.jsonl` session logs located in `~/.claude-*/projects/**/`.
- **Timezone:** All hourly reports are converted from UTC to **UK Time (BST/UTC+1)** for user alignment.
- **Pricing:** Costs are calculated based on standard Claude 3.5/4.5 pricing tiers defined in `hourly_comprehensive_report.py`.

## ⚠️ Known Issue: The "Dual Session" Conflict
Running two concurrent sessions on the same project (e.g., two terminal windows in `StratSense`) triggers immediate **cache invalidation**. Each session will force a "cold start" read on every turn, driving the **Cache Efficiency** below 20:1 and rapidly depleting weekly rate limits. Always work sequentially on large projects.

---
*Created: 2026-04-18*
