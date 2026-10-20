"""Standalone, stdlib-first compute toolkit for direct ``.kicad_pcb`` queries.

This package is independent of (and does not import) the analyzer/triage
pipeline (``skills/kicad/scripts/analyze_pcb.py`` and friends), the review
builder, or the kicad-cli module — it is a separate, read-only query layer
over a fresh parse of the raw PCB file, for the cases where a direct
geometric answer (exact routed length, exact keepout polygon, exact
copper-to-copper distance) is more useful than running the full analyzer.
It deliberately reuses the fork's own ``skills/kicad/scripts/sexp_parser.py``
(see ``toolkit/sexp_compat.py``) so there is exactly one S-expression parser
in the repository, not two incompatible ones.

Design rules (see also each module's docstring for specifics):
  * stdlib-only by default; NumPy/SciPy are not used at all (no field
    solver is included — see the scope note below). Shapely is an optional,
    lazily-imported accelerator for one specific case (distance queries
    against very large zone fill polygons) — see ``toolkit.deps``.
  * No venv bootstrapping, no automatic ``pip install`` anywhere in this
    package. If an optional dependency is missing, functions fall back to
    the stdlib path and say so in the result, or raise a plain
    ``ImportError`` with the install command.
  * Every CLI command (``toolkit/cli.py``) prints JSON to stdout; there is
    no ``--out`` file-writing flag.
  * Numbers that are not yet validated against real hardware (e.g. copper
    resistance / IR drop / trace loss from Ohm's law on the parsed
    geometry) are intentionally NOT computed here — only `net_routing`'s
    length/width/layer facts are exposed, which the caller can combine with
    a resistivity model elsewhere if they explicitly want to label that
    assumption themselves.

Modules:
  geometry      — pure-stdlib 2-D primitives (arc discretization, exact
                  segment/polygon distance, shoelace area, pad outlines)
  deps          — lazy optional-dependency detection (``has_shapely()``)
  sexp_compat   — loads the fork's own scripts/sexp_parser.py
  model         — ``parse_pcb()`` -> ``Board`` dataclass (footprints, pads,
                  tracks/arcs incl. net-assigned gr_* graphic copper, vias,
                  zones/keepouts, Edge.Cuts outline)
  queries       — ``stackup_summary``, ``component_inventory``,
                  ``net_routing``, ``keepout_summary``, ``edge_distance``,
                  ``net_clearance``, ``analyzer_json_overlap``
  cli           — ``python -m toolkit <command>``, prints JSON to stdout
"""

__all__ = ["geometry", "deps", "sexp_compat", "model", "queries", "cli"]
