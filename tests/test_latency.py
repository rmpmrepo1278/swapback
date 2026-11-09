"""latency on a synthetic ~25k-character chat request (made-up values). Numbers are printed (pytest -s) and bounded
loosely so the test only fails on a real regression."""
import json, random, statistics, time

from swapback import core as s

WORDS = ("the program review moved to next week and the team asked for a short status on risks open items and "
         "the integration plan for the release").split()


def _body(seed):
    r = random.Random(seed)
    msgs = [{"role": "system", "content": " ".join(r.choice(WORDS) for _ in range(1500))}]
    for i in range(12):
        t = " ".join(r.choice(WORDS) for _ in range(150))
        msgs.append({"role": "user" if i % 2 else "assistant", "content": t + " cc Jane Q Doe jdoe@corp.example "
                     "call (425) 555-0182 from 1234 Elm Street, see /home/jane/notes.md req %d" % (seed * 100 + i)})
    return {"model": "m", "messages": msgs}


def test_latency(tmp_path):
    cfg = dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"))
    sc = s.Scrubber(cfg, known=[("Jane Q Doe", "PERSON"), ("jdoe@corp.example", "EMAIL")])
    cold, warm = [], []
    for seed in range(20):
        raw = json.dumps(_body(seed))
        sc.cache.clear()
        sc.verified.clear()
        t = time.perf_counter()
        sc.scrub_request(json.loads(raw))
        cold.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        sc.scrub_request(json.loads(raw))
        warm.append((time.perf_counter() - t) * 1000)
    out = {"chars": len(raw), "cold_ms_p50": round(statistics.median(cold), 1), "cold_ms_max": round(max(cold), 1),
           "warm_ms_p50": round(statistics.median(warm), 2), "warm_ms_max": round(max(warm), 2)}
    print("LATENCY " + json.dumps(out))
    assert out["cold_ms_p50"] < 500 and out["warm_ms_p50"] < 50
