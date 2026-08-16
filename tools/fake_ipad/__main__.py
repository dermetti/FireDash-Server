"""Allow ``python -m tools.fake_ipad`` to behave like the script entry point."""

from tools.fake_ipad.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
