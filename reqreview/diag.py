"""Make failures easy to understand and fix.

* `explain(exc_or_text)` turns a raw provider/library error into: what happened, likely cause, how to fix.
* `describe_retry(kind, note)` does the same for the softer `llm.retry` events (throttling, failover...).
* `DiagLog` writes human-readable lines to `data/logs/studio.log` and full tracebacks to `data/logs/errors.log`.

Read them with `reqreview logs` (add `--errors` for only the problems), or open the Diagnostics tab (key 4).
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Explanation:
    title: str
    cause: str
    fix: str
    level: str = "ERROR"   # ERROR | WARN | INFO

    def as_dict(self) -> dict:
        return {"title": self.title, "cause": self.cause, "fix": self.fix, "level": self.level}


# (keywords that must ALL appear in the lowercased text, explanation). First match wins, order matters.
_RULES: list[tuple[tuple[str, ...], Explanation]] = [
    (("is not set",), Explanation(
        "No API key found",
        "The key for the selected provider is missing from the environment and from .env.",
        "Copy .env.example to .env and set OPENROUTER_API_KEY (or OPENAI_API_KEY with LLM_PROVIDER=openai). "
        "No key? Run `reqreview tui --demo`.")),
    (("insufficient_quota",), Explanation(
        "The account has no credit left",
        "The provider reports insufficient quota: a spending limit or an empty balance.",
        "Add credit or raise the limit in the provider dashboard, then retry. On OpenRouter, free models still "
        "need a small lifetime top-up for the higher daily request cap.")),
    (("401",), Explanation(
        "The provider rejected your API key (401)",
        "The key is wrong, revoked, or belongs to a different provider than LLM_PROVIDER says.",
        "Check the key in .env (no quotes or spaces), and that LLM_PROVIDER matches it. `reqreview doctor` shows which provider is active.")),
    (("invalid_api_key",), Explanation(
        "The provider rejected your API key",
        "The key is wrong or revoked.", "Create a new key and update .env.")),
    (("authenticationerror",), Explanation(
        "The provider rejected your API key",
        "Authentication failed.", "Check the key in .env and LLM_PROVIDER.")),
    (("403",), Explanation(
        "The provider refused this request (403)",
        "Usually one of: the model's provider blocks your region or account; the model needs access you don't have "
        "(OpenAI: some models need a verified organization); OpenRouter privacy settings exclude free endpoints; "
        "or the content was flagged by moderation.",
        "Run `reqreview doctor` to see which models work for you. Set REVIEW_MODEL and GRADER_MODEL to one that does. "
        "On OpenRouter also check Settings > Privacy (allow free endpoints). The app already skips a refused model and retries once.")),
    (("permissiondenied",), Explanation(
        "The provider refused this request (403)",
        "Permission denied for this model or region.", "Run `reqreview doctor` and pick a model that passes.")),
    (("no endpoints", ),  Explanation(
        "No provider is serving this model for you",
        "OpenRouter found no endpoint matching your privacy settings or required parameters (tool calling).",
        "Allow free endpoints in OpenRouter privacy settings, pick another model, or set OPENROUTER_REQUIRE_PARAMS=0 to test.")),
    (("model_not_found",), Explanation(
        "Model not found", "The model id does not exist for this provider or your account.",
        "Fix the id in REVIEW_MODEL / GRADER_MODEL / ANALYST_MODEL, or remove the override to use the default.")),
    (("does not exist",), Explanation(
        "Model not found", "The model id does not exist for this provider or your account.",
        "Fix the id in REVIEW_MODEL / GRADER_MODEL / ANALYST_MODEL, or remove the override to use the default.")),
    (("404",), Explanation(
        "Model or endpoint not found (404)", "The model id is wrong, retired, or unavailable to you.",
        "Free model ids rotate. Run `reqreview doctor` to see current candidates.")),
    (("429",), Explanation(
        "Rate limited (429)", "Too many requests per minute or per day for this key/model.",
        "Wait a minute and retry. On the OpenRouter free tier, top up $10 once to raise the daily cap, or lower OPENROUTER_RPS.", "WARN")),
    (("rate limit",), Explanation(
        "Rate limited", "Too many requests for this key/model.", "Wait and retry, or lower OPENROUTER_RPS / OPENAI_RPS.", "WARN")),
    (("timed out",), Explanation(
        "The model took too long (timeout)", "The provider is slow or overloaded, common for free models at peak hours.",
        "Retry, or switch to a faster model with REVIEW_MODEL.", "WARN")),
    (("timeout",), Explanation(
        "The model took too long (timeout)", "The provider is slow or overloaded.", "Retry, or switch model.", "WARN")),
    (("no usable model",), Explanation(
        "No model passed the pre-flight check", "Every candidate failed a real tool-calling test (see the probe lines above).",
        "Run `reqreview doctor` for the exact error per model, then set REVIEW_MODEL / GRADER_MODEL.")),
    (("no tool call",), Explanation(
        "The model does not call tools", "The agent needs tool calling; this model answered in plain text.",
        "Choose a model that supports tools (OpenRouter: filter models by 'tools').")),
    (("modulenotfounderror",), Explanation(
        "A Python dependency is missing", "A package this feature imports is not installed in this environment.",
        "Run `pip install -e .` in the project virtualenv. Demo mode needs only textual, rich, watchfiles and pydantic.")),
    (("no module named",), Explanation(
        "A Python dependency is missing", "A package is not installed in this environment.", "Run `pip install -e .`.")),
    (("importerror",), Explanation(
        "A library changed or is missing", "deepagents/langchain are beta and their import paths move between versions.",
        "Compare `pip freeze` with the versions you tested and pin them (see MASTER_PLAN.md section 14).")),
    (("unexpected keyword",), Explanation(
        "A library API changed", "A keyword argument is not accepted by your installed version (deepagents/langchain/textual).",
        "Look at the line in errors.log, then pin the library version that matches this code, or adjust the call.")),
    (("recursion", ), Explanation(
        "The agent hit its step limit", "It looped without finishing within the allowed number of turns.",
        "Check the trace for repeated tool calls; try a stronger model or raise VERIFICATION_MAX_TURNS.", "WARN")),
    (("jsondecodeerror",), Explanation(
        "A JSON file is not valid", "requirements.json (or another data file) has a syntax error.",
        "Open the file, fix the syntax (it must look like {\"ID\": \"text\"}), and save again.")),
]

_FALLBACK = Explanation(
    "Unexpected error", "This error is not one the app recognises.",
    "Open data/logs/errors.log for the full traceback (or run `reqreview logs --errors`). "
    "If it comes from deepagents, langchain or textual, a version mismatch is the usual cause.")


def explain(err: Any) -> Explanation:
    text = (f"{type(err).__name__} {err}" if isinstance(err, BaseException) else str(err)).lower()
    for keys, exp in _RULES:
        if all(k in text for k in keys):
            return exp
    return _FALLBACK


_RETRY_TEXT = {
    "throttle": ("INFO", "Waiting on purpose: the client-side rate limiter keeps requests under the provider's limit. Not an error."),
    "rate_limit": ("WARN", "The provider said 'too many requests'. The client is retrying with backoff."),
    "timeout": ("WARN", "A model call timed out. The client is retrying."),
    "probe_failed": ("WARN", "A model failed its pre-flight test and was skipped"),
    "model_failover": ("WARN", "A model was refused, so the job is retrying with another one"),
    "error": ("WARN", "A model call failed and is being retried"),
}


def describe_retry(kind: str, note: str = "") -> tuple[str, str]:
    """(level, plain sentence) for an llm.retry event."""
    level, text = _RETRY_TEXT.get(kind, ("WARN", f"LLM issue ({kind})"))
    if note and kind not in ("throttle",):
        exp = explain(note)
        if exp is not _FALLBACK:
            text += f". Likely cause: {exp.title.lower()}. {exp.fix}"
        else:
            text += f": {note[:200]}"
    return level, text


class DiagLog:
    """Readable log lines + full tracebacks. Safe to call from any thread; never raises."""

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.studio = self.dir / "studio.log"
        self.errors = self.dir / "errors.log"
        self._lock = threading.Lock()

    def _write(self, path: Path, line: str) -> None:
        try:
            with self._lock, path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def line(self, level: str, subject: str, text: str) -> None:
        self._write(self.studio, f"{self._now()} {level:<5} {subject:<11} {text}")

    def event(self, ev: Any) -> None:
        p = ev.payload
        bits = " ".join(f"{k}={v}" for k, v in p.items() if v not in ("", None, [], {}) and k not in ("cause", "fix"))
        level = "ERROR" if ev.type.endswith("failed") and ev.type.startswith("review") else \
                "WARN" if ev.type in ("grade.failed", "requirement.invalid") or ev.type == "llm.retry" else "INFO"
        self.line(level, ev.subject, f"{ev.type:<18} {bits[:300]} [{ev.correlation_id}]")

    def step(self, tid: str, step: Any) -> None:
        self.line("STEP", tid, f"#{step.step} {step.actor}: {step.action}" + (f"  -> {step.detail[:140]}" if step.detail else ""))

    def grade(self, tid: str, gp: Any) -> None:
        failed = ",".join(gp.failed) or "-"
        self.line("GRADE", tid, f"pass {gp.iteration} {gp.result} failed={failed}")
        for c in gp.criteria:
            if not c.get("passed") and c.get("gap"):
                self.line("GRADE", tid, f"  gap[{c.get('name')}]: {str(c['gap'])[:240]}")

    def error(self, tid: str, exc: BaseException, info: Optional[Explanation] = None) -> None:
        info = info or explain(exc)
        self.line("ERROR", tid, f"{info.title} | cause: {info.cause} | fix: {info.fix}")
        self._write(self.errors, f"==== {self._now()}  {tid}  {info.title}\n"
                                 f"cause: {info.cause}\nfix:   {info.fix}\n"
                                 + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def tail(self, n: int = 60, errors_only: bool = False) -> list[str]:
        path = self.studio
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        if errors_only:
            lines = [ln for ln in lines if " ERROR " in ln or " WARN " in ln]
        return lines[-n:]
