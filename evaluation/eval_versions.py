"""
Run the v3 evaluation against an older version of the agent.

The pass rates reported for v1 and v2 were produced by the metric of the day -
substring matching on the generated SQL. Comparing those figures against a v3
number produced by execution accuracy is not a like-for-like comparison, and
whichever version is measured more strictly will look worse for reasons that
have nothing to do with the agent. This script closes that gap by running the
SAME harness (evaluation/eval_agent.py) and the SAME golden dataset against an
older checkout of sql_agent/.

Set the older version up as a git worktree first:

    git worktree add ../pbrain_v1 37ee3db --detach   # before directives 6/7
    git worktree add ../pbrain_v2 e473bd6 --detach   # v2, pre-capstone-finals

    python -m evaluation.eval_versions --path ../pbrain_v1 --label v1 --runs 1

What is held constant, so the prompt is the only variable:
  * the database (COURSE_DB_PATH points at the current course_offerings.db, so
    every version sees the same catalog);
  * the model (DEEPSEEK_MODEL is passed through unchanged);
  * the test cases and gold queries (both come from the current checkout).

What CANNOT be held constant, and must be stated with any figure this produces:
the original v1/v2 runs happened months ago against whichever DeepSeek model
was current then. This is therefore a *prompt ablation on today's model*, not a
reproduction of the historical runs.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True,
                        help="path to a checkout/worktree containing sql_agent/")
    parser.add_argument("--label", required=True,
                        help="version label for the report filename, e.g. v1")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    version_root = Path(args.path).resolve()
    if not (version_root / "sql_agent").is_dir():
        parser.error(f"no sql_agent/ package under {version_root}")

    repo_root = Path(__file__).resolve().parent.parent

    # The old checkout's config calls load_dotenv(), which resolves relative to
    # its own location - a worktree, where .env does not exist because it is
    # gitignored. Load the current .env into the environment here, before the
    # old config module reads it, and pin the database to the current one so the
    # only thing that differs between versions is the agent code itself.
    from dotenv import load_dotenv

    load_dotenv(repo_root / ".env")
    os.environ.setdefault("COURSE_DB_PATH", str(repo_root / "course_offerings.db"))

    # The old checkout must win the import race for `sql_agent`, while
    # `evaluation` and `tests` still come from the current one. Both go on the
    # path with the old checkout first; only the old tree has sql_agent/, and
    # only the current tree has evaluation/.
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(version_root))

    import sql_agent  # noqa: E402  - resolved against version_root

    resolved = Path(sql_agent.__file__).resolve()
    if version_root not in resolved.parents:
        raise SystemExit(f"imported the wrong sql_agent ({resolved}); "
                         "is the current package already imported?")
    print(f"Evaluating {args.label}: {resolved.parent}")

    from evaluation.eval_agent import evaluate, report  # noqa: E402

    data = evaluate(args.runs)
    text = report(data)
    header = (f"# SQL Agent Evaluation - {args.label}\n\n"
              f"Agent code: `{version_root}`\n"
              f"Harness and golden dataset: current checkout. Prompt ablation "
              f"on the current model - not a reproduction of the original "
              f"{args.label} run.\n\n")
    out = repo_root / "evaluation" / f"eval_agent_results_{args.label}.md"
    out.write_text(header + text + "\n", encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
