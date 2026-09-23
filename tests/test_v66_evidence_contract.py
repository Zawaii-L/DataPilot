from types import SimpleNamespace

from core.task_planner import TaskPlanner
from verification.verification_engine import VerificationEngine


UAV_TASK = """
我正在经营无人机培训基地。半年累计大约有80多人咨询，实际报名14人。
培训费8800元/人，单学员毛利6900元。营销费用约1000元左右。
如果营销费和咨询/报名人数属于同一个统计周期，再计算获客成本 CAC；
统计口径不清楚的指标必须标注“暂不能准确计算”。
请联网调研中山、珠海无人机培训、低空经济、就业岗位、职业教育和企业需求，
优先使用政府官网、招聘平台和企业官网。
建立保守、基准、乐观三种经营情景，预测未来3个月、6个月和12个月的
咨询人数、报名人数、收入和毛利，所有预测必须明确假设。
最后给出未来90天只能做3件事时的Top 3行动项。
第一轮只输出分析结果，不生成文件。
"""


def _contracts():
    return TaskPlanner._build_evidence_contract(UAV_TASK)


def _contract(contract_type):
    for item in _contracts():
        if item.get("type") == contract_type:
            return item
    raise AssertionError(f"missing contract: {contract_type}")


def _web_result(url, text):
    return SimpleNamespace(
        tool_name="read_webpage",
        success=True,
        arguments={"url": url},
        output={"url": url, "text": text},
    )


def test_v66_planner_builds_five_high_value_evidence_contracts():
    types = {item.get("type") for item in _contracts()}
    assert "approximate_numeric_fidelity" in types
    assert "conditional_calculation" in types
    assert "external_numeric_grounding" in types
    assert "forecast_matrix" in types
    assert "required_action_count" in types


def test_v66_evidence_contract_rejects_invented_exact_85_for_80_plus():
    item = _contract("approximate_numeric_fidelity")
    checks = VerificationEngine._verify_evidence_contract(
        contracts=[item],
        goal=UAV_TASK,
        final_answer=(
            "咨询人数：80多人。为便于估算取近似值85人。"
            "实际报名14人，因此14/85≈16.5%。"
        ),
        tool_results=[],
    )
    assert len(checks) == 1
    assert checks[0]["passed"] is False
    assert "精确化" in checks[0]["message"]


def test_v66_evidence_contract_rejects_numeric_cac_when_period_unknown():
    item = _contract("conditional_calculation")
    checks = VerificationEngine._verify_evidence_contract(
        contracts=[item],
        goal=UAV_TASK,
        final_answer=(
            "CAC：暂不能准确计算。若直接按现有数字，CAC = 1000 / 14 ≈ 71.4 元/人。"
        ),
        tool_results=[],
    )
    assert checks[0]["passed"] is False
    assert "CAC" in checks[0]["message"]


def test_v66_evidence_contract_rejects_unsupported_industry_benchmark():
    item = _contract("external_numeric_grounding")
    tools = [
        _web_result(
            "https://example.gov.cn/policy",
            "珠海高新区已聚集40多家低空经济企业，2023年产值42.45亿元。",
        )
    ]
    checks = VerificationEngine._verify_evidence_contract(
        contracts=[item],
        goal=UAV_TASK,
        final_answer=(
            "公开资料显示珠海高新区聚集40多家企业。"
            "职业教育行业咨询转化率通常为25%-40%。"
        ),
        tool_results=tools,
    )
    assert checks[0]["passed"] is False
    assert any("25" in item for item in checks[0]["evidence"])


def test_v66_evidence_contract_rejects_incomplete_forecast_cell():
    item = _contract("forecast_matrix")
    final_answer = """
### 保守情景
假设：流量不变。
- 3个月：咨询35人，报名6人，收入5.28万，毛利4.14万。
- 6个月：咨询70人，报名12人，收入10.56万，毛利8.28万。
- 12个月：咨询140人，报名24人，收入21.12万，毛利16.56万。
### 基准情景
假设：咨询与转化改善。
- 3个月：咨询50人，报名10人，收入8.8万，毛利6.9万。
- 6个月：咨询100人，报名20人，收入17.6万，毛利13.8万。
- 12个月：咨询200人，报名40人，收入35.2万，毛利27.6万。
### 乐观情景
假设：B端合作形成增量。
- 3个月：咨询80人，报名20人，收入17.6万，毛利13.8万。
- 6个月：咨询160人，报名40人，收入35.2万，毛利27.6万。
- 12个月：咨询300人，报名60人，收入52.8万。
"""
    checks = VerificationEngine._verify_evidence_contract(
        contracts=[item],
        goal=UAV_TASK,
        final_answer=final_answer,
        tool_results=[],
    )
    assert checks[0]["passed"] is False
    assert any("乐观-12个月" in item and "毛利" in item for item in checks[0]["evidence"])


def test_v66_evidence_contract_rejects_missing_top3():
    item = _contract("required_action_count")
    checks = VerificationEngine._verify_evidence_contract(
        contracts=[item],
        goal=UAV_TASK,
        final_answer="未来90天经营优先级：第一，优化内容；第二，增加体验课。",
        tool_results=[],
    )
    assert checks[0]["passed"] is False
    assert "3" in checks[0]["message"]


def test_v66_evidence_contract_passes_compliant_response_only_answer():
    contracts = _contracts()
    final_answer = """
# 经营诊断
当前咨询为80+人、报名14人。按80这个已知下界计算，14/80=17.5%，
因此真实咨询→报名转化率不高于17.5%，不是把80+改写成精确80。
CAC：暂不能准确计算，因为营销费用与咨询/报名人数的统计周期尚未确认一致。

外部资料显示：珠海高新区聚集40多家低空经济上下游企业，2023年产值42.45亿元；
猎聘当前页面显示本期新增1538个无人机飞手相关职位。以上只作为外部市场证据，
不能直接推导本基地一定增长。

### 保守情景
假设：流量基本不变。
- 3个月：咨询35人，报名6人，收入5.28万元，毛利4.14万元。
- 6个月：咨询70人，报名12人，收入10.56万元，毛利8.28万元。
- 12个月：咨询140人，报名24人，收入21.12万元，毛利16.56万元。
### 基准情景
假设：内容和体验课使咨询、转化小幅改善。
- 3个月：咨询50人，报名10人，收入8.8万元，毛利6.9万元。
- 6个月：咨询100人，报名20人，收入17.6万元，毛利13.8万元。
- 12个月：咨询200人，报名40人，收入35.2万元，毛利27.6万元。
### 乐观情景
假设：学校或企业合作带来新增线索。
- 3个月：咨询80人，报名20人，收入17.6万元，毛利13.8万元。
- 6个月：咨询160人，报名40人，收入35.2万元，毛利27.6万元。
- 12个月：咨询240人，报名60人，收入52.8万元，毛利41.4万元。

## 未来90天优先级 Top 3
第一：建立咨询漏斗台账，并记录来源、到店和成交。
第二：上线固定体验课，用到店率和体验后成交率验证价值表达。
第三：验证一个学校或企业合作试点，并设置明确继续/停止标准。
"""
    tools = [
        _web_result(
            "https://www.zhuhai-hitech.gov.cn/example",
            "珠海高新区聚集40多家低空经济上下游企业，其中2023年产值42.45亿元。",
        ),
        _web_result(
            "https://www.liepin.com/example",
            "无人机飞手招聘专场，本期新增1538个职位。",
        ),
    ]
    checks = VerificationEngine._verify_evidence_contract(
        contracts=contracts,
        goal=UAV_TASK,
        final_answer=final_answer,
        tool_results=tools,
    )
    assert checks
    assert all(item["passed"] for item in checks), checks


def test_v66_completion_gate_is_blocked_by_evidence_contract_failure():
    contract = _contract("conditional_calculation")
    engine = VerificationEngine()
    loop_result = SimpleNamespace(
        success=True,
        stop_reason="completed",
        goal=UAV_TASK,
        final_answer="CAC = 1000 / 14 ≈ 71.4 元/人。",
        tool_results=[],
    )
    report = engine.verify(
        task_plan={
            "task_goal": UAV_TASK,
            "deliverable_requirements": ["向用户直接给出分析结果和结论。"],
            "evidence_requirements": [],
            "source_requirements": [],
            "execution_requirements": [],
            "verification_requirements": [],
            "safety_requirements": [],
            "assumptions": [],
            "evidence_contract": [contract],
        },
        loop_result=loop_result,
        runtime_context={},
        workspace_summary={},
    )
    assert report.verified is False
    assert any(check.category == "evidence_contract" and not check.passed for check in report.checks)
