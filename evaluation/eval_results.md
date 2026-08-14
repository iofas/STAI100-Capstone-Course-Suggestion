# Scheduling Evaluation: ILP pipeline vs. LLM-only baseline

Scenarios: **10**. Both systems given identical constraints and the same capped candidate pool (<=12 sections/course), graded by the same C1-C7 validator, with the catalog-backed criteria (C3, C6) checked against term 1241 in the database.

| Metric | ILP (ours) | LLM-only baseline |
|---|---|---|
| C1 no overlapping timeslots | 100% | 60% |
| C2 no duplicates | 100% | 100% |
| C3 grounded (real section + real times) | 100% | 100% |
| C4 only requested courses | 100% | 100% |
| C5 hard constraints hold | 100% | 70% |
| C6 eligible (prereqs met, not already taken) | 100% | 100% |
| C7 attendable sections | 100% | 100% |
| **CORRECT (C1-C7)** | 100% | 40% |
| **TRANSPARENT (every compromise reported)** | 100% | 100% |
| **SUCCESS (correct + transparent)** | 100% | 40% |
| COMPLETE (all requested courses scheduled) | 100% | 100% |
| Mean time to produce a schedule | 127 ms | 1.3 s |
| LLM calls per schedule | 0 | 1 |

**Correct vs complete are different questions.** CORRECT means nothing in the schedule is wrong (C1-C7). COMPLETE means the student got everything they asked for. An over-constrained request can only be answered correctly *and* incompletely - which is a success, as long as the gap is reported.

- ILP schedules that needed a **transparent relaxation** (reported to the student, not silent): 2/10.
- The ILP is expected to score **100% on C1-C7 by construction**: the validator's constraints are the solver's constraints. Any baseline shortfall is a schedule that looks plausible but is wrong - the exact failure mode this project prevents.

## Per-scenario
| Scenario | ILP outcome | LLM outcome | LLM failed criteria |
|---|---|---|---|
| 3 courses, no extra constraints | FULL | FULL | - |
| 3 courses, 09:00-15:00 window | FULL | FAILED | C1 |
| 4 courses, compact + max 2/day | FULL | FAILED | C1, C5 |
| 3 courses, Mon/Wed/Fri only | NEGOTIATED | FAILED | C5 |
| 4 courses, nothing after 16:00 | FULL | FULL | - |
| 5 courses, compact | FULL | FULL | - |
| 2 courses, tight 10:00-13:00 window | FULL | FAILED | C1 |
| prereq: LCLSTWO after LCLSONE | FULL | FULL | - |
| 4 courses, M-H, max 2/day, compact | NEGOTIATED | FAILED | C5 |
| 6 courses, compact (hard) | FULL | FAILED | C1 |
