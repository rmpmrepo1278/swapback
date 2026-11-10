r'''0.4.14: re-check #14. F6: an AWS key right after a Go-escaped tag or "&" (\u003e, \u0026) or a JSON \n reached the
model unswapped, because the secret rules and the local patterns read the text as written while the email and phone
detectors read it escape-blanked. They all read the same blanked text now. F7: an unlisted email whose local part holds
an escaped letter (s\u00f8ren.vik@) showed its first letters before the placeholder. F8: an unlisted US phone after
style="color:#555"\u003e was not swapped. Also: a Stripe key followed by markup or punctuation is a secret.'''
import json
import random

import pytest

from swapback import core as c
from swapback import detect

_R = random.Random(14014)
AWS = "AK" + "IA" + "".join(_R.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
STRIPE = "sk" + "_live_" + "".join(_R.choice("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24))
EMAILS = ("s\u00f8ren.vik@kelmarsh-post.example", "\u00e9lodie@kelmarsh-post.example",
          "\u00c5ngstr\u00f6m.lab@kelmarsh-post.example")


def _go(s):
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _lv(x, k):
    for _ in range(k - 1):
        x = json.dumps({"v": x})
    return x


@pytest.fixture(scope="module")
def scr(tmp_path_factory):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path_factory.mktemp("st"))),
                      known=[("wpool@fenwright-mail.example", "EMAIL")])


def _round(s, content):
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "read"}, {"role": "tool", "tool_call_id": "c", "content": content}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo)


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("shape", ["<td>%s</td>", "<b>x</b>&%s", "a&<i>%s</i>", "<br>%s"])
def test_f6_aws_key_after_go_escape(scr, shape, lv):
    content = _lv(_go(json.dumps({"v": shape % AWS})), lv)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert AWS[4:] not in sent and "SECRET_" in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
def test_f6_aws_key_after_json_newline(scr, go, lv):
    x = json.dumps({"v": ("<p>note\n%s</p>" if go else "note\n%s") % AWS})
    content = _lv(_go(x) if go else x, lv)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert AWS[4:] not in sent and back == content


def test_f6_local_patterns_read_blanked_text(scr):
    tok = "Ab3" + "".join(_R.choice("abcdefghijkmnop0123456789") for _ in range(20))
    content = json.dumps({"v": "see https://x.example/cb?access_token=%s\nnext" % tok})
    sent, back = _round(scr, content)
    assert tok not in sent and back == content
    content = json.dumps({"v": "srv\nbox-1.tail0a1b.ts.net"})
    sent, back = _round(scr, content)
    assert "tail0a1b" not in sent and "nbox" not in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
@pytest.mark.parametrize("em", EMAILS)
def test_f7_escaped_letter_in_local_part(scr, em, go, lv):
    x = json.dumps({"v": "<td>%s</td>" % em})
    content = _lv(_go(x) if go else x, lv)
    sent, back = _round(scr, content)
    json.loads(sent)
    for bit in ("ren.vik", "lodie", "m.lab", "u00f8", "u00e9", "u00f6", "u00c5", "kelmarsh"):
        assert bit not in sent
    assert sent.count("EMAIL_") == 1 and back == content


def test_f7_detector_span_starts_at_letter_escape():
    t = '"\\u00e9lodie@kelmarsh-post.example"'
    hits = [h for h in detect.Presidio().find(t, t.lower()) if h[2] == "EMAIL"]
    assert [(h[0], h[1]) for h in detect.clamp_to_escapes(hits, t)] == [(1, len(t) - 1)]
    assert detect.clamp_to_escapes([(1, 9, "T")], "\\u003ejo@x") == [(6, 9, "T")]     # 0.4.13 still holds


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
def test_f8_phone_after_style_attr(scr, go, lv):
    x = json.dumps({"v": '<p style="color:#555">(206) 555-0142</p>'})
    content = _lv(_go(x) if go else x, lv)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert "555-0142" not in sent and back == content


def test_f8_phone_candidates_split_at_blank_runs():
    assert [h[3] for h in detect.find_phones("#555        (206) 555-0142", ("US",))] == ["(206) 555-0142"]
    assert [h[3] for h in detect.find_phones("call (206) 555-0142 now", ("US",))] == ["(206) 555-0142"]


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
@pytest.mark.parametrize("shape", ["<td>%s</td>", "key %s.", "%s,x"])
def test_stripe_key_in_markup(scr, shape, go, lv):
    x = json.dumps({"v": shape % STRIPE})
    content = _lv(_go(x) if go else x, lv)
    sent, back = _round(scr, content)
    assert STRIPE[8:] not in sent and back == content


def test_stripe_key_plain_text(scr):
    for t in ("<td>%s</td>", "use %s.", "(%s)"):
        o = scr.scrub_text(t % STRIPE)
        assert STRIPE[8:] not in o and "SECRET_" in o
