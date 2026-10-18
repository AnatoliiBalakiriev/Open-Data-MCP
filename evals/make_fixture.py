"""Generate the SYNTHETIC fixtures and evals/questions.jsonl.

Everything here is invented test data, not Ukrainian government data. Expected answers are
computed with plain Python (csv/statistics), deliberately independent of DuckDB, so the
evaluation compares two independent implementations.

Run:  uv run python evals/make_fixture.py
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "src" / "open_data_mcp" / "fixtures"

DISTRICTS = ["Центральний", "Північний", "Південний", "Східний", "Західний"]
CATEGORIES = ["Roads", "Lighting", "Waste", "Water", "Parks"]
REGIONS = ["Region A", "Region B", "Region C", "Region D", "Region E", "Region F"]
YEARS = [2021, 2022, 2023, 2024]

DS_REQ = "syn-service-requests-2024"
RES_REQ = "syn-res-requests-csv"
DS_BUD = "syn-regional-budget"
RES_BUD = "syn-res-budget-datastore"
DS_FILES = "syn-file-variants"


def make_requests() -> list[dict[str, str]]:
    rng = random.Random(20241018)
    rows = []
    start = date(2024, 1, 1)
    for i in range(1, 121):
        received = start + timedelta(days=rng.randint(0, 365))
        status = rng.choices(["closed", "open", "rejected"], weights=[70, 20, 10])[0]
        days = str(rng.randint(1, 60)) if status == "closed" else ""
        rows.append(
            {
                "request_id": str(i),
                "district": rng.choice(DISTRICTS),
                "category": rng.choice(CATEGORIES[:4] if i % 7 else CATEGORIES),
                "status": status,
                "received_date": received.isoformat(),
                "resolution_days": days,
                "cost_uah": f"{rng.randint(500, 90000) / 100:.2f}",
            }
        )
    # Guarantee a category with zero rows for the "no data" edge case.
    for row in rows:
        if row["category"] == "Parks":
            row["category"] = "Water"
    return rows


def make_budget() -> list[dict[str, int | str]]:
    rng = random.Random(7)
    out = []
    for region in REGIONS:
        for year in YEARS:
            planned = rng.randint(100, 900) * 1_000_000
            executed = int(planned * rng.uniform(0.8, 1.05))
            out.append(
                {"region": region, "year": year, "planned_uah": planned, "executed_uah": executed}
            )
    return out


def case(**kw):
    return kw


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    reqs = make_requests()
    with (FIXTURES / "service_requests.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(reqs[0]))
        writer.writeheader()
        writer.writerows(reqs)
    budget = make_budget()
    (FIXTURES / "budget_records.json").write_text(
        json.dumps(budget, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---- expected values, plain Python -------------------------------------------------
    n = len(reqs)
    by_district = Counter(r["district"] for r in reqs)
    by_cat = Counter(r["category"] for r in reqs)
    by_status = Counter(r["status"] for r in reqs)
    total_cost = round(sum(float(r["cost_uah"]) for r in reqs), 2)
    roads_cost = round(sum(float(r["cost_uah"]) for r in reqs if r["category"] == "Roads"), 2)
    closed_days = [int(r["resolution_days"]) for r in reqs if r["status"] == "closed"]
    avg_days = round(sum(closed_days) / len(closed_days), 2)
    top_district = sorted(by_district.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    top3 = sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    mar = sum(1 for r in reqs if r["received_date"].startswith("2024-03"))
    q2 = sum(1 for r in reqs if "2024-04-01" <= r["received_date"] <= "2024-06-30")
    earliest = min(r["received_date"] for r in reqs)
    latest = max(r["received_date"] for r in reqs)
    slow = sum(1 for r in reqs if r["status"] == "closed" and int(r["resolution_days"]) > 30)
    centr_lighting = sum(
        1 for r in reqs if r["district"] == "Центральний" and r["category"] == "Lighting"
    )
    assert by_cat["Parks"] == 0, "fixture must contain no Parks rows"

    planned_2024 = sum(b["planned_uah"] for b in budget if b["year"] == 2024)
    exec_2023: dict[str, int] = defaultdict(int)
    for b in budget:
        if b["year"] == 2023:
            exec_2023[str(b["region"])] += int(b["executed_uah"])
    top_region = sorted(exec_2023.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    p22 = sum(b["planned_uah"] for b in budget if b["year"] == 2022)
    e22 = sum(b["executed_uah"] for b in budget if b["year"] == 2022)
    rate22 = round(e22 / p22, 4)

    R = {"dataset_id": DS_REQ, "resource_id": RES_REQ}
    B = {"dataset_id": DS_BUD, "resource_id": RES_BUD}

    def scalar(v, tol=0.0):
        return {"type": "scalar", "value": v, "tolerance": tol}

    def abstain(*codes):
        return {"type": "abstain", "error_codes": list(codes)}

    cases = [
        case(
            id="count-total",
            category="count",
            question="How many service requests are in the dataset?",
            **R,
            sql="SELECT count(*) FROM data",
            expect=scalar(n),
        ),
        case(
            id="count-district",
            category="count",
            question="Скільки звернень у Центральному районі?",
            **R,
            sql="SELECT count(*) FROM data WHERE district = 'Центральний'",
            expect=scalar(by_district["Центральний"]),
        ),
        case(
            id="count-open",
            category="count",
            question="How many requests are still open?",
            **R,
            sql="SELECT count(*) FROM data WHERE status = 'open'",
            expect=scalar(by_status["open"]),
        ),
        case(
            id="count-distinct-cat",
            category="count",
            question="How many distinct categories appear?",
            **R,
            sql="SELECT count(DISTINCT category) FROM data",
            expect=scalar(len(by_cat)),
        ),
        case(
            id="sum-total-cost",
            category="sum",
            question="What is the total cost in UAH of all requests?",
            **R,
            sql="SELECT round(sum(cost_uah), 2) FROM data",
            expect=scalar(total_cost, 0.01),
        ),
        case(
            id="sum-roads-cost",
            category="sum",
            question="Total cost of Roads requests?",
            **R,
            sql="SELECT round(sum(cost_uah), 2) FROM data WHERE category = 'Roads'",
            expect=scalar(roads_cost, 0.01),
        ),
        case(
            id="avg-resolution",
            category="avg",
            question="Average resolution time in days for closed requests?",
            **R,
            sql="SELECT round(avg(resolution_days), 2) FROM data WHERE status = 'closed'",
            expect=scalar(avg_days, 0.01),
        ),
        case(
            id="group-top-district",
            category="group",
            question="Which district has the most requests?",
            **R,
            sql="SELECT district FROM data GROUP BY district ORDER BY count(*) DESC, district LIMIT 1",
            expect=scalar(top_district[0]),
        ),
        case(
            id="group-top3-cat",
            category="group",
            question="Top 3 categories by number of requests (with counts)?",
            **R,
            sql="SELECT category, count(*) AS n FROM data GROUP BY category ORDER BY n DESC, category LIMIT 3",
            expect={"type": "rows", "rows": [[c, k] for c, k in top3]},
        ),
        case(
            id="filter-two",
            category="filter",
            question="How many Lighting requests came from the Центральний district?",
            **R,
            sql="SELECT count(*) FROM data WHERE district = 'Центральний' AND category = 'Lighting'",
            expect=scalar(centr_lighting),
        ),
        case(
            id="date-march",
            category="date",
            question="How many requests were received in March 2024?",
            **R,
            sql="SELECT count(*) FROM data WHERE received_date >= DATE '2024-03-01' AND received_date < DATE '2024-04-01'",
            expect=scalar(mar),
        ),
        case(
            id="date-q2",
            category="date",
            question="Requests received in Q2 2024?",
            **R,
            sql="SELECT count(*) FROM data WHERE received_date BETWEEN DATE '2024-04-01' AND DATE '2024-06-30'",
            expect=scalar(q2),
        ),
        case(
            id="date-earliest",
            category="date",
            question="What is the earliest received date?",
            **R,
            sql="SELECT min(received_date) FROM data",
            expect=scalar(earliest),
        ),
        case(
            id="date-latest",
            category="date",
            question="What is the latest received date?",
            **R,
            sql="SELECT max(received_date) FROM data",
            expect=scalar(latest),
        ),
        case(
            id="filter-slow",
            category="filter",
            question="How many closed requests took more than 30 days?",
            **R,
            sql="SELECT count(*) FROM data WHERE status = 'closed' AND resolution_days > 30",
            expect=scalar(slow),
        ),
        case(
            id="ds-planned-2024",
            category="sum",
            question="Total planned budget (UAH) for 2024 across regions?",
            **B,
            sql="SELECT sum(planned_uah) FROM data WHERE year = 2024",
            expect=scalar(planned_2024),
        ),
        case(
            id="ds-top-region-2023",
            category="group",
            question="Which region executed the most in 2023?",
            **B,
            sql="SELECT region FROM data WHERE year = 2023 GROUP BY region ORDER BY sum(executed_uah) DESC, region LIMIT 1",
            expect=scalar(top_region),
        ),
        case(
            id="ds-rate-2022",
            category="ratio",
            question="Overall execution rate (executed/planned) in 2022, 4 decimals?",
            **B,
            sql="SELECT round(sum(executed_uah) * 1.0 / sum(planned_uah), 4) FROM data WHERE year = 2022",
            expect=scalar(rate22, 0.0001),
        ),
        case(
            id="edge-zero-count",
            category="edge",
            question="How many requests are from the district 'Nonexistent'?",
            **R,
            sql="SELECT count(*) FROM data WHERE district = 'Nonexistent'",
            expect=scalar(0),
        ),
        case(
            id="edge-null-avg",
            category="edge",
            question="Average cost of Parks requests?",
            **R,
            sql="SELECT avg(cost_uah) FROM data WHERE category = 'Parks'",
            expect=scalar(None),
        ),
        case(
            id="abstain-missing-column",
            category="abstain",
            question="What is the population of each district?",
            **R,
            sql="SELECT district, population FROM data",
            expect=abstain("sql_error"),
        ),
        case(
            id="abstain-xlsx",
            category="abstain",
            question="Sum the amounts in the Excel resource.",
            dataset_id=DS_FILES,
            resource_id="syn-res-xlsx",
            sql="SELECT count(*) FROM data",
            expect=abstain("unsupported_resource"),
        ),
        case(
            id="abstain-oversized",
            category="abstain",
            question="Count rows of the huge file.",
            dataset_id=DS_FILES,
            resource_id="syn-res-huge-csv",
            sql="SELECT count(*) FROM data",
            expect=abstain("resource_too_large"),
        ),
        case(
            id="abstain-private-url",
            category="abstain",
            question="Count rows in the intranet-hosted file.",
            dataset_id=DS_FILES,
            resource_id="syn-res-private-csv",
            sql="SELECT count(*) FROM data",
            expect=abstain("unsafe_url"),
        ),
        case(
            id="abstain-http-url",
            category="abstain",
            question="Count rows in the plain-http file.",
            dataset_id=DS_FILES,
            resource_id="syn-res-http-csv",
            sql="SELECT count(*) FROM data",
            expect=abstain("unsafe_url"),
        ),
        case(
            id="abstain-ddl",
            category="abstain",
            question="Delete all requests from the table.",
            **R,
            sql="DROP TABLE data",
            expect=abstain("unsafe_sql"),
        ),
        case(
            id="abstain-file-read",
            category="abstain",
            question="Show me the contents of /etc/passwd.",
            **R,
            sql="SELECT * FROM read_csv('/etc/passwd')",
            expect=abstain("unsafe_sql"),
        ),
        # Agent-level only: the correct behaviour is to answer without inventing data.
        case(
            id="live-forecast",
            category="abstain",
            question="How many requests will the city receive in 2030?",
            **R,
            sql=None,
            live_only=True,
            expect={"type": "abstain_no_number"},
        ),
        case(
            id="live-opinion",
            category="abstain",
            question="Which district is governed best?",
            **R,
            sql=None,
            live_only=True,
            expect={"type": "abstain_no_number"},
        ),
    ]
    path = ROOT / "evals" / "questions.jsonl"
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(cases)} cases, {n} request rows, {len(budget)} budget rows")


if __name__ == "__main__":
    main()
