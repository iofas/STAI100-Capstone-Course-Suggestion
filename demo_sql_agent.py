"""
Quick manual test harness for the SQL Agent module.

Usage:
    python demo_sql_agent.py "What sections of GEARTAP are on Mondays?"
    python demo_sql_agent.py            # interactive prompt loop
"""
import json
import sys

from sql_agent import ask


def print_result(result: dict) -> None:
    print("\nQuestion :", result["question"])
    print("Reasoning:", result["reasoning"])
    print("SQL      :", result["sql"])
    if result["error"]:
        print("Error    :", result["error"])
    else:
        print(f"Rows     : {len(result['rows'])}")
        print(json.dumps(result["rows"][:10], indent=2, default=str))
        if len(result["rows"]) > 10:
            print(f"... ({len(result['rows']) - 10} more rows)")


def main() -> None:
    if len(sys.argv) > 1:
        print_result(ask(" ".join(sys.argv[1:])))
        return

    print("SQL Agent demo. Type a question, or 'quit' to exit.")
    while True:
        question = input("\n> ").strip()
        if question.lower() in {"quit", "exit"}:
            break
        if not question:
            continue
        print_result(ask(question))


if __name__ == "__main__":
    main()
