"""Load the fork's own ``../scripts/sexp_parser.py`` without duplicating it.

``skills/kicad/scripts/`` is a flat script directory (no ``__init__.py``,
loaded by every analyzer via ``sys.path.insert``). This module applies the
same pattern so ``toolkit`` parses S-expressions with the exact same code
path as ``analyze_pcb.py`` — one parser, one set of brace-escape/edge-case
fixes, two consumers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_sexp_parser() -> ModuleType:
    """Import and return ``sexp_parser`` from the sibling ``scripts/`` dir."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import sexp_parser  # noqa: E402
    return sexp_parser
