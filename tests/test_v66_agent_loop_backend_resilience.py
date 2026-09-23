from types import SimpleNamespace

import pytest

from core.agent_loop import AgentLoop


class FakeBackendError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status={status_code}")
        self.status_code = status_code


class ScriptedCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if not self.outcomes:
            raise AssertionError("No scripted outcome left.")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.completions = ScriptedCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(content='{"action_type":"finish","final_answer":"ok"}'):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content)
            )
        ]
    )


def _bare_loop(base_url, outcomes):
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = base_url
    loop.client = FakeClient(outcomes)
    loop.progress_callback = None
    return loop


def test_local_backend_retries_502_then_succeeds(monkeypatch):
    monkeypatch.setattr("core.agent_loop.time.sleep", lambda *_: None)

    loop = _bare_loop(
        "http://127.0.0.1:11434/v1",
        [
            FakeBackendError(502),
            FakeBackendError(502),
            _response(),
        ],
    )

    response = loop._create_decision_completion_with_backend_retry(
        {"model": "qwen3.5:9b", "messages": []}
    )

    assert response.choices[0].message.content
    assert loop.client.completions.calls == 3


def test_local_backend_does_not_retry_non_retryable_400(monkeypatch):
    monkeypatch.setattr("core.agent_loop.time.sleep", lambda *_: None)

    loop = _bare_loop(
        "http://127.0.0.1:11434/v1",
        [FakeBackendError(400), _response()],
    )

    with pytest.raises(FakeBackendError):
        loop._create_decision_completion_with_backend_retry(
            {"model": "qwen3.5:9b", "messages": []}
        )

    assert loop.client.completions.calls == 1


def test_cloud_backend_keeps_single_request_semantics(monkeypatch):
    monkeypatch.setattr("core.agent_loop.time.sleep", lambda *_: None)

    loop = _bare_loop(
        "https://api.deepseek.com",
        [FakeBackendError(502), _response()],
    )

    with pytest.raises(FakeBackendError):
        loop._create_decision_completion_with_backend_retry(
            {"model": "deepseek-flash", "messages": []}
        )

    assert loop.client.completions.calls == 1


def test_local_acquisition_tool_allowlist_is_compact():
    state = {
        "runtime_context": {
            "current_stage": "acquisition",
        }
    }

    allowed = AgentLoop._local_stage_tool_allowlist(state)

    assert "search_web" in allowed
    assert "read_webpage" in allowed
    assert "download_data_file" in allowed
    assert "read_document" in allowed
    assert "read_office_data" in allowed

    assert "create_professional_word_report" not in allowed
    assert "export_multi_sheet_excel" not in allowed
    assert "apply_excel_edits" not in allowed


def test_non_acquisition_keeps_full_catalog():
    state = {
        "runtime_context": {
            "current_stage": "processing",
        }
    }

    assert AgentLoop._local_stage_tool_allowlist(state) is None


def test_finish_only_exposes_no_tools():
    state = {
        "runtime_context": {
            "current_stage": "verification",
        }
    }

    assert AgentLoop._local_stage_tool_allowlist(
        state,
        finish_only=True,
    ) == set()
