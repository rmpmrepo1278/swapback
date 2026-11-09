"""CLI: --help, --version and bad arguments exit at once; they never load the config, start the proxy or bind a port."""
import json, os, pathlib, socket, subprocess, sys
import pytest

import swapback

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS = ("stats", "check-config", "health", "canary", "scrub-file", "known")


def _run(entry, args, env_extra, cwd, timeout=30):
    code = "import sys; from swapback.%s import main; main()" % entry
    env = dict(os.environ, **env_extra)
    for k in list(env):                                   # no stray settings from the caller's shell
        if (k.startswith("SWAPBACK_") or k.startswith("PII_SCRUBBER_")) and k not in env_extra:
            del env[k]
    # a timeout here means the process kept running, i.e. it started serving instead of exiting
    return subprocess.run([sys.executable, "-B", "-c", code, *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


@pytest.fixture
def held_port():
    """a loopback port we hold open: a proxy that tried to listen on it would fail with 'Address already in use'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def cfg_env(tmp_path, held_port):
    """a valid config whose listen address is the held port, so any start attempt is visible."""
    st = tmp_path / "st"
    st.mkdir()
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("mode: \"on\"\nlisten: 127.0.0.1:%d\nupstream: http://127.0.0.1:9\nstate_dir: %s\n" % (held_port, st))
    return {"SWAPBACK_CONFIG": str(cfgp)}


def test_port_hold_detects_a_start(cfg_env, tmp_path):
    # control: with no arguments the proxy really tries to listen, and the held port makes that fail fast
    r = _run("proxy", [], cfg_env, tmp_path)
    assert r.returncode != 0 and "Address already in use" in r.stderr


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_proxy_help_exits_without_starting(flag, cfg_env, tmp_path):
    r = _run("proxy", [flag], cfg_env, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: swapback-proxy")
    for word in ("SWAPBACK_CONFIG", "PII_SCRUBBER_CONFIG", "SWAPBACK_LISTEN", "127.0.0.1:8787", "allow_remote",
                 "swapback check-config", "swapback health"):
        assert word in r.stdout, word
    assert '"listening"' not in r.stdout and "Address already in use" not in r.stderr


def test_proxy_help_needs_no_config(tmp_path):
    r = _run("proxy", ["--help"], {"SWAPBACK_CONFIG": str(tmp_path / "missing.yaml")}, tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("usage: swapback-proxy")


@pytest.mark.parametrize("args", [["--bogus"], ["--port", "9000"], ["serve"]])
def test_proxy_unknown_argument_exits_2_without_starting(args, cfg_env, tmp_path):
    r = _run("proxy", args, cfg_env, tmp_path)
    assert r.returncode == 2
    assert "usage: swapback-proxy" in r.stderr and "unrecognized arguments" in r.stderr
    assert '"listening"' not in r.stdout and "Address already in use" not in r.stderr


def test_proxy_version(cfg_env, tmp_path):
    r = _run("proxy", ["--version"], cfg_env, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "swapback-proxy " + swapback.__version__


def test_version_matches_pyproject():
    import tomllib
    with open(ROOT / "pyproject.toml", "rb") as f:
        assert tomllib.load(f)["project"]["version"] == swapback.__version__


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_cli_help_lists_commands(flag, tmp_path):
    r = _run("core", [flag], {"SWAPBACK_CONFIG": str(tmp_path / "missing.yaml")}, tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("usage: swapback")
    for c in COMMANDS:
        assert c in r.stdout, c
    assert "SWAPBACK_CONFIG" in r.stdout and "swapback-proxy" in r.stdout


@pytest.mark.parametrize("cmd", COMMANDS)
def test_cli_subcommand_help(cmd, tmp_path):
    r = _run("core", [cmd, "--help"], {"SWAPBACK_CONFIG": str(tmp_path / "missing.yaml")}, tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("usage: swapback " + cmd), r.stderr


def test_cli_version(tmp_path):
    r = _run("core", ["--version"], {}, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "swapback " + swapback.__version__


@pytest.mark.parametrize("args", [["bogus"], ["--bogus"], ["scrub-file"]])
def test_cli_bad_input_exits_2(args, tmp_path):
    r = _run("core", args, {}, tmp_path)
    assert r.returncode == 2 and "usage: swapback" in r.stderr


def test_cli_no_command_still_runs_stats(cfg_env, tmp_path):
    r = _run("core", [], cfg_env, tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["mode"] == "on" and "vault" in out
