# Deep Review Domain Task

This is the shared prompt for every `deep_review_domain` task emitted by
`build_review_plan.py`. It describes **work for an interactive Claude Code
session** (or an equivalent interactive agent session) reading the design
directly — nothing here spawns a subprocess, calls an agent API, or runs
unattended. Read the dispatched task object from `review_plan.json` (the
`tasks[]` entry whose `task_id` matches the one you were pointed at) for the
specifics: `domain`, `title`, `checklist`, `input_artifacts`, `result_path`,
`result_schema`, and `notes`.

## What this task is

One domain slice of the Deep Review pass described in
`skills/kicad/SKILL.md` → "Deep Review Pass" and detailed in
`skills/kicad/references/deep-review.md`. That reference is the methodology
of record — this prompt and the `checklist` field on your task are a
starting-point lens for **this domain**, not a replacement for it. Read
`deep-review.md` before starting if you have not already read it in this
session.

## Inputs

- `analysis/review_coverage.json` — the shared datasheet coverage roster
  (candidates / missing / excluded MPN-bearing components). This is a
  plan-level prerequisite (see the plan's `prerequisites[]` entry emitted by
  `build_review_plan.py`), not something this task builds itself: before the
  Vibe datasheet fetch and before any task — including this one — is
  dispatched, the orchestrating session runs it once with
  `python3 skills/datasheets/scripts/review_coverage.py
  analysis/schematic.json [analysis/pcb.json] --output
  analysis/review_coverage.json`. If it's missing when you start this task,
  say so and request it be run rather than re-deriving the roster yourself.
- The analyzer JSON paths listed in your task's `input_artifacts` (schematic,
  PCB, EMC, thermal, cross-domain — whichever apply to this domain). A path
  that doesn't exist means that analyzer hasn't been run yet or doesn't apply
  (e.g. no PCB yet) — say so in your findings rather than failing silently.
- `datasheets/extracted/<MPN>.json` (cache) or `datasheets/<MPN>.pdf`
  (fallback) for every IC relevant to this domain.
- `analysis/design_context.json`, if present — steers priority, doesn't gate
  scope (see "Scope discipline" below).
- `.kicad-happy.json`, if present — user-declared design intent.

## Scope discipline

1. **The `checklist` field is a starting point, not a ceiling.** It names
   the highest-value checks for this domain so you don't start from a blank
   page; derive additional checks from each part's actual datasheet, same as
   the main Deep Review loop.
2. **Stay inside your domain, but cover it completely.** Every component,
   net, and pin that plausibly belongs to this domain gets checked — not a
   sample. Don't stop at the first instance of a pattern when the design has
   several (e.g. two independent I2C buses, three regulators).
3. **`notes` is emphasis, never a scope filter.** When your task carries a
   `notes` field, treat it as "pay extra attention to this" layered on top of
   full domain coverage — never as "only check this." An empty/absent
   `notes` field means no extra emphasis; it does not reduce scope.
4. If your domain doesn't apply to this design at all (e.g. `rf` on a board
   with no RF components), say so explicitly in one line rather than
   fabricating findings or silently producing an empty result.

## Output

Write a JSON array of finding objects to `result_path` — each object matches
the shape of one entry in `findings[]` from
`skills/kicad/review/schemas/deep_review.schema.json` (the fragment your
task's `result_schema` points at with a `#/...` JSON Pointer): `detector`
(always the literal string `"deep_review"`), `category` (use the task's
`category_hint` as the prefix — e.g. `"power_input"`, `"power_sequencing"` for
the `power` domain — stay inside one small stable vocabulary per project),
`severity`, `confidence`, `summary`, and an `evidence` block with at least one
design anchor (component/net/pin) and at least one source (datasheet quote
and/or computation). Omit `finding_id` — the gate stamps it. This mirrors the
"Fan-out for big BOMs" dispatch shape in `deep-review.md`: a bare findings
array per dispatched unit, concatenated centrally afterward.

Do not write directly to `analysis/deep_review.json` from this task — that
file is assembled once, after every dispatched domain task (and the main-loop
pass, if one is also running) has produced its array. Whoever is
orchestrating the full review:

1. Reads every `analysis/deep_review/<domain>.json` array produced by these
   tasks.
2. Concatenates them into one `findings[]` list.
3. Writes the full document shape (`schema_version`, `produced_for_run_id`,
   `produced_at`, `findings`, `quarantined: []`) to `analysis/deep_review.json`.
4. Runs `deep_review_gate.py` once, centrally, per `deep-review.md`.

## Evidence quality

Same bar as the main Deep Review loop — no exceptions for being a narrower
slice:

- Datasheet quotes must be verbatim from the cited page (elision with `...`
  is fine; each segment must still appear on the page, in order).
- Computations cite a helper script path (`analysis/helpers/check_<topic>.py`)
  and paste its printed result line.
- When no datasheet is available, record an info finding
  `"<REF> unverified — no datasheet available"` and move on — never silently
  skip a component because it lacks a datasheet.

See `skills/kicad/references/deep-review.md` for the full comparison
heuristics by part class, interacting-pair checks, and the degradation table.
