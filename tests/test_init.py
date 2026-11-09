"""tests for `swapback init` and the example files shipped inside the package."""
import json, os, stat, subprocess, sys
import pytest

from swapback import core as s

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("src", [x[0] for x in s.EXAMPLES])
def test_packaged_examples_match_the_repo_copies(src):
    root = os.path.join(ROOT, src)
    if not os.path.exists(root):                          # running against an installed wheel without the repo
        pytest.skip("repo copy not present")
    assert s.example_text(src) == open(root, encoding="utf-8").read()


def test_init_writes_private_files_and_keeps_existing(tmp_path):
    d = tmp_path / "cfg"
    r = subprocess.run([sys.executable, "-m", "swapback.core", "init", "--dir", str(d)], capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out["files"].values()) == {"written"}
    for _, dst in s.EXAMPLES:
        assert stat.S_IMODE(os.stat(d / dst).st_mode) == 0o600
    (d / "known.yaml").write_text("PERSON:\n  - Jane Q Doe\n")
    r = subprocess.run([sys.executable, "-m", "swapback.core", "init", "--dir", str(d)], capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert "exists" in json.loads(r.stdout)["files"]["known.yaml"]
    assert (d / "known.yaml").read_text() == "PERSON:\n  - Jane Q Doe\n"
    cfg = s.load_cfg(str(d / "swapback.yaml"))            # the starter config loads as written
    assert cfg["mode"] == "on"
    s.init_files(str(d), force=True)
    assert (d / "known.yaml").read_text() == s.example_text("known.example.yaml")
