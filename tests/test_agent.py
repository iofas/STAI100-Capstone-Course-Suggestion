import pytest
from sql_agent import ask
from test_cases import TEST_CASES

@pytest.mark.parametrize("case", TEST_CASES, ids=lambda c: c["test_id"])
def test_sql_agent(case):
    # Get history
    chat_history = case.get("history", None)
    
    # Run the agent
    result = ask(case["query"], history=chat_history)
    
    # Assert against expected errors (Guardrails or unanswerable queries)
    if case["expect_error"]:
        is_safely_handled = (result["sql"].upper() == "NONE") or (result["error"] is not None)
        assert is_safely_handled, f"Agent generated unsafe SQL that bypassed guardrails: {result['sql']}"
        
        if "expected_sql" in case and result["error"] is None:
            assert result["sql"].upper() == case["expected_sql"].upper()
            
    # Assert against successful query generation
    else:
        assert result["error"] is None, f"Agent threw an unexpected error: {result['error']}"
        
        sql_upper = result["sql"].upper()
        
        # Check for AND conditions (ALL elements must be present)
        if "expected_sql_contains_all" in case:
            expected = case["expected_sql_contains_all"]
            missing_clauses = [opt for opt in expected if opt.upper() not in sql_upper]
            assert not missing_clauses, f"Missing required clauses {missing_clauses} in generated SQL."

        # Check for OR conditions (AT LEAST ONE element must be present)
        if "expected_sql_contains_any" in case:
            expected = case["expected_sql_contains_any"]
            match_found = any(opt.upper() in sql_upper for opt in expected)
            assert match_found, f"Missing any expected clauses {expected} in generated SQL."
        
        # Check for standard single string conditions
        if "expected_sql_contains" in case:
            expected = case["expected_sql_contains"]
            assert expected.upper() in sql_upper, f"Missing expected clause '{expected}' in generated SQL."