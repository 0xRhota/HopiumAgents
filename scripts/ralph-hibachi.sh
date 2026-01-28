#!/bin/bash
# Ralph Loop 1: Hibachi Improvements
# Usage: ./scripts/ralph-hibachi.sh [max_iterations]

set -e

MAX_ITERATIONS=${1:-10}
PROJECT_DIR="/Users/admin/Documents/Projects/pacifica-trading-bot"
PRD_FILE="$PROJECT_DIR/prd-hibachi.json"
PROGRESS_FILE="$PROJECT_DIR/progress-hibachi.txt"
LOG_FILE="$PROJECT_DIR/logs/ralph-hibachi.log"

echo "========================================================"
echo "  RALPH LOOP 1: HIBACHI IMPROVEMENTS"
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
  echo "  HIBACHI Iteration $i of $MAX_ITERATIONS"
  echo "========================================================"

  REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
  if [ "$REMAINING" -eq 0 ]; then
    echo "All Hibachi stories complete!"
    exit 0
  fi

  NEXT=$(jq -r '[.userStories[] | select(.passes == false)] | sort_by(.priority) | .[0] | "\(.id): \(.title)"' "$PRD_FILE")
  echo "Next: $NEXT"

  cd "$PROJECT_DIR"

  # Run Claude with Hibachi-specific prompt
  PROMPT="You are Ralph working on HIBACHI improvements.

1. Read PRD: prd-hibachi.json
2. Read progress: progress-hibachi.txt
3. Pick highest priority story where passes=false
4. Implement it (focus on hibachi_agent/ and scripts/grid_mm_hibachi.py)
5. Run: python3 -m py_compile <changed_files>
6. Commit: git commit -m 'feat: [Story ID] - [Title]'
7. Update prd-hibachi.json: set passes=true
8. Append to progress-hibachi.txt

Key files:
- scripts/grid_mm_hibachi.py (Grid MM)
- hibachi_agent/bot_hibachi.py (LLM bot)
- hibachi_agent/execution/fast_exit_monitor.py (exit logic)
- dexes/hibachi/hibachi_sdk.py (SDK)

If ALL stories pass, reply: <promise>COMPLETE</promise>"

  OUTPUT=$(claude --dangerously-skip-permissions --print "$PROMPT" 2>&1 | tee -a "$LOG_FILE") || true

  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo "HIBACHI LOOP COMPLETE!"
    exit 0
  fi

  sleep 5
done

echo "Hibachi loop reached max iterations"
exit 1
