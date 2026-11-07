import pytest

from reqreview import llm
from reqreview.diag import DiagLog, describe_retry, explain


@pytest.mark.parametrize("text,needle", [
    ("OpenAIPermissionDeniedError: Error code: 403 - Provider returned error", "403"),
    ("AuthenticationError: Error code: 401 - invalid_api_key", "API key"),
    ("Error code: 429 - rate limit exceeded", "Rate limited"),
    ("openai.NotFoundError: 404 No endpoints found for model", "No provider"),
    ("OPENROUTER_API_KEY is not set (LLM_PROVIDER=openrouter)", "No API key"),
    ("ModuleNotFoundError: No module named 'deepagents'", "dependency"),
])
def test_explain_known_errors(text, needle):
    e = explain(text)
    assert needle.lower() in (e.title + e.cause).lower()
    assert e.fix


def test_unknown_error_points_to_logs():
    assert "errors.log" in explain("some brand new failure").fix


def test_describe_retry_is_plain_english():
    level, text = describe_retry("throttle")
    assert level == "INFO" and "Not an error" in text


def test_difflog_writes_readable_lines_and_traceback(tmp_path):
    d = DiagLog(tmp_path)
    try:
        raise RuntimeError("Error code: 403 - Provider returned error")
    except RuntimeError as exc:
        d.error("AUTH-142", exc)
    assert "403" in d.errors.read_text(encoding="utf-8") and "Traceback" in d.errors.read_text(encoding="utf-8")
    assert any("ERROR" in ln and "AUTH-142" in ln for ln in d.tail(5, errors_only=True))


def test_provider_selection(monkeypatch):
    for k in ("LLM_PROVIDER", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "REVIEW_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert llm.provider() == "openai" and llm.has_key()
    assert llm.pick_models("reviewer")[0] == llm.OPENAI_DEFAULTS["reviewer"]
    monkeypatch.setenv("REVIEW_MODEL", "openai:gpt-test")
    assert llm.pick_models("reviewer")[0] == "gpt-test"        # legacy 'openai:' prefix accepted
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    assert llm.provider() == "openrouter"                       # openrouter wins when both keys exist
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert llm.provider() == "openai"                           # explicit choice wins
    assert llm.key_env_name() == "OPENAI_API_KEY"
