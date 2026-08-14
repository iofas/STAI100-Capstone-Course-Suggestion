"""
SQL Agent evaluation suite.

The pytest suite in tests/ answers one question - "does the generated SQL
contain the substrings we expect?" - and a query can pass that check while
returning the wrong rows. This module scores the same agent on the metrics that
actually matter, following the three evaluation layers:

  LAYER 1 (unit)        Is each generated query right?
                        - execution accuracy: run the agent's SQL and the gold
                          SQL against the database and compare the sections
                          returned (the fragile-string alternative is what the
                          pytest suite already does)
                        - structural validity: does it parse (sqlglot AST) as a
                          single read-only SELECT over whitelisted tables
                        - schema adherence: does it honour the term-scoping
                          instruction the system prompt gives it
  LAYER 2 (trajectory)  Does the agent behave consistently across repeats, and
                        does it carry conversation state correctly? Determinism
                        is scored explicitly because a temperature-0 agent that
                        answers differently run to run cannot be regression
                        tested at all.
  LAYER 3 (end-to-end)  Task success, refusal rate on adversarial input, and
                        the cost of an answer: latency and tokens.

Run (from the repo root):
    python -m evaluation.eval_agent            # default 3 repeats
    python -m evaluation.eval_agent --runs 10  # the reported figure

Writes evaluation/eval_agent_results.md. Costs (cases x runs) DeepSeek calls.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path

from sql_agent import config as _agent_config
from sql_agent.agent import _is_safe_select, ask
from sql_agent.config import DB_PATH
from sql_agent.db import PREREQUISITES_TABLE_NAME, TABLE_NAME

# evaluation/eval_versions.py points this module at an older checkout of
# sql_agent/ to compare versions on one metric, and those versions predate some
# of the current config. Read anything that might be missing defensively rather
# than failing at import time.
SCHEDULE_TERM = getattr(_agent_config, "SCHEDULE_TERM", "1261")

from .eval_cases import all_cases

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - sqlglot is a dev dependency
    sqlglot = None

ALLOWED_TABLES = {TABLE_NAME.lower(), PREREQUISITES_TABLE_NAME.lower()}
_TERM_PATTERN = re.compile(r"\bterm\s*(=|IN|LIKE)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Layer 1: execution accuracy
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _project_to_key(sql: str) -> str | None:
    """Rewrite a SELECT's projection to (course_code, section) via the AST.

    Execution accuracy has to compare *which sections* two queries return, but
    the agent legitimately chooses its own column list, so comparing raw rows
    would fail on formatting rather than meaning. Rewriting the projection with
    sqlglot - instead of string-hacking the SELECT clause - keeps the WHERE
    logic untouched, which is the whole point of comparing at the AST level.

    Returns:
        The rewritten query, or None if it could not be parsed or rewritten.
    """
    if sqlglot is None:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - any parse failure is just "unscorable"
        return None
    if not isinstance(tree, exp.Select):
        return None
    alias = None
    src = tree.args.get("from")
    if src is not None:
        table = src.this
        if isinstance(table, exp.Table) and table.alias:
            alias = table.alias
    prefix = f"{alias}." if alias else ""
    try:
        rewritten = tree.select(f"{prefix}course_code", f"{prefix}section",
                                append=False)
        return rewritten.sql(dialect="sqlite")
    except Exception:  # noqa: BLE001
        return None


def _section_keys(sql: str) -> set[tuple] | None:
    """Run a query projected to (course_code, section) and return the row set."""
    projected = _project_to_key(sql)
    if projected is None:
        return None
    try:
        with _connect() as conn:
            return {tuple(r) for r in conn.execute(projected).fetchall()}
    except sqlite3.Error:
        return None


def _execution_accuracy(candidate_sql: str, gold_sql: str) -> tuple[str, float]:
    """Compare the sections a generated query returns against the gold query.

    Returns:
        (verdict, jaccard) where verdict is "match", "mismatch", or
        "unscorable", and jaccard is the overlap of the two section sets
        (partial credit, so a query that gets most of the answer right is not
        scored the same as one that returns nothing).
    """
    got = _section_keys(candidate_sql)
    want = _section_keys(gold_sql)
    if want is None:
        raise RuntimeError(f"gold query is not runnable: {gold_sql}")
    if got is None:
        return "unscorable", 0.0
    if got == want:
        return "match", 1.0
    union = got | want
    return "mismatch", (len(got & want) / len(union) if union else 1.0)


# --------------------------------------------------------------------------- #
# Layer 1: structural validity (AST), independent of the regex guardrail
# --------------------------------------------------------------------------- #
def _ast_verdict(sql: str) -> tuple[bool, str]:
    """Judge a query's safety by parsing it, not by matching keywords.

    The shipped guardrail (`agent._is_safe_select`) is regex-based, so this is
    deliberately a second opinion from a real SQL parser: any disagreement
    between the two is either a hole in the regex or an over-rejection, and
    both are worth knowing about.

    Returns:
        (safe, reason) - reason is "ok" when safe.
    """
    if sqlglot is None:
        return True, "sqlglot unavailable"
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except Exception as exc:  # noqa: BLE001
        return False, f"unparseable: {type(exc).__name__}"
    if len(statements) != 1:
        return False, f"{len(statements)} statements"
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        return False, f"not a SELECT ({type(tree).__name__})"
    for table in tree.find_all(exp.Table):
        if table.name and table.name.lower() not in ALLOWED_TABLES:
            return False, f"table not whitelisted: {table.name}"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Running one case
# --------------------------------------------------------------------------- #
def _normalise(sql: str) -> str:
    """Whitespace/case-insensitive form, for comparing runs to each other."""
    if sqlglot is not None:
        try:
            return sqlglot.parse_one(sql, read="sqlite").sql(dialect="sqlite")
        except Exception:  # noqa: BLE001
            pass
    return " ".join(sql.split()).rstrip(";").upper()


def _run_once(case: dict) -> dict:
    """One agent call, with everything the metrics need recorded."""
    started = time.perf_counter()
    result = ask(case["query"], history=case.get("history"))
    elapsed = time.perf_counter() - started

    sql = (result.get("sql") or "").strip()
    refused = sql.upper() == "NONE" or result.get("error") is not None
    record = {
        "sql": sql,
        "refused": refused,
        "error": result.get("error"),
        "rows": len(result.get("rows") or []),
        "latency_s": elapsed,
        "tokens": (result.get("tokens") or {}).get("total_tokens"),
        "normalised": None if refused else _normalise(sql),
        "term_scoped": bool(_TERM_PATTERN.search(sql)) if not refused else None,
    }

    if refused:
        record["ast_safe"], record["ast_reason"] = True, "refused"
        record["regex_safe"] = True
    else:
        record["ast_safe"], record["ast_reason"] = _ast_verdict(sql)
        record["regex_safe"] = _is_safe_select(sql)

    # Layer 1: execution accuracy against the gold query.
    if case.get("gold_sql") and not refused:
        record["exec_verdict"], record["exec_overlap"] = _execution_accuracy(
            sql, case["gold_sql"])
    else:
        record["exec_verdict"], record["exec_overlap"] = None, None

    # Legacy metric: the substring assertions the pytest suite uses, so the new
    # numbers can be compared against the v1/v2 figures on equal footing.
    record["legacy_pass"] = _legacy_pass(case, result)
    return record


def _legacy_pass(case: dict, result: dict) -> bool:
    """Reproduce tests/test_agent.py's assertion logic as a metric."""
    sql_upper = (result.get("sql") or "").upper()
    if case.get("expect_error"):
        return sql_upper == "NONE" or result.get("error") is not None
    if result.get("error") is not None:
        return False
    if "expected_sql_contains_all" in case:
        if any(o.upper() not in sql_upper for o in case["expected_sql_contains_all"]):
            return False
    if "expected_sql_contains_any" in case:
        if not any(o.upper() in sql_upper for o in case["expected_sql_contains_any"]):
            return False
    if "expected_sql_contains" in case:
        if case["expected_sql_contains"].upper() not in sql_upper:
            return False
    return True


def _case_success(case: dict, record: dict) -> bool:
    """Layer 3 task success: did the agent do the right thing for this case?

    Answerable case  -> a safe query whose result set matches the gold answer.
    Refusal case     -> refused.
    Adversarial safe -> a safe query (plus zero rows, where that is the point).
    """
    if case.get("expect_error"):
        return record["refused"]
    if record["refused"]:
        # For a probe where refusing is also a safe outcome, a refusal counts;
        # everywhere else, declining an answerable question is a failure.
        return bool(case.get("refusal_acceptable"))
    if not record["ast_safe"]:
        return False
    if case.get("expect_zero_rows") and record["rows"] != 0:
        return False
    if case.get("requires_term_filter") and not record["term_scoped"]:
        return False
    if case.get("gold_sql") and not case.get("exclude_reason"):
        return record["exec_verdict"] == "match"
    return True


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _pct(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def evaluate(runs: int) -> dict:
    cases = all_cases()
    results = []
    for i, case in enumerate(cases, 1):
        records = [_run_once(case) for _ in range(runs)]
        successes = sum(1 for r in records if _case_success(case, r))
        legacy = sum(1 for r in records if r["legacy_pass"])
        variants = {r["normalised"] for r in records}
        row = {
            "case": case,
            "records": records,
            "success_rate": successes / runs,
            "legacy_rate": legacy / runs,
            "stable": len(variants) == 1,
            "variants": len(variants),
        }
        results.append(row)
        flag = "OK " if successes == runs else ("~  " if successes else "FAIL")
        print(f"[{i:2d}/{len(cases)}] {flag} {case['test_id']:14s} "
              f"success {successes}/{runs}  legacy {legacy}/{runs}  "
              f"{'stable' if row['stable'] else str(row['variants']) + ' variants'}")
    return {"runs": runs, "results": results}


def _slice_table(results: list[dict], key: str) -> list[tuple[str, int, float, float]]:
    """Per-slice success and legacy pass rates (slice analysis)."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        buckets[row["case"].get(key, "?")].append(row)
    out = []
    for name, rows in sorted(buckets.items()):
        n = len(rows)
        success = 100.0 * sum(r["success_rate"] for r in rows) / n
        legacy = 100.0 * sum(r["legacy_rate"] for r in rows) / n
        out.append((name, n, success, legacy))
    return out


REPORT_CATEGORIES = {
    "basic_lookup": "Basic Lookup",
    "time_window": "Time Window Constraints",
    "complex_exclusion": "Prerequisite/Exclusion Logic",
    "prerequisite_check": "Prerequisite/Exclusion Logic",
    "prof_lookup": "Professor Lookups",
    "status_filter": "Status Filters",
    "security": "Guardrail Checks / SQL Injection",
    "nonsense_input": "Edge Cases",
    "ambiguous_input": "Edge Cases",
    "gibberish": "Edge Cases",
    "massive_exclusion": "Edge Cases",
    "no_results": "Edge Cases",
    "memory_carryover": "Memory",
    "memory_exclusion_carryover": "Memory",
    "memory_constraint_removal": "Memory",
    "no_memory": "Memory",
}


def report(data: dict) -> str:
    runs = data["runs"]
    results = data["results"]
    regression = [r for r in results if r["case"]["source"] == "regression"]
    adversarial = [r for r in results if r["case"]["source"] == "adversarial"]
    every = [rec for r in results for rec in r["records"]]
    answerable = [r for r in results if not r["case"].get("expect_error")]

    lines = ["# SQL Agent Evaluation", ""]
    lines.append(f"{len(results)} cases x **{runs} run(s)** = {len(every)} agent "
                 f"calls ({len(regression)} regression cases from "
                 f"tests/test_cases.py + {len(adversarial)} adversarial probes "
                 "defined in evaluation/eval_cases.py).")
    lines.append("")

    # ---- headline metrics -------------------------------------------------
    exec_scored = [r for r in results
                   if r["case"].get("gold_sql")
                   and not r["case"].get("exclude_reason")]
    exec_records = [rec for r in exec_scored for rec in r["records"]
                    if rec["exec_verdict"] is not None]
    exec_match = sum(1 for rec in exec_records if rec["exec_verdict"] == "match")
    overlap = [rec["exec_overlap"] for rec in exec_records]

    legacy_all = _pct(sum(1 for rec in every if rec["legacy_pass"]), len(every))
    success_all = 100.0 * sum(r["success_rate"] for r in results) / len(results)
    stable = _pct(sum(1 for r in results if r["stable"]), len(results))
    answered = [rec for r in answerable for rec in r["records"] if not rec["refused"]]
    term_ok = sum(1 for rec in answered if rec["term_scoped"])
    latencies = sorted(rec["latency_s"] for rec in every)
    tokens = [rec["tokens"] for rec in every if rec["tokens"]]
    refusal_needed = [rec for r in results if r["case"].get("expect_error")
                      for rec in r["records"]]
    refused_ok = sum(1 for rec in refusal_needed if rec["refused"])
    ast_unsafe = [rec for rec in every if not rec["ast_safe"]]
    disagree = [rec for rec in every if rec["ast_safe"] != rec["regex_safe"]]

    def p(idx: float) -> float:
        return latencies[min(int(idx * len(latencies)), len(latencies) - 1)]

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Layer | Result |")
    lines.append("|---|---|---|")
    lines.append(f"| **Execution accuracy** (result set == gold) | 1 | "
                 f"**{_pct(exec_match, len(exec_records)):.0f}%** "
                 f"({exec_match}/{len(exec_records)} runs) |")
    lines.append(f"| Mean result-set overlap (partial credit) | 1 | "
                 f"{100.0 * statistics.fmean(overlap):.0f}% |" if overlap else
                 "| Mean result-set overlap | 1 | n/a |")
    lines.append(f"| Substring pass rate (the v1/v2 metric) | 1 | "
                 f"{legacy_all:.0f}% |")
    lines.append(f"| Term-scoping compliance | 1 | "
                 f"{_pct(term_ok, len(answered)):.0f}% "
                 f"({term_ok}/{len(answered)} answered runs) |")
    lines.append(f"| Structurally unsafe SQL produced | 1 | "
                 f"{len(ast_unsafe)}/{len(every)} runs |")
    lines.append(f"| Guardrail agreement (regex vs AST parser) | 1 | "
                 f"{_pct(len(every) - len(disagree), len(every)):.0f}% |")
    lines.append(f"| **Run-to-run determinism** (identical SQL every run) | 2 | "
                 f"**{stable:.0f}%** of cases |")
    lines.append(f"| **Task success** (right answer, safely) | 3 | "
                 f"**{success_all:.0f}%** |")
    lines.append(f"| Refusal rate on must-refuse cases | 3 | "
                 f"{_pct(refused_ok, len(refusal_needed)):.0f}% |")
    lines.append(f"| Latency p50 / p95 | 3 | {p(0.5):.1f}s / {p(0.95):.1f}s |")
    if tokens:
        lines.append(f"| Mean tokens per query | 3 | {statistics.fmean(tokens):.0f} |")
    lines.append("")

    # ---- per-category slice ----------------------------------------------
    lines.append("## Slice analysis: by test category")
    lines.append("")
    lines.append("| Category | Cases | Task success | Substring pass rate |")
    lines.append("|---|---|---|---|")
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in regression:
        buckets[REPORT_CATEGORIES.get(row["case"]["category"], "Other")].append(row)
    for name, rows in sorted(buckets.items()):
        n = len(rows)
        lines.append(f"| {name} | {n} | "
                     f"{100.0 * sum(r['success_rate'] for r in rows) / n:.0f}% | "
                     f"{100.0 * sum(r['legacy_rate'] for r in rows) / n:.0f}% |")
    n = len(regression)
    lines.append(f"| **Total (regression set)** | {n} | "
                 f"{100.0 * sum(r['success_rate'] for r in regression) / n:.0f}% | "
                 f"{100.0 * sum(r['legacy_rate'] for r in regression) / n:.0f}% |")
    lines.append("")

    # ---- adversarial ------------------------------------------------------
    lines.append("## Adversarial probes")
    lines.append("")
    lines.append("| Probe | What it tests | Success | Behaviour |")
    lines.append("|---|---|---|---|")
    for row in adversarial:
        case, rec = row["case"], row["records"][0]
        if rec["refused"]:
            behaviour = "refused (NONE)"
        elif not rec["ast_safe"]:
            behaviour = f"UNSAFE: {rec['ast_reason']}"
        else:
            behaviour = f"answered, {rec['rows']} rows"
        lines.append(f"| `{case['test_id']}` {case['category']} | {case['note']} | "
                     f"{row['success_rate'] * runs:.0f}/{runs} | {behaviour} |")
    lines.append("")

    # ---- failures ---------------------------------------------------------
    failures = [r for r in results if r["success_rate"] < 1.0]
    lines.append("## Cases that did not succeed on every run")
    lines.append("")
    if not failures:
        lines.append("None.")
    else:
        lines.append("| Case | Success | Diagnosis |")
        lines.append("|---|---|---|")
        for row in sorted(failures, key=lambda r: r["success_rate"]):
            case = row["case"]
            rec = next((rec for rec in row["records"]
                        if not _case_success(case, rec)), row["records"][0])
            if rec["refused"] and not case.get("expect_error"):
                why = "refused an answerable question"
            elif not rec["ast_safe"]:
                why = f"unsafe SQL: {rec['ast_reason']}"
            elif rec["exec_verdict"] == "mismatch":
                why = (f"wrong result set (overlap "
                       f"{rec['exec_overlap'] * 100:.0f}%)")
            elif rec["exec_verdict"] == "unscorable":
                why = "query could not be projected/executed for comparison"
            elif case.get("requires_term_filter") and not rec["term_scoped"]:
                why = "no term filter, so archived sections are included"
            elif case.get("expect_zero_rows") and rec["rows"]:
                why = f"returned {rec['rows']} rows where none should match"
            elif not rec["refused"] and case.get("expect_error"):
                why = "answered a request it should have refused"
            else:
                why = "see raw record"
            lines.append(f"| `{case['test_id']}` | "
                         f"{row['success_rate'] * runs:.0f}/{runs} | {why} |")
    lines.append("")

    # ---- instability ------------------------------------------------------
    unstable = [r for r in results if not r["stable"]]
    lines.append("## Run-to-run instability (temperature 0)")
    lines.append("")
    if runs == 1:
        lines.append("_Single run - determinism not measured._")
    elif not unstable:
        lines.append(f"Every case produced identical SQL across all {runs} runs.")
    else:
        lines.append(f"{len(unstable)}/{len(results)} cases produced more than one "
                     f"distinct query across {runs} runs:")
        lines.append("")
        lines.append("| Case | Distinct queries | Success |")
        lines.append("|---|---|---|")
        for row in unstable:
            lines.append(f"| `{row['case']['test_id']}` | {row['variants']} | "
                         f"{row['success_rate'] * runs:.0f}/{runs} |")
    # ---- what is excluded from execution accuracy, and why ----------------
    excluded = [r for r in results if r["case"].get("exclude_reason")]
    lines.append("## Excluded from the execution-accuracy figure")
    lines.append("")
    lines.append(f"Execution accuracy is computed over the {len(exec_scored)} "
                 "answerable cases with a defensible single gold answer. These "
                 "cases are still run and still counted in task success, but no "
                 "gold result set can be justified for them:")
    lines.append("")
    lines.append("| Case | Why it cannot be scored |")
    lines.append("|---|---|")
    for row in excluded:
        lines.append(f"| `{row['case']['test_id']}` | "
                     f"{row['case']['exclude_reason']} |")
    lines.append("")
    lines.append(f"_Gold queries carry no term filter, so term-scoping is "
                 f"measured on its own line rather than contaminating every "
                 f"other number. Current term: {SCHEDULE_TERM}._")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3,
                        help="repeats per case (default 3; 10 for the reported run)")
    args = parser.parse_args()

    data = evaluate(args.runs)
    text = report(data)
    out_path = Path(__file__).resolve().parent / "eval_agent_results.md"
    out_path.write_text(text + "\n", encoding="utf-8")
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
