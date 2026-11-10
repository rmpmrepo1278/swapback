r"""0.4.15: re-check #15. F11: the F8 fix (0.4.14) ended a phone candidate at any run of 3+ blanks, so an unlisted
phone typed with 3 or more spaces between its digit groups ("Call (406)   555-0191") was no longer swapped. Only an
escape's blanks end a candidate now; typed spaces and JSON \t / \n stay inside it. F12: the Stripe rule took
sk_test_environment.py for a key; a key body now holds a digit."""
import json

import pytest

from swapback import core as c
from swapback import detect

PHONES = [("Call (406)   555-0191", "0191"), ("+1   646 555 0139", "0139"), ("ring 503    555    0147 today", "0147"),
          ("Call (406)\t\t\t555-0191", "0191")]
TABBED = [("Call 503\t555\t0147", "0147"), ("Call (406)\t555-0191", "0191"), ("+1\t646\t555\t0139", "0139")]
F8_SHAPES = ['<p style="color:#555">(206) 555-0142</p>', '<td style="padding:4px">(206) 555-0142</td>',
             '<font color="#333">(206) 555-0142</font>', '<span class="tel">(206) 555-0142</span>',
             '<div style="font:12px Arial;color:#1a1a1a">Tel: (206) 555-0142</div>']
RK = "rk" + "_live_" + "Q4mZ8xLp2Wv7Rt9Kc3Nd5Bh6"
SK = "sk" + "_live_" + "Xb7Qm2Lp9Rt4Vw6Yz8Nc3Kd5"


def _go(s):
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _lv(x, k):
    for _ in range(k):
        x = json.dumps({"v": x})
    return x


@pytest.fixture(scope="module")
def scr(tmp_path_factory):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path_factory.mktemp("st"))), known=[])


def _round(s, content, role="tool"):
    fo = c.Forms()
    msg = {"role": role, "content": content}
    if role == "tool":
        msg["tool_call_id"] = "c"
    body = {"messages": [{"role": "user", "content": "read"}, msg]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo)


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("text,tail", PHONES)
def test_f11_phone_with_three_spaces(scr, text, tail, lv):
    content = _lv(text, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    if lv:
        json.loads(sent)
    assert tail not in sent and "PHONE_" in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("text,tail", TABBED)
def test_f11_phone_with_json_tab(scr, text, tail, lv):
    content = _lv(text, lv)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert tail not in sent and "PHONE_" in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
@pytest.mark.parametrize("shape", F8_SHAPES)
def test_f8_shapes_still_swapped(scr, shape, go, lv):
    x = json.dumps({"v": shape})
    content = _lv(_go(x) if go else x, lv - 1)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert "555-0142" not in sent and back == content


def test_f11_detector_spans():
    assert [h[3] for h in detect.find_phones("Call (406)   555-0191", ("US",))] == ["(406)   555-0191"]
    assert [h[3] for h in detect.find_phones("+1   646 555 0139", ("US",))] == ["+1   646 555 0139"]
    t = '#555\\"\\u003e(206) 555-0142'
    assert [(h[0], h[1]) for h in detect.find_phones(t, ("US",))] == [(len(t) - 14, len(t))]
    assert [h[3] for h in detect.find_phones("#555        (206) 555-0142", ("US",))] == ["(206) 555-0142"]


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("text", ["run pytest sk_test_environment.py now", "see sk_live_placeholdervalue, then",
                                  "<code>rk_test_documentationonly</code>"])
def test_f12_stripe_word_without_digit_left_alone(scr, text, lv):
    content = _lv(text, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert sent == content and "SECRET_" not in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("go", [False, True])
@pytest.mark.parametrize("key", [SK, RK])
@pytest.mark.parametrize("shape", ["<td>%s</td>", "<code>%s</code>", "key %s.", "%s,x", "KEY=%s&b=1"])
def test_f12_real_keys_in_markup_still_swapped(scr, key, shape, go, lv):
    x = json.dumps({"v": shape % key})
    content = _lv(_go(x) if go else x, lv - 1)
    sent, back = _round(scr, content)
    json.loads(sent)
    assert key[8:] not in sent and "SECRET_" in sent and back == content
