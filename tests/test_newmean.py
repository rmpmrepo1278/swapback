"""Tests for the optional newmean-pii detector backend (swapback[newmean]). Made-up values only."""
from __future__ import annotations

import builtins
import http.server
import json
import os
import socket
import threading
import urllib.request
from typing import NamedTuple

import pytest

from swapback import core as s
from swapback import proxy as px
from swapback.newmean import LABEL_MAP, NewmeanDetector, normalize_config


class FakeSpan(NamedTuple):
    start: int
    end: int
    label: str
    source: str = "model"


class FakeRedactor:
    """Deterministic stand-in for newmean_pii.Redactor so tests run offline without model weights."""

    def __init__(self, rules: list[tuple[str, str]], *, fail_on: str | None = None):
        self.rules = rules
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.closed = False

    def detect(self, text: str) -> list[FakeSpan]:
        self.calls.append(text)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("simulated model crash")
        out: list[FakeSpan] = []
        for needle, label in self.rules:
            pos = 0
            while True:
                idx = text.find(needle, pos)
                if idx < 0:
                    break
                out.append(FakeSpan(idx, idx + len(needle), label, "model"))
                pos = idx + len(needle)
        out.sort(key=lambda sp: (sp.start, -(sp.end - sp.start)))
        # Keep non-overlapping
        res: list[FakeSpan] = []
        last = -1
        for sp in out:
            if sp.start >= last:
                res.append(sp)
                last = sp.end
        return res

    def info(self) -> dict:
        return {"size": "medium", "model_version": "fake-1.0"}

    def close(self) -> None:
        self.closed = True


def test_label_map_covers_all_newmean_labels_into_canonical_swapback_types():
    expected_newmean_labels = {
        "PERSON",
        "PERSON_L2",
        "EMAIL",
        "PHONE",
        "ADDRESS",
        "ID_NUMBER",
        "FINANCIAL",
        "ORG",
        "USERNAME",
        "URL",
        "URL_L2",
        "DATE_OF_BIRTH",
        "SECRET",
    }
    assert set(LABEL_MAP) == expected_newmean_labels
    for lbl, sb_type in LABEL_MAP.items():
        assert sb_type in s.TYPES, f"{lbl} mapped to unknown Swapback type {sb_type}"


def test_import_error_message_when_newmean_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "newmean_pii":
            raise ImportError("No module named 'newmean_pii'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"pip install 'swapback\[newmean\]'"):
        NewmeanDetector(size="medium")


def test_normalize_config_shapes_and_validation():
    assert normalize_config(False) is None
    assert normalize_config(None) is None
    assert normalize_config("off") is None
    assert normalize_config("false") is None
    assert normalize_config({"enabled": False, "size": "nano"}) is None

    assert normalize_config(True) == {"size": "medium"}
    assert normalize_config("nano") == {"size": "nano"}
    assert normalize_config("LARGE") == {"size": "large"}
    assert normalize_config({"size": "small", "sensitivity": 0.25, "strict_short_names": True}) == {
        "size": "small",
        "sensitivity": 0.25,
        "strict_short_names": True,
    }
    assert normalize_config(
        {"enabled": True, "labels": ["person", "org"], "allow": ["Acme", ["Bob", "PERSON"]], "block": [["X", "ORG"]]}
    ) == {
        "size": "medium",
        "labels": ["PERSON", "ORG"],
        "allow": ["Acme", ("Bob", "PERSON")],
        "block": [("X", "ORG")],
    }

    for bad in ("huge", 123, {"size": "tiny"}, {"unknown_key": 1}, {"sensitivity": float("nan")}, {"sensitivity": True}):
        with pytest.raises(ValueError):
            normalize_config(bad)


def test_load_cfg_validates_newmean_yaml_and_env(tmp_path, monkeypatch):
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("newmean:\n  enabled: true\n  size: nano\n  sensitivity: 0.2\n", encoding="utf-8")
    cfg = s.load_cfg(str(cfgp))
    assert cfg["newmean"] == {"size": "nano", "sensitivity": 0.2}

    monkeypatch.setenv("SWAPBACK_NEWMEAN", "small")
    cfg2 = s.load_cfg(str(cfgp))
    assert cfg2["newmean"] == {"size": "small"}

    monkeypatch.setenv("SWAPBACK_NEWMEAN", "off")
    cfg3 = s.load_cfg(str(cfgp))
    assert cfg3["newmean"] is False

    monkeypatch.delenv("SWAPBACK_NEWMEAN")
    bad = tmp_path / "bad.yaml"
    bad.write_text("newmean:\n  size: giant\n", encoding="utf-8")
    with pytest.raises(s.ConfigError, match="newmean.size"):
        s.load_cfg(str(bad))


def test_newmean_detector_swap_and_restore_all_labels():
    fake = FakeRedactor(
        [
            ("Priya Nair", "PERSON"),
            ("Dr. Sato", "PERSON_L2"),
            ("Globex Bio", "ORG"),
            ("@priyanair", "USERNAME"),
            ("ID-998877", "ID_NUMBER"),
            ("1991-04-12", "DATE_OF_BIRTH"),
            ("42 Rue de Rivoli, Paris", "ADDRESS"),
            ("https://social.example/priya", "URL_L2"),
        ]
    )
    det = NewmeanDetector(redactor=fake, label_map={"ORG": "ORG"})
    scr = s.Scrubber(known=[], vault_path=":memory:", extra_detectors=[det])

    text = (
        "Contact Priya Nair (@priyanair) or Dr. Sato at Globex Bio, "
        "42 Rue de Rivoli, Paris. DOB 1991-04-12, badge ID-998877, "
        "profile https://social.example/priya."
    )
    forms = s.Forms()
    scrubbed = scr.scrub_text(text, forms=forms)

    for secret in (
        "Priya Nair",
        "Dr. Sato",
        "Globex Bio",
        "@priyanair",
        "ID-998877",
        "1991-04-12",
        "42 Rue de Rivoli, Paris",
        "https://social.example/priya",
    ):
        assert secret not in scrubbed

    assert "«PERSON_" in scrubbed
    assert "«ORG_" in scrubbed
    assert "«PROFILE_" in scrubbed
    assert "«ID_" in scrubbed
    assert "«DOB_" in scrubbed
    assert "«ADDRESS_" in scrubbed

    # Simulated LLM response echoing stand-ins
    reply = f"Got it! I will update {scrubbed}"
    restored = scr.restore_text(reply, forms=forms)
    assert restored == f"Got it! I will update {text}"


def test_newmean_priority_and_overlap_rules():
    # Presidio Luhn CARD (prio 1) beats newmean FINANCIAL->ACCOUNT (prio 2) on exact span;
    # Fuller newmean ADDRESS (prio 2) extends regex street-only ADDRESS (prio 1) at same start;
    # Known PERSON (prio 0) keeps canonical key on exact span.
    fake = FakeRedactor(
        [
            ("4532-0151-1283-0366", "FINANCIAL"),
            ("742 Evergreen Terrace, Springfield, IL 62704", "ADDRESS"),
            ("Jane Q Doe", "PERSON"),
        ]
    )
    det = NewmeanDetector(redactor=fake)
    scr = s.Scrubber(
        known=[("Jane Q Doe", "PERSON")],
        vault_path=":memory:",
        extra_detectors=[det],
    )
    text = "Jane Q Doe paid with 4532-0151-1283-0366 from 742 Evergreen Terrace, Springfield, IL 62704."
    spans = scr.spans(text)
    by_val = {v: t for _s, _e, t, v in spans}
    assert by_val["Jane Q Doe"] == "PERSON"
    assert by_val["4532-0151-1283-0366"] == "CARD"
    assert by_val["742 Evergreen Terrace, Springfield, IL 62704"] == "ADDRESS"

    scrubbed = scr.scrub_text(text)
    assert "Springfield" not in scrubbed
    assert "62704" not in scrubbed
    assert scr.restore_text(scrubbed) == text


def test_newmean_skips_existing_placeholders_fs_paths_and_disabled_patterns():
    fake = FakeRedactor(
        [
            ("«PERSON_1»", "ORG"),
            ("alice", "PERSON"),
            ("Globex Bio", "ORG"),
        ]
    )
    det = NewmeanDetector(redactor=fake)
    scr = s.Scrubber(
        cfg=dict(s.DEFAULTS, patterns={"ORG": False}),
        known=[],
        vault_path=":memory:",
        extra_detectors=[det],
    )
    text = "Check /home/alice/notes.md for «PERSON_1» at Globex Bio."
    scrubbed = scr.scrub_text(text)
    assert "/home/alice/notes.md" in scrubbed
    assert "«PERSON_1»" in scrubbed
    assert "Globex Bio" in scrubbed  # ORG disabled via patterns


def test_newmean_clamps_backslash_escapes():
    fake = FakeRedactor([(r"\nPriya Nair", "PERSON")])
    det = NewmeanDetector(redactor=fake)
    scr = s.Scrubber(known=[], vault_path=":memory:", extra_detectors=[det])
    raw = r"line1\nPriya Nair"
    scrubbed = scr.scrub_text(raw)
    assert r"\n«PERSON_" in scrubbed
    assert "Priya Nair" not in scrubbed
    assert scr.restore_text(scrubbed) == raw


def test_newmean_fail_closed_on_detector_exception():
    fake = FakeRedactor([("Priya Nair", "PERSON")], fail_on="BOOM")
    det = NewmeanDetector(redactor=fake)
    scr = s.Scrubber(known=[], vault_path=":memory:", extra_detectors=[det])
    body = {"model": "gpt-test", "messages": [{"role": "user", "content": "BOOM Priya Nair"}]}
    with pytest.raises(s.ScrubError, match="scrub failed: RuntimeError"):
        scr.scrub_request(body)


def test_newmean_thread_safety_and_labels_filter():
    fake = FakeRedactor([("Priya Nair", "PERSON"), ("Globex Bio", "ORG")])
    det = NewmeanDetector(redactor=fake, labels=["PERSON"])
    scr = s.Scrubber(known=[], vault_path=":memory:", extra_detectors=[det])

    results: list[str] = []
    errors: list[Exception] = []

    def worker(i: int):
        try:
            t = f"Msg {i}: Priya Nair works at Globex Bio."
            sc = scr.scrub_text(t)
            assert "Priya Nair" not in sc
            assert "Globex Bio" in sc  # filtered out by labels=["PERSON"]
            assert scr.restore_text(sc) == t
            results.append(sc)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len(results) == 12


def test_newmean_proxy_end_to_end_and_health(tmp_path):
    received_bodies: list[dict] = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            received_bodies.append(body)
            user_msg = body["messages"][-1]["content"]
            reply = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": f"Echo: {user_msg}"}}],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up_port = up.server_address[1]
    threading.Thread(target=up.serve_forever, daemon=True).start()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    px_port = sock.getsockname()[1]
    sock.close()

    fake = FakeRedactor([("Priya Nair", "PERSON"), ("42 Rue de Rivoli, Paris", "ADDRESS")])
    det = NewmeanDetector(redactor=fake, size="nano")
    st = tmp_path / "state"
    st.mkdir()
    cfg = dict(
        s.DEFAULTS,
        mode="on",
        listen=f"127.0.0.1:{px_port}",
        upstream=f"http://127.0.0.1:{up_port}",
        state_dir=str(st),
        allow_empty_known=True,
        newmean=det,
    )
    srv = px.make_server(cfg=cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 1. /scrubber/health exposes safe newmean metadata (no values)
        with urllib.request.urlopen(f"http://127.0.0.1:{px_port}/scrubber/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
        assert health["ok"] is True
        assert health["newmean"]["enabled"] is True
        assert health["newmean"]["size"] == "nano"

        # 2. Chat completion swaps unlisted name/address + email on wire, restores byte-exact in reply
        prompt = "Mail Priya Nair at priya.nair@acme.io (42 Rue de Rivoli, Paris)."
        req = urllib.request.Request(
            f"http://127.0.0.1:{px_port}/v1/chat/completions",
            data=json.dumps({"model": "gpt-test", "messages": [{"role": "user", "content": prompt}]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))

        sent_to_llm = received_bodies[0]["messages"][-1]["content"]
        assert "Priya Nair" not in sent_to_llm
        assert "priya.nair@acme.io" not in sent_to_llm
        assert "42 Rue de Rivoli, Paris" not in sent_to_llm
        assert "«PERSON_" in sent_to_llm
        assert "«EMAIL_" in sent_to_llm
        assert "«ADDRESS_" in sent_to_llm

        assert resp["choices"][0]["message"]["content"] == f"Echo: {prompt}"
    finally:
        srv.shutdown()
        srv.server_close()
        up.shutdown()
        up.server_close()




def test_newmean_trims_trailing_sentence_punct_but_keeps_abbreviations():
    fake = FakeRedactor(
        [
            ("742 Evergreen Terrace, Springfield, IL 62704.", "ADDRESS"),
            ("Acme Inc.", "ORG"),
            ("Priya Nair,", "PERSON"),
        ]
    )
    det = NewmeanDetector(redactor=fake)
    text = "Priya Nair, at Acme Inc. lives at 742 Evergreen Terrace, Springfield, IL 62704."
    spans = det.find(text)
    assert [(t, v) for _s, _e, t, v in spans] == [
        ("PERSON", "Priya Nair"),
        ("ORG", "Acme Inc."),
        ("ADDRESS", "742 Evergreen Terrace, Springfield, IL 62704"),
    ]


@pytest.mark.skipif(
    os.environ.get("SWAPBACK_TEST_NEWMEAN_LIVE") != "1",
    reason="live newmean-pii model download test (set SWAPBACK_TEST_NEWMEAN_LIVE=1 to run)",
)
def test_newmean_live_e2e_round_trip(tmp_path):
    """Real end-to-end test with the real newmean-pii nano model: swap -> fake LLM reply -> restore."""
    det = NewmeanDetector(size="nano", cache_dir=str(tmp_path / "nm-cache"))
    try:
        scr = s.Scrubber(known=[], vault_path=":memory:", extra_detectors=[det])
        text = (
            "Please email Priya Nair at priya.nair@acme.io or call (415) 555-0192 "
            "about shipping to 742 Evergreen Terrace, Springfield, IL 62704."
        )
        forms = s.Forms()
        scrubbed = scr.scrub_text(text, forms=forms)
        assert "Priya Nair" not in scrubbed
        assert "priya.nair@acme.io" not in scrubbed
        assert "555-0192" not in scrubbed
        assert "742 Evergreen Terrace" not in scrubbed

        # Fake LLM uses the swapped stand-ins in a new sentence
        llm_reply = f"Confirmed — I will follow up on: {scrubbed}"
        restored = scr.restore_text(llm_reply, forms=forms)
        assert restored == f"Confirmed — I will follow up on: {text}"
    finally:
        det.close()
