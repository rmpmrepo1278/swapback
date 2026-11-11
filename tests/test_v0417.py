r"""0.4.17: re-check #17. F15: 0.4.16 held every phone to "at most one 1-digit group", so an unlisted phone typed one digit
at a time ("my number is 4 0 6 5 5 5 0 1 9 1", "4-0-6-5-5-5-0-1-9-1", "Tel 406 555 0 1 9 1") went out in clear. The rule now
applies only inside the blank-run fallback (a match that spans 2+ blanks, a tab, or an escaped \n / \u00a0), and there it
counts the pieces between those runs. Elsewhere 0.4.15's behaviour is back. The F13 rows and the F14 pairs stay fixed."""
import json
import re

import pytest

from swapback import core as c
from swapback import detect

# the 12 shapes of re-check #17 (each sent in clear 4 of 4 on 0.4.16, swapped 4 of 4 on 0.4.13-0.4.15), with the
# 7-digit local tail that must not reach the model
F15 = [("4 0 6 5 5 5 0 1 9 1", "5550191"), ("my number is 4 0 6 5 5 5 0 1 9 1", "5550191"),
       ("phone: 2 1 2 5 5 5 0 1 6 2 thanks", "5550162"), ("1 4 0 6 5 5 5 0 1 9 1", "5550191"),
       ("+1 4 0 6 5 5 5 0 1 9 1", "5550191"), ("4-0-6-5-5-5-0-1-9-1", "5550191"), ("4.0.6.5.5.5.0.1.9.1", "5550191"),
       ("(4 0 6) 5 5 5-0 1 9 1", "5550191"), ("4 0 6  5 5 5  0 1 9 1", "5550191"), ("406 5 5 5 0 1 9 1", "5550191"),
       ("4 0 6 555 0191", "5550191"), ("Tel 406 555 0 1 9 1", "5550191")]
# rows of single digits split by 2+ blanks, tabs or escapes: still not phones (#16 F13)
F13 = ["Mon\t4\t1\t5\t5\t5\t5\t0\t1\t8\t8", "votes:  2  0  6  5  5  5  0  1  0  0", "1   2   3   4   5   6   7   8   9   10",
       "seats\t2\t0\t6\t5\t5\t5\t0\t1\t0\t0", "dice:  6  2  3  4  5  1  6  2  3  4", "4 \t0 \t6 \t5 \t5 \t5 \t0 \t1 \t9 \t1"]
PH = re.compile(r"«[A-Z_]+_\d+»")


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
@pytest.mark.parametrize("text,tail", F15)
def test_f15_digit_by_digit_phone_swapped(scr, text, tail, lv):
    content = _lv(text, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    if lv:
        json.loads(sent)
    assert "PHONE_" in sent and tail not in re.sub(r"\D", "", PH.sub("", sent)) and back == content


@pytest.mark.parametrize("text,tail", F15)
def test_f15_detector_finds_it(text, tail):
    hits = detect.find_phones(text)
    assert len(hits) == 1 and re.sub(r"\D", "", hits[0][3]).endswith(tail)


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("row", F13)
def test_f13_blank_run_digit_rows_still_left_alone(scr, row, lv):
    content = _lv(row, lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert sent == content and back == content


@pytest.mark.parametrize("lv", [0, 1, 2, 3])
@pytest.mark.parametrize("sep", ["   ", "\t", "  "])
def test_f14_pair_still_swapped(scr, sep, lv):
    content = _lv("Ana" + sep + "(503) 555-0147" + sep + "(212) 555-0162", lv)
    sent, back = _round(scr, content, "user" if lv == 0 else "tool")
    assert "0147" not in sent and "0162" not in sent and back == content
