"""CLI for the standalone kicad-happy compute toolkit.

    python -m skills.kicad.toolkit <command> [args]      (from the repo root)
    python -m toolkit <command> [args]                    (from skills/kicad/)

All commands take ``--pcb <file.kicad_pcb>`` and print one JSON object/array
to stdout — there is no ``--out`` flag; redirect stdout (``> file.json``) if
you want a file. This mirrors the "read-only query, evidence on stdout"
contract the rest of this skill's scripts use.

Commands:
  stackup                         board thickness, layer stack, outline bbox
  components [--ref GLOB]         footprint inventory
  net NET                         per-layer routing length/width/vias/pads (incl. gr_* copper)
  keepouts                        keepout/rule-area zones (exact polygon when available)
  edge NET                        min distance from a net's copper to the board outline
  clearance NET_A NET_B [--layer L] [--shapely]   min copper-to-copper distance between two nets
  overlap --analyzer-json FILE REF_A REF_B        approximate bbox proximity from analyzer JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load_board(pcb_arg: str | None):
    from .model import parse_pcb
    pcb = pcb_arg or os.environ.get("TRACTIAN_TOOLKIT_PCB")
    if not pcb:
        sys.exit("error: --pcb (or TRACTIAN_TOOLKIT_PCB) is required")
    return parse_pcb(Path(pcb))


def _out(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="toolkit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", help="path to a .kicad_pcb file (or set TRACTIAN_TOOLKIT_PCB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stackup", help="board thickness, layer stack, outline bbox")

    comp = sub.add_parser("components", help="footprint inventory")
    comp.add_argument("--ref", help="glob filter on reference designator, e.g. 'C*'")

    net = sub.add_parser("net", help="per-layer routing for one net")
    net.add_argument("net")

    sub.add_parser("keepouts", help="keepout/rule-area zones")

    edge = sub.add_parser("edge", help="min distance from a net's copper to the board outline")
    edge.add_argument("net")

    clr = sub.add_parser("clearance", help="min copper-to-copper distance between two nets")
    clr.add_argument("net_a")
    clr.add_argument("net_b")
    clr.add_argument("--layer", help="restrict to one copper layer")
    clr.add_argument("--shapely", action="store_true",
                     help="prefer Shapely (if installed) for large zone fills")

    ov = sub.add_parser("overlap", help="approximate bbox proximity from an analyze_pcb.py JSON")
    ov.add_argument("ref_a")
    ov.add_argument("ref_b")
    ov.add_argument("--analyzer-json", required=True, help="path to analyze_pcb.py JSON output")

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "overlap":
        from .queries import analyzer_json_overlap
        try:
            data = json.loads(Path(args.analyzer_json).read_text(encoding="utf-8"))
        except OSError as exc:
            sys.exit(f"error: could not read --analyzer-json: {exc}")
        _out(analyzer_json_overlap(data, args.ref_a, args.ref_b))
        return 0

    board = _load_board(args.pcb)
    from . import queries as q

    if args.cmd == "stackup":
        _out(q.stackup_summary(board))
    elif args.cmd == "components":
        _out(q.component_inventory(board, args.ref))
    elif args.cmd == "net":
        _out(q.net_routing(board, args.net))
    elif args.cmd == "keepouts":
        _out(q.keepout_summary(board))
    elif args.cmd == "edge":
        _out(q.edge_distance(board, args.net))
    elif args.cmd == "clearance":
        _out(q.net_clearance(board, args.net_a, args.net_b, args.layer, prefer_shapely=args.shapely))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
