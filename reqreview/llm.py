"""Model layer: OpenRouter (default, free models) or OpenAI, selected with LLM_PROVIDER.

    LLM_PROVIDER=openrouter | openai      (default: whichever key is set; openrouter wins if both are)
    OPENROUTER_API_KEY / OPENAI_API_KEY   (OPENAI_BASE_URL for Azure or any OpenAI-compatible server)
    REVIEW_MODEL / GRADER_MODEL / ANALYST_MODEL   override the model per role

* `get_chat_model(role)` returns a LangChain `ChatOpenAI` (deepagents needs a LangChain chat model).
* Every model is verified with a real tool-calling probe before use; models that return 403/404 are skipped.
* OpenRouter only: free model discovery, request-level fallbacks, client-side rate limiter.
* Failures are explained in plain English by `reqreview.diag.explain`.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Literal, Optional

import httpx

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

Role = Literal["reviewer", "grader", "analyst"]
ENV_OVERRIDE = {"reviewer": "REVIEW_MODEL", "grader": "GRADER_MODEL", "analyst": "ANALYST_MODEL"}

# OpenRouter free models: kept only if currently listed as free + tool-capable.
PREFERRED: dict[str, list[str]] = {
    "reviewer": ["nvidia/nemotron-3-super-120b-a12b:free", "openai/gpt-oss-20b:free",
                 "google/gemma-4-31b-it:free", "qwen/qwen3-coder:free"],
    "grader": ["openai/gpt-oss-20b:free", "nvidia/nemotron-3-super-120b-a12b:free",
               "google/gemma-4-31b-it:free", "qwen/qwen3-coder:free"],
    "analyst": ["nvidia/nemotron-3-super-120b-a12b:free", "openai/gpt-oss-120b:free",
                "qwen/qwen3-coder:free", "openai/gpt-oss-20b:free"],
}
# OpenAI defaults (the original project used gpt-5.4-mini). Override per role with the env vars above.
OPENAI_DEFAULTS = {"reviewer": "gpt-5.4-mini", "grader": "gpt-5.4-mini", "analyst": "gpt-5.4-mini"}

# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def provider() -> str:
    """'openrouter' or 'openai'. Explicit LLM_PROVIDER wins, then whichever key exists."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit in ("openrouter", "openai"):
        return explicit
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "openrouter"


def api_key() -> Optional[str]:
    return os.getenv("OPENAI_API_KEY") if provider() == "openai" else os.getenv("OPENROUTER_API_KEY")


def key_env_name() -> str:
    return "OPENAI_API_KEY" if provider() == "openai" else "OPENROUTER_API_KEY"


def has_key() -> bool:
    return bool(api_key())


def base_url() -> str:
    if provider() == "openai":
        return os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL)
    return OPENROUTER_BASE_URL


def _clean(model: str) -> str:
    """Accept 'openai:gpt-5.4-mini' style ids from older configs."""
    return model.split(":", 1)[1] if (provider() == "openai" and model.startswith("openai:")) else model


# ---------------------------------------------------------------------------
# Event sink: the runner points this at the dispatcher while a job runs.
# ---------------------------------------------------------------------------

_sink: Optional[Callable[[str, dict], None]] = None


def set_event_sink(fn: Optional[Callable[[str, dict], None]]) -> None:
    global _sink
    _sink = fn


def _emit(kind: str, payload: dict) -> None:
    fn = _sink
    if fn is not None:
        try:
            fn(kind, payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Discovery (OpenRouter only)
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {"t": 0.0, "models": None}


def discover_free_models(force: bool = False, ttl: float = 300.0) -> Optional[list[dict]]:
    """Free models that support tool calling, or None if OpenRouter is unreachable / not in use."""
    if provider() != "openrouter":
        return None
    if not force and _cache["models"] is not None and time.time() - _cache["t"] < ttl:
        return _cache["models"]
    try:
        r = httpx.get(f"{OPENROUTER_BASE_URL}/models", timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception:
        return None
    out: list[dict] = []
    for m in data:
        pricing = m.get("pricing") or {}
        try:
            free = float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
        except (TypeError, ValueError):
            free = False
        if free and "tools" in (m.get("supported_parameters") or []):
            out.append({"id": m["id"], "context": m.get("context_length") or 0})
    _cache.update(t=time.time(), models=out)
    return out


def pick_models(role: str) -> list[str]:
    """Ordered candidates for a role (env override first)."""
    override = os.getenv(ENV_OVERRIDE[role], "").strip()
    if provider() == "openai":
        ordered = [OPENAI_DEFAULTS[role]]
        if override:
            ordered = [_clean(override)] + [m for m in ordered if m != _clean(override)]
        return ordered
    pref = PREFERRED[role]
    found = discover_free_models()
    if found is None:  # offline: let OpenRouter be the judge
        ordered = list(pref)
    else:
        ids = {m["id"] for m in found}
        ordered = [m for m in pref if m in ids]
        extra = sorted((m for m in found if m["id"] not in ordered), key=lambda m: -m["context"])
        ordered += [m["id"] for m in extra[:3]]
        if not ordered:
            ordered = list(pref)
    if override:
        ordered = [override] + [m for m in ordered if m != override]
    return ordered


# ---------------------------------------------------------------------------
# Rate limiting + callbacks
# ---------------------------------------------------------------------------

_limiter: Any = None


def _get_limiter() -> Any:
    """Shared limiter. OpenRouter free tier is ~20 req/min per key; OpenAI is unlimited unless OPENAI_RPS is set."""
    global _limiter
    on_or = provider() == "openrouter"
    rps = os.getenv("OPENROUTER_RPS", "0.3") if on_or else os.getenv("OPENAI_RPS", "")
    if not rps:
        return None
    if _limiter is None:
        from langchain_core.rate_limiters import InMemoryRateLimiter

        class EventingRateLimiter(InMemoryRateLimiter):
            def acquire(self, *, blocking: bool = True) -> bool:
                if super().acquire(blocking=False):
                    return True
                if not blocking:
                    return False
                _emit("throttle", {"note": "client-side limiter is holding a request"})
                return super().acquire(blocking=True)

        _limiter = EventingRateLimiter(requests_per_second=float(rps), check_every_n_seconds=0.1, max_bucket_size=2)
    return _limiter


def _make_callback() -> Any:
    from langchain_core.callbacks import BaseCallbackHandler

    class _ErrorCallback(BaseCallbackHandler):
        def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
            msg = f"{type(error).__name__}: {error}"
            low = msg.lower()
            kind = "rate_limit" if ("429" in msg or "rate" in low) else ("timeout" if "time" in low else "error")
            _emit(kind, {"note": msg[:300]})

    return _ErrorCallback()


# ---------------------------------------------------------------------------
# Model selection with verification + failover
# ---------------------------------------------------------------------------

_verified: dict[str, str] = {}   # role -> model id that passed a probe this session
_bad: set[str] = set()           # models that returned 403/404/provider errors this session


def mark_bad(*model_ids: str) -> None:
    for m in model_ids:
        if m:
            _bad.add(m)
    for role, m in list(_verified.items()):
        if m in _bad:
            del _verified[role]


def is_access_error(exc: BaseException) -> bool:
    """403 / 404 / 'no endpoints' / provider refusals: this model is unusable for this account or region."""
    low = f"{type(exc).__name__} {exc}".lower()
    return any(k in low for k in ("403", "permissiondenied", "permission denied", "no endpoints", "404",
                                  "provider returned error", "not available in your region",
                                  "user location is not supported", "model_not_found", "does not exist"))


def current_model(role: str) -> str:
    return _verified.get(role) or ([m for m in pick_models(role) if m not in _bad] or pick_models(role))[0]


def select_model(role: str) -> str:
    """First candidate that passes a real tool-calling probe."""
    if role in _verified:
        return _verified[role]
    cands = [m for m in pick_models(role) if m not in _bad]
    if (os.getenv("LLM_SKIP_PROBE") == "1" or os.getenv("OPENROUTER_SKIP_PROBE") == "1") and cands:
        return cands[0]
    last = "no candidates"
    for m in cands[:6]:
        res = probe(m)
        if res["ok"]:
            _verified[role] = m
            return m
        _bad.add(m)
        last = f"{m}: {res['detail']}"
        _emit("probe_failed", {"note": last[:400]})
    raise RuntimeError(
        f"No usable model for role '{role}' on {provider()}. Last probe failed with: {last}. "
        f"Run `reqreview doctor`, or set {ENV_OVERRIDE[role]} to a model your account can use.")


def get_chat_model(role: Role, model_id: Optional[str] = None, fallbacks: bool = True) -> Any:
    """LangChain chat model for a role on the selected provider."""
    from langchain_openai import ChatOpenAI

    key = api_key()
    if not key:
        raise RuntimeError(f"{key_env_name()} is not set (LLM_PROVIDER={provider()}). Add it to .env, or run with --demo.")
    primary = model_id or select_model(role)
    kwargs: dict[str, Any] = dict(model=primary, api_key=key, base_url=base_url(), timeout=120, max_retries=5,
                                  callbacks=[_make_callback()])
    limiter = _get_limiter()
    if limiter is not None:
        kwargs["rate_limiter"] = limiter
    if provider() == "openrouter":
        extra: dict[str, Any] = {}
        if os.getenv("OPENROUTER_REQUIRE_PARAMS", "1") != "0":
            extra["provider"] = {"require_parameters": True}   # route only to providers that honour tools
        if fallbacks and os.getenv("OPENROUTER_FALLBACKS", "1") != "0":
            alts = [m for m in pick_models(role) if m != primary and m not in _bad][:3]
            if alts:
                extra["models"] = alts                          # OpenRouter retries the next one on failure
        if extra:
            kwargs["extra_body"] = extra
    return ChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Probe + quota (used by `reqreview doctor` and the TUI header)
# ---------------------------------------------------------------------------

def probe(model_id: str) -> dict:
    """One call: the model must answer with a valid tool call."""
    t0 = time.time()
    try:
        from langchain.tools import tool

        @tool
        def ping(word: str) -> str:
            """Echo a word."""
            return word

        llm = get_chat_model("reviewer", model_id=model_id, fallbacks=False).bind_tools([ping])
        resp = llm.invoke("Call the ping tool with word='ok'. Do not answer in text.")
        ok = bool(getattr(resp, "tool_calls", None))
        detail = "tool call returned" if ok else "answered in text, no tool call"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:400]
    return {"model": model_id, "ok": ok, "seconds": round(time.time() - t0, 1), "detail": detail}


def quota() -> Optional[dict]:
    """OpenRouter key usage/limit, best effort. OpenAI has no public per-key quota endpoint."""
    key = os.getenv("OPENROUTER_API_KEY")
    if provider() != "openrouter" or not key:
        return None
    for path in ("/key", "/auth/key"):
        try:
            r = httpx.get(OPENROUTER_BASE_URL + path, headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                body = r.json()
                d = body.get("data", body)
                return {"usage": d.get("usage"), "limit": d.get("limit"),
                        "is_free_tier": d.get("is_free_tier"), "label": d.get("label")}
        except Exception:
            continue
    return None


def quota_text() -> str:
    if provider() == "openai":
        return "OpenAI billing: see platform.openai.com/usage"
    q = quota()
    if not q:
        return "quota n/a"
    usage, limit = q.get("usage"), q.get("limit")
    tier = "free tier" if q.get("is_free_tier") else "credits"
    if usage is None:
        return tier
    return f"${usage:.2f} used" + (f" of ${limit:.2f}" if limit else "") + f" · {tier}"
