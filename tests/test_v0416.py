r"""0.4.16: re-check #16. F14: two unlisted phones with only blanks between them ("Ana   (503) 555-0147   (212) 555-0162",
a TSV Home/Mobile row) were one phone candidate; the first was swapped and the second went out in clear. The detector
now keeps scanning right after each match. F13: a row of single digits split by 2+ blanks, tabs or escaped \n / \u00a0
("Mon\t4\t1\t5\t5\t5\t5\t0\t1\t8\t8") was swapped as a phone; a number may now hold at most one 1-digit group, and where
blank runs are read as one space its groups must be laid out like a phone."""
import json

import pytest

from swapback import core as c
from swapback import detect

A, B, C3 = "(503) 555-0147", "(212) 555-0162", "(720) 555-0114"
PAIRS = [(A, B), ("212-555-0162", "503-555-0147"), ("+1 646 555 0139", "(919) 555-0173"), ("720.555.0114", "415.555.0188"),
         (A, "212-555-0162")]
SEPS = ["   ", "    ", "\t", "\t\t", "  "]
ROWS = ["Ana   %s   %s   Seattle" % (A, B), "Ana\t%s\t%s\tSeattle" % (A, B), "Home   Mobile\n%s   %s" % (A, B),
        "%s   %s   %s" % (A, B, C3)]
DIGIT_ROWS = ["Mon\t4\t1\t5\t5\t5\t5\t0\t1\t8\t8", "votes:  2  0  6  5  5  5  0  1  0  0", "1   2   3   4   5   6   7   8   9   10",
              "seats 2\t3\t4\t5\t6\t7\t8\t9\t0\t1", "grid\t9\t1\t9\t5\t5\t5\t0\t1\t7\t3", "1 2 3 4 5 6 7 8 9 10",
              "dice: 6 2 3 4 5 1 6 2 3 4"]
STILL = [("Call (406)   555-0191", "0191"), ("+1   646 555 0139", "0139"), ("Call 503\t555\t0147", "0147"),
         ("#555        (206) 555-0142", "0142"), ("+91   98765   43210", "43210"), ("call 1-800-555-0199", "0199")]


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


@pytest.mark.parametrize("sep", SEPS)
@pytest.mark.parametrize("p,q", PAIRS)
def test_f14_detector_finds_both(p, q, sep):
    assert [h[3] for h in detect.find_phones(p + sep + q)] == [p, q]


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("sep", SEPS + ["\n"])
@pytest.mark.parametrize("p,q", PAIRS)
def test_f14_second_phone_swapped(scr, p, q, sep, lv):
    content = _lv(p + sep + q, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    if lv:
        json.loads(sent)
    assert p[-4:] not in sent and q[-4:] not in sent and back == content


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("row", ROWS)
def test_f14_contact_rows(scr, row, lv):
    content = _lv(row, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert "0147" not in sent and "0162" not in sent and "0114" not in sent and back == content


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("row", DIGIT_ROWS)
def test_f13_digit_rows_left_alone(scr, row, lv):
    content = _lv(row, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert sent == content and "PHONE_" not in sent and back == content


@pytest.mark.parametrize("lv", [1, 2, 3])
@pytest.mark.parametrize("blank", ["\\n", "\\u00a0"])
def test_f13_escaped_blank_rows_left_alone(scr, blank, lv):
    row = json.dumps({"v": "votes" + "".join(blank + d for d in "2065550100")})
    content = _lv(row, lv - 1)
    sent, back = _round(scr, content)
    assert sent == content and back == content


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("text,tail", STILL)
def test_f11_f8_shapes_still_swapped(scr, text, tail, lv):
    content = _lv(text, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert tail not in sent and "PHONE_" in sent and back == content


def test_f8_escape_still_ends_candidate():
    t = '#555\\"\\u003e(206) 555-0142'
    assert [(h[0], h[1]) for h in detect.find_phones(t, ("US",))] == [(len(t) - 14, len(t))]
