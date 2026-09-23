import json

import pytest

from core.task_planner import TaskPlanner


class Fake502(Exception):
    status_code = 502


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, payload):
        self.choices = [FakeChoice(json.dumps(payload, ensure_ascii=False))]


class ScriptedCompletions:
    def __init__(self, failures_before_success=0, payload=None, always_fail=False):
        self.failures_before_success = failures_before_success
        self.payload = payload or {
            "task_goal": "测试",
            "evidence_requirements": [],
            "source_requirements": [],
            "deliverable_requirements": [],
            "execution_requirements": ["完成测试"],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
        }
        self.always_fail = always_fail
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.always_fail or self.calls <= self.failures_before_success:
            raise Fake502("502")
        return FakeResponse(self.payload)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def _local_planner(monkeypatch, completions, **kwargs):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    return TaskPlanner(
        client=FakeClient(completions),
        model="qwen3.5:9b",
        backend_retry_delay=0,
        **kwargs,
    )


def test_local_502_retries_then_succeeds(monkeypatch):
    completions = ScriptedCompletions(failures_before_success=2)
    planner = _local_planner(monkeypatch, completions)

    plan = planner.create_plan("请分析这个任务")

    assert completions.calls == 3
    assert plan.execution_requirements == ["完成测试"]
    assert completions.last_kwargs["reasoning_effort"] == "none"


def test_local_repeated_502_uses_deterministic_fallback(monkeypatch):
    completions = ScriptedCompletions(always_fail=True)
    planner = _local_planner(monkeypatch, completions)

    task = (
        "请先使用我提供的数据做经营诊断，再联网调研公开资料，"
        "并做保守、基准、乐观预测。统计周期不一致时不要混算 CAC。"
        "第一轮只告诉我结果，不要生成任何文件。"
    )
    plan = planner.create_plan(task)

    assert completions.calls == 3
    assert plan.task_goal == task
    assert any("真实公开资料" in x for x in plan.evidence_requirements)
    assert any("未来预测" in x and "假设" in x for x in plan.execution_requirements)
    assert any("CAC" in x for x in plan.assumptions)
    assert plan.deliverable_requirements == ["向用户直接给出分析结果和结论。"]


def test_local_compact_prompt_is_used(monkeypatch):
    completions = ScriptedCompletions()
    planner = _local_planner(monkeypatch, completions)

    planner.create_plan("请联网调研，不要生成文件，只告诉我结果")

    system_prompt = completions.last_kwargs["messages"][0]["content"]
    assert "DataPilot Task Planner" in system_prompt
    assert "核心规则" not in system_prompt
    assert len(system_prompt) < len(planner._build_system_prompt())


def test_cloud_502_is_not_silently_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    completions = ScriptedCompletions(always_fail=True)
    planner = TaskPlanner(
        client=FakeClient(completions),
        model="deepseek-flash",
        backend_retry_delay=0,
    )

    with pytest.raises(Fake502):
        planner.create_plan("测试")

    assert completions.calls == 1


def test_local_non_retryable_400_is_not_hidden(monkeypatch):
    class Fake400(Exception):
        status_code = 400

    class BadRequestCompletions:
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise Fake400("bad request")

    completions = BadRequestCompletions()
    planner = _local_planner(monkeypatch, completions)

    with pytest.raises(Fake400):
        planner.create_plan("测试")

    assert completions.calls == 1


def test_local_connection_failure_is_not_disguised_as_plan(monkeypatch):
    class APIConnectionError(Exception):
        pass

    class ConnectionCompletions:
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise APIConnectionError("connection refused")

    completions = ConnectionCompletions()
    planner = _local_planner(monkeypatch, completions)

    with pytest.raises(APIConnectionError):
        planner.create_plan("测试")

    assert completions.calls == 3
