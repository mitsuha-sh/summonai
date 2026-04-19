# CLAUDE.md Template

## memory_save usage & tagging
Use `memory_save` to persist anything your agent wants to recall. Always attach tags that describe the topic, source, or workflow phase so you can filter later.
```python
memory_save(
    content="Completed the hybrid search doc rewrite.",
    memory_type="semantic",
    category="docs",
    importance=7,
    tags_csv="docs,hybrid-search,outline",
    source_agent="worker3",
    source_cmd="task_629",
)
```
This snippet creates a semantic memory with a tag set you can reference in future calls. Adjust `importance` and `tags_csv` as needed; the tag taxonomy is user-defined and may change per project.

## memory_search example
When you need to recall lessons, combine FTS5 syntax with tags to narrow results.
```python
memory_search(
    query='"hybrid search" NEAR/3 ranking +FTS5',
    tags="docs,hybrid-search",
    min_importance=6,
    top_k=5,
)
```
The query above searches for the phrase `hybrid search` near `ranking`, enforces an importance floor, and restricts hits to the `docs` hierarchy.

## Session Start memory_load example
Use `memory_load` at session startup to hydrate context before executing new commands.
```python
memory_load(
    tags="docs,rule",
    min_importance=5,
    before="2026-04-01T00:00:00",
)
```
This returns the most important doc- or rule-related memories created before April 1, 2026.

## Copy/paste snippet guidance
Include these sections in your CLAUDE.md so other agents know how to interact:
```markdown
## Memory-saving pattern
Describe what to store and which tags to assign.

## Search recipe
Document typical FTS5 queries, tag filters, and importance thresholds.

## Session load plan
List the `memory_load` parameters you expect a caller to use on startup.
```
Edit the markdown above for each project; the template is a launching pad, not a finished guide.
