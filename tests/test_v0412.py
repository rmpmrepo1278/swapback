r'''0.4.12: review #12. A listed name split across markdown table cells (" | " between its words) is swapped like a
tab-separated one and restored exactly; a non-numeric, negative or NaN scrub_timeout_s is a one-line config error
(exit 2), like every other bad setting.'''
import json
import os
import subprocess
import sys

import pytest

from swapback import core as c


def _scr(tmp_path):
    known = [("Penny Crane", "PERSON"), ("Faith Ridge", "PERSON"), ("Bartholomew Szczepanski", "PERSON")]
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st")), known=known)


def _round(s, content):
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "read"}, {"role": "tool", "tool_call_id": "c", "content": content}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo)


@pytest.mark.parametrize("content", [
    "| 7 | Penny | Crane |", "Owners: Penny | Crane", "| Faith | Ridge | Reno |", "| Bartholomew | Szczepanski |",
    "| id | first | last |\n|---|---|---|\n| 1 | Penny | Crane |\n| 2 | Faith | Ridge |",
    json.dumps({"md": "| 1 | Penny | Crane |\n| 2 | Faith\t|\tRidge |"}),
    json.dumps({"v": json.dumps({"md": "| Penny | Crane |"})}),
])
def test_markdown_cells_swapped_and_restored(tmp_path, content):
    s = _scr(tmp_path)
    sent, back = _round(s, content)
    assert "Penny" not in sent and "Ridge" not in sent and "Szczepanski" not in sent
    assert back == content


@pytest.mark.parametrize("content", ["| Status | Owner |\n|---|---|\n| Open | Hunter |", "ps aux | grep -i crane | sort",
                                     "penny | crane", "| faith | ridge |", "Penny Crane's? no: Penny || Crane"])
def test_ordinary_pipes_left_alone_or_exact(tmp_path, content):
    s = _scr(tmp_path)
    try:
        sent, back = _round(s, content)
    except c.ScrubError:
        assert content == "Penny Crane's? no: Penny || Crane"       # "||" is not a cell separator: the re-check blocks
        return
    assert back == content
    if content.islower() or "Penny" not in content:
        assert sent == content


@pytest.mark.parametrize("val", ["banana", "-1", "-0.5", ".nan", "true", "[1]"])
def test_bad_scrub_timeout_one_line(tmp_path, val):
    p = tmp_path / "c.yaml"
    p.write_text("scrub_timeout_s: %s\n" % val)
    with pytest.raises(c.ConfigError) as e:
        c.load_cfg(str(p))
    assert "scrub_timeout_s" in str(e.value) and str(p) in str(e.value) and "\n" not in str(e.value)


@pytest.mark.parametrize("val,want", [("0", 0.0), ("2.5", 2.5), ("'7'", 7.0), ("10", 10.0)])
def test_good_scrub_timeout(tmp_path, val, want):
    p = tmp_path / "c.yaml"
    p.write_text("scrub_timeout_s: %s\n" % val)
    assert c.load_cfg(str(p))["scrub_timeout_s"] == want


@pytest.mark.parametrize("cmd", [["-m", "swapback.proxy"], ["-c", "import sys; sys.argv=['swapback','check-config']; "
                                                                  "from swapback.core import main; main()"]])
def test_bad_scrub_timeout_exit_2(tmp_path, cmd):
    p = tmp_path / "c.yaml"
    p.write_text("mode: on\nscrub_timeout_s: banana\n")
    pr = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, timeout=60,
                        env=dict(os.environ, SWAPBACK_CONFIG=str(p)))
    assert pr.returncode == 2 and "Traceback" not in pr.stderr
    assert len(pr.stderr.strip().splitlines()) == 1 and "scrub_timeout_s" in pr.stderr
