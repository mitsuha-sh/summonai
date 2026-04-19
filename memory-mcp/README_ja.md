# summonai-memory-mcp

SQLite + FTS5 + sqlite-vec のハイブリッド検索を提供する、オープンソースエージェント向けの Memory MCP server です。
単一の SQLite backend で、高速な全文検索、vector similarity、設定可能な ranking を実現します。

## Features
1. メモリ本文、カテゴリ、source context に対する **FTS5 full-text search**（trigram tokenizer）。
2. `cl-nagoya/ruri-v3-130m` embeddings を使った `sqlite-vec` ベースの **vector similarity search**。
3. FTS ヒット、vector distance、effective importance scoring を組み合わせる **RRF hybrid ranking**。
4. tags、importance threshold、bi-temporal window による **filtering**。
5. 履歴推論を支える **bi-temporal management**（valid_from/valid_until）。
6. 新規メモリ保存時の **automatic embedding generation**（vector storage が使えない場合は graceful degradation）。

## Prerequisites
- Python 3.10 以上
- `pip`（Python 依存関係のインストール用）
- Claude Code CLI（`claude`）が PATH で利用可能

## Quick Start
最短導入（推奨）は `setup.sh` を使う方法です。

```bash
git clone https://github.com/mitsuha-sh/summonai-memory-mcp.git
cd summonai-memory-mcp
bash setup.sh
```

`setup.sh` では次を自動実行します。
- `.venv` が無ければ作成
- `requirements.txt` の依存インストール
- MCP サーバーを user scope で登録（`claude mcp add -s user`）

`setup.sh` を使わない手動手順:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
claude mcp add summonai-memory-mcp -- \
  "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

動作確認:

```bash
claude mcp list | rg summonai-memory-mcp
```

persona テンプレートの初期化:

```bash
cd persona
cp USER.md.example USER.md
cp SOUL.md.example SOUL.md
# Edit to customize
```

## Global Registration（グローバル登録）
`~/.claude.json` で MCP を管理する場合は、次のように追加します。

```json
{
  "mcpServers": {
    "summonai-memory-mcp": {
      "command": "/path/to/summonai-memory-mcp/.venv/bin/python",
      "args": [
        "/path/to/summonai-memory-mcp/server.py"
      ],
      "env": {
        "SUMMONAI_MEMORY_DB": "/path/to/summonai_memory.db"
      }
    }
  }
}
```

パスは絶対パスを使い、編集後は Claude Code を再起動してください。

## Project-scoped Usage（プロジェクト別利用）
`setup.sh` は引数で project ディレクトリを受け取れます。

```bash
# user-global 登録（複数プロジェクトで共有）
bash setup.sh

# project スコープ登録（プロジェクトごとに分離）
bash setup.sh /abs/path/to/your-project
```

使い分けの目安:
- `scope_type=user`: 同一ユーザー内でプロジェクト横断の会話履歴を共有したい場合
- `scope_type=project`: プロジェクトごとに履歴を分離したい場合
- `scope_id`: 安定した識別子（例: 絶対パス、固定 slug）を使う

`conversation_save` / `conversation_load_recent` 呼び出し時は、同じ `scope_type` と `scope_id` を渡してください。

## Configuration
`SUMMONAI_MEMORY_DB` に利用する SQLite file のパスを設定してください。未設定の場合は、コードと同じ階層に
`db/summonai_memory.db` が作成されます。

`claude mcp` 登録用 `settings.json` の例:

```json
{
  "mcpServers": {
    "summonai-memory-mcp": {
      "command": "/path/to/.venv/bin/python",
      "args": [
        "/path/to/summonai-memory-mcp/server.py"
      ],
      "env": {
        "SUMMONAI_MEMORY_DB": "/path/to/summonai_memory.db"
      }
    }
  }
}
```

`/path/to/*` のプレースホルダーは、実際の workspace 構成に合わせて調整してください。

## SessionStart Hook Setup
Claude Code 起動時にメモリ文脈を自動で読み込むには、`scripts/session_start_memory_context.sh` を設定します。

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "bash /abs/path/to/summonai-memory-mcp/scripts/session_start_memory_context.sh"
      }
    ]
  }
}
```

内部では `scripts/session_start_memory_context.py` を呼び出し、runtime identity は次の優先順で解決します。
`environment > .summonai/memory.toml > tmux option/payload/cwd defaults`

project-local config は任意です。各 project で `.summonai/memory.toml.example` を `.summonai/memory.toml` にコピーして編集します。

```toml
agent_id = "default"
project = "example-project"
scope_type = "project"
scope_id = "example-project"
persona_dir = "/absolute/path/to/personas/default"
```

環境変数で上書きできます。
- `SUMMONAI_MEMORY_CONFIG`: config file path を明示
- `SUMMONAI_AGENT_ID`: `agent_id` を上書き
- `SUMMONAI_PROJECT`: `project` を上書き
- `SUMMONAI_SCOPE_TYPE` / `SUMMONAI_SCOPE_ID`: scope metadata を上書き
- `SUMMONAI_PERSONA_DIR`: `persona_dir` を上書き

persona を共有する場合は、`USER.md` と `SOUL.md` を単一ディレクトリに置き、各 project の `persona_dir` から同じ場所を参照します。

## Stop Hook Setup
セッション終了時に `conversation_save` へ自動保存するには、`scripts/stop_hook_conversation_save.sh` を設定します。

```json
{
  "hooks": {
    "Stop": [
      {
        "type": "command",
        "command": "bash /abs/path/to/summonai-memory-mcp/scripts/stop_hook_conversation_save.sh"
      }
    ]
  }
}
```

Python ブリッジを直接使う場合:

```bash
cat stop_event.json | python scripts/stop_hook_conversation_save.py
```

想定 JSON field:
- `session_id`（または `conversation_id`）
- `agent_id`（または `agent`）
- `project`（任意）
- `task_id` または `cmd_id`（任意）
- `ended_at` または `timestamp`（任意。未指定時は現在時刻）
- `transcript`（文字列）または `messages`（配列）
- `transcript_path` または `transcriptPath`（任意 JSONL パス）

transcript 復元の優先順:
`transcript > messages > transcript_path > last_assistant_message`

## Available Tools
server は次の tools（`mcp.tool()`）を公開します。
- `memory_search(query, memory_type, tags, top_k, min_importance, include_invalid, after, before)` - FTS5 + vector rerank + filtering のハイブリッド検索。
- `memory_save(content, memory_type, category, importance, emotional_impact, source_context, source_agent, source_cmd, tags_csv)` - メモリ保存と embedding 自動生成。
- `memory_invalidate(memory_id, reason)` - メモリ無効化と理由メタデータ追記。
- `memory_stats()` - 件数・タグ・平均値などの JSON 統計。
- `memory_load(min_importance, tags, memory_type, after, before)` - 起動時向けメモリ文脈をプレーンテキストで取得。
- `conversation_save(session_id, agent_id, project, task_id, transcript, ended_at, scope_type, scope_id)` - 会話チャンクを重複排除つきで保存。
- `conversation_load_recent(agent_id, project, scope_type, scope_id, limit_chunks, since_days)` - 直近チャンクを読み込んで文脈復元。`agent_id` は必須、`project` は任意です。`project` と scope filter を省略すると、同じ agent の会話を project 横断で取得します。

## CLAUDE.md Guide
呼び出し側で memory に保存したい prompt や persona cue は、`docs/CLAUDE_TEMPLATE.md` に沿って記述してください。
このテンプレートには `memory_save` の呼び出し方、tag 運用、セッション開始時の memory 再読み込み方法、tag taxonomy が user-defined である理由が記載されています。

## Schema
- `memories`: コア table（type、category、content、source metadata、importance、emotional_impact、bi-temporal timestamps、confidence）。
- `tags`: memory ごとの many-to-many tags。
- `action_triggers`: alert、command suggestion、dashboard update などを行う rule 定義。
- `decision_patterns`: 観測された状況に対する reasoning snippet の保存。
- `memories_fts`: 高速 text search 用に trigger で同期される FTS5 virtual table。
- `memories_vec`: vector retrieval 用 512-d embeddings を保持する `sqlite-vec` table。
- `conversation_sessions`: stop hook 自動保存された会話セッションのメタ情報。
- `conversation_chunks`: 分割済み transcript（`(session_id, chunk_index)` と `(session_id, chunk_hash)` で冪等性担保）+ `scope_type/scope_id` による分離。
- `conversation_chunks_fts`: 会話検索用の FTS5 trigram index（trigger 同期）。

## License
MIT
