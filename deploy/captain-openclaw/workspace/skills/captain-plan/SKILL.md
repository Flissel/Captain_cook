---
name: captain-plan
description: Plan a project for Captain - draft a SuppliedPlan, pass check_constitution and compile_plan, then publish it with the operator's consent. Use whenever the operator asks to plan, re-plan or split up work for Captain.
---

# Planning for Captain

<!-- Captain-owned: the seed service overwrites this file on every start. -->

Captain accepts one JSON object, the `plan` argument of `check_constitution`,
`compile_plan` and `publish_plan`. All three take the same plan; Captain
recompiles it on publish, so publish exactly the plan that compiled.

## Plan shape

```json
{
  "project_description": "What the whole project delivers, in full sentences.",
  "subtasks": [
    {"subtask_id": "s1", "description": "One concrete, checkable step", "capability_tags": ["python"]}
  ],
  "alignment": {
    "batches": [
      {"batch_id": "collect-data", "title": "Collect data", "subtask_ids": ["s1"], "depends_on": [], "target": null}
    ]
  },
  "enrichments": {
    "collect-data": {
      "goal": "What this batch must deliver",
      "constraints": [],
      "capability_tags": ["python"],
      "acceptance_criteria": [
        {"assertion_id": "run-succeeds", "kind": "status_equals", "expected": "succeeded"}
      ],
      "golden_cases": [],
      "holdout_cases": [
        {"case_id": "hidden-1", "input": {}, "expected_observations": {}}
      ]
    }
  }
}
```

No other fields are accepted anywhere.

## Rules Captain enforces

Constitution (`check_constitution`; `publish_plan` refuses on failure):

- Every subtask description and every batch `goal`: at least 8 and at most
  4000 characters, and **not longer than `project_description`**. Write a
  detailed project description so the parts can be specific.
- Every subtask and every enrichment has 1 to 20 non-empty
  `capability_tags`.
- No two subtasks, and no two batch goals, may be identical (ignoring case
  and whitespace).
- At most the constitution's subproblem budget (default 200) subtasks and
  batches.
- The semantic scope/quality review is not run by this tool. Still plan
  only work that is in scope for the project and free of prohibited topics.

Compilation (`compile_plan`):

- Every subtask id appears in exactly one batch; `depends_on` names only
  other batch ids and has no cycles.
- `batch_id`: lowercase letters, digits, `-`, at most 32 characters, starting
  with a letter or digit. `assertion_id` and `case_id`: same alphabet, at
  most 64 characters.
- `enrichments` has exactly one entry per batch id, no more, no fewer.
- `capability_tags` must come from the server's allowed set; an "unknown
  capability tags" error lists the offending tags. No duplicate tags.
- `target` is optional; when set it must be an allowed target, otherwise
  the server's default target applies.
- Acceptance assertion kinds: `output_equals`, `output_contains`
  (both need `path` and `expected`), `status_equals`, `schema_matches`
  (need `expected`), `side_effect_observed`.
- At least one acceptance assertion and one holdout case per batch.
  Holdout case ids are unique, must not reuse a golden case id, and no
  holdout case may have the same content as any golden case in the whole
  plan.

## Procedure

1. Write `project_description` first, completely. Split into subtasks that
   a reviewer could check one by one; group them into batches that one
   worker can deliver and test in one go; order batches with `depends_on`.
2. Call `check_constitution`. For each line in `rejections` (`<id>: <reason>:
   <detail>`; batch goals appear as `batch:<batch_id>`), fix that item.
   Repeat until `accepted` is true.
3. Call `compile_plan`. Fix the input named in any error and repeat until
   it returns batches.
4. Summarise for the operator: each batch id, title, depends_on, target,
   capability tags and holdout case **count**. Never show holdout contents.
5. Ask: "Publish these N batches? They will wait for your approval." Call
   `publish_plan` only after a clear yes.
6. `publish_plan` refusing a batch id as already present means part of the
   plan is in the ledger. Check with `get_batch`, report what exists, and
   ask the operator before choosing new batch ids.
