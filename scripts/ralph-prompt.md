# Ralph Agent Instructions - Hibachi Self-Improvement

You are an autonomous coding agent working to improve the Hibachi dual trading strategy until it is profitable.

## Your Task

1. Read the PRD at `prd.json` (project root)
2. Read the progress log at `progress.txt` (check Codebase Patterns section first)
3. Read `LEARNINGS.md` and `PROGRESS.md` for context
4. Pick the **highest priority** user story where `passes: false`
5. Implement that single user story
6. Run quality checks (see below)
7. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
8. Update the PRD to set `passes: true` for the completed story
9. Append your progress to `progress.txt`

## Quality Checks (MUST PASS)

```bash
# Syntax check all Grid MM scripts
python3 -m py_compile scripts/grid_mm_hibachi.py
python3 -m py_compile scripts/grid_mm_nado_v8.py
python3 -m py_compile scripts/grid_mm_live.py

# Run dynamic spread tests
pytest tests/test_dynamic_spread.py -v --tb=short
```

## Progress Report Format

APPEND to progress.txt (never replace, always append):

```
---

## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- Quality check results
- **Learnings for future iterations:**
  - Patterns discovered
  - Gotchas encountered
  - What worked/didn't work
```

## Key Files Reference

| File | Purpose |
|------|---------|
| `scripts/grid_mm_hibachi.py` | Hibachi Grid MM (BTC) - WORKING |
| `scripts/grid_mm_nado_v8.py` | Nado Grid MM (ETH) - NEEDS FIX |
| `scripts/grid_mm_live.py` | Paradex Grid MM (BTC) - NEEDS FIX |
| `hibachi_agent/bot_hibachi.py` | LLM Directional bot |
| `hibachi_agent/execution/fast_exit_monitor.py` | Exit monitoring |
| `tests/test_dynamic_spread.py` | Dynamic spread unit tests |
| `LEARNINGS.md` | Strategy learnings |
| `PROGRESS.md` | Bot configurations |

## Important Context

1. **Hibachi Grid MM is the model** - it uses 30s time-based refresh, which works
2. **Nado/Paradex are broken** - they use price/fill-based refresh, orders go stale
3. **Dynamic spread** is calculated in `_calculate_dynamic_spread()` method
4. **All bots use POST_ONLY** orders to guarantee maker fills
5. **Nado has liquidity issues** - may not get fills even with working code

## Stop Condition

After completing a user story, check if ALL stories have `passes: true`.

If ALL stories are complete, reply with:
<promise>COMPLETE</promise>

If there are still stories with `passes: false`, end your response normally.

## Rules

- Work on ONE story per iteration
- Keep changes focused and minimal
- DO NOT commit broken code
- DO NOT modify code you haven't read
- Follow existing code patterns
- Update LEARNINGS.md if you discover something important
