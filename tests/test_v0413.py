r'''0.4.13: re-check #13 F1. With Go-style escaping (\u003c, \u003e, \u0026) an email or phone right after a tag or
"&" was matched together with the escape's tail: a listed one was blocked, an unlisted one gave the model invalid JSON.
No pattern match starts inside a backslash escape now.'''
import json

import pytest

from swapback import core as c
from swapback import detect

LISTED, UNLISTED = "wpool@fenwright-mail.example", "kdrum@larchmoor-post.example"
PHONES = ("+1 415 555 0142", "+1 206 555 0199")
SHAPES = ("<td>%s</td>", "<b>x</b>&%s", "a&amp;<i>%s</i>", "<a href=x>%s</a><br>", "<%s>", "&%s&", "x<%s")


def _go(s):
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _scr(tmp_path):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st")),
                      known=[(LISTED, "EMAIL"), (PHONES[0], "PHONE")])


def _round(s, content):
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "read"}, {"role": "tool", "tool_call_id": "c", "content": content}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo)


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("val", [LISTED, UNLISTED] + list(PHONES))
def test_value_after_go_escape(tmp_path, val, shape, lv):
    content = _go(json.dumps({"v": shape % val}))
    for _ in range(lv - 1):
        content = json.dumps({"v": content})
    sent, back = _round(_scr(tmp_path), content)
    json.loads(sent)                                   # valid JSON for the model
    assert val.split("@")[0][-6:] not in sent
    assert back == content
    if "@" in val:
        assert sent.count("EMAIL_") == 1            # one address, one placeholder


def test_repro_exact(tmp_path):
    content = _go(json.dumps({"v": "<td>%s</td>" % LISTED}))
    sent, back = _round(_scr(tmp_path), content)
    assert "fenwright" not in sent and back == content and "\\\u00ab" not in sent


@pytest.mark.parametrize("text", ["\\u003ejo@x.example", "\\\\u003ejo@x.example", "\\\\\\\\u0026jo@x.example",
                                  '\\"jo@x.example\\"'])
def test_detector_starts_after_escape(text):
    p = detect.Presidio()
    hits = [h for h in p.find(text, text.lower()) if h[2] == "EMAIL"]
    assert [h[3] for h in hits] == ["jo@x.example"]


def test_escaped_letter_kept():
    # 0.4.14 (#14 F7): an escaped letter is shown to the detectors as letters of the same length
    assert detect.blank_escapes("S\\u00f8ren \\u003e") == "Saaaaaaren       "
    assert detect.clamp_to_escapes([(1, 9, "T")], "\\u003ejo@x") == [(6, 9, "T")]
