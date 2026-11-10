"""0.4.5: review #5 repros. JSON in a ```json fence or inline in prose stays valid on restore, a listed name spread
over several path segments is swapped, health "known" counts unique listed values, large %XX / base64 / form bodies are
checked in linear time, addresses joined by <br> are matched."""
import base64, json, re, time
import pytest
from swapback import core as c

ADDR = "88 Larkspur Terrace\nApt 4B\nPortland, OR 97214"
KNOWN = [(ADDR, "ADDRESS"), ("Hazel Wood", "PERSON"), ("Ngọc Trâm Vũ", "PERSON"), ("Thaddeus Quillfeather", "PERSON"),
         ("+1 503 555 0147", "PHONE"), ("zq.marlow@example.net", "EMAIL")]


def _scr(tmp_path, known=KNOWN, **cfg):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path), **cfg), known=known)


def _ph(text, typ):
    return re.findall("\u00ab%s_\\d+\u00bb" % typ, text)[0]


def _block(text):
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


@pytest.mark.parametrize("wrap", ["Here you go:\n```json\n%s\n```\nDone.", "```\n%s\n```", "The record is %s as asked.",
                                  "Result:\n\n%s", "- item: %s"])
def test_restore_into_json_inside_prose_stays_valid(tmp_path, wrap):
    s = _scr(tmp_path)
    fo = c.Forms()
    out = s.scrub_text("Extract as JSON:\n" + ADDR + "\nfor Hazel Wood", forms=fo)
    doc = json.dumps({"addr": _ph(out, "ADDRESS"), "who": _ph(out, "PERSON"), "n": [1, 2]}, ensure_ascii=False)
    back = s.restore_text(wrap % doc, forms=fo)
    assert _block(back) == {"addr": ADDR, "who": "Hazel Wood", "n": [1, 2]}
    sr = s.stream_restorer(forms=c.Forms())             # streamed in 1 to 4 character chunks: the same
    fo2 = c.Forms()
    s.scrub_text("Extract as JSON:\n" + ADDR + "\nfor Hazel Wood", forms=fo2)
    sr = s.stream_restorer(forms=fo2)
    reply, got, i = wrap % doc, "", 0
    while i < len(reply):
        k = 1 + i % 4
        got += sr.feed(reply[i:i + k])
        i += k
    got += sr.flush()
    assert got == back


def test_pasted_json_with_escaped_newlines_comes_back_exact(tmp_path):
    s = _scr(tmp_path)
    t = "Fix this:\n```json\n" + json.dumps({"addr": ADDR, "who": "Hazel Wood"}) + "\n```"
    fo = c.Forms()
    out = s.scrub_text(t, forms=fo)
    assert "ADDRESS_" in out and s.restore_text(out, forms=fo) == t


@pytest.mark.parametrize("text", ["Larkspur says {braces} and \"quotes\"\n" + "x", "a {b: \"c\"} d", "see [1] and [2]",
                                  "use {name} here"])
def test_prose_with_braces_restores_as_written(tmp_path, text):
    s = _scr(tmp_path)
    fo = c.Forms()
    t = text + "\nship to " + ADDR
    out = s.scrub_text(t, forms=fo)
    assert s.restore_text(out, forms=fo) == t


@pytest.mark.parametrize("text", ["Ngọc/Trâm/Vũ", "/srv/Ngọc/Trâm/Vũ/file", "/Users/Thaddeus/Quillfeather/notes.txt",
                                  "~/thaddeus/quillfeather/x", "a/b/Thaddeus/Quillfeather"])
def test_name_spread_over_path_segments_is_swapped(tmp_path, text):
    s = _scr(tmp_path)
    fo = c.Forms()
    out = s.scrub_text("open " + text, forms=fo)
    assert "PERSON_" in out and s.leftover_known(out) == 0 and s.restore_text(out, forms=fo) == "open " + text
    assert s.leftover_known("open " + text) > 0 and s.skeleton_hits("open " + text)   # both checks see it


def test_windows_path_name_is_blocked_by_the_skeleton_check(tmp_path):
    s = _scr(tmp_path)                                    # a backslash is not a matcher separator; the re-check blocks
    t = "open C:\\Users\\Thaddeus\\Quillfeather\\notes.txt"
    out = s.scrub_text(t)
    assert "PERSON_" in out or s.skeleton_hits(out)
    with pytest.raises(c.ScrubError) if "PERSON_" not in out else _null():
        s.scrub_request({"messages": [{"role": "user", "content": t}]})


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("text", ["/home/Hazel.Wood/notes.md", "~/Thaddeus_Quillfeather/x", "/home/hazelwood/x"])
def test_name_in_one_path_segment_is_left(tmp_path, text):
    s = _scr(tmp_path)
    assert s.scrub_text(text) == text and s.leftover_known(text) == 0 and not s.skeleton_hits(text)


def test_known_counts_unique_listed_values(tmp_path):
    k = tmp_path / "known.yaml"
    k.write_text("PERSON: [Hazel Wood, Ngọc Trâm Vũ]\nPHONE: ['+1 503 555 0147', '(206) 555-0199', '+44 7700 900193', "
                 "'+91 98765 43210', '0412 345 678']\nEMAIL: [zq.marlow@example.net]\n", encoding="utf-8")
    pairs = c.load_known(str(k))
    assert len(pairs) > 8                                    # the phones are expanded to their written formats ...
    assert c.listed_count(pairs) == 8                        # ... but count once each
    assert c.listed_count(pairs, by_type=True) == {"PERSON": 2, "PHONE": 5, "EMAIL": 1}
    s = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path), known_file=str(k)))
    assert s.known_count() == 8


@pytest.mark.parametrize("name,body", [
    ("pct", "%41" * 66000), ("pct_utf8", "%C3%A9" * 33000),
    ("b64", base64.b64encode(bytes(range(256)) * 800).decode()[:200000]),
    ("form", "&".join("field%d=value+with+%%2Fslash%%20and%%3D%d" % (i, i) for i in range(5000))),
    ("entities", "&#65;" * 40000), ("at", ("a" * 255 + "@") * 780), ("tags", "<br>x" * 50000)])
def test_large_encoded_bodies_are_linear(tmp_path, name, body):
    s = _scr(tmp_path)
    t0 = time.time()
    try:
        s.scrub_request({"messages": [{"role": "user", "content": body}]})
    except c.ScrubError:
        pass
    assert time.time() - t0 < 5, name                    # about 0.7 s on a laptop; was 18 s at 48 KB of %41


def test_email_window_scan_matches_full_scan():
    from swapback import detect as d
    import random
    rx = [r for r in d.Presidio().rules if r[0] == "EMAIL"][0][1]
    random.seed(7)
    for _ in range(2000):
        s = "".join(random.choice("ab.c_%-+@ x1Z\n'\"<>") for _ in range(random.randint(1, 60)))
        s += random.choice(["", " jo.e@ex-ample.com", " a@b.co ", "x@@y.org"])
        assert [m.span() for m in rx.finditer(s)] == [m.span() for m in d._at_windows(rx, s)]


@pytest.mark.parametrize("text", ["ship to 88 Larkspur Terrace<br>Apt 4B<br>Portland, OR 97214 ok",
                                  "88 Larkspur Terrace<br/>Apt 4B<br />Portland, OR 97214",
                                  "<p>88 Larkspur Terrace</p><p>Apt 4B</p><p>Portland, OR 97214</p>",
                                  "<b>Thaddeus</b> <b>Quillfeather</b>"])
def test_html_joined_lines_and_tagged_parts(tmp_path, text):
    s = _scr(tmp_path)
    fo = c.Forms()
    out = s.scrub_text(text, forms=fo)
    assert "\u00ab" in out and "Portland" not in out and "Quillfeather" not in out
    assert s.leftover_known(out) == 0 and s.restore_text(out, forms=fo) == text
    assert s.skeleton_hits(text)


@pytest.mark.parametrize("text", ["use <br> tags and <p> for layout in Portland", "a<b>b</b>c", "x < y > z"])
def test_html_tags_in_ordinary_text_left(tmp_path, text):
    s = _scr(tmp_path)
    assert s.scrub_text(text) == text and not s.skeleton_hits(text)
