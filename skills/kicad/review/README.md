# `skills/kicad/review/` — Deep Review Sub-component (v2.0)

This is an internal sub-component of the kicad skill. It is NOT a standalone skill (no nested SKILL.md — confirmed unsupported across Claude Code, Codex, Gemini CLI). Referenced from `skills/kicad/SKILL.md` as a progressive-disclosure link.

## Purpose

This directory hosts the v2.0 Deep Review infrastructure:

- **(a)** The Deep Review evidence gate + `deep_review.schema.json` (spec §3.C/§3.D): per-IC LLM datasheet comparison with durable, evidence-linked findings in `analysis/deep_review.json`.
- **(b)** The optional `design_context` subagent input (spec §3.B): reads schematic + BOM + design intent, emits `design_context.json`. Used as an optional input to the Deep Review pass.
- **(c)** The annotation merge machinery (optional detector-finding triage): `merge_annotations.py` validates `review_annotations.json`, applies `llm_review` overlays to raw analyzer findings, and verifies HI-3 strip round-trip. Authority caps removed in v2.0 — a `suppressed` annotation now always applies its overlay. Quarantined findings remain visible in the merge report.
- **(d)** `_mini_jsonschema.py`: stdlib-only JSON Schema Draft 2020-12 validator (keyword subset used by Layer 2 schemas). No third-party `jsonschema` dep at runtime.

## Structure

| Path | Purpose |
|------|---------|
| `prompts/design_context.md` | Subagent prompt: read schematic + BOM + design intent, emit design_context.json |
| `prompts/domain_task.md` | Shared prompt for every `deep_review_domain` task — one domain slice of the Deep Review pass; describes work for an interactive session, inputs, scope discipline (full coverage, notes are emphasis only), output shape, and the central merge/gate contract |
| `prompts/domain_checklists.json` | Catalog of 8 review domains consumed by `build_review_plan.py` (RF, Ethernet, modem, MCU/memory, power, I/O, interfaces, board EMC) — each entry's starting-point checklist, input artifacts, and finding-category prefix |
| `prompts/synthesis.md` | Describes the post-gate synthesis roll-up artifact (verdict, top risks, verified-OK, not-checked, contradictions) authored after `deep_review_gate.py` runs |
| `schemas/design_context.schema.json` | JSON Schema for design context output |
| `schemas/review_annotations.schema.json` | JSON Schema for review annotations |
| `schemas/deep_review.schema.json` | JSON Schema for Deep Review evidence-gated findings |
| `schemas/synthesis.schema.json` | JSON Schema for the synthesis roll-up artifact |
| `scripts/build_review_plan.py` | Emits `review_plan.json`: an optional `design_context` task plus one `deep_review_domain` task per catalog domain (default: all 8, narrowable via `--domains`) — see "Deep Review dispatch plan" below |
| `scripts/merge_annotations.py` | Validates + applies overlays to raw analyzer JSONs → `analysis/merged/<analyzer>.json` |
| `scripts/validate_review.py` | Standalone CLI for review_annotations.json validation |
| `scripts/deep_review_gate.py` | Evidence gate: validates deep_review.json, writes durable findings |
| `scripts/normalize_deep_review.py` | Adapts plugin-style agent/verifier findings into the canonical `deep_review.json` shape, and validates synthesis artifacts against their `deep_review.json` |
| `scripts/render_report.py` | Standalone static Markdown report renderer; optional self-contained HTML triage view — see "Static report renderer" below |
| `scripts/run_phase4_exercise.py` | Orchestrates the end-to-end fixture exercise |
| `scripts/test_build_review_plan.py` | Unit tests for `build_review_plan.py`'s plan shape, domain catalog, and notes semantics |
| `tests/test_render_report.py` | `unittest` coverage for report-data loading, Markdown/HTML rendering, and CLI output paths |
| `fixtures/*.example.json` | Round-trip fixtures for schema contract tests |

## Deep Review dispatch plan (design_context + domain tasks)

`build_review_plan.py` emits `review_plan.json`: an optional `design_context`
task (spec §3.B) plus one `deep_review_domain` task per domain in
`prompts/domain_checklists.json` — RF, Ethernet, modem, MCU/memory, power,
I/O, interfaces, board EMC (all 8 by default; narrow with `--domains`).

- **No nested Claude process.** `build_review_plan.py` never spawns a
  subprocess, invokes the Claude CLI, or calls an agent API — it only emits
  a JSON description of work. An **interactive Claude Code session** (or
  equivalent interactive agent session) reads the plan and performs each
  task itself.
- **`notes` is emphasis only.** The optional `--notes` / `--notes-file`
  reviewer note is attached verbatim to every task. It directs extra
  attention within each task's full-coverage checklist; it never narrows
  which domains run or truncates a domain's scope. See
  `reviewer_notes_semantics` on the emitted plan and
  `prompts/domain_task.md` → "Scope discipline".
- **Task outputs.** The `design_context` task writes
  `analysis/design_context.json` (unchanged contract). Each
  `deep_review_domain` task writes a bare `findings[]` array fragment to
  `analysis/deep_review/<domain>.json` — never directly to
  `analysis/deep_review.json`.
- **Central merge + gate.** Whoever orchestrates the full review reads
  every `analysis/deep_review/<domain>.json` array, concatenates them into
  one `findings[]` list, writes the full `analysis/deep_review.json`
  document shape (`schema_version`, `produced_for_run_id`, `produced_at`,
  `findings`, `quarantined: []`), and runs `deep_review_gate.py` once,
  centrally (see `prompts/domain_task.md`). This is a separate merge step
  from `merge_annotations.py`, which applies `review_annotations.json`
  overlays to raw Layer 1 analyzer output.
- `scripts/run_phase4_exercise.py` step 3 builds the plan, asserts every
  expected domain task is present, and prints the dispatch instructions
  above — it does not dispatch or run the workflow itself.

## Static report renderer

`render_report.py` is a standalone, stdlib-only renderer for existing review
artifacts. From the repository root:

```bash
python3 skills/kicad/review/scripts/render_report.py --analysis-dir analysis/
python3 skills/kicad/review/scripts/render_report.py --analysis-dir analysis/ --html
python3 skills/kicad/review/scripts/render_report.py --analysis-dir analysis/ --stdout
```

It writes Markdown to `analysis/deep_review_report.md` by default; `--html`
adds a self-contained HTML triage view at `analysis/deep_review_report.html`.
`--output` and `--html-output` override those destinations. `--run` selects
analyzer artifacts through `analysis/manifest.json`; `--project-dir` resolves
relative task result paths in `review_plan.json`; optional artifact paths can
be set explicitly with `--synthesis`, `--review-plan`, `--review-annotations`,
`--merge-report`, and `--design-context`. Missing or malformed artifacts are
listed as gaps rather than treated as clean results.

The renderer only displays the state already recorded by upstream artifacts.
It does not invoke an analyzer, the evidence gate, or verification, and report
generation does not verify a claim. Availability of a PDF, cached extraction,
artifact, or tool is not itself verification; distinguish that availability
from a cited/evidence-checked result and keep skipped or missing evidence as a
gap. The HTML page is static/local: annotations remain in browser local storage
and its downloaded JSON sidecar is informal triage, not a schema-valid review
record or an upstream verification state. Renderer status/exit code is not an
analysis verdict; the evidence gate and report-section checker retain pass/fail
semantics.

When recording model usage, report only input tokens, output tokens, cache-read
tokens, cache-write tokens, and elapsed duration. Do not report currency or
estimated monetary cost. Credential injection is supported through the process
environment; do not commit credentials. The secret-store mechanism remains
undecided.

## Surviving invariants (v2.0)

- **HI-1:** Layer 1 findings are immutable — detector-owned fields are never mutated by any overlay machinery.
- **HI-3:** `strip_llm_overlays(merged)` == raw input, byte-identical. The merge is always reversible.
- **HI-6:** `reviewer_observations[]` lives in `review_annotations.json`, never merged into `findings[]`.
- **No-LLM run yields identical analyzer output:** stripping `llm_*` fields recovers the deterministic baseline.
- **Stable finding identity:** `finding_id` is stable across runs; this invariant extends to Deep Review findings (`deep_review:<12-hex>` form).
- **No hidden suppression:** quarantined/suppressed annotations are visible in the merge report; nothing is silently discarded.

## Retired in v2.0 (spec §5)

The following v1.4 Layer 2 cage constraints are removed. Trust now comes from the Deep Review evidence gate, not permission rules:

- **Overlay-only constraint (HI-2 label):** the merge still only writes `llm_review` siblings, but this is now a design property, not a cage rule.
- **Observations-are-not-findings (HI-6 framing):** still true mechanically; but no longer framed as a restriction.
- **All authority caps and suppression rules (HI-8/HI-9):** `reviewer_observations[]` no longer has a `maxItems` cap; `confidence` is no longer capped at `medium`; `severity` is no longer capped at `warning`; suppressing an `error`-severity or `datasheet-backed` finding now applies normally; the 30% suppression rate cap is removed.
- **Severity tuning matrix:** `severity_tuning.json` and `severity_tuning.schema.json` deleted; `make_finding(design_context=...)` accepts the parameter for call-site compatibility but ignores it.
- **Layer 2 reviewer prompt:** archived to `old/layer2/reviewer.md` (gitignored on disk).
