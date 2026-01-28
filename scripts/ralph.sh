#!/bin/bash
# Ralph - Autonomous AI agent loop for Hibachi self-improvement
# Usage: ./scripts/ralph.sh [max_iterations]

set -e

MAX_ITERATIONS=${1:-10}
PROJECT_DIR="/Users/admin/Documents/Projects/pacifica-trading-bot"
PRD_FILE="$PROJECT_DIR/prd.json"
PROGRESS_FILE="$PROJECT_DIR/progress.txt"
PROMPT_FILE="$PROJECT_DIR/scripts/ralph-prompt.md"

echo "========================================================"
echo "  RALPH - Hibachi Self-Improvement Loop"
echo "========================================================"
echo "Max iterations: $MAX_ITERATIONS"
echo "PRD: $PRD_FILE"
echo "Progress: $PROGRESS_FILE"
echo ""

# Check prerequisites
if [ ! -f "$PRD_FILE" ]; then
  echo "Error: prd.json not found at $PRD_FILE"
  exit 1
fi

if ! command -v jq &> /dev/null; then
  echo "Error: jq not installed. Run: brew install jq"
  exit 1
fi

if ! command -v claude &> /dev/null; then
  echo "Error: claude CLI not installed. Run: npm install -g @anthropic-ai/claude-code"
  exit 1
fi

# Show current PRD status
echo "Current PRD status:"
jq -r '.userStories[] | "  \(.id): \(.title) - passes: \(.passes)"' "$PRD_FILE"
echo ""

# Check if all stories already pass
REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
if [ "$REMAINING" -eq 0 ]; then
  echo "All stories already pass! Nothing to do."
  exit 0
fi
echo "Stories remaining: $REMAINING"
echo ""

for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo "========================================================"
  echo "  Ralph Iteration $i of $MAX_ITERATIONS"
  echo "========================================================"

  # Check remaining stories
  REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
  if [ "$REMAINING" -eq 0 ]; then
    echo ""
    echo "All stories complete!"
    exit 0
  fi

  # Get next story
  NEXT_STORY=$(jq -r '[.userStories[] | select(.passes == false)] | sort_by(.priority) | .[0] | "\(.id): \(.title)"' "$PRD_FILE")
  echo "Next story: $NEXT_STORY"
  echo ""

  # Run Claude with the prompt
  cd "$PROJECT_DIR"
  OUTPUT=$(claude --dangerously-skip-permissions --print "$(cat "$PROMPT_FILE")" 2>&1 | tee /dev/stderr) || true

  # Check for completion signal
  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo ""
    echo "========================================================"
    echo "  RALPH COMPLETE - All tasks finished!"
    echo "========================================================"
    exit 0
  fi

  echo ""
  echo "Iteration $i complete. Sleeping 5s before next..."
  sleep 5
done

echo ""
echo "========================================================"
echo "  Ralph reached max iterations ($MAX_ITERATIONS)"
echo "========================================================"
echo "Check progress.txt for status."
REMAINING=$(jq '[.userStories[] | select(.passes == false)] | length' "$PRD_FILE")
echo "Stories remaining: $REMAINING"
exit 1
