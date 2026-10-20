# Deep Review Synthesis Artifact

After `deep_review_gate.py` has gated a review's findings (stamped
`finding_id`s on everything in `findings[]`), produce a synthesis artifact
that rolls the durable result up for a human reader: one verdict, the risks
that matter most, what was checked and found fine, what could not be
checked, and any findings that contradict each other. This is read-only
roll-up content — author it by hand (or have an agent author it), then
validate it:

```bash
python3 skills/kicad/review/scripts/normalize_deep_review.py \
    validate-synthesis analysis/synthesis.json analysis/deep_review.json
```

The validator checks the artifact against
`skills/kicad/review/schemas/synthesis.schema.json` and cross-references
every `finding_id` it cites against the gated `deep_review.json`
`findings[]` array. It never edits or deletes anything in the artifact —
only schema violations and unresolved references are reported.

## Shape

```json
{
  "schema_version": "1.0",
  "produced_for_run_id": "<same run id as the deep_review.json it summarizes>",
  "produced_at": "2026-10-19T00:00:00Z",
  "verdict": {"status": "ready|needs_review|blocked", "summary": "one paragraph"},
  "top_risks": [{"finding_id": "deep_review:<hex>", "reason": "why this one matters most"}],
  "verified_ok": [{"finding_id": "deep_review:<hex>", "note": "optional"}, {"note": "board-wide check with no single finding"}],
  "not_checked": [{"reason": "no datasheet for U9 — unverified", "finding_id": "optional"}],
  "contradictions": [{"finding_ids": ["deep_review:<a>", "deep_review:<b>"], "description": "what conflicts"}]
}
```

## Rules

- `top_risks` and `contradictions` entries MUST cite at least one durable
  `finding_id` — a risk or a contradiction that isn't anchored to a finding
  the gate actually kept isn't a risk/contradiction, it's a new finding;
  put it through the normal Deep Review pass instead.
- `verified_ok` and `not_checked` entries MAY omit `finding_id` when the
  confirmation/gap isn't tied to one specific finding, but every
  `finding_id` that IS present must resolve against `deep_review.json`.
- Quarantined findings (`deep_review.json` `quarantined[]`) have no stamped
  `finding_id` — they cannot be cited here. Describe them in prose in
  `not_checked` or `verdict.summary` instead if they matter to the verdict.
- Never silently drop a disagreement between agents/passes — if two
  findings conflict, that's a `contradictions` entry, not a decision one of
  them is right made unilaterally by whoever writes this file.
