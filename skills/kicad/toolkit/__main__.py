"""Entry point for ``python -m toolkit`` (see ``toolkit/cli.py`` for commands)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
