"""Optional local multilingual PII detector backed by newmean-pii (Apache-2.0, by newmean.ai).

Install the optional extra to use this backend:
    pip install 'swapback[newmean]'

newmean-pii runs a small quantized token-classification model (2.6-15.5 MB, 54 languages) on
CPU with no PyTorch or spaCy. Swapback wraps its spans with reversible swap-and-restore,
vault persistence, streaming restore, egress tool policies, and fail-closed checks.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "LABEL_MAP",
    "VALID_SIZES",
    "NewmeanDetector",
    "normalize_config",
]

VALID_SIZES = ("nano", "small", "medium", "large")

# Map newmean-pii labels (level 1 + level 2) to Swapback's canonical entity types.
LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "PERSON_L2": "PERSON",
    "EMAIL": "EMAIL",
    "PHONE": "PHONE",
    "ADDRESS": "ADDRESS",
    "ID_NUMBER": "ID",
    "FINANCIAL": "ACCOUNT",
    "ORG": "ORG",
    "USERNAME": "PROFILE",
    "URL": "PROFILE",
    "URL_L2": "PROFILE",
    "DATE_OF_BIRTH": "DOB",
    "SECRET": "SECRET",
}

_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "size",
        "variant",
        "sensitivity",
        "labels",
        "regex",
        "keep_emoji",
        "whole_words",
        "strict_short_names",
        "allow",
        "block",
        "lists_id",
        "model_dir",
        "cache_dir",
        "model_url",
        "label_map",
    }
)

_INSTALL_HINT = (
    "newmean-pii is not installed. Install the optional backend with: "
    "pip install 'swapback[newmean]'"
)



_KEEP_DOT_ABBREV = frozenset(
    {
        "st", "ave", "rd", "blvd", "dr", "ln", "ct", "pl", "ter", "pkwy", "hwy", "cir", "trl",
        "apt", "ste", "bldg", "fl", "rm", "no", "jr", "sr", "mr", "mrs", "ms", "dr", "prof",
        "inc", "ltd", "llc", "corp", "co", "dept", "univ",
    }
)


def _trim_span(text: str, s: int, e: int, label: str) -> tuple[int, int]:
    """Trim trailing sentence punctuation that NER models occasionally attach to the final token."""
    if label in ("SECRET", "URL", "URL_L2"):
        return s, e
    while e > s and text[e - 1] in " 	,;!?":
        e -= 1
    if e > s and text[e - 1] == ".":
        # Preserve initials ("J.", "U.S.") and common street/name/org abbreviations ("St.", "Inc.").
        head = text[s : e - 1]
        tail_word = ""
        i = len(head) - 1
        while i >= 0 and head[i].isalpha():
            i -= 1
        tail_word = head[i + 1 :]
        is_initial = len(tail_word) == 1 and tail_word.isupper()
        is_abbrev = tail_word.lower() in _KEEP_DOT_ABBREV
        if not is_initial and not is_abbrev:
            e -= 1
    return s, e


def _import_newmean() -> Any:
    try:
        import newmean_pii  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return newmean_pii


def normalize_config(value: Any) -> dict[str, Any] | None:
    """Normalize a ``newmean:`` config value (bool | str | dict | None) to options or None.

    Raises ValueError on malformed values so ``load_cfg`` can surface a single-line ConfigError.
    """
    if value is None or value is False:
        return None
    if value is True:
        return {"size": "medium"}
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "0", "false", "off", "no", "none"):
            return None
        if s in ("1", "true", "on", "yes"):
            return {"size": "medium"}
        if s in VALID_SIZES:
            return {"size": s}
        raise ValueError(
            "newmean must be false, true, a model size (%s), or a mapping; got %r"
            % (", ".join(VALID_SIZES), value)
        )
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - _ALLOWED_CONFIG_KEYS)
        if unknown:
            raise ValueError("newmean has unknown key(s): %s" % ", ".join(unknown))
        enabled = value.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("0", "false", "off", "no", "")
        if not enabled:
            return None
        opts = {k: v for k, v in value.items() if k != "enabled"}
        size = opts.get("size", "medium")
        if not isinstance(size, str) or size.strip().lower() not in VALID_SIZES:
            raise ValueError(
                "newmean.size must be one of %s, not %r" % (", ".join(VALID_SIZES), size)
            )
        opts["size"] = size.strip().lower()
        if "sensitivity" in opts and opts["sensitivity"] is not None:
            sens = opts["sensitivity"]
            if isinstance(sens, bool):
                raise ValueError("newmean.sensitivity must be a number, not bool")
            try:
                f = float(sens)
            except (TypeError, ValueError) as exc:
                raise ValueError("newmean.sensitivity must be a number, not %r" % (sens,)) from exc
            if f != f or f in (float("inf"), float("-inf")):
                raise ValueError("newmean.sensitivity must be finite, not %r" % (sens,))
            opts["sensitivity"] = f
        if "labels" in opts and opts["labels"] is not None:
            labels = opts["labels"]
            if isinstance(labels, str) or not isinstance(labels, Iterable):
                raise ValueError("newmean.labels must be a list of label names")
            opts["labels"] = [str(x).strip().upper() for x in labels]
        if "allow" in opts and opts["allow"] is not None:
            allow = opts["allow"]
            if isinstance(allow, str) or not isinstance(allow, Iterable):
                raise ValueError("newmean.allow must be a list of strings or [value, label] pairs")
            norm_allow = []
            for item in allow:
                if isinstance(item, str):
                    norm_allow.append(item)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    norm_allow.append((str(item[0]), None if item[1] is None else str(item[1])))
                else:
                    raise ValueError("newmean.allow entries must be a string or [value, label] pair")
            opts["allow"] = norm_allow
        if "block" in opts and opts["block"] is not None:
            block = opts["block"]
            if isinstance(block, str) or not isinstance(block, Iterable):
                raise ValueError("newmean.block must be a list of [value, label] pairs")
            norm_block = []
            for item in block:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    norm_block.append((str(item[0]), str(item[1])))
                else:
                    raise ValueError("newmean.block entries must be [value, label] pairs")
            opts["block"] = norm_block
        if "label_map" in opts and opts["label_map"] is not None:
            lm = opts["label_map"]
            if not isinstance(lm, Mapping):
                raise ValueError("newmean.label_map must be a mapping of label -> Swapback type")
            opts["label_map"] = {str(k).upper(): str(v).upper() for k, v in lm.items()}
        return opts
    raise ValueError(
        "newmean must be false, true, a model size (%s), or a mapping; got %r"
        % (", ".join(VALID_SIZES), value)
    )


class NewmeanDetector:
    """Swapback detector adapter around ``newmean_pii.create_redactor``.

    Returns Swapback-shaped spans ``[(start, end, TYPE, value)]`` using ``LABEL_MAP``.
    The underlying ONNX/C model is loaded lazily on first ``find()`` (or eagerly via ``load()``),
    and all calls into the redactor are serialized under a lock for thread safety.
    """

    def __init__(
        self,
        size: str = "medium",
        variant: str | None = None,
        *,
        sensitivity: float = 0.0,
        labels: Iterable[str] | None = None,
        regex: bool = False,
        keep_emoji: bool = False,
        whole_words: bool = True,
        strict_short_names: bool = False,
        allow: Iterable[Any] | None = None,
        block: Iterable[tuple[str, str]] | None = None,
        lists_id: str | None = None,
        model_dir: str | None = None,
        cache_dir: str | None = None,
        model_url: str | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        label_map: Mapping[str, str] | None = None,
        redactor: Any = None,
    ) -> None:
        size_norm = str(size).strip().lower()
        if size_norm not in VALID_SIZES:
            raise ValueError(
                "size must be one of %s, not %r" % (", ".join(VALID_SIZES), size)
            )
        if isinstance(sensitivity, bool) or sensitivity != sensitivity or sensitivity in (
            float("inf"),
            float("-inf"),
        ):
            raise ValueError("sensitivity must be a finite number")

        self.size = size_norm
        self.variant = variant
        self.sensitivity = float(sensitivity)
        self.labels = None if labels is None else tuple(str(x).upper() for x in labels)
        self.regex = bool(regex)
        self.keep_emoji = bool(keep_emoji)
        self.whole_words = bool(whole_words)
        self.strict_short_names = bool(strict_short_names)
        self.allow = None if allow is None else list(allow)
        self.block = None if block is None else list(block)
        self.lists_id = lists_id
        self.model_dir = model_dir
        self.cache_dir = cache_dir
        self.model_url = model_url
        self.on_progress = on_progress

        merged_map = dict(LABEL_MAP)
        if label_map:
            for k, v in label_map.items():
                merged_map[str(k).upper()] = str(v).upper()
        self.label_map = merged_map

        self._lock = threading.Lock()
        self._redactor = redactor
        self._owns_redactor = redactor is None
        if redactor is None:
            # Verify the optional dependency is installed immediately so fail-closed
            # callers get a clear ImportError at startup rather than mid-request.
            _import_newmean()

    @classmethod
    def from_config(cls, value: Any) -> "NewmeanDetector | None":
        """Build a NewmeanDetector from a ``swapback.yaml`` ``newmean:`` entry (or None if off)."""
        opts = normalize_config(value)
        if opts is None:
            return None
        return cls(**opts)

    def _create_redactor(self) -> Any:
        mod = _import_newmean()
        kwargs: dict[str, Any] = {
            "size": self.size,
            "variant": self.variant,
            "sensitivity": self.sensitivity,
            "labels": list(self.labels) if self.labels is not None else None,
            "regex": self.regex,
            "keep_emoji": self.keep_emoji,
            "whole_words": self.whole_words,
            "strict_short_names": self.strict_short_names,
            "allow": self.allow,
            "block": self.block,
            "lists_id": self.lists_id,
            "model_dir": self.model_dir,
            "cache_dir": self.cache_dir,
            "model_url": self.model_url,
            "on_progress": self.on_progress,
        }
        # Drop None values for optional arguments so older newmean-pii versions stay compatible.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return mod.create_redactor(**kwargs)

    def load(self) -> Any:
        """Load the underlying newmean-pii redactor now (thread-safe, idempotent)."""
        if self._redactor is None:
            with self._lock:
                if self._redactor is None:
                    self._redactor = self._create_redactor()
        return self._redactor

    def map_label(self, label: str) -> str:
        """Map a newmean-pii label name to a Swapback entity type."""
        key = str(label).upper()
        return self.label_map.get(key, key)

    def find(self, text: str) -> list[tuple[int, int, str, str]]:
        """Detect PII in ``text`` and return ``[(start, end, TYPE, value)]``."""
        if not text:
            return []
        redactor = self.load()
        with self._lock:
            spans = redactor.detect(text)
        out: list[tuple[int, int, str, str]] = []
        n = len(text)
        wanted = None if self.labels is None else set(self.labels)
        for sp in spans:
            s = int(sp.start)
            e = int(sp.end)
            lbl = str(sp.label).upper()
            if wanted is not None and lbl not in wanted:
                continue
            if s < 0 or e <= s or e > n:
                continue
            s, e = _trim_span(text, s, e, lbl)
            if e <= s:
                continue
            out.append((s, e, self.map_label(lbl), text[s:e]))
        return out

    def __call__(self, text: str) -> list[tuple[int, int, str, str]]:
        return self.find(text)

    def info(self) -> dict[str, Any]:
        """Return model metadata from the underlying redactor (loads if not loaded)."""
        redactor = self.load()
        fn = getattr(redactor, "info", None)
        return dict(fn()) if callable(fn) else {"size": self.size}

    def summary(self) -> dict[str, Any]:
        """Safe, value-free config summary for check-config and /scrubber/health."""
        return {
            "enabled": True,
            "size": self.size,
            "loaded": self._redactor is not None,
            "sensitivity": self.sensitivity,
            "labels": list(self.labels) if self.labels is not None else "all",
        }

    def close(self) -> None:
        with self._lock:
            red = self._redactor
            if red is not None and self._owns_redactor:
                close_fn = getattr(red, "close", None)
                if callable(close_fn):
                    close_fn()
                self._redactor = None

    def __enter__(self) -> "NewmeanDetector":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
