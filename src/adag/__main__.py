"""Allow ``python -m adag`` to invoke the CLI."""

from adag.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
