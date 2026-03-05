"""EvalView built-in benchmark packs.

Each pack is a list of test-case dicts that can be written as YAML and run
against any configured agent.  Tests are scored by LLM-as-judge (output
quality) and tool-category matching, so they work regardless of what specific
tool names your agent uses.

Available domains:
    rag              Retrieval-augmented generation patterns
    coding           Programming and code-generation tasks
    customer-support Support agent scenarios
    research         Multi-source synthesis and analysis

Usage::
    from evalview.benchmarks import get_pack, DOMAINS
    cases = get_pack("rag")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# ── RAG benchmark ──────────────────────────────────────────────────────────────
# Tests retrieval, synthesis, and grounded responses.
# Agents should search/retrieve before answering.

_RAG: List[Dict[str, Any]] = [
    {
        "name": "rag-basic-factual-lookup",
        "description": "Agent retrieves a specific fact before answering",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {"query": "What is the refund policy for annual subscriptions?"},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 60},
    },
    {
        "name": "rag-date-sensitive-fact",
        "description": "Agent retrieves current information rather than guessing",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {"query": "What are the current pricing tiers and their limits?"},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 60},
    },
    {
        "name": "rag-multi-doc-synthesis",
        "description": "Agent retrieves from multiple sources and synthesizes",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {"query": "Compare the features available in the Starter and Pro plans, and recommend the best one for a team of 5 developers."},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "rag-clarification-before-retrieval",
        "description": "Agent asks for clarification when the query is ambiguous",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {"query": "How does the integration work?"},
        "expected": {
            "output": {"contains": []},
        },
        "thresholds": {"min_score": 55},
    },
    {
        "name": "rag-no-hallucination-on-unknown",
        "description": "Agent says it doesn't know rather than making something up",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {"query": "What was the company's revenue in 1987?"},
        "expected": {
            "output": {"contains": []},
            "hallucination": {"check": True, "allow": False},
        },
        "thresholds": {"min_score": 65},
    },
    {
        "name": "rag-long-context-extraction",
        "description": "Agent extracts the right detail from a large document",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {"query": "According to the terms of service, what happens to user data after account deletion?"},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 60},
    },
    {
        "name": "rag-contradictory-sources",
        "description": "Agent surfaces conflicting information rather than picking one arbitrarily",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {"query": "I've seen different numbers for the API rate limit — can you clarify what it actually is?"},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 60},
    },
    {
        "name": "rag-step-by-step-procedure",
        "description": "Agent retrieves and presents a multi-step process correctly",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {"query": "Walk me through the steps to set up SSO for my organization."},
        "expected": {"tool_categories": ["retrieval", "search"]},
        "thresholds": {"min_score": 70},
    },
]

# ── Coding benchmark ───────────────────────────────────────────────────────────
# Tests code generation, debugging, and explanation tasks.
# Agents with code execution tools get extra signal.

_CODING: List[Dict[str, Any]] = [
    {
        "name": "coding-fizzbuzz",
        "description": "Basic code generation — FizzBuzz in Python",
        "difficulty": "trivial",
        "suite_type": "capability",
        "input": {"query": "Write a Python function that implements FizzBuzz for numbers 1 to 100."},
        "expected": {
            "output": {"contains": ["def ", "FizzBuzz", "fizzbuzz", "Fizz", "Buzz"]},
        },
        "thresholds": {"min_score": 75},
    },
    {
        "name": "coding-bug-fix",
        "description": "Agent identifies and fixes a subtle bug",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {
            "query": (
                "Fix the bug in this Python code:\n"
                "def avg(numbers):\n"
                "    return sum(numbers) / len(numbers)\n"
                "print(avg([]))"
            )
        },
        "expected": {
            "output": {"contains": []},
        },
        "thresholds": {"min_score": 70},
    },
    {
        "name": "coding-explain-code",
        "description": "Agent explains what a piece of code does clearly",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {
            "query": (
                "Explain what this code does in plain English:\n"
                "result = [x**2 for x in range(10) if x % 2 == 0]"
            )
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 70},
    },
    {
        "name": "coding-refactor-for-readability",
        "description": "Agent refactors nested loops into cleaner code",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": (
                "Refactor this code to be more readable and Pythonic:\n"
                "result = []\n"
                "for i in range(len(items)):\n"
                "    if items[i] > 0:\n"
                "        result.append(items[i] * 2)"
            )
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 70},
    },
    {
        "name": "coding-write-unit-tests",
        "description": "Agent writes meaningful unit tests for a function",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": (
                "Write unit tests for this function using pytest:\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('Cannot divide by zero')\n"
                "    return a / b"
            )
        },
        "expected": {
            "output": {"contains": ["def test_", "pytest", "assert"]},
        },
        "thresholds": {"min_score": 70},
    },
    {
        "name": "coding-sql-query",
        "description": "Agent writes a correct SQL query for a business question",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "Write a SQL query to find the top 5 customers by total order value in the last 30 days."
        },
        "expected": {
            "output": {"contains": ["SELECT", "ORDER BY", "LIMIT"]},
        },
        "thresholds": {"min_score": 70},
    },
    {
        "name": "coding-async-pattern",
        "description": "Agent correctly uses async/await for concurrent API calls",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": "Write an async Python function that fetches data from three URLs concurrently and returns all results."
        },
        "expected": {
            "output": {"contains": ["async def", "await", "asyncio"]},
        },
        "thresholds": {"min_score": 65},
    },
    {
        "name": "coding-algorithm-complexity",
        "description": "Agent optimises an O(n²) algorithm to O(n log n)",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": (
                "This function is too slow for large inputs. Optimise it and explain the improvement:\n"
                "def has_duplicates(lst):\n"
                "    for i in range(len(lst)):\n"
                "        for j in range(i+1, len(lst)):\n"
                "            if lst[i] == lst[j]:\n"
                "                return True\n"
                "    return False"
            )
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
]

# ── Customer support benchmark ─────────────────────────────────────────────────
# Tests support agent quality: empathy, resolution, escalation judgement.

_CUSTOMER_SUPPORT: List[Dict[str, Any]] = [
    {
        "name": "support-refund-request",
        "description": "Agent handles a standard refund request correctly",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {"query": "I was charged twice for my subscription this month. I'd like a refund for the duplicate charge."},
        "expected": {
            "output": {"contains": []},
            "tool_categories": ["crm", "billing", "lookup"],
        },
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-password-reset",
        "description": "Agent guides user through password reset clearly",
        "difficulty": "trivial",
        "suite_type": "capability",
        "input": {"query": "I forgot my password and can't log in. How do I reset it?"},
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 70},
    },
    {
        "name": "support-angry-customer",
        "description": "Agent stays professional and empathetic with a frustrated customer",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "This is ridiculous! My order has been 'processing' for two weeks and nobody is helping me. I want answers NOW."
        },
        "expected": {
            "output": {"contains": []},
            "tool_categories": ["order_lookup", "crm", "lookup"],
        },
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-escalation-judgement",
        "description": "Agent correctly identifies when to escalate to human",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "I've been trying to get this resolved for 3 weeks. Five different agents told me different things and nothing has been fixed. I'm about to cancel."
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-feature-request",
        "description": "Agent acknowledges a feature request and sets correct expectations",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {"query": "I really wish your product had a dark mode. Is that something you're planning to add?"},
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-technical-troubleshoot",
        "description": "Agent systematically troubleshoots a technical issue",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "The export to CSV feature stopped working yesterday. I click the button but nothing happens. I'm using Chrome on Windows 11."
        },
        "expected": {
            "output": {"contains": []},
            "tool_categories": ["knowledge_base", "retrieval", "lookup"],
        },
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-account-cancellation",
        "description": "Agent handles cancellation request professionally — neither pushy nor dismissive",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": "I want to cancel my account. Please don't try to talk me out of it, just tell me how to proceed."
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "support-billing-dispute",
        "description": "Agent investigates a billing discrepancy and explains it clearly",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": "My bill this month was $127 but I'm on the $49/month plan. Can you explain all the charges?"
        },
        "expected": {
            "tool_categories": ["billing", "crm", "lookup"],
        },
        "thresholds": {"min_score": 65},
    },
]

# ── Research benchmark ─────────────────────────────────────────────────────────
# Tests multi-step synthesis, comparison, and structured output.

_RESEARCH: List[Dict[str, Any]] = [
    {
        "name": "research-pros-cons",
        "description": "Agent produces a balanced pros/cons analysis",
        "difficulty": "easy",
        "suite_type": "capability",
        "input": {"query": "What are the pros and cons of using microservices vs a monolithic architecture for a startup?"},
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "research-market-comparison",
        "description": "Agent compares three options across multiple dimensions",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "Compare PostgreSQL, MySQL, and MongoDB for a web application that needs both structured and semi-structured data."
        },
        "expected": {"output": {"contains": ["PostgreSQL", "MySQL", "MongoDB"]}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "research-structured-summary",
        "description": "Agent produces a structured, well-organised summary",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "Give me a structured overview of how transformer models work — suitable for a software engineer with no ML background."
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "research-trend-analysis",
        "description": "Agent identifies patterns and draws reasoned conclusions",
        "difficulty": "medium",
        "suite_type": "capability",
        "input": {
            "query": "What trends are driving adoption of Rust for systems programming, and what are the remaining barriers?"
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 60},
    },
    {
        "name": "research-executive-brief",
        "description": "Agent distils complex topic into a concise executive brief",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": "Write a two-paragraph executive brief on the business risks of using LLMs in customer-facing products — suitable for a non-technical VP."
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
    {
        "name": "research-counter-argument",
        "description": "Agent steelmans the opposite position fairly",
        "difficulty": "hard",
        "suite_type": "capability",
        "input": {
            "query": "I believe remote-first companies are strictly more productive than office-first. Give me the strongest case for why I might be wrong."
        },
        "expected": {"output": {"contains": []}},
        "thresholds": {"min_score": 65},
    },
]

# ── Registry ───────────────────────────────────────────────────────────────────

DOMAINS: Dict[str, str] = {
    "rag":              "Retrieval-augmented generation patterns (8 tests)",
    "coding":           "Programming and code-generation tasks (8 tests)",
    "customer-support": "Support agent scenarios (8 tests)",
    "research":         "Multi-source synthesis and analysis (6 tests)",
}

_PACKS: Dict[str, List[Dict[str, Any]]] = {
    "rag":              _RAG,
    "coding":           _CODING,
    "customer-support": _CUSTOMER_SUPPORT,
    "research":         _RESEARCH,
}


def get_pack(domain: str) -> List[Dict[str, Any]]:
    """Return the test-case list for a domain.

    Raises ValueError for unknown domains.
    """
    if domain not in _PACKS:
        available = ", ".join(sorted(_PACKS))
        raise ValueError(f"Unknown benchmark domain '{domain}'. Available: {available}")
    return _PACKS[domain]


def write_pack_yaml(domain: str, output_dir: Path) -> List[Path]:
    """Write a benchmark pack as YAML files to output_dir.

    Returns the list of paths written.
    """
    import yaml  # type: ignore[import-untyped]

    cases = get_pack(domain)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    for case in cases:
        name = case["name"]
        out_path = output_dir / f"{name}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(case, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        written.append(out_path)

    return written
