"""swapback: swap personal data for placeholders before an LLM call, restore it after, fail closed."""
from .core import Scrubber, ScrubError, load_cfg, load_known, build_private_terms  # noqa: F401

__version__ = "0.2.0"
