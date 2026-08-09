# Scheduling Evaluation: ILP pipeline vs. LLM-only baseline

Scenarios: **10**. Both systems given identical constraints and the same capped candidate pool (≤12 sections/course). Scored by the C1-C6 validator against the original constraints (C3 grounding checked against the DB).

| Metric | ILP (ours) | LLM-only baseline |
|---|---|---|
| C1 no overlapping timeslots | 100% | 50% |
| C2 no duplicate courses | 100% | 100% |
| C3 grounded (real sections) | 100% | 100% |
| C4 only requested courses | 100% | 100% |
| C5 hard constraints hold | 80% | 70% |
| **Valid schedule (C1-C4)** | 100% | 50% |
| All requested courses scheduled | 100% | 100% |

- ILP schedules that needed a **transparent relaxation** (reported to the student, not silent): 2/10.
- The ILP is expected to score **100% on C1-C4 by construction**: the validator's constraints are the solver's constraints. Any baseline shortfall on C1/C2/C3 is a schedule that looks plausible but is wrong - the exact failure mode the project prevents.

## Per-scenario
| Scenario | ILP valid | ILP complete | LLM valid | LLM complete |
|---|---|---|---|---|
| 3 courses, no extra constraints | Y | Y | Y | Y |
| 3 courses, 09:00-15:00 window | Y | Y | N | Y |
| 4 courses, compact + max 2/day | Y | Y | N | Y |
| 3 courses, Mon/Wed/Fri only | Y | Y | N | Y |
| 4 courses, nothing after 16:00 | Y | Y | Y | Y |
| 5 courses, compact | Y | Y | Y | Y |
| 2 courses, tight 10:00-13:00 window | Y | Y | N | Y |
| prereq: LCLSTWO after LCLSONE | Y | Y | Y | Y |
| 4 courses, M-H, max 2/day, compact | Y | Y | N | Y |
| 6 courses, compact (hard) | Y | Y | Y | Y |
