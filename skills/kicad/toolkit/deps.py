"""Optional third-party dependency detection, stdlib-first.

Nothing in ``toolkit`` imports Shapely/NumPy/SciPy at module import time —
every optional library is probed lazily, on first actual use, through the
helpers below. This keeps ``import toolkit`` (and the CLI's ``--help``)
working in a bare KiCad-bundled or CI Python with no extra packages
installed; only the specific query that benefits from the library pays the
import cost, and degrades to a clearly-labelled stdlib approximation when the
library is missing. No module here ever calls ``pip install`` or creates a
virtualenv — if a library is absent, the caller is told how to get exact
results (``pip install shapely``), not auto-bootstrapped.
"""

from __future__ import annotations

_shapely_checked = False
_shapely_available = False


def has_shapely() -> bool:
    """True if Shapely is importable. Cached after the first check."""
    global _shapely_checked, _shapely_available
    if not _shapely_checked:
        try:
            import shapely.geometry  # noqa: F401
            _shapely_available = True
        except Exception:
            _shapely_available = False
        _shapely_checked = True
    return _shapely_available


def shapely_geometry():
    """Return the ``shapely.geometry`` module, or raise ImportError with a
    clear, actionable message. Callers should check :func:`has_shapely`
    first and prefer the stdlib fallback path over catching this."""
    if not has_shapely():
        raise ImportError(
            "shapely is not installed - this is an optional dependency. "
            "Install it for exact polygon distance/overlap on large zone "
            "fills: pip install shapely. The stdlib approximate path "
            "(toolkit.geometry) is used automatically when it is absent."
        )
    import shapely.geometry
    return shapely.geometry
