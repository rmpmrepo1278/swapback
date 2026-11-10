"""swapback: swap personal data for placeholders before an LLM call, restore it after, fail closed."""
__version__ = "0.4.11"   # keep in step with pyproject.toml (tests/test_cli.py checks)

from .core import Scrubber, ScrubError, load_cfg, load_known, build_private_terms  # noqa: E402,F401
