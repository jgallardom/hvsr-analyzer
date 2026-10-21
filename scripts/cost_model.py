"""Monthly operating-cost model for 100, 1,000, and 10,000 new measurements per month.

    uv run python scripts/cost_model.py          # writes docs/costs.md

Inputs: measured per-measurement sizes and compute (docs/evidence/stage9/performance.json), official
prices copied on a stated date (docs/evidence/stage9/prices.json), the deployment in render.yaml, and
the workload assumptions declared in WORKLOAD below. Record count alone does not determine cost:
recording length and rate, retention, page views, and downloads matter, so every assumption is
printed next to the result.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/stage9"
OUT = ROOT / "docs/costs.md"
GB = 1e9
TB = 1e12

WORKLOAD = {
    "monthly_new_measurements": [100, 1_000, 10_000],
    "size_profiles": {
        "medium": "1 h at 100 Hz (typical HVSR survey point)",
        "large": "2 h at 200 Hz",
        "eight_hours": "8 h at 50 Hz (largest the render.yaml limits admit)",
    },
    "analyses_per_measurement": 1,
    "retention": "everything kept; nothing deleted",
    "class_a_writes_per_measurement": 60,
    "page_views_per_stored_measurement_month": 10,
    "bucket_reads_per_page_view": 10,
    "response_bytes_per_page_view": 200_000,
    "downloads_per_month_fraction_of_stored": 1.0,
    "health_checks_per_month": 30 * 24 * 3600 // 5,
    "postgres_empty_bytes": 10_000_000,
    "hosted_cpu_slowdown": 2.0,
    "workspace": "hobby",
}


def _gb_month_billed(gb: float, free: float) -> int:
    return max(0, math.ceil(gb - free)) if gb > free else 0


def _millions_billed(n: float, free: float) -> int:
    return math.ceil((n - free) / 1e6) if n > free else 0


def postgres_disk_gb(db_bytes: float) -> int:
    need = db_bytes / GB / 0.9
    return 1 if need <= 1 else 5 * math.ceil(need / 5)


def footprint(case: dict) -> dict:
    runs = case["runs"]
    artifacts = sum(r["artifact_bytes"] for r in runs.values())
    stored = case["submission"]["stored_bytes"] + artifacts
    return {
        "original_bytes": case["submission"]["stored_bytes"],
        "artifact_bytes": artifacts,
        "stored_bytes": stored,
        "db_bytes": case["database_growth_bytes"],
        "cold_cpu_s": case["cold"]["process_cpu_s"],
        "submit_cpu_s": case["submission"]["cpu_s"],
    }


def month(prices: dict, plan: dict, fp: dict, m: int, k: int) -> dict:
    """Costs in month `k` (1-based) with `m` new measurements per month, all retained."""
    w, r, r2 = WORKLOAD, prices["render"], prices["cloudflare_r2"]
    stored_n = k * m
    stored = stored_n * fp["stored_bytes"]
    db_plan = plan["postgres_plan"]
    disk = postgres_disk_gb(w["postgres_empty_bytes"] + stored_n * fp["db_bytes"])
    views = stored_n * w["page_views_per_stored_measurement_month"]
    outbound = m * fp["stored_bytes"] + views * w["response_bytes_per_page_view"]
    included_bw = r["bandwidth_included_gb"][w["workspace"]]
    reads = views * w["bucket_reads_per_page_view"] + w["health_checks_per_month"]
    writes = m * w["class_a_writes_per_measurement"]
    out = {
        "render_workspace": r["workspace_month"][w["workspace"]],
        "render_web": r["service_month"][plan["web_plan"]],
        "render_postgres": r["postgres_month"][db_plan]
        + max(0, disk - r["postgres_storage_included_gb"]) * r["postgres_storage_gb_month"],
        "render_bandwidth": max(0.0, outbound / GB - included_bw) * r["bandwidth_gb"],
        "r2_storage": _gb_month_billed(stored / GB, r2["free_storage_gb_month"]) * r2["storage_gb_month"],
        "r2_requests": _millions_billed(writes, r2["free_class_a"]) * r2["class_a_per_million"]
        + _millions_billed(reads, r2["free_class_b"]) * r2["class_b_per_million"],
    }
    out["total"] = sum(out.values())
    out.update(
        stored_bytes=stored, postgres_disk_gb=disk, outbound_bytes=outbound, reads=reads, writes=writes
    )
    return out


def storage_alternatives(prices: dict, stored: float) -> dict:
    downloads = WORKLOAD["downloads_per_month_fraction_of_stored"] * stored
    b2, hz = prices["backblaze_b2"], prices["hetzner_object_storage_eur"]
    b2_storage = max(0.0, stored - b2["free_storage_gb"] * GB) / TB * b2["storage_tb_month"]
    b2_egress = (
        max(0.0, downloads - b2["free_egress_multiple_of_stored"] * stored) / GB * b2["egress_gb_beyond_free"]
    )
    hz_storage = max(0.0, stored / TB - hz["included_storage_tb"]) * hz["storage_tb_hour"] * 730
    hz_egress = max(0.0, downloads / TB - hz["included_egress_tb"]) * hz["egress_tb"]
    return {"b2_usd": b2_storage + b2_egress, "hetzner_eur": hz["base_month"] + hz_storage + hz_egress}


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _size(b: float) -> str:
    return f"{b / TB:,.2f} TB" if b >= TB else f"{b / GB:,.1f} GB" if b >= GB else f"{b / 1e6:,.1f} MB"


def render(perf: dict, prices: dict, blueprint: dict) -> str:
    [web] = [s for s in blueprint["services"] if s["type"] == "web"]
    db = blueprint["databases"][0]
    cases = {c["case"]: c for c in perf["cases"]}
    fps = {name: footprint(cases[name]) for name in WORKLOAD["size_profiles"]}
    w = WORKLOAD
    lines = [
        "# Monthly operating costs",
        "",
        f"Generated by `scripts/cost_model.py` from measured sizes (`docs/evidence/stage9/performance.json`, "
        f"commit `{(perf['git_commit'] or 'unknown')[:12]}`) and official prices copied on {prices['retrieved_utc']} "
        "(`docs/evidence/stage9/prices.json`, sources listed at the end). This is an estimate, not a quote: "
        "prices change, taxes are excluded, and the workload below is an assumption to replace with real usage.",
        "",
        "## Deployment priced",
        "",
        f"The topology in `render.yaml`: one Render web service on plan `{web['plan']}` running the job worker "
        f"in-process, one Render PostgreSQL database on plan `{db['plan']}`, and an S3-compatible bucket "
        "(Cloudflare R2 Standard) holding every original and artifact. Render's free plans are not used: a "
        "free web service spins down and loses local files, and a free database expires after 30 days. "
        f"The workspace is on the `{w['workspace']}` plan (one person; the Pro plan adds "
        f"{_usd(prices['render']['workspace_month']['pro'])} per month for teams).",
        "",
        "## Workload assumptions",
        "",
        f"- New measurements per month: {', '.join(f'{m:,}' for m in w['monthly_new_measurements'])}; "
        f"{w['analyses_per_measurement']} default analysis each (runs A and B); {w['retention']}.",
        "- Recording size profiles, measured on synthetic recordings submitted as gzip CSV:",
    ]
    lines += [f"  - `{name}`: {desc}" for name, desc in w["size_profiles"].items()]
    lines += [
        f"- Object writes (R2 Class A): {w['class_a_writes_per_measurement']} per measurement. A real "
        "submission and its default analysis wrote 39 objects locally (identical bytes are stored once); "
        "S3 staging adds copies, so this is rounded up.",
        f"- Page views: {w['page_views_per_stored_measurement_month']} per stored measurement per month, each "
        f"causing {w['bucket_reads_per_page_view']} bucket reads (R2 Class B) and "
        f"{w['response_bytes_per_page_view'] / 1e3:.0f} kB of HTML/JSON from Render. Figures and downloads are "
        "redirected to presigned bucket URLs, so their bytes leave the bucket, not Render.",
        f"- Downloads: {w['downloads_per_month_fraction_of_stored']:g} times everything stored, every month "
        "(deliberately high; only matters for B2 and Hetzner, R2 egress is free).",
        f"- Health checks: one `/readyz` per 5 s ({w['health_checks_per_month']:,} bucket reads per month).",
        "- Render outbound bandwidth: every stored byte is uploaded once from Render to the bucket "
        "(uploads to storage outside Render are billed as outbound traffic), plus page responses.",
        "- PostgreSQL disk: measured database growth per measurement plus 10 MB, kept under 90% full on a 1 GB "
        "or multiple-of-5 GB disk.",
        "",
        "## Per-measurement footprint (measured)",
        "",
        "| Profile | Original stored | Artifacts (runs A+B) | Database rows | Cold analysis CPU | Upload parse CPU |",
        "|---|---|---|---|---|---|",
    ]
    for name, fp in fps.items():
        lines.append(
            f"| `{name}` | {_size(fp['original_bytes'])} | {_size(fp['artifact_bytes'])} | "
            f"{fp['db_bytes'] / 1e3:.0f} kB | {fp['cold_cpu_s']:.1f} s | {fp['submit_cpu_s']:.1f} s |"
        )
    plan = {"web_plan": web["plan"], "postgres_plan": db["plan"]}
    lines += [
        "",
        f"CPU times are from the declared development machine (`{perf['hardware']['cpu_model']}`); "
        f"the capacity check below assumes hosted CPUs are {w['hosted_cpu_slowdown']:g} times slower.",
        "",
        "## Monthly totals (USD, R2 storage)",
        "",
        "Month 1 is the first month of operation; month 12 carries eleven earlier months of retained data.",
        "",
        "| New / month | Profile | Month 1 | Month 12 | Stored at month 12 | Month-12 breakdown |",
        "|---|---|---|---|---|---|",
    ]
    for m in w["monthly_new_measurements"]:
        for name, fp in fps.items():
            m1, m12 = month(prices, plan, fp, m, 1), month(prices, plan, fp, m, 12)
            parts = (
                f"Render web {_usd(m12['render_web'])}, Postgres {_usd(m12['render_postgres'])} "
                f"({m12['postgres_disk_gb']} GB disk), bandwidth {_usd(m12['render_bandwidth'])}, "
                f"R2 storage {_usd(m12['r2_storage'])}, R2 requests {_usd(m12['r2_requests'])}"
            )
            lines.append(
                f"| {m:,} | `{name}` | {_usd(m1['total'])} | {_usd(m12['total'])} | "
                f"{_size(m12['stored_bytes'])} | {parts} |"
            )
    lines += [
        "",
        "## Storage provider comparison (month 12, storage and egress only)",
        "",
        "Same stored bytes and download assumption. B2 charges no API requests for classes A to C; Hetzner "
        "charges none at all and bills objects under 64 kB as 64 kB (negligible here). Hetzner prices are in "
        "EUR excluding VAT and are not converted.",
        "",
        "| New / month | Profile | Stored | R2 (storage + requests) | Backblaze B2 | Hetzner Object Storage |",
        "|---|---|---|---|---|---|",
    ]
    for m in w["monthly_new_measurements"]:
        for name, fp in fps.items():
            m12 = month(prices, plan, fp, m, 12)
            alt = storage_alternatives(prices, m12["stored_bytes"])
            lines.append(
                f"| {m:,} | `{name}` | {_size(m12['stored_bytes'])} | {_usd(m12['r2_storage'] + m12['r2_requests'])} | "
                f"{_usd(alt['b2_usd'])} | EUR {alt['hetzner_eur']:,.2f} |"
            )
    worst = max(fps.values(), key=lambda f: f["cold_cpu_s"] + f["submit_cpu_s"])
    lines += [
        "",
        "R2 is the default because egress is free and it needs no regional pairing; B2 stores more cheaply "
        "and is a good off-site backup target; Hetzner suits a large EU archive with modest downloads. All "
        "three speak the S3 API (`HVSR_OBJECT_STORE=s3` with the provider's endpoint).",
        "",
        "## Capacity check",
        "",
    ]
    for m in w["monthly_new_measurements"]:
        hours = m * (worst["cold_cpu_s"] + worst["submit_cpu_s"]) * w["hosted_cpu_slowdown"] / 3600
        lines.append(
            f"- {m:,} measurements per month of the largest profile need about {hours:,.1f} CPU hours "
            f"({hours / 720:.1%} of one instance-month) at one job at a time."
        )
    lines += [
        "- Bursts queue: jobs run one at a time (`HVSR_MAX_CONCURRENT_JOBS=1`), uploads are parsed one at a "
        "time per process, and further uploads wait up to `HVSR_UPLOAD_WAIT_S` and then get 503 busy.",
        "- The 10,000-per-month rows keep the `0.1c-256mb` database; its 256 MB of RAM was not load-tested. "
        f"Moving to `0.5c-1g` adds {_usd(prices['render']['postgres_month']['0.5c-1g'] - prices['render']['postgres_month'][db['plan']])} per month.",
        "",
        "## Not included",
        "",
        "- Taxes, a custom domain, monitoring, and the Render Pro workspace for teams.",
        "- An off-site copy of the bucket and database backups (recommended, see `docs/backup-restore.md`): "
        "at B2 prices about the B2 column again.",
        "- Reanalyses and cosmetic figure variants: each extra analysis adds the artifact bytes again; figure "
        "variants are capped per run (`HVSR_MAX_PLOT_VARIANTS_PER_RUN`).",
        "- Render's point-in-time recovery for paid databases is part of the database plan; its window depends "
        "on the workspace plan (see Render's documentation). It was not exercised here.",
        "",
        "## Sources",
        "",
    ]
    lines += [f"- {name}: {url}" for name, url in prices["sources"].items()]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    perf = json.loads((EVIDENCE / "performance.json").read_text())
    prices = json.loads((EVIDENCE / "prices.json").read_text())
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    OUT.write_text(render(perf, prices, blueprint))
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
