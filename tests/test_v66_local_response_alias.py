from types import SimpleNamespace

from core.agent_loop import AgentLoop


def _build_local_loop(registered_names=None):
    registered = set(registered_names or [])
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.registry = SimpleNamespace(
        resolve_name=lambda name: name if name in registered else None
    )
    return loop


def test_v66_local_generate_text_is_converted_to_finish():
    loop = _build_local_loop()

    decision = AgentLoop._normalize_decision_contract(
        {
            "action": "generate_text",
            "arguments": {
                "text": "先检查缺失值，再检查重复记录。",
            },
        }
    )

    normalized = loop._normalize_local_response_pseudo_action(decision)

    assert normalized["action_type"] == "finish"
    assert normalized["final_answer"] == "先检查缺失值，再检查重复记录。"
    assert normalized["_local_response_alias"] == "generate_text"


def test_v66_local_generate_response_content_is_converted_to_finish():
    loop = _build_local_loop()

    normalized = loop._normalize_local_response_pseudo_action(
        {
            "action_type": "tool",
            "tool": "generate_response",
            "arguments": {
                "content": "这是直接给用户的回答。",
            },
        }
    )

    assert normalized["action_type"] == "finish"
    assert normalized["final_answer"] == "这是直接给用户的回答。"


def test_v66_registered_same_name_tool_is_not_rewritten():
    loop = _build_local_loop({"generate_text"})
    decision = {
        "action_type": "tool",
        "tool": "generate_text",
        "arguments": {"text": "真实工具参数"},
    }

    normalized = loop._normalize_local_response_pseudo_action(decision)

    assert normalized == decision


def test_v66_pseudo_response_without_text_is_not_rewritten():
    loop = _build_local_loop()
    decision = {
        "action_type": "tool",
        "tool": "generate_text",
        "arguments": {},
    }

    normalized = loop._normalize_local_response_pseudo_action(decision)

    assert normalized == decision


def test_v66_cloud_backend_does_not_apply_local_response_alias():
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "https://api.deepseek.com"
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    decision = {
        "action_type": "tool",
        "tool": "generate_text",
        "arguments": {"text": "云端保持原协议，不做本地兼容。"},
    }

    normalized = loop._normalize_local_response_pseudo_action(decision)

    assert normalized == decision


def test_v66_merge_keeps_v64_response_only_helpers():
    assert hasattr(AgentLoop, "_goal_explicitly_requests_response_only")
    assert hasattr(AgentLoop, "_response_only_analysis_ready")
    assert hasattr(AgentLoop, "_build_response_only_analysis_final_answer")


def test_v66_merge_keeps_v64_response_only_file_guard():
    source = __import__("inspect").getsource(AgentLoop._decide_next_action)
    assert "_looks_like_file_output_tool" in source
    assert "_response_only_analysis_ready" in source
    assert "[Response-Only Guard]" in source


class _CaptureCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


def test_v66_local_advisory_goal_is_detected_strictly():
    goal = (
        "请简单说明你准备如何检查一个 Excel 数据表中的缺失值和重复记录。"
        "不需要读取文件，不需要联网，不需要生成任何文件，只告诉我处理思路。"
    )
    assert AgentLoop._goal_explicitly_requests_advisory_only(goal) is True


def test_v66_real_excel_execution_is_not_misclassified_as_advisory():
    goal = (
        "请读取我选择的 Excel 文件，检查缺失值和重复记录，"
        "按城市统计销售额合计并告诉我结果。不要联网，不需要生成文件。"
    )
    assert AgentLoop._goal_explicitly_requests_advisory_only(goal) is False


def test_v66_local_advisory_uses_compact_finish_only_prompt_without_retry():
    completions = _CaptureCompletions(
        '{"action_type":"finish","final_answer":"先检查缺失值，再检查重复记录。"}'
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.model = "qwen3.5:9b"
    loop.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    logs = []
    loop.report_progress = logs.append

    goal = (
        "请简单说明你准备如何检查一个 Excel 数据表中的缺失值和重复记录。"
        "不需要读取文件，不需要联网，不需要生成任何文件，只告诉我处理思路。"
    )
    decision = loop._decide_next_action(
        goal=goal,
        state={"runtime_context": {}},
    )

    assert decision["action_type"] == "finish"
    assert "缺失值" in decision["final_answer"]
    assert len(completions.calls) == 1
    call = completions.calls[0]
    assert call["reasoning_effort"] == "none"
    system_prompt = call["messages"][0]["content"]
    user_prompt = call["messages"][1]["content"]
    assert "不要搜索" in system_prompt
    assert "真实 Tool Registry" not in system_prompt
    assert "当前真实执行状态" not in user_prompt
    assert not any("纠错重试" in item for item in logs)


def test_v66_standard_finish_does_not_log_none_response_alias():
    completions = _CaptureCompletions(
        '{"action_type":"finish","final_answer":"完成。"}'
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.base_url = "http://127.0.0.1:11434/v1"
    loop.model = "qwen3.5:9b"
    loop.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    loop.registry = SimpleNamespace(resolve_name=lambda name: None)
    loop._build_system_prompt = lambda **kwargs: "Return JSON only."
    logs = []
    loop.report_progress = logs.append

    decision = loop._decide_next_action(
        goal="直接回答即可。",
        state={"runtime_context": {}},
    )

    assert decision["action_type"] == "finish"
    assert not any("[Local Response Alias]" in item for item in logs)
