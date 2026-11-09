"""swapback.hooks: optional extension points for the proxy (0.4.0).

A hooks module is plain Python that you name in swapback.yaml:

    hooks:
      module: my_hooks            # an importable module name, or a path to a .py file (relative to the config file)
      options: {daily_usd: 5}     # any mapping; handed to setup()

Every function is optional; define only the ones you need:

    setup(options, cfg)                  once, when the proxy starts (start your own background threads here)
    before_forward(req, ctx)             before each request is sent upstream
    on_upstream_response(resp, ctx)      when the upstream answers an error status (>= 400) or cannot be reached
    on_usage(obj, ctx)                   each JSON response object or SSE event, as the provider sent it
    on_complete(result, ctx)             once per request, after the reply went to the client
    extra_routes()                       {"/path": fn} extra GET endpoints; fn() returns a JSON-able object
                                         or (status, object)
    alert(text)                          deliver swapback's alert lines yourself (instead of alert_webhook)

What a hook sees: the request AFTER scrubbing (mode on), so placeholders such as \u00abEMAIL_1\u00bb, never the real
values, and the provider's replies BEFORE they are restored. In mode watch or off nothing is scrubbed, so a hook
sees what is forwarded.

before_forward and on_upstream_response may return None (carry on) or a dict that changes where the request goes:
    {"url": "http://127.0.0.1:8080/v1/chat/completions",   # send it here instead (full URL); optional
     "body": {...},                                         # replace the (scrubbed) JSON body; optional
     "headers": {...},                                      # replace the outgoing headers; optional
     "timeout_s": 600}
or answer the client directly, without calling any upstream (before_forward only):
    {"respond": {"status": 429, "json": {"error": {"message": "daily spend cap reached"}}}}
req.headers is a dict of the outgoing headers; changing it in place changes what is sent (for example to drop a
header that only your hook reads). Replies from a changed route are restored exactly like the upstream's.
on_upstream_response gets resp with .status (None when unreachable), .body (bytes, the error the upstream sent),
.headers and .error; its route is tried once, and if that also fails the client gets the original answer.

Failure policy: an exception in setup stops the proxy from starting; an exception in before_forward or
on_upstream_response fails closed (503, nothing forwarded), like a scrub error. on_usage, on_complete and alert run
after the reply has started, so their exceptions are counted (hook_errors in /scrubber/health) and ignored.
"""
import importlib
import importlib.util
import os
import sys

NAMES = ("setup", "before_forward", "on_upstream_response", "on_usage", "on_complete", "extra_routes", "alert")


class HookError(Exception):
    pass


class Request:
    """what before_forward sees. body: the parsed JSON body as it will be forwarded (scrubbed in mode on), or None."""

    def __init__(self, method, path, headers, body, api, mode):
        self.method, self.path, self.headers, self.body, self.api, self.mode = method, path, headers, body, api, mode


class Response:
    """what on_upstream_response sees: status None means the upstream could not be reached (error names why)."""

    def __init__(self, status=None, body=b"", headers=None, error=""):
        self.status, self.body, self.headers, self.error = status, body, dict(headers or {}), error

    def json(self):
        import json
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


def spec(cfg):
    """the hooks section as {"module": str, "options": dict}, or None. A bare string is the module."""
    h = (cfg or {}).get("hooks")
    if not h:
        return None
    if isinstance(h, str):
        h = {"module": h}
    if not isinstance(h, dict) or not (h.get("module") or h.get("entry_point")):
        raise ValueError("hooks needs module: <module name or path to a .py file> (or entry_point: <name>)")
    opts = h.get("options") or {}
    if not isinstance(opts, dict):
        raise ValueError("hooks.options must be a mapping")
    return {"module": h.get("module"), "entry_point": h.get("entry_point"), "options": opts}


def _import(name, base):
    if name.endswith(".py") or os.sep in name or "/" in name:
        path = os.path.join(base or os.getcwd(), os.path.expanduser(name))
        if not os.path.exists(path):
            raise HookError("hooks module not found: %s" % os.path.basename(path))
        mod_name = "swapback_hooks_" + os.path.splitext(os.path.basename(path))[0]
        sp = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(sp)
        sys.modules[mod_name] = mod
        d = os.path.dirname(path)
        if d not in sys.path:                    # so the hooks file can import its neighbours
            sys.path.insert(0, d)
        sp.loader.exec_module(mod)
        return mod
    return importlib.import_module(name)


def _entry_point(name):
    from importlib.metadata import entry_points
    eps = entry_points()
    group = eps.select(group="swapback.hooks") if hasattr(eps, "select") else eps.get("swapback.hooks", [])
    for ep in group:
        if ep.name == name:
            return ep.load()
    raise HookError("no swapback.hooks entry point named %s" % name)


class Hooks:
    """the loaded hooks (or none). Calls are no-ops for functions the module does not define."""

    def __init__(self, cfg=None, module=None, options=None):
        self.mod, self.options, self.errors, self.routes = None, options or {}, 0, {}
        if module is not None:                   # tests and embedders can pass a module object directly
            self.mod = module
        else:
            sp = spec(cfg)
            if sp:
                self.options = sp["options"]
                base = (cfg or {}).get("_config_dir") or os.getcwd()
                self.mod = _entry_point(sp["entry_point"]) if sp.get("entry_point") else _import(sp["module"], base)
        self.fns = {n: getattr(self.mod, n) for n in NAMES if self.mod is not None and callable(getattr(self.mod, n, None))}

    def __bool__(self):
        return bool(self.fns)

    def has(self, name):
        return name in self.fns

    @property
    def name(self):
        return getattr(self.mod, "__name__", "") if self.mod is not None else ""

    def setup(self, cfg):
        if "setup" in self.fns:
            self.fns["setup"](self.options, cfg)
        if "extra_routes" in self.fns:
            r = self.fns["extra_routes"]() or {}
            if not isinstance(r, dict) or not all(isinstance(k, str) and k.startswith("/") and callable(v)
                                                  for k, v in r.items()):
                raise HookError("extra_routes() must return {\"/path\": callable}")
            self.routes = r

    def call(self, name, *args):
        """strict call (fail closed): exceptions propagate to the proxy."""
        fn = self.fns.get(name)
        return fn(*args) if fn else None

    def soft(self, name, *args):
        """after the reply started: exceptions are counted, never raised."""
        fn = self.fns.get(name)
        if not fn:
            return None
        try:
            return fn(*args)
        except Exception:
            self.errors += 1
            return None

    def route(self, path):
        p = path.split("?")[0]
        for k, fn in self.routes.items():
            if p == k or p.rstrip("/") == k.rstrip("/"):
                return fn
        return None


def check_action(a, where):
    """validate a hook's return value; returns it normalised (or None)."""
    if a is None:
        return None
    if not isinstance(a, dict):
        raise HookError("%s must return None or a dict" % where)
    if "respond" in a:
        if where != "before_forward":
            raise HookError("only before_forward may answer directly (respond)")
        r = a["respond"]
        if not isinstance(r, dict) or not isinstance(r.get("status", 200), int):
            raise HookError("respond must be {\"status\": int, \"json\": ...}")
        return {"respond": {"status": int(r.get("status", 200)), "json": r.get("json"),
                            "headers": dict(r.get("headers") or {})}}
    bad = set(a) - {"url", "body", "headers", "timeout_s", "method"}
    if bad:
        raise HookError("%s returned unknown keys: %s" % (where, ", ".join(sorted(bad))))
    if a.get("url"):
        import urllib.parse
        u = urllib.parse.urlparse(str(a["url"]))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise HookError("%s url must be an http(s) URL" % where)
    return a
