#!/usr/bin/env bash
# EvalView Model Benchmark: Gemma 4 26B vs Qwen3 Coder 30B vs Claude Sonnet 4.6
# 3 coding tasks × 3 models = 9 runs, all through OpenCode
#
# Usage: ./demo/run_benchmark.sh [--skip-gemma] [--skip-qwen] [--skip-sonnet]
#        ./demo/run_benchmark.sh --skip-gemma --skip-qwen   # Sonnet only

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURES="$REPO_ROOT/demo/fixtures"

# ── helpers ────────────────────────────────────────────────────────────────────

reset_fixtures() {
    echo ""
    echo "🔄  Resetting fixtures..."
    cp "$FIXTURES/.originals/buggy.py"  "$FIXTURES/buggy.py"
    cp "$FIXTURES/.originals/stub.py"   "$FIXTURES/stub.py"
    cp "$FIXTURES/.originals/messy.py"  "$FIXTURES/messy.py"
}

verify_fixtures() {
    echo ""
    echo "🔍  Verifying correctness after run:"
    echo -n "    buggy.py  → "; python3 "$FIXTURES/buggy.py"  2>&1
    echo -n "    stub.py   → "; python3 "$FIXTURES/stub.py"   2>&1
    echo -n "    messy.py  → "; python3 "$FIXTURES/messy.py"  2>&1
}

# Run a single test (sequential — Ollama handles one request at a time)
run_test_sequential() {
    local yaml_dir="$1"
    local test="$2"
    local cmd="$3"   # "run" or "check"

    reset_fixtures
    cd "$REPO_ROOT"

    if [ "$cmd" = "run" ]; then
        evalview run "$yaml_dir/$test.yaml" --no-judge
    else
        evalview check "$yaml_dir/$test.yaml" --timeout 400 --no-judge 2>/dev/null || true
    fi
}

skip_sonnet=false
skip_gemma=false
skip_qwen=false

for arg in "$@"; do
    case $arg in
        --skip-sonnet) skip_sonnet=true ;;
        --skip-gemma)  skip_gemma=true  ;;
        --skip-qwen)   skip_qwen=true   ;;
    esac
done

# Save originals once
mkdir -p "$FIXTURES/.originals"
[ -f "$FIXTURES/.originals/buggy.py" ] || cp "$FIXTURES/buggy.py" "$FIXTURES/.originals/buggy.py"
[ -f "$FIXTURES/.originals/stub.py"  ] || cp "$FIXTURES/stub.py"  "$FIXTURES/.originals/stub.py"
[ -f "$FIXTURES/.originals/messy.py" ] || cp "$FIXTURES/messy.py" "$FIXTURES/.originals/messy.py"

# ── Phase 1: Sonnet baseline ───────────────────────────────────────────────────

if [ "$skip_sonnet" = false ]; then
    echo ""
    echo "════════════════════════════════════════════════════"
    echo "  Phase 1: Claude Sonnet 4.6  →  production baseline"
    echo "════════════════════════════════════════════════════"

    for task in bug-fix implement refactor; do
        echo ""
        echo "  ▶ $task"
        run_test_sequential "demo/tests/sonnet" "$task" "run"
    done

    verify_fixtures

    echo ""
    echo "📸  Saving Sonnet as the production baseline..."
    cd "$REPO_ROOT" && evalview snapshot --path demo/tests/sonnet/ < /dev/null || true
    echo "✓  Baseline captured."
fi

# ── Phase 2: Gemma 4 26B ──────────────────────────────────────────────────────

if [ "$skip_gemma" = false ]; then
    echo ""
    echo "════════════════════════════════════════════════════"
    echo "  Phase 2: Gemma 4 26B  →  regression check"
    echo "  ⏱  Local inference — ~2-4 min per task"
    echo "════════════════════════════════════════════════════"

    for task in bug-fix implement refactor; do
        echo ""
        echo "  ▶ $task"
        run_test_sequential "demo/tests/gemma" "$task" "check"
    done

    verify_fixtures
fi

# ── Phase 3: Qwen3 Coder 30B ──────────────────────────────────────────────────

if [ "$skip_qwen" = false ]; then
    echo ""
    echo "════════════════════════════════════════════════════"
    echo "  Phase 3: Qwen3 Coder 30B  →  regression check"
    echo "  ⏱  Local inference — ~2-4 min per task"
    echo "════════════════════════════════════════════════════"

    for task in bug-fix implement refactor; do
        echo ""
        echo "  ▶ $task"
        run_test_sequential "demo/tests/qwen" "$task" "check"
    done

    verify_fixtures
fi

# ── Summary ────────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════"
echo "  Benchmark complete."
echo "════════════════════════════════════════════════════"
echo ""
echo "  Results: .evalview/results/"
echo "  Reports: ls -t .evalview/reports/*.html | head -5"
echo ""
echo "  What EvalView measured per model:"
echo "    • Tool sequence: read → edit → bash verify?"
echo "    • Latency: how long per task?"
echo "    • Score: did it actually solve the problem?"
echo "    • Regression: PASSED / TOOLS_CHANGED / REGRESSION vs Sonnet"
echo ""
