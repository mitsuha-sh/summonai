# Task 020 — Bloom Routing Fix Report

## 修正内容

### 設計欠陥の内容
task 019 で導入した bloom routing では `[[capability_tiers]].executor` フィールドが
tier ニックネーム（haiku/sonnet/opus）として使われており、「CLIツール種別」という本来の意味と矛盾していた。

### 修正方針
`executor` = CLI ツール名（claude / codex / opencode）に意味論を統一。
同一 executor 内で複数 tier を `max_bloom` で区別する構造に変更。

---

## 変更詳細

### config/executors.toml.example（summonai リポ）

- `[[capability_tiers]].executor` を `"claude"` に統一（3 tier: haiku / sonnet / opus モデル）
- `[runners.default]` → `[runners.claude]` に変更。コメントで `[runners.codex]` 例を追加
- `[defaults]` セクションを新規追加:
  ```toml
  [defaults]
  bloom_level = 3
  # executor = "claude"
  ```

### task-mcp: _load_executors_config

- `[defaults]` セクションを読み込み、`defaults` キーで返す
- `config_loaded` フラグを追加（ファイルが実在した場合 `True`）

### task-mcp: task_create（初回コミット + Codex P1 修正）

- **シグネチャ**: `bloom_level: int = 3` → `bloom_level: int | None = None`（sentinel 方式）
- **defaults 適用**: `bloom_level is None` のときのみ `defaults.bloom_level` を適用、なければ 3 を確定
  - ⚠️ 初回実装では `bloom_level == 3` で判定していたため、明示的に 3 を渡した場合も上書きされるバグがあった。Codex P1 指摘により sentinel 方式に修正。
- **executor**: `executor is None` かつ `defaults.executor` が設定されていれば適用（変更なし）
- **未知 executor 拒否**: `config_loaded=True` のときのみ、`capability_tiers` に存在しない executor 名を ValueError で拒否
- エラーメッセージに利用可能な executor 名一覧を含む

---

## 未知 executor 拒否の動作

```python
# executors.toml が存在する場合
task_create(..., executor="codex")  # capability_tiers に "codex" がなければ:
# ValueError: Unknown executor: 'codex'. Available executors: ['claude']

# executors.toml が存在しない場合（レガシー環境）
task_create(..., executor="any-tool")  # OK — 検証スキップ
```

---

## defaults 適用の動作

```toml
# .summonai/executors.toml
[defaults]
bloom_level = 5
executor = "claude"
```

```python
task_create(title=..., ...)          # bloom_level 未指定 → bloom_level=5, executor="claude"
task_create(title=..., bloom_level=3)  # 明示的に 3 を指定 → bloom_level=3（defaults に上書きされない）
```

---

## テスト結果

追加テスト 14 件（既存 81 件 + 追加 14 件 = 計 95 件、全件 pass）:

| テスト | 内容 |
|--------|------|
| `test_select_model_tier_same_executor_bloom2_picks_haiku` | executor=claude + bloom_level=2 → haiku |
| `test_select_model_tier_same_executor_bloom4_picks_sonnet` | executor=claude + bloom_level=4 → sonnet |
| `test_select_model_tier_same_executor_bloom5_picks_sonnet` | executor=claude + bloom_level=5 → sonnet |
| `test_select_model_tier_same_executor_bloom6_picks_opus` | executor=claude + bloom_level=6 → opus |
| `test_load_executors_config_reads_defaults` | [defaults] 読み込み確認 |
| `test_load_executors_config_missing_config_loaded_false` | 設定なし → config_loaded=False |
| `test_task_create_applies_defaults_bloom_level` | 未指定 → defaults.bloom_level 適用 |
| `test_task_create_applies_defaults_executor` | 未指定 → defaults.executor 適用 |
| `test_task_create_rejects_unknown_executor_when_config_loaded` | 未知 executor → ValueError |
| `test_task_create_unknown_executor_error_lists_available` | エラーに一覧含む |
| `test_task_create_accepts_unknown_executor_without_config` | 設定なし → 任意 executor OK |
| `test_task_create_explicit_bloom3_not_overridden_by_defaults` | **明示的 bloom_level=3 は defaults で上書きされない** |
| `test_task_create_unspecified_bloom_gets_defaults` | 未指定の場合は defaults が適用される |

---

## PR URLs

- **task-mcp**: https://github.com/mitsuha-sh/summonai-task-mcp/pull/20
- **summonai**: https://github.com/mitsuha-sh/summonai/pull/19
