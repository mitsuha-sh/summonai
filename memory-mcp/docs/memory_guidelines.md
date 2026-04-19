# Memory Save Guidelines (Short)

## 1) bucket
- `code`: Coding rules, architecture decisions, bug fix patterns, test tactics.
  - Example: "For failing flake tests, isolate random seeds before retry."
- `knowledge`: Project policy, operating principles, session summaries, domain facts, people/org context.
  - Example: "Release checklist requires security review before merge."
- `content`: Draft ideas, story seeds, article outlines, writing material.
  - Example: "Post idea: why event-driven inbox beat polling in agent ops."

## 2) importance (1-10)
- `1-3`: Disposable detail, low reuse, expires quickly.
- `4-6`: Useful local lesson, moderate reuse for one project.
- `7-8`: Team-level rule or repeated failure pattern worth preserving.
- `9-10`: Critical policy, safety rule, or high-cost lesson to never forget.

## 3) memory_type
- `episodic`: What happened in a specific task/session.
- `semantic`: Stable facts, definitions, decisions, references.
- `procedural`: Repeatable steps, playbooks, checklists.
- `idea`: Hypotheses, proposals, future options not yet validated.

## 4) tags
- Tags are **search keywords**, not bucket aliases. Never use a bucket name (`code`, `knowledge`, `content`) as a tag.
- Use short, descriptive labels: `policy`, `architecture`, `lesson`, `bug-pattern`, `naming`, etc.
- 2-4 tags per memory. More than 5 is noise.

## 5) confidence (0.0-1.0)
- `0.9-1.0`: Verified fact, tested fix, confirmed policy.
- `0.6-0.8`: Strong belief based on evidence, but not fully verified.
- `0.3-0.5`: Hypothesis, educated guess, or single-instance observation.
- `0.0-0.2`: Speculation, unvalidated idea.

## 6) Save / Don't Save
- Save:
  - Repeated mistakes with clear prevention steps.
  - Decisions that affect future implementation or operations.
  - Reusable procedures and proven troubleshooting patterns.
- Don't Save:
  - Raw chat transcripts without distilled insight. **Always distill to a concise lesson or fact before saving.**
  - Temporary status updates ("running tests now", "brb", etc.).
  - Duplicates of existing memories with no new signal.
  - Conversation logs as-is — these belong in L1 (conversation_save), not L2 (memory_save).
