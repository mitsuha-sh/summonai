# Report: Bloom-based Executor Model Routing (Phase 1) — Task 019

## PR URLs

- **task-mcp PR**: https://github.com/mitsuha-sh/summonai-task-mcp/pull/19
- **summonai PR**: https://github.com/mitsuha-sh/summonai/pull/18

## Migration Procedure

Migration `V006_add_bloom_level_executor.sql` is automatically applied on server startup via `ensure_schema()`. No manual intervention required.

```sql
ALTER TABLE tasks ADD COLUMN bloom_level INTEGER NOT NULL DEFAULT 3;
ALTER TABLE tasks ADD COLUMN executor TEXT;
```

Existing tasks receive `bloom_level=3` (Apply) and `executor=NULL` (auto-select), preserving existing behavior.

## Selection Logic (Pseudocode)

```python
def select_model_tier(bloom_level, executor, tiers):
    # Filter by executor name if specified
    candidates = [t for t in tiers if t.executor == executor] if executor else tiers

    # Find tiers that cover the bloom level
    covering = [t for t in candidates if t.max_bloom >= bloom_level]

    if covering:
        # Cheapest = smallest max_bloom
        return min(covering, key=lambda t: t.max_bloom), is_gap=False
    else:
        # Coverage gap: use largest max_bloom as fallback
        fallback_pool = candidates if candidates else tiers
        return max(fallback_pool, key=lambda t: t.max_bloom), is_gap=True

def build_executor_command(tier, runners, is_gap, bloom_level):
    if tier is None:
        return "claude --dangerously-skip-permissions"  # no config
    if is_gap:
        WARN to stderr: "bloom_level={} exceeds max_bloom ..."
    runner = runners.get(tier.executor) or runners.get("default")
    if runner:
        return runner.template.replace("{model}", tier.model)
    return f"claude --model {tier.model} --dangerously-skip-permissions"
```

## Manual Verification: Sample capability_tiers Model Selection

Given `config/executors.toml.example` with haiku (max_bloom=3), sonnet (max_bloom=5), opus (max_bloom=6):

| bloom_level | executor  | Selected tier | is_gap | Command                                                    |
|-------------|-----------|---------------|--------|------------------------------------------------------------|
| 2           | None      | haiku         | False  | `claude --model claude-haiku-4-5-20251001 ...`             |
| 3           | None      | haiku         | False  | `claude --model claude-haiku-4-5-20251001 ...`             |
| 4           | None      | sonnet        | False  | `claude --model claude-sonnet-4-6 ...`                     |
| 6           | None      | opus          | False  | `claude --model claude-opus-4-7 ...`                       |
| 7           | None      | opus          | True   | WARN + `claude --model claude-opus-4-7 ...`                |
| 3           | "sonnet"  | sonnet        | False  | `claude --model claude-sonnet-4-6 ...`                     |
| 6           | "haiku"   | haiku         | True   | WARN + `claude --model claude-haiku-4-5-20251001 ...`      |
| 3           | None      | (no config)   | N/A    | `claude --dangerously-skip-permissions` (legacy)           |

## Test Results

- 77 pre-existing tests: all pass
- 20 new tests added in `tests/test_server.py`:
  - `test_select_model_tier_*` (6 cases): unit tests for tier selection logic
  - `test_build_executor_command_*` (4 cases): command building with gap WARN
  - `test_task_create_stores_bloom_level_and_executor`: DB storage verified
  - `test_task_create_default_bloom_level`: default value = 3
  - `test_task_create_rejects_invalid_bloom_level`: bloom_level=7 rejected
  - `test_load_executors_config_*` (3 cases): TOML loading from env/dir/missing
  - `test_spawn_uses_bloom_model_selection`: integration test with haiku selection
  - `test_spawn_bloom_gap_uses_fallback`: integration test with gap fallback
  - `test_schema_includes_bloom_level_executor`: migration V006 applied

Total: **97 tests, all passing**.

## Files Changed

### task-mcp

- `src/summonai_task/db/migrations/V006_add_bloom_level_executor.sql` — new migration
- `src/summonai_task/server.py` — TOML loading, selection logic, command building, updated API
- `tests/test_server.py` — 20 new tests

### summonai

- `config/executors.toml.example` — public TOML template (no absolute paths / personal data)
- `setup.sh` — generates `.summonai/executors.toml`, passes `SUMMONAI_EXECUTORS_CONFIG` env var to MCP
- `README.md` — Bloom routing section added near Persona
- `task-mcp` — submodule pointer updated to bloom-routing commit
- `reports/019_bloom_routing_phase1.md` — this report
