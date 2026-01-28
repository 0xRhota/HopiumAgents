#!/bin/bash
# Ralph Loop 2: Nado/Paradex Fixes
# Usage: ./scripts/ralph-nado-paradex.sh [max_iterations]

set -e

MAX_ITERATIONS=${1:-10}
PROJECT_DIR="/Users/admin/Documents/Projects/pacifica-trading-bot"
PRD_FILE="$PROJECT_DIR/prd-nado-paradex.json"
PROGRESS_FILE="$PROJECT_DIR/progress-nado-paradex.txt"
LOG_FILE="$PROJECT_DIR/logs/ralph-nado-paradex.log"

echo "========================================================"
echo "  RALPH LOOP 2: NADO/PARADEX FIXES"
echo "========================================================"
echo "PRD: $PRD_FILE"
echo "Progress: $PROGRESS_FILE"
echo "Log: $LOG_FILE"
echo ""

# Check prerequisites
if [ ! -f "$PRD_FILE" ]; then
  echo "Error: PRD not found at $PRD_FILE"
  exit 1
fi

# Show status
REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
echo "Stories remaining: $REMAINING"
jq -r '.userStories[] | "  \(.id): \(.title) - passes: \(.passes)"' "$PRD_FILE"
echo ""

for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo "========================================================"
  echo "  NADO/PARADEX Iteration $i of $MAX_ITERATIONS"
  echo "========================================================"

  REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
  if [ "$REMAINING" -eq 0 ]; then
    echo "All Nado/Paradex stories complete!"
    exit 0
  fi

  NEXT=$(jq -r '[.userStories[] | select(.passes == false)] | sort_by(.priority) | .[0] | "\(.id): \(.title)"' "$PRD_FILE")
  echo "Next: $NEXT"

  cd "$PROJECT_DIR"

  # Run Claude with Nado/Paradex-specific prompt
  PROMPT="You are Ralph working on NADO/PARADEX Grid MM fixes.

1. Read PRD: prd-nado-paradex.json
2. Read progress: progress-nado-paradex.txt
3. Pick highest priority story where passes=false
4. Implement it (focus on scripts/grid_mm_nado_v8.py and scripts/grid_mm_live.py)
5. Run: python3 -m py_compile <changed_files>
6. Commit: git commit -m 'feat: [Story ID] - [Title]'
7. Update prd-nado-paradex.json: set passes=true
8. Append to progress-nado-paradex.txt

Key files:
- scripts/grid_mm_nado_v8.py (Nado Grid MM - ETH)
- scripts/grid_mm_live.py (Paradex Grid MM - BTC)
- dexes/nado/nado_sdk.py (Nado SDK)
- tests/test_dynamic_spread.py (tests)

If ALL stories pass, reply: <promise>COMPLETE</promise>"

  OUTPUT=$(claude --dangerously-skip-permissions --print "$PROMPT" 2>&1 | tee -a "$LOG_FILE") || true

  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo "NADO/PARADEX LOOP COMPLETE!"
    exit 0
  fi

  sleep 5
done

echo "Nado/Paradex loop reached max iterations"
exit 1
