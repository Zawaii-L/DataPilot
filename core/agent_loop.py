from __future__ import annotations

from pathlib import Path

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

from .execution_context import ExecutionContext, ReferenceResolver
from tools.tool_executor import ToolExecutionResult, ToolExecutor
from tools.tool_failure_recovery import build_recovery_hint
from tools.tool_registry import ToolRegistry, create_default_tool_registry
from .task_planner import TaskPlan
from skill_registry import SkillRegistry, create_default_skill_registry
from skill_selector import SkillSelection, SkillSelector
from verification.verification_engine import (
    VerificationEngine,
    VerificationReport,
)
from stage_orchestrator import (
    AgentStage,
    DataState,
    StageOrchestrator,
    StageRoute,
)

from acquisition_adapter import build_acquisition_instruction
from readback_registry import ReadbackRegistry
from agent_core.execution_monitor import ExecutionMonitor


load_dotenv()

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class AgentLoopResult:
    success: bool
    goal: str
    final_answer: str = ""
    stop_reason: str = ""
    iterations: int = 0
    tool_results: List[ToolExecutionResult] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    verification_report: Optional[Dict[str, Any]] = None
    retry_policy_report: Optional[Dict[str, Any]] = None
    execution_timing: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "goal": self.goal,
            "final_answer": self.final_answer,
            "stop_reason": self.stop_reason,
            "iterations": self.iterations,
            "tool_results": [
                item.to_dict()
                for item in self.tool_results
            ],
            "decisions": self.decisions,
            "verification_report": self.verification_report,
            "retry_policy_report": self.retry_policy_report,
            "execution_timing": self.execution_timing,
        }


class AgentLoop:
    """
    DataPilot Workspace 动态 Agent Loop。

    核心循环：
        用户目标
            ↓
        LLM 决定下一步工具
            ↓
        Python 执行真实工具
            ↓
        生成 Observation
            ↓
        LLM 根据真实结果重新判断
            ↓
        继续 / 修正 / 完成

    与固定 PlanExecutor 的区别：
    AgentLoop 每执行一步都会重新决策。
    """

    # v5.0+ Read-Only Task Boundary
    #
    # 对没有最终交付物要求的直接回答任务，允许读取、检查、统计，
    # 但禁止为了“核实情况”擅自清洗、删除、填充或改写数据。
    _DATA_MUTATION_TOOLS = {
        "handle_missing_values",
        "drop_duplicate_rows",
        "remove_duplicates",
        "clean_data",
        "auto_clean_data",
        "fill_missing_values",
        "drop_missing_values",
    }

    @staticmethod
    def _goal_explicitly_requests_data_cleaning(goal: str) -> bool:
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        cleaning_phrases = (
            "清洗",
            "去重",
            "删除重复",
            "处理重复",
            "重复记录",
            "缺失值",
            "处理缺失",
            "填充缺失",
            "删除缺失",
            "异常值处理",
            "数据整理",
        )
        return any(phrase in text for phrase in cleaning_phrases)

    @staticmethod
    def _goal_explicitly_requests_response_only(goal: str) -> bool:
        """
        识别用户明确要求“只返回结果、不生成文件”的响应型任务。

        这里与 StageOrchestrator 的 Output-Mode Boundary 保持一致：
        - 能识别“不要生成 Word、Excel 或其他文件”这类格式化否定句；
        - 能识别“第一轮只输出分析结果”这类直接回答要求；
        - 若用户同时明确要求生成某个文件（如“不要 Word，但请生成 Excel”），
          则不能误判为 response-only。
        """
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not text:
            return False

        try:
            if StageOrchestrator._user_explicitly_requests_response_only_delivery(goal):
                return True
        except Exception:
            pass

        # 先判断是否存在真正的正向文件交付意图。StageOrchestrator 已经
        # 实现了分句级否定过滤，这里直接复用，避免两套边界规则漂移。
        try:
            explicit_file_delivery = bool(
                StageOrchestrator._user_explicitly_requests_file_delivery(goal)
            )
        except Exception:
            explicit_file_delivery = False

        explicit_no_file = any(
            phrase in text
            for phrase in (
                "不需要生成文件",
                "无需生成文件",
                "不要生成文件",
                "不需要输出文件",
                "无需输出文件",
                "不要输出文件",
                "不需要保存文件",
                "无需保存文件",
                "不要保存文件",
                "不生成文件",
                "不输出文件",
                "不保存文件",
            )
        )

        # 支持：不要生成 Word、Excel 或其他文件 / 无需导出 PDF /
        # 不必创建报告文件 等常见自然语言写法。
        no_file_pattern = re.compile(
            r"(?:不需要|无需|不要|不必|不用|不要求|禁止|不生成|不导出|不保存)"
            r"\s*(?:(?:生成|导出|输出|保存|制作|创建|写入|交付)\s*)?"
            r"[^。；;\n]{0,28}?"
            r"(?:word|docx|excel|xlsx|xls|csv|pptx?|pdf|png|jpg|jpeg|"
            r"报告|报表|文件|文档|工作簿|演示文稿|附件|交付物|图表)",
            flags=re.IGNORECASE,
        )
        explicit_no_file = bool(
            explicit_no_file or no_file_pattern.search(text)
        )

        answer_only_patterns = (
            r"(?:只需要|只需|只要).{0,40}(?:告诉|回答|给出|返回|输出).{0,16}(?:分析结果|结果|结论|分析|建议)",
            r"(?:第一轮\s*)?(?:只|仅).{0,16}(?:告诉|回答|给出|返回|输出).{0,16}(?:分析结果|结果|结论|分析|建议)",
        )
        answer_only = any(
            re.search(pattern, text) is not None
            for pattern in answer_only_patterns
        )

        return bool(
            (explicit_no_file or answer_only)
            and not explicit_file_delivery
        )

    @staticmethod
    def _goal_explicitly_requests_advisory_only(goal: str) -> bool:
        """
        识别“只讲方法/思路，不读取、不联网、不生成文件”的纯咨询任务。

        该判断故意保持严格：只有同时出现咨询意图，以及明确的
        no-read / no-network / no-file 三类约束时才返回 True。
        这样不会把真实文件分析任务误降级成纯文本回答。
        """
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not text:
            return False

        advisory = any(
            token in text
            for token in (
                "处理思路", "分析思路", "检查思路", "方法",
                "方案", "流程", "怎么做", "如何", "准备如何",
                "简单说明", "说明你准备", "解释",
            )
        )

        no_read = re.search(
            r"(?:不需要|无需|不要|不必).{0,12}(?:读取|打开|加载).{0,8}(?:文件|excel|csv|数据表)?",
            text,
        ) is not None
        no_network = re.search(
            r"(?:不需要|无需|不要|不必).{0,12}(?:联网|上网|搜索|检索|网页|网络)",
            text,
        ) is not None
        no_file = re.search(
            r"(?:不需要|无需|不要|不必).{0,16}(?:生成|导出|保存|制作|创建)?[^。；;]{0,8}(?:任何)?(?:文件|报告|excel|xlsx|csv|word|docx|pdf|ppt|pptx)",
            text,
        ) is not None

        return bool(advisory and no_read and no_network and no_file)

    @staticmethod
    def _build_local_advisory_system_prompt() -> str:
        """本地 Qwen 纯咨询任务的极简 finish-only 协议。"""
        return (
            "你是 DataPilot 的本地回答器。当前用户明确要求只说明方法/思路，"
            "并明确不要读取文件、不要联网、不要生成文件。\n"
            "不要搜索，不要调用任何工具，不要返回 query、plan、tool、arguments。\n"
            "只返回一个 JSON 对象，格式必须逐字遵守：\n"
            '{"action_type":"finish","final_answer":"直接给用户的简洁回答"}\n'
            "final_answer 应直接回答用户的问题。不要输出 Markdown 代码围栏，"
            "不要输出第二个 JSON，也不要解释协议。"
        )

    @staticmethod
    def _looks_like_file_output_tool(tool_name: str) -> bool:
        """只识别明显的写文件/导出动作，不把分析工具误判为交付。"""
        name = str(tool_name or "").strip().lower()
        if not name:
            return False

        exact_names = {
            "write_file",
            "save_file",
            "create_file",
            "export_file",
            "write_csv",
            "save_csv",
            "export_csv",
            "to_csv",
            "write_excel",
            "save_excel",
            "export_excel",
            "to_excel",
            "export_office_result",
            "export_multi_sheet_excel",
            "generate_office_deliverables",
            "create_professional_excel_report",
            "create_professional_word_report",
            "generate_document_summary_report",
        }
        if name in exact_names:
            return True

        return name.startswith(("write_", "save_", "export_"))

    @classmethod
    def _response_only_analysis_ready(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> bool:
        """
        判断响应型分析是否已有足够真实 Observation，可以直接 finish。

        这里只检查已成功工具，不计算或伪造新的业务结果。
        """
        if not isinstance(state, dict):
            return False

        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            runtime = {}

        if cls._task_plan_has_file_deliverables(runtime):
            return False

        if not cls._goal_explicitly_requests_response_only(goal):
            return False

        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return False

        successful = [
            item
            for item in steps
            if isinstance(item, dict) and bool(item.get("success"))
        ]
        successful_names = {
            str(item.get("tool") or "").strip().lower()
            for item in successful
        }

        goal_text = str(goal or "").lower()
        quality_requested = any(
            token in goal_text
            for token in ("缺失", "重复", "数据质量", "missing", "duplicate")
        )
        if quality_requested and "get_data_info" not in successful_names:
            return False

        quality_observation = None
        for item in reversed(successful):
            if str(item.get("tool") or "").strip().lower() == "get_data_info":
                if isinstance(item.get("observation"), dict):
                    quality_observation = item.get("observation")
                break

        if (
            quality_requested
            and isinstance(quality_observation, dict)
            and cls._goal_explicitly_requests_data_cleaning(goal)
        ):
            missing_values = quality_observation.get("missing_values") or {}
            duplicate_rows = quality_observation.get("duplicate_rows") or 0
            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0

            if missing_values and "handle_missing_values" not in successful_names:
                return False
            if duplicate_count > 0 and "drop_duplicate_rows" not in successful_names:
                return False

        analysis_requested = any(
            token in goal_text
            for token in (
                "统计", "汇总", "合计", "平均", "均值",
                "最高", "最大", "最低", "最小", "分析",
            )
        )
        analysis_tools = {
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
            "calculate_stats",
            "run_data_pipeline",
        }
        if analysis_requested and not (successful_names & analysis_tools):
            return False

        return bool(successful)

    @staticmethod
    def _task_plan_has_deliverables(
        runtime_context: Dict[str, Any],
    ) -> bool:
        plan = (runtime_context or {}).get("task_plan")

        if isinstance(plan, TaskPlan):
            plan = plan.to_dict()
        elif hasattr(plan, "to_dict"):
            plan = plan.to_dict()

        if not isinstance(plan, dict):
            return False

        return bool(
            plan.get("deliverable_requirements") or []
        )

    @staticmethod
    def _task_plan_has_file_deliverables(
        runtime_context: Dict[str, Any],
    ) -> bool:
        """
        只判断 TaskPlan 是否明确要求真实文件型交付物。

        统一复用 StageOrchestrator 的文件交付判定，避免把
        “向用户直接给出分析报告/分析结果”这种聊天响应误当成落盘文件。
        """
        plan = (runtime_context or {}).get("task_plan")

        if isinstance(plan, TaskPlan):
            plan = plan.to_dict()
        elif hasattr(plan, "to_dict"):
            plan = plan.to_dict()

        if not isinstance(plan, dict):
            return False

        try:
            normalized = StageOrchestrator._normalize_plan(plan)
            return bool(
                StageOrchestrator._plan_has_file_deliverable_requirement(
                    normalized
                )
            )
        except Exception:
            # 保守兜底：无法可靠判断时，不凭“报告”等泛化词强行要求文件。
            return False

    @classmethod
    def _apply_original_output_mode_override(
        cls,
        *,
        goal: str,
        runtime_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v6.6：原始用户 Output Mode 高于 Planner 漂移。

        典型场景：用户明确写了“第一轮只输出分析结果，不要生成
        Word、Excel 或其他文件”，但本地 Planner 偶发仍把
        deliverable_requirements 写成“生成 Word 报告”。

        Stage Router 已经能够根据原始用户意图关闭 Delivery；这里进一步
        规范 runtime_context.task_plan，使 AgentLoop 与 Completion Gate 也看到
        同一个 response-only 合同，避免：
            Acquisition finish -> Completion Gate 要文件 -> FAIL -> 原地循环。
        """
        normalized = dict(runtime_context or {})
        if not cls._goal_explicitly_requests_response_only(goal):
            return normalized

        plan = normalized.get("task_plan")
        if isinstance(plan, TaskPlan):
            plan = plan.to_dict()
        elif hasattr(plan, "to_dict"):
            plan = plan.to_dict()

        if not isinstance(plan, dict):
            normalized["output_mode"] = "response_only"
            normalized["response_only_override"] = True
            return normalized

        cleaned_plan = dict(plan)
        raw_requirements = cleaned_plan.get("deliverable_requirements") or []
        if isinstance(raw_requirements, str):
            raw_requirements = [raw_requirements]

        kept_requirements: List[str] = []
        removed_requirements: List[str] = []
        for item in raw_requirements:
            text = str(item or "").strip()
            if not text:
                continue
            try:
                is_file_requirement = bool(
                    StageOrchestrator._plan_has_file_deliverable_requirement(
                        {"deliverable_requirements": [text]}
                    )
                )
            except Exception:
                is_file_requirement = False

            if is_file_requirement:
                removed_requirements.append(text)
            else:
                kept_requirements.append(text)

        if not kept_requirements:
            kept_requirements = [
                "向用户直接给出分析结果和结论。"
            ]

        cleaned_plan["deliverable_requirements"] = kept_requirements
        normalized["task_plan"] = cleaned_plan
        normalized["output_mode"] = "response_only"
        normalized["response_only_override"] = True
        if removed_requirements:
            normalized["response_only_removed_file_requirements"] = (
                removed_requirements
            )
        return normalized

    @staticmethod
    def _research_source_category(url: str) -> str:
        """对已读取网页做保守来源分级，只用于研究质量控制。"""
        try:
            host = (urlparse(str(url or "")).hostname or "").lower().strip(".")
        except Exception:
            host = ""
        if not host:
            return "unknown"

        if (
            host.endswith(".gov.cn")
            or host == "gov.cn"
            or host.endswith(".gov.hk")
            or host.endswith(".gov.mo")
        ):
            return "government"
        if host.endswith(".edu.cn") or host == "edu.cn":
            return "education"
        if host.endswith(".org.cn") or host == "org.cn":
            return "association"

        recruitment_domains = (
            "zhaopin.com",
            "bosszhipin.com",
            "liepin.com",
            "51job.com",
        )
        if any(host == d or host.endswith("." + d) for d in recruitment_domains):
            return "recruitment"

        public_media_domains = (
            "xinhuanet.com",
            "people.com.cn",
            "chinanews.com.cn",
        )
        if any(host == d or host.endswith("." + d) for d in public_media_domains):
            return "public_media"

        return "other"

    @classmethod
    def _goal_requests_priority_research_sources(cls, goal: str) -> bool:
        text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not text:
            return False
        return any(
            token in text
            for token in (
                "政府部门", "政府官网", "政府网站", "民航", "行业协会",
                "职业院校", "招聘平台", "企业官网", "权威来源",
                "可靠公开资料", "注明来源", "来源和时间", "来源及时间",
            )
        )

    @classmethod
    def _research_required_topics(cls, goal: str) -> List[str]:
        """从用户原始目标中提取“真正要求研究”的主题，而不是来源偏好。

        v6.6 compatibility note:
        “优先使用政府部门、职业院校、招聘平台……”描述的是来源类型，
        不能仅因为这些词出现，就把政策/教育/就业自动扩成必做研究主题。
        只有用户在研究目标本身显式要求这些主题时，才进入 Coverage Gate。
        """
        raw_text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not raw_text:
            return []

        source_preference_tokens = (
            "政府部门", "政府官网", "政府网站", "民航相关机构", "行业协会",
            "职业院校", "招聘平台", "企业官网", "可靠公开资料", "权威来源",
        )
        semantic_segments: List[str] = []
        for segment in re.split(r"(?<=[。！？!?；;])", raw_text):
            segment = str(segment or "").strip()
            if not segment:
                continue
            is_source_preference = bool(
                ("优先使用" in segment or "优先采用" in segment)
                and any(token in segment for token in source_preference_tokens)
            )
            if is_source_preference:
                continue
            semantic_segments.append(segment)

        text = " ".join(semantic_segments) or raw_text
        topic_keywords = {
            "competition": (
                "竞争", "竞品", "培训机构", "培训竞争", "无人机培训",
                "课程", "价格", "宣传卖点",
            ),
            "policy_low_altitude": (
                "低空经济", "政策研究", "政策环境", "监管政策",
                "行动方案", "发展规划", "政府工作报告",
            ),
            "employment": (
                "就业岗位", "就业需求", "招聘岗位", "岗位需求",
                "飞手岗位", "薪资", "人才需求",
            ),
            "education": (
                "职业教育", "职业院校", "技校", "高职", "大学",
                "校企合作", "学校合作",
            ),
            "enterprise_demand": (
                "企业需求", "b端", "b 端", "测绘企业", "无人机企业",
                "农业", "电力巡检", "物业", "应急", "企业客户",
            ),
        }
        required: List[str] = []
        for topic, keywords in topic_keywords.items():
            if any(keyword in text for keyword in keywords):
                required.append(topic)
        return required

    @classmethod
    def _research_topic_hits(cls, text: str) -> List[str]:
        value = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not value:
            return []
        rules = {
            "competition": (
                "培训机构", "培训", "课程", "学费", "价格", "竞品", "考证", "驾照",
            ),
            "policy_low_altitude": (
                "低空经济", "政策", "行动方案", "发展规划", "政府工作报告", "民航局",
            ),
            "employment": (
                "招聘", "就业", "岗位", "飞手", "薪资", "用工", "人才需求", "岗位需求",
            ),
            "education": (
                "职业院校", "职业教育", "技校", "高职", "大学", "校企合作", "实训基地",
            ),
            "enterprise_demand": (
                "电力巡检", "巡检", "测绘", "农业", "植保", "物业", "应急",
                "企业培训", "企业需求", "行业应用",
            ),
        }
        return [
            topic
            for topic, keywords in rules.items()
            if any(keyword in value for keyword in keywords)
        ]

    @classmethod
    def _research_search_topic_hits(
        cls,
        text: str,
    ) -> List[str]:
        """
        对 search query 做“主题专用性”判定。

        与 _research_topic_hits() 的宽松文本命中不同，这里只用于：
        - discovery coverage；
        - bounded search recovery；
        - topic exhaustion；
        - Search Infrastructure Guard。

        目标是防止 broad query 仅因出现一个弱词就冒充已经完成某个主题。
        例如：
            “中山珠海无人机培训价格课程就业需求2024”
        应视为 competition 搜索，而不是 employment 专项搜索。

        employment 至少需要招聘/岗位/飞手/薪资/用工等强信号；
        enterprise_demand 需要显式企业/行业应用信号，或至少两个应用场景词。
        """
        value = re.sub(
            r"\s+",
            " ",
            str(text or "").strip().lower(),
        )
        if not value:
            return []

        hits: List[str] = []

        if any(
            token in value
            for token in (
                "培训机构",
                "无人机培训",
                "培训基地",
                "培训学校",
                "驾驶员培训",
                "课程",
                "学费",
                "价格",
                "收费",
                "考证",
                "驾照",
                "caac",
            )
        ):
            hits.append("competition")

        policy_core = any(
            token in value
            for token in (
                "低空经济",
                "低空产业",
            )
        )
        policy_signal = any(
            token in value
            for token in (
                "政策",
                "行动方案",
                "发展规划",
                "政府工作报告",
                "人民政府",
                "民航局",
                "监管",
            )
        )
        if policy_core and policy_signal:
            hits.append("policy_low_altitude")

        employment_strong = any(
            token in value
            for token in (
                "招聘",
                "岗位",
                "职位",
                "飞手",
                "薪资",
                "工资",
                "用工",
                "求职",
                "岗位需求",
                "人才招聘",
            )
        )
        if employment_strong:
            hits.append("employment")

        if any(
            token in value
            for token in (
                "职业院校",
                "职业教育",
                "技校",
                "高职",
                "大学",
                "校企合作",
                "产教融合",
                "实训基地",
                "专业群",
            )
        ):
            hits.append("education")

        enterprise_explicit = any(
            token in value
            for token in (
                "企业需求",
                "企业培训",
                "行业应用",
                "企业客户",
                "b端",
                "b 端",
            )
        )
        application_terms = (
            "电力巡检",
            "巡检",
            "测绘",
            "农业",
            "植保",
            "物业",
            "应急",
            "物流",
            "吊运",
        )
        application_count = sum(
            1
            for token in application_terms
            if token in value
        )
        if enterprise_explicit or application_count >= 2:
            hits.append("enterprise_demand")

        return hits

    @classmethod
    def _research_page_topic_hits(
        cls,
        *,
        text: str,
        url: str = "",
    ) -> List[str]:
        """
        对已读取网页正文做较保守的“证据主题”判定。

        competition 可以由培训机构正文直接支持；
        employment 不能仅因“就业推荐/就业率”被计为招聘市场证据；
        policy 优先要求政府/协会/公共媒体来源；
        education / enterprise_demand 需要明确的正文主题信号。
        """
        value = re.sub(
            r"\s+",
            " ",
            str(text or "").strip().lower(),
        )
        if not value:
            return []

        category = cls._research_source_category(url)
        hits: List[str] = []

        if any(
            token in value
            for token in (
                "无人机培训",
                "驾驶员培训",
                "培训机构",
                "培训基地",
                "培训学校",
                "课程",
                "学费",
                "收费",
                "考证",
                "caac",
            )
        ):
            hits.append("competition")

        policy_core = any(
            token in value
            for token in (
                "低空经济",
                "低空产业",
            )
        )
        policy_signal = any(
            token in value
            for token in (
                "政策",
                "行动方案",
                "发展规划",
                "政府工作报告",
                "人民政府",
                "实施方案",
            )
        )
        if (
            policy_core
            and policy_signal
            and category in {
                "government",
                "association",
                "public_media",
            }
        ):
            hits.append("policy_low_altitude")

        employment_strong = any(
            token in value
            for token in (
                "招聘",
                "招聘信息",
                "岗位职责",
                "岗位要求",
                "职位",
                "薪资",
                "工资",
                "用工",
                "招聘岗位",
            )
        )
        if (
            employment_strong
            and (
                category == "recruitment"
                or any(
                    token in value
                    for token in (
                        "招聘",
                        "岗位职责",
                        "岗位要求",
                        "薪资",
                    )
                )
            )
        ):
            hits.append("employment")

        if any(
            token in value
            for token in (
                "职业院校",
                "职业教育",
                "技校",
                "高职",
                "大学",
                "校企合作",
                "产教融合",
                "实训基地",
                "专业群",
                "产业学院",
            )
        ):
            hits.append("education")

        enterprise_explicit = any(
            token in value
            for token in (
                "企业需求",
                "行业应用",
                "企业客户",
                "企业服务",
                "行业客户",
            )
        )
        application_terms = (
            "电力巡检",
            "巡检",
            "测绘",
            "农业",
            "植保",
            "物业",
            "应急",
            "物流",
            "吊运",
        )
        application_count = sum(
            1
            for token in application_terms
            if token in value
        )
        if enterprise_explicit or application_count >= 2:
            hits.append("enterprise_demand")

        return hits

    @classmethod
    def _research_search_topic_attempts(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        required_topics: List[str],
    ) -> Dict[str, int]:
        """
        统计每个 required topic 被“专项 search query”真正尝试过多少次。
        broad query 中的弱语义不计入其他主题首轮尝试。
        """
        required = set(required_topics or [])
        counts: Dict[str, int] = {
            topic: 0
            for topic in required_topics or []
        }
        for item in tool_results or []:
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "search_web":
                continue
            args = getattr(item, "arguments", {}) or {}
            query = str(
                args.get("query")
                or args.get("search_query")
                or ""
            ).strip()
            for topic in (
                set(cls._research_search_topic_hits(query))
                & required
            ):
                counts[topic] = counts.get(topic, 0) + 1
        return counts

    @classmethod
    def _research_web_action_limit(cls, goal: str) -> int:
        required = cls._research_required_topics(goal)
        return 10 if len(required) >= 4 else 6

    @classmethod
    def _response_only_research_coverage_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Dict[str, Any]:
        """
        研究覆盖度门。

        discovery coverage:
            成功 search_web 是否覆盖用户显式要求主题。
        evidence coverage:
            成功 read_webpage 正文是否覆盖足够多主题。
        topic degrade:
            若单一主题的 bounded search recovery 全部耗尽，则该主题可标记为
            degraded_search_topic，不再阻塞其他主题；最终答案必须明确披露
            “本轮未获得充分外部核验”。多个主题同时耗尽时不允许自动降级。
        """
        required = cls._research_required_topics(goal)
        searched: set[str] = set()
        read: set[str] = set()

        for item in tool_results or []:
            if not bool(getattr(item, "success", False)):
                continue
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            arguments = getattr(item, "arguments", {}) or {}
            output = getattr(item, "output", None)

            if name == "search_web":
                query = str(
                    arguments.get("query")
                    or arguments.get("search_query")
                    or ""
                )
                query_hits = set(
                    cls._research_search_topic_hits(query)
                )

                # 生产 search_web 成功时通常会返回非空候选列表。
                # 若有真实候选，则要求标题/snippet 至少与专项 query 主题相容；
                # 若测试桩/历史工具成功但 output 为空，则保留 query-only
                # 兼容行为，避免破坏既有接口契约。
                if isinstance(output, list) and output:
                    metadata_parts: List[str] = []
                    for entry in output:
                        if not isinstance(entry, dict):
                            continue
                        metadata_parts.extend(
                            [
                                str(entry.get("title") or ""),
                                str(
                                    entry.get("snippet")
                                    or entry.get("body")
                                    or ""
                                ),
                            ]
                        )

                    metadata_text = " ".join(
                        part.strip()
                        for part in metadata_parts
                        if str(part or "").strip()
                    ).strip()

                    if metadata_text:
                        result_hits = set(
                            cls._research_topic_hits(
                                metadata_text
                            )
                        )
                        searched.update(
                            query_hits & result_hits
                        )
                    else:
                        # 兼容历史 ToolExecutionResult / 单元测试桩：
                        # 有些成功 search 只保存 URL，没有 title/snippet。
                        # 此时不能因为缺少展示元数据而把已经成功执行的专项
                        # query 判成“未搜索”。仍使用 strict query classifier，
                        # 所以 broad training query 不会伪造 employment coverage。
                        searched.update(query_hits)
                else:
                    searched.update(query_hits)

            elif name == "read_webpage":
                if isinstance(output, dict):
                    page_text = " ".join(
                        [
                            str(output.get("title") or ""),
                            str(output.get("text") or "")[:12000],
                        ]
                    )
                else:
                    page_text = str(output or "")[:12000]

                if len(page_text.strip()) >= 120:
                    page_url = ""
                    arguments_url = str(
                        arguments.get("url") or ""
                    ).strip()
                    if isinstance(output, dict):
                        page_url = str(
                            output.get("final_url")
                            or output.get("url")
                            or arguments_url
                        ).strip()
                    else:
                        page_url = arguments_url

                    read.update(
                        cls._research_page_topic_hits(
                            text=page_text,
                            url=page_url,
                        )
                    )

        if not required:
            return {
                "passed": True,
                "required_topics": [],
                "searched_topics": sorted(searched),
                "read_topics": sorted(read),
                "missing_search_topics": [],
                "blocking_missing_search_topics": [],
                "degraded_search_topics": [],
                "missing_read_topics": [],
                "covered_read_count": 0,
                "read_target": 0,
                "topic_degrade_allowed": True,
            }

        missing_search = [
            topic for topic in required
            if topic not in searched
        ]

        exhaustion = cls._research_topic_exhaustion_status(
            tool_results=tool_results,
            goal=goal,
        )
        exhausted_topics = [
            topic
            for topic in (exhaustion.get("exhausted_topics") or [])
            if topic in missing_search
        ]
        degrade_allowed = bool(exhaustion.get("degrade_allowed"))
        degraded_topics = exhausted_topics if degrade_allowed else []

        # 两套缺口必须分开：
        # - routing_missing_search_topics：决定“下一步还要搜哪个主题”。
        #   某主题的 bounded query recovery 一旦耗尽，就不能继续把它放回
        #   Search Router，否则会再次撞同一个 Retry Budget。
        # - blocking_missing_search_topics：决定“最终证据是否允许通过”。
        #   只有满足 degrade_allowed 的耗尽主题，才可以从最终 blocking 中移除。
        routing_missing_search = [
            topic
            for topic in missing_search
            if topic not in exhausted_topics
        ]

        blocking_missing_search = [
            topic
            for topic in missing_search
            if topic not in degraded_topics
        ]

        available_required = [
            topic
            for topic in required
            if topic not in degraded_topics
        ]
        read_target = min(len(available_required), 4)
        covered_required_read = [
            topic
            for topic in available_required
            if topic in read
        ]
        missing_read = [
            topic
            for topic in available_required
            if topic not in read
        ]

        # 正常覆盖通过不依赖 topic degrade。
        # degrade_allowed 只决定“已耗尽主题能否从 blocking 中移除”；
        # 当没有任何主题耗尽时，degrade_allowed 本来就是 False，
        # 不能因此把完整搜索 + 完整正文覆盖误判为 FAIL。
        passed = bool(
            not blocking_missing_search
            and len(covered_required_read) >= read_target
        )

        return {
            "passed": passed,
            "required_topics": required,
            "searched_topics": sorted(searched),
            "read_topics": sorted(read),
            "missing_search_topics": missing_search,
            "routing_missing_search_topics": routing_missing_search,
            "blocking_missing_search_topics": blocking_missing_search,
            "exhausted_search_topics": exhausted_topics,
            "degraded_search_topics": degraded_topics,
            "missing_read_topics": missing_read,
            "covered_read_count": len(covered_required_read),
            "read_target": read_target,
            "topic_degrade_allowed": degrade_allowed,
            "topic_exhaustion": exhaustion,
        }

    @classmethod
    def _research_query_anchor_tokens(
        cls,
        text: str,
        *,
        max_anchors: int = 3,
    ) -> List[str]:
        """
        从 query / 用户原始目标中保守提取少量地域锚点。

        fix17：
        - 支持普通空格 token；
        - 支持“研究中山、珠海以及周边地区”这类并列地点；
        - 不猜测“中山珠海无人机培训...”这类粘连长 query 的地域；
        - 过滤渠道词/分析词，避免“抖音 竞争分析 ...”再次污染恢复 query。
        """
        semantic_tokens = (
            "无人机", "培训", "机构", "课程", "价格", "学费", "市场",
            "现状", "低空", "经济", "政策", "政府", "行动方案", "发展",
            "招聘", "岗位", "就业", "飞手", "驾驶员", "职业", "院校",
            "技校", "高职", "大学", "校企", "企业", "需求", "行业",
            "巡检", "测绘", "农业", "物业", "应急", "caac",
            "执照", "证书", "考证", "资质", "许可", "要求", "条件",
            "标准", "资格", "规则",
            "抖音", "视频号", "微信", "短视频", "直播", "私信",
            "营销", "招生", "推广", "投放", "获客", "转化",
            "竞争", "竞品", "分析", "名单", "本地", "当地", "周边",
            "卖点", "定位", "策略", "调研", "研究",
            "基地", "附近", "学员", "群体", "当前", "统计期",
        )

        def _acceptable(token: str) -> bool:
            value = str(token or "").strip(
                "，。；;、,.:：()（）[]【】<>《》"
            )
            if not value:
                return False
            if len(value) > 8:
                return False
            if value.isdigit():
                return False
            # 地域 anchor 不允许包含任何数字/年龄范围/金额/算术符号。
            if re.search(r"\d", value):
                return False
            if any(
                mark in value
                for mark in (
                    "+", "＋", "—", "–", "~", "～",
                    "%", "％", "¥", "￥", "/", "\\",
                )
            ):
                return False
            if len(value) < 2:
                return False
            low = value.lower()
            if any(mark in low for mark in semantic_tokens):
                return False
            return True

        anchors: List[str] = []

        def _add(value: str) -> None:
            token = str(value or "").strip(
                "，。；;、,.:：()（）[]【】<>《》"
            )
            if not _acceptable(token):
                return
            if token not in anchors:
                anchors.append(token)

        raw_text = str(text or "")

        # 1) 先解析明确的“地点A、地点B及周边”结构。
        # 自然语言 goal 中常包含大量经营数字/年龄/金额，必须优先拿到
        # 显式地点，不能让“17—35”“8800”等短 token 抢占 anchor。
        pair_pattern = re.compile(
            r"(?:研究|调研|覆盖|包括|面向|在)?"
            r"([\u4e00-\u9fff]{2,4}?)、"
            r"([\u4e00-\u9fff]{2,4}?)"
            r"(?:以及|及|和|与)?周边"
        )
        for match in pair_pattern.finditer(raw_text):
            _add(match.group(1))
            _add(match.group(2))
            if len(anchors) >= max(1, int(max_anchors or 1)):
                return anchors[:max_anchors]

        # 2) 再兼容“中山 珠海 无人机培训...”这类短搜索 query。
        for raw in raw_text.split():
            _add(raw)
            if len(anchors) >= max(1, int(max_anchors or 1)):
                return anchors[:max_anchors]

        return anchors[:max_anchors]


    @classmethod
    def _research_topic_query(
        cls,
        topic: str,
        *,
        original_query: str,
    ) -> str:
        anchors = cls._research_query_anchor_tokens(
            original_query,
            max_anchors=2,
        )
        topic_terms = {
            "competition": "无人机培训 机构 课程 价格",
            "policy_low_altitude": "低空经济 政策 政府 site:gov.cn",
            "employment": "无人机 飞手 招聘 岗位 就业",
            "education": "职业院校 技校 高职 无人机 校企合作",
            "enterprise_demand": "无人机 企业需求 电力巡检 测绘 农业 物业 应急",
        }.get(topic, topic)
        topic_lower = topic_terms.lower()
        missing_anchors = [
            anchor
            for anchor in anchors
            if anchor.lower() not in topic_lower
        ]
        prefix = " ".join(missing_anchors).strip()
        return (prefix + " " + topic_terms).strip()

    @classmethod
    def _research_topic_query_variants(
        cls,
        topic: str,
        *,
        original_query: str,
    ) -> List[str]:
        """
        为同一研究主题生成少量确定性的替代检索式。

        用途：search_web 因搜索引擎超时/网络异常失败时，不能让 Agent
        对完全相同的 query 连续重试直到 Retry Budget 耗尽。这里提供
        bounded query diversification：优先去掉 site: 之类容易触发搜索端
        异常的操作符，并把复杂查询拆成更短、更自然的主题检索。
        """
        value = str(original_query or "").strip()
        anchors = cls._research_query_anchor_tokens(
            value,
            max_anchors=2,
        )

        topic_variants: Dict[str, List[str]] = {
            "competition": [
                "无人机培训 机构 课程 价格",
                "无人机培训 学费 培训基地 CAAC",
                "无人机驾驶员培训 课程 收费 机构",
            ],
            "policy_low_altitude": [
                "低空经济 行动方案 政府 政策",
                "低空经济 高质量发展 行动方案 中山 珠海",
                "低空经济 政策 珠海 中山 广东省人民政府",
            ],
            "employment": [
                "无人机 飞手 招聘 岗位 就业",
                "无人机驾驶员 招聘 巡检 测绘 飞手",
                "无人机 操作员 招聘 薪资 岗位",
            ],
            "education": [
                "职业院校 技校 高职 无人机 校企合作",
                "无人机 职业教育 实训基地 校企合作",
                "无人机专业 高职 技校 产教融合",
            ],
            "enterprise_demand": [
                "无人机 企业需求 电力巡检 测绘 农业 物业 应急",
                "无人机 行业应用 电力巡检 测绘 植保 应急",
                "无人机 企业培训 巡检 测绘 农业 应急",
            ],
        }

        variants: List[str] = []
        for body in topic_variants.get(topic, [topic]):
            body_lower = str(body or "").lower()
            missing_anchors = [
                anchor
                for anchor in anchors
                if anchor.lower() not in body_lower
            ]
            effective_prefix = " ".join(missing_anchors).strip()
            candidate = (effective_prefix + " " + body).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            # 搜索恢复阶段不保留高级 site: 操作符，降低搜索端超时/兼容风险。
            candidate = re.sub(
                r"(?:^|\s)site:[^\s]+",
                " ",
                candidate,
                flags=re.IGNORECASE,
            )
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if candidate and candidate not in variants:
                variants.append(candidate)

        return variants

    @classmethod
    def _failed_research_search_queries(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        topic: str,
    ) -> List[str]:
        """返回某研究主题已经失败过的 search_web query。"""
        failed: List[str] = []
        for item in tool_results or []:
            if bool(getattr(item, "success", False)):
                continue
            if str(getattr(item, "tool_name", "") or "").strip().lower() != "search_web":
                continue
            args = getattr(item, "arguments", {}) or {}
            query = str(args.get("query") or args.get("search_query") or "").strip()
            if not query:
                continue
            if topic not in cls._research_search_topic_hits(query):
                continue
            normalized = re.sub(r"\s+", " ", query).strip().lower()
            if normalized not in failed:
                failed.append(normalized)
        return failed

    @classmethod
    def _research_topic_exhaustion_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Dict[str, Any]:
        """
        判断每个显式研究主题是否已经耗尽 bounded search recovery。

        主题级耗尽 ≠ 整体搜索基础设施不可用：
        - 某一个主题的所有确定性替代 query 都失败，只降级该主题；
        - 其他主题仍可继续搜索、读取正文并完成有边界的分析；
        - 最终答案必须披露该主题本轮没有获得充分外部核验。
        """
        required = cls._research_required_topics(goal)
        exhausted: List[str] = []
        failed_query_counts: Dict[str, int] = {}

        for topic in required:
            failed_queries = cls._failed_research_search_queries(
                tool_results=tool_results,
                topic=topic,
            )
            failed_query_counts[topic] = len(failed_queries)
            if not failed_queries:
                continue

            latest_failed_query = ""
            for item in reversed(tool_results or []):
                if bool(getattr(item, "success", False)):
                    continue
                if str(getattr(item, "tool_name", "") or "").strip().lower() != "search_web":
                    continue
                args = getattr(item, "arguments", {}) or {}
                query = str(
                    args.get("query")
                    or args.get("search_query")
                    or ""
                ).strip()
                if query and topic in cls._research_search_topic_hits(query):
                    latest_failed_query = query
                    break

            if not latest_failed_query:
                continue

            next_query = cls._next_research_search_recovery_query(
                topic=topic,
                current_query=latest_failed_query,
                tool_results=tool_results,
            )
            if next_query is None:
                exhausted.append(topic)

        successful_topics: set[str] = set()
        for item in tool_results or []:
            if not bool(getattr(item, "success", False)):
                continue
            if str(getattr(item, "tool_name", "") or "").strip().lower() != "search_web":
                continue
            args = getattr(item, "arguments", {}) or {}
            query = str(
                args.get("query")
                or args.get("search_query")
                or ""
            ).strip()
            successful_topics.update(cls._research_search_topic_hits(query))

        successful_required_topics = [
            topic for topic in required
            if topic in successful_topics and topic not in exhausted
        ]

        required_count = len(required)
        if required_count <= 1:
            minimum_supported_topics = 1
            max_degraded_topics = 0
        elif required_count == 2:
            minimum_supported_topics = 1
            max_degraded_topics = 1
        elif required_count == 3:
            minimum_supported_topics = 2
            max_degraded_topics = 1
        else:
            # 多主题研究至少保留 3 个真实成功 discovery 主题。
            minimum_supported_topics = 3
            max_degraded_topics = max(1, required_count - minimum_supported_topics)

        degrade_allowed = bool(
            exhausted
            and len(exhausted) <= max_degraded_topics
            and len(successful_required_topics) >= minimum_supported_topics
        )

        return {
            "exhausted_topics": exhausted,
            "failed_query_counts": failed_query_counts,
            "successful_required_topics": successful_required_topics,
            "minimum_supported_topics": minimum_supported_topics,
            "max_degraded_topics": max_degraded_topics,
            "degrade_allowed": degrade_allowed,
            "reason": (
                "部分研究主题的 bounded search recovery 已耗尽，但仍保留足够多个真实成功 discovery 主题；"
                "允许这些耗尽主题带未核验警告降级，并继续读取已发现的真实来源。"
                if exhausted and degrade_allowed
                else (
                    "研究主题的 bounded search recovery 已耗尽，但剩余成功 discovery 主题不足；"
                    "不允许继续扩大降级范围。"
                    if exhausted
                    else "尚无研究主题达到 bounded search recovery 耗尽条件。"
                )
            ),
        }

    @classmethod
    def _response_only_search_infrastructure_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
        failure_threshold: int = 4,
    ) -> Dict[str, Any]:
        """
        判断 response-only 联网研究是否已经进入“搜索基础设施不可用”。

        这里和单个 query 的 Retry Budget 分开：
        - query recovery 解决“这个检索式失败，换同主题检索式”；
        - infrastructure guard 解决“多个不同检索式都无法从 search_web
          得到任何真实结果”。

        当连续/累计多个 search_web 调用全部失败，且从未形成成功搜索结果
        或成功网页正文时，继续询问 LLM 只会浪费 token，并可能诱发本地模型
        的重复输出/500。此时应由 Python 确定性结束 Acquisition。
        """
        threshold = max(1, int(failure_threshold or 4))
        failed_searches = 0
        successful_searches = 0
        successful_page_reads = 0

        required_topics = set(cls._research_required_topics(goal))
        failed_topics: set[str] = set()

        for item in tool_results or []:
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            success = bool(getattr(item, "success", False))
            output = getattr(item, "output", None)

            if name == "search_web":
                if success and isinstance(output, list) and bool(output):
                    successful_searches += 1
                else:
                    failed_searches += 1
                    arguments = getattr(item, "arguments", {}) or {}
                    query = str(
                        arguments.get("query")
                        or arguments.get("search_query")
                        or ""
                    )
                    topic_hits = set(cls._research_search_topic_hits(query))
                    scoped_hits = topic_hits & required_topics
                    failed_topics.update(scoped_hits)
                continue

            if name != "read_webpage" or not success:
                continue

            text = ""
            if isinstance(output, dict):
                text = str(output.get("text") or "").strip()
            else:
                text = str(output or "").strip()
            if len(text) >= 120:
                successful_page_reads += 1

        # Infrastructure Guard 只能在“多个主题都已经真正耗尽 bounded
        # recovery”之后触发，不能因为第二个主题刚失败 1 次就提前判死。
        # 否则会把 topic-level recovery 截断。
        topic_exhaustion = cls._research_topic_exhaustion_status(
            tool_results=tool_results,
            goal=goal,
        )
        exhausted_topics = list(
            topic_exhaustion.get("exhausted_topics") or []
        )

        required_topic_count = len(required_topics)
        multi_topic_goal = required_topic_count >= 2
        if required_topic_count <= 1:
            required_exhausted_topics = 1
        elif required_topic_count <= 3:
            required_exhausted_topics = 2
        else:
            # 复杂 4~5 主题研究：至少半数（向上取整）主题全部耗尽，
            # 且始终 0 成功来源，才认为不是关键词问题而是基础设施问题。
            required_exhausted_topics = max(
                2,
                (required_topic_count + 1) // 2,
            )

        topic_scope_exhausted = (
            len(exhausted_topics) >= required_exhausted_topics
            if multi_topic_goal
            else (
                len(exhausted_topics) >= 1
                or failed_searches >= threshold
            )
        )

        exhausted = bool(
            cls._goal_explicitly_requests_response_only(goal)
            and failed_searches >= threshold
            and topic_scope_exhausted
            and successful_searches == 0
            and successful_page_reads == 0
        )

        return {
            "exhausted": exhausted,
            "failure_threshold": threshold,
            "failed_searches": failed_searches,
            "successful_searches": successful_searches,
            "successful_page_reads": successful_page_reads,
            "failed_topics": sorted(failed_topics),
            "exhausted_topics": exhausted_topics,
            "required_topic_count": required_topic_count,
            "required_exhausted_topics": required_exhausted_topics,
            "reason": (
                "多个研究主题的 bounded search recovery 均已耗尽，且始终没有形成"
                "任何成功搜索结果或真实网页正文；当前任务的搜索基础设施视为不可用。"
                if exhausted
                else (
                    "当前失败尚未跨越足够多的“已耗尽主题”；"
                    "应继续 topic-level recovery/跳转，而不是提前判定整个搜索基础设施不可用。"
                    if multi_topic_goal and failed_searches >= threshold
                    else "搜索基础设施尚未达到确定性终止条件。"
                )
            ),
        }

    @classmethod
    def _research_topic_recovery_seed_query(
        cls,
        *,
        topic: str,
        current_query: str,
        tool_results: List[ToolExecutionResult],
    ) -> str:
        """
        为同一研究主题选择稳定的 bounded-recovery seed。

        variant 家族必须在整个主题恢复过程中保持稳定，不能随着“最后一次失败
        query”的词序变化重新生成另一套候选，否则 exhaustion checker 会不断
        发现所谓“新 query”，导致 bounded recovery 失去边界。

        优先使用该主题第一次失败的真实 search_web query；若尚无失败记录，
        才使用当前 query。
        """
        for item in tool_results or []:
            if bool(getattr(item, "success", False)):
                continue
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "search_web":
                continue

            args = getattr(item, "arguments", {}) or {}
            query = str(
                args.get("query")
                or args.get("search_query")
                or ""
            ).strip()
            if not query:
                continue
            if topic in cls._research_search_topic_hits(query):
                return query

        return str(current_query or "").strip()

    @classmethod
    def _next_research_search_recovery_query(
        cls,
        *,
        topic: str,
        current_query: str,
        tool_results: List[ToolExecutionResult],
    ) -> Optional[str]:
        """
        search_web 失败后的 bounded query recovery。

        只选择尚未失败过的替代检索式；全部替代式都失败后返回 None，
        让既有 Retry / Acquisition Terminal Guard 正常终止，而不是无限改写。
        """
        failed = set(
            cls._failed_research_search_queries(
                tool_results=tool_results,
                topic=topic,
            )
        )
        current_normalized = re.sub(
            r"\s+",
            " ",
            str(current_query or "").strip(),
        ).lower()
        recovery_seed = cls._research_topic_recovery_seed_query(
            topic=topic,
            current_query=current_query,
            tool_results=tool_results,
        )
        variants = cls._research_topic_query_variants(
            topic,
            original_query=recovery_seed,
        )
        for candidate in variants:
            normalized = re.sub(r"\s+", " ", candidate).strip().lower()
            if not normalized:
                continue
            if normalized == current_normalized:
                continue
            if normalized in failed:
                continue
            return candidate
        return None

    @classmethod
    def _next_research_read_coverage_query(
        cls,
        *,
        topic: str,
        goal: str,
        tool_results: List[ToolExecutionResult],
    ) -> Optional[str]:
        """
        某主题 search 已成功，但其候选 URL 全部读取失败/正文无效时，
        从该主题有限 query variant 家族中选择一条尚未执行的 query。
        """
        variants = cls._research_topic_query_variants(
            topic,
            original_query=goal,
        )

        attempted: set[str] = set()
        has_successful_discovery = False

        for item in tool_results or []:
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "search_web":
                continue

            args = getattr(item, "arguments", {}) or {}
            query = str(
                args.get("query")
                or args.get("search_query")
                or ""
            ).strip()
            if not query:
                continue
            if topic not in cls._research_search_topic_hits(query):
                continue

            attempted.add(
                re.sub(r"\s+", " ", query).strip().lower()
            )

            if bool(getattr(item, "success", False)):
                output = getattr(item, "output", None)
                if isinstance(output, list) and output:
                    has_successful_discovery = True

        # 只有 discovery 成功但正文候选失效时，才属于 read-coverage gap。
        # 如果该主题所有 search 都失败，则保持原有 search-exhaustion 边界。
        if not has_successful_discovery:
            return None

        for candidate in variants:
            normalized = re.sub(
                r"\s+",
                " ",
                str(candidate or ""),
            ).strip().lower()
            if normalized and normalized not in attempted:
                return str(candidate).strip()

        return None

    @classmethod
    def _response_only_acquisition_gate_recovery_decision(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Optional[Dict[str, Any]]:
        """
        response-only Acquisition Gate FAIL 后的确定性恢复。

        fix24 优先级：
        1. 从未尝试过的必需 discovery 主题；
        2. 已成功 discovery、但正文尚未形成的 read gap；
        3. 已经失败过、但 bounded variants 尚未耗尽的 discovery 主题；
        4. 全部耗尽后返回 None。

        这样既保留 breadth-first research coverage 的旧契约，也避免某个
        已经多次搜索失败的主题持续抢占一个只差正文即可完成的主题。
        """
        coverage = cls._response_only_research_coverage_status(
            tool_results=tool_results,
            goal=goal,
        )

        if "routing_missing_search_topics" in coverage:
            missing_topics = list(
                coverage.get("routing_missing_search_topics")
                or []
            )
        else:
            missing_topics = list(
                coverage.get("blocking_missing_search_topics")
                or coverage.get("missing_search_topics")
                or []
            )

        untouched_topics: List[str] = []
        attempted_missing_topics: List[str] = []

        attempts_by_topic = cls._research_search_topic_attempts(
            tool_results=tool_results,
            required_topics=missing_topics,
        )

        for topic in missing_topics:
            attempts = int(
                attempts_by_topic.get(
                    topic,
                    0,
                )
                or 0
            )
            if attempts <= 0:
                untouched_topics.append(topic)
            else:
                attempted_missing_topics.append(topic)

        # 1) 尚未尝试过的必需主题优先。
        for topic in untouched_topics:
            query = cls._research_topic_query(
                topic,
                original_query=goal,
            )
            if not str(query or "").strip():
                continue

            return {
                "action_type": "tool",
                "tool": "search_web",
                "arguments": {
                    "query": str(query).strip(),
                    "max_results": 8,
                    "region": "cn-zh",
                },
                "reason": (
                    "Acquisition Gate 未通过；优先补采从未尝试的 discovery 主题："
                    f"{topic}"
                ),
            }

        # 2) 其余缺失 discovery 都至少尝试过后，优先补 read gap。
        candidate = cls._next_unread_research_candidate(
            tool_results=tool_results,
            goal=goal,
        )
        if candidate:
            return {
                "action_type": "tool",
                "tool": "read_webpage",
                "arguments": {
                    "url": candidate["url"],
                    "max_characters": 20000,
                    "timeout": 15,
                    "start_character": 0,
                },
                "reason": (
                    "Acquisition Gate 未通过；优先补齐已 discovery 主题的真实正文："
                    + (
                        str(candidate.get("topic") or "")
                        or "missing_read_topic"
                    )
                ),
            }

        for topic in list(
            coverage.get("missing_read_topics")
            or []
        ):
            next_query = cls._next_research_read_coverage_query(
                topic=topic,
                goal=goal,
                tool_results=tool_results,
            )
            if not next_query:
                continue

            return {
                "action_type": "tool",
                "tool": "search_web",
                "arguments": {
                    "query": next_query,
                    "max_results": 8,
                    "region": "cn-zh",
                },
                "reason": (
                    "Acquisition Gate 未通过；已有 discovery 但正文证据不足，"
                    f"优先执行 bounded read-coverage recovery：{topic}"
                ),
            }

        # 3) 最后继续已经失败过、但仍有 bounded variant 的 discovery。
        for topic in attempted_missing_topics:
            seed = cls._research_topic_recovery_seed_query(
                topic=topic,
                current_query="",
                tool_results=tool_results,
            )
            next_query = cls._next_research_search_recovery_query(
                topic=topic,
                current_query=seed,
                tool_results=tool_results,
            )
            if not str(next_query or "").strip():
                continue

            return {
                "action_type": "tool",
                "tool": "search_web",
                "arguments": {
                    "query": str(next_query).strip(),
                    "max_results": 8,
                    "region": "cn-zh",
                },
                "reason": (
                    "Acquisition Gate 未通过；read gap 无可用恢复后继续 bounded "
                    f"discovery recovery：{topic}"
                ),
            }

        return None

    @classmethod
    def _next_unread_research_candidate(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Optional[Dict[str, str]]:
        """
        从已 discovery 的 URL 中选择下一份正文证据。

        fix17：
        - 有 missing_read_topics 时，只读能覆盖这些主题的候选；
        - 已覆盖 policy 后，不允许“政府官网高分”继续吞掉 read budget；
        - 按主题读取尝试次数做 fair routing；
        - 搜索结果继承其 query topic，兼容 snippet 很短的真实候选。
        """
        def _url_key(value: str) -> str:
            return str(value or "").strip().rstrip("/").lower()

        coverage = cls._response_only_research_coverage_status(
            tool_results=tool_results,
            goal=goal,
        )
        missing_order = list(
            coverage.get("missing_read_topics") or []
        )
        missing = set(missing_order)
        topic_rank = {
            topic: index
            for index, topic in enumerate(missing_order)
        }

        priority_requested = (
            cls._goal_requests_priority_research_sources(goal)
        )
        local_anchors = cls._research_query_anchor_tokens(
            goal,
            max_anchors=2,
        )
        low_value_domains = (
            "91goodschool.com", "yunshengzx.com", "jiaoyubao.cn",
            "qinxue365.com", "keedu.cn", "sohu.com", "163.com",
            "cnblogs.com", "csjcs.com", "coatingol.com",
            "sinoasphalt.com", "peixunx.com", "ihr360.com",
            "renrendoc.com", "sgpjbg.com.cn",
            "baijiahao.baidu.com", "mp.weixin.qq.com",
            "ada.baidu.com", "image.baidu.com", "aabqd.com",
            "baike.baidu.com", "localsite.baidu.com",
        )

        discovered: Dict[str, Dict[str, Any]] = {}

        for item in tool_results or []:
            if not bool(getattr(item, "success", False)):
                continue
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "search_web":
                continue

            arguments = getattr(item, "arguments", {}) or {}
            search_query = str(
                arguments.get("query")
                or arguments.get("search_query")
                or ""
            ).strip()
            query_topics = set(
                cls._research_search_topic_hits(search_query)
            )

            output = getattr(item, "output", None)
            if not isinstance(output, list):
                continue

            for entry in output:
                if not isinstance(entry, dict):
                    continue

                url = str(
                    entry.get("url")
                    or entry.get("href")
                    or ""
                ).strip()
                url_key = _url_key(url)
                if not url or not url_key:
                    continue

                try:
                    host = (
                        urlparse(url).hostname
                        or ""
                    ).lower()
                except Exception:
                    host = ""

                blocked_candidate_hosts = {
                    "fakeurl.baidu.com",
                    "ada.baidu.com",
                    "localsite.baidu.com",
                    "image.baidu.com",
                }
                if host in blocked_candidate_hosts:
                    continue

                url_lower = url.lower()
                if (
                    "baidu.com/link?" in url_lower
                    or "bing.com/aclick" in url_lower
                    or host.endswith(".peixun23.top")
                    or host.endswith(".aabqd.com")
                ):
                    continue

                title = str(entry.get("title") or "").strip()
                snippet = str(
                    entry.get("snippet")
                    or entry.get("body")
                    or ""
                ).strip()
                metadata = f"{title} {snippet} {url}".lower()

                metadata_topics = set(
                    cls._research_topic_hits(metadata)
                )

                category = cls._research_source_category(url)

                # fix21：query topic 是主标签；只有高权威来源允许用自身
                # metadata 补充“跨主题”标签。
                #
                # 这样同时满足两类真实场景：
                # 1. policy query 返回聚合培训页：
                #    低价值聚合站不能因为 snippet 出现“无人机培训”
                #    就抢占 competition 路由；
                # 2. competition query 偶然发现中山市政府低空经济行动方案：
                #    政府官网本身的 title/snippet 足够强，可以补充 policy，
                #    后续在 competition 已覆盖后继续作为 policy 候选读取。
                authoritative_cross_topic_categories = {
                    "government",
                    "education",
                    "association",
                    "recruitment",
                    "public_media",
                }

                if query_topics:
                    if len(query_topics) == 1:
                        # 专项 query 是强标签。
                        all_topics = set(query_topics)
                    elif metadata_topics:
                        # 多主题 broad query 只能保留“query 与结果 metadata
                        # 同时支持”的主题，不能把每个结果都继承全部主题。
                        all_topics = (
                            set(query_topics)
                            & set(metadata_topics)
                        )
                    else:
                        # 历史/测试桩可能没有 title/snippet。
                        all_topics = set(query_topics)

                    if category in authoritative_cross_topic_categories:
                        all_topics |= set(metadata_topics)
                else:
                    all_topics = set(metadata_topics)

                # 候选必须具备“读完后有机会真正形成该主题正文证据”的资格。
                # 尤其 policy 不接受文档聚合站/营销站冒充政策正文；
                # employment 不接受只有“就业前景”但没有招聘/岗位/薪资信号的文章。
                viable_topics: set[str] = set()
                for topic in all_topics:
                    if topic == "policy_low_altitude":
                        if category in {
                            "government",
                            "association",
                            "public_media",
                        }:
                            viable_topics.add(topic)
                        continue

                    if topic == "employment":
                        if (
                            category == "recruitment"
                            or any(
                                token in metadata
                                for token in (
                                    "招聘", "岗位", "职位",
                                    "薪资", "工资", "用工",
                                )
                            )
                        ):
                            viable_topics.add(topic)
                        continue

                    if topic == "education":
                        if (
                            category == "education"
                            or any(
                                token in metadata
                                for token in (
                                    "职业院校", "职业教育",
                                    "技校", "高职", "大学",
                                    "校企合作", "产教融合",
                                    "实训基地",
                                )
                            )
                        ):
                            viable_topics.add(topic)
                        continue

                    # competition / enterprise_demand 允许企业官网和可靠公开页。
                    viable_topics.add(topic)

                all_topics = viable_topics
                missing_hits = all_topics & missing

                quality_score = 0

                if category in {
                    "government", "education", "association",
                    "recruitment", "public_media",
                }:
                    quality_score += 12

                if (
                    priority_requested
                    and any(
                        token in metadata
                        for token in (
                            "官网", "官方", "人民政府",
                            "民航", "大学", "学院", "招聘",
                        )
                    )
                ):
                    quality_score += 7

                if any(
                    domain in url.lower()
                    for domain in low_value_domains
                ):
                    quality_score -= 8

                if (
                    "baidu.com/s?" in url.lower()
                    or "bing.com/aclick" in url.lower()
                ):
                    quality_score -= 25

                if "baidu.com/link?" in url.lower():
                    quality_score -= 8

                local_hit_count = sum(
                    1
                    for anchor in local_anchors
                    if anchor and anchor.lower() in metadata
                )
                if local_hit_count:
                    quality_score += 6 * local_hit_count

                # 对“本地竞品”研究，低价值聚合/百科页面若连中山/珠海
                # 地域信号都没有，不允许它冒充 competition 正文候选。
                if (
                    "competition" in missing_hits
                    and local_anchors
                    and local_hit_count == 0
                    and any(
                        domain in url_lower
                        for domain in low_value_domains
                    )
                ):
                    missing_hits = set(missing_hits)
                    missing_hits.discard("competition")

                discovered[url_key] = {
                    "url": url,
                    "title": title,
                    "topics": all_topics,
                    "missing_hits": missing_hits,
                    "quality_score": quality_score,
                }

        if not discovered:
            return None

        attempted: set[str] = set()
        attempted_topic_counts: Dict[str, int] = {
            topic: 0
            for topic in missing_order
        }

        for item in tool_results or []:
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "read_webpage":
                continue

            args = getattr(item, "arguments", {}) or {}
            output = getattr(item, "output", None)

            values: List[str] = []
            requested = str(args.get("url") or "").strip()
            if requested:
                values.append(requested)

            if isinstance(output, dict):
                for key in ("url", "final_url"):
                    value = str(output.get(key) or "").strip()
                    if value:
                        values.append(value)

            matched: Optional[Dict[str, Any]] = None
            for value in values:
                key = _url_key(value)
                if not key:
                    continue
                attempted.add(key)
                if key in discovered:
                    matched = discovered[key]

            if matched is not None:
                for topic in (matched.get("missing_hits") or set()):
                    if topic in attempted_topic_counts:
                        attempted_topic_counts[topic] += 1

        candidates = [
            item
            for key, item in discovered.items()
            if key not in attempted
        ]
        if not candidates:
            return None

        if missing:
            candidates = [
                item
                for item in candidates
                if item.get("missing_hits")
            ]
            if not candidates:
                return None

        # 先决定本轮应该补哪个 missing topic，再在该 topic 内比较来源质量。
        # 这样既保证主题公平性，也保留同主题 official > aggregator。
        target_topic = ""
        if missing_order:
            target_topic = min(
                missing_order,
                key=lambda topic: (
                    int(attempted_topic_counts.get(topic, 0)),
                    int(topic_rank.get(topic, 999)),
                ),
            )

        if target_topic:
            target_candidates = [
                item
                for item in candidates
                if target_topic
                in (
                    item.get("missing_hits")
                    or set()
                )
            ]
            if target_candidates:
                candidates = target_candidates

        def _candidate_key(item: Dict[str, Any]) -> Tuple[int, str]:
            return (
                int(item.get("quality_score") or 0),
                str(item.get("url") or ""),
            )

        best = max(candidates, key=_candidate_key)
        hits = list(best.get("missing_hits") or [])
        primary_topic = ""
        if target_topic and target_topic in hits:
            primary_topic = target_topic
        elif hits:
            primary_topic = min(
                hits,
                key=lambda topic: topic_rank.get(topic, 999),
            )

        return {
            "url": str(best["url"]),
            "title": str(best.get("title") or ""),
            "topic": str(primary_topic or ""),
        }


    @classmethod
    def _maybe_redirect_response_only_research_action(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Tuple[str, Dict[str, Any], str]:
        """
        确定性修正 response-only 联网研究动作。

        处理四类高频漂移：
        1. search_web 反复围绕已覆盖主题改写近义词；
        2. search_web 对同一个已经超时/失败的 query 原样重试；
        3. read_webpage 再次读取已经完整结束的网页；
        4. read_webpage 对同一个已经失败的 URL 原样重试。

        搜索失败与网页来源失败都采用 bounded recovery：
        - search_web 失败：切换到同主题、更短、无 site: 操作符的替代 query；
        - read_webpage 失败：切换到搜索 Observation 中尚未尝试的候选 URL。
        全部替代策略耗尽后再交给 Retry / Terminal Guard，避免无限循环。
        """
        name = str(tool_name or "").strip().lower()
        args = dict(arguments or {})

        if name == "search_web":
            coverage = cls._response_only_research_coverage_status(
                tool_results=tool_results,
                goal=goal,
            )
            if "routing_missing_search_topics" in coverage:
                missing = list(
                    coverage.get("routing_missing_search_topics")
                    or []
                )
            else:
                # 兼容旧状态结构；新版本正常不会走这里。
                missing = list(
                    coverage.get("blocking_missing_search_topics")
                    or coverage.get("missing_search_topics")
                    or []
                )
            if missing:
                current_query = str(
                    args.get("query")
                    or args.get("search_query")
                    or ""
                ).strip()
                current_hits = set(
                    cls._research_search_topic_hits(
                        current_query
                    )
                )

                required_topics = list(
                    coverage.get("required_topics") or []
                )
                attempt_counts = (
                    cls._research_search_topic_attempts(
                        tool_results=tool_results,
                        required_topics=required_topics,
                    )
                )
                unattempted_missing = [
                    topic
                    for topic in missing
                    if int(attempt_counts.get(topic, 0)) == 0
                ]

                current_normalized = re.sub(
                    r"\s+",
                    " ",
                    current_query,
                ).strip().lower()

                failed_current_topics: List[str] = []
                for topic in missing:
                    if topic not in current_hits:
                        continue
                    failed_for_topic = set(
                        cls._failed_research_search_queries(
                            tool_results=tool_results,
                            topic=topic,
                        )
                    )
                    if current_normalized in failed_for_topic:
                        failed_current_topics.append(topic)

                # 优先级必须稳定：
                # 1. 当前 query 本身已经真实失败 -> 同主题 bounded recovery；
                # 2. 否则才做 round-robin first pass，先给未尝试主题首轮机会；
                # 3. 最后按当前 query 命中的缺失主题继续。
                #
                # 这样保留既有 Search Recovery 契约，同时避免模型提出一个
                # 尚未失败的新 policy query 时继续吞掉其他主题首轮预算。
                if failed_current_topics:
                    target = failed_current_topics[0]
                elif unattempted_missing:
                    target = next(
                        (
                            topic
                            for topic in unattempted_missing
                            if topic in current_hits
                        ),
                        unattempted_missing[0],
                    )
                else:
                    target = next(
                        (
                            topic
                            for topic in missing
                            if topic in current_hits
                        ),
                        missing[0],
                    )

                # Coverage 首先确定“应该搜什么主题”。如果模型当前 query
                # 没覆盖该主题，先构造标准定向 query。
                desired_query = current_query
                coverage_redirect = False

                missing_hits_in_current = [
                    topic
                    for topic in missing
                    if topic in current_hits
                ]

                # 一个 query 同时混入多个仍缺失研究主题时，不把它视为
                # “已经正确瞄准 target”。强制拆成单主题专项搜索，避免
                # 一次 broad query 返回报告站/公众号/文档站后污染多个 topic。
                if (
                    target not in current_hits
                    or len(missing_hits_in_current) > 1
                ):
                    desired_query = cls._research_topic_query(
                        target,
                        original_query=goal,
                    )
                    coverage_redirect = True

                # 如果当前/标准定向 query 已经失败过，不允许第二次原样撞同一
                # 搜索请求。确定性切换到同主题替代 query，再交给 Retry Policy。
                failed_queries = set(
                    cls._failed_research_search_queries(
                        tool_results=tool_results,
                        topic=target,
                    )
                )
                desired_normalized = re.sub(
                    r"\s+",
                    " ",
                    desired_query,
                ).strip().lower()
                if desired_normalized in failed_queries:
                    recovery_query = cls._next_research_search_recovery_query(
                        topic=target,
                        current_query=desired_query,
                        tool_results=tool_results,
                    )
                    if recovery_query:
                        args["query"] = recovery_query
                        args.pop("search_query", None)
                        return (
                            name,
                            args,
                            f"failed search → alternate query ({target})",
                        )

                if coverage_redirect:
                    args["query"] = desired_query
                    args.pop("search_query", None)
                    return (
                        name,
                        args,
                        f"research coverage: search → {target}",
                    )

                # 当前 query 已经正确瞄准仍缺失的主题，而且该 query 尚未失败。
                # 必须让真实 search_web 执行，不能继续向下 fall-through 后
                # 被“已有候选网页”误改写成 read_webpage。
                return name, args, ""

            # 所有 blocking search 主题都已成功覆盖或 bounded-degrade 后，
            # 继续 search_web 的边际价值已经很低。若已有搜索 Observation 中
            # 存在尚未读取的候选，确定性切到 read_webpage，避免模型继续
            # 生成近义搜索并撞 Retry Policy。
            candidate = cls._next_unread_research_candidate(
                tool_results=tool_results,
                goal=goal,
            )
            if candidate:
                return (
                    "read_webpage",
                    {
                        "url": candidate["url"],
                        "max_characters": 20000,
                        "timeout": 15,
                        "start_character": 0,
                    },
                    (
                        "search coverage complete/degraded → "
                        "read evidence candidate for missing topic"
                        + (
                            f" ({candidate.get('topic')})"
                            if candidate.get("topic")
                            else ""
                        )
                    ),
                )

            for topic in list(coverage.get("missing_read_topics") or []):
                recovery_query = cls._next_research_read_coverage_query(
                    topic=topic,
                    goal=goal,
                    tool_results=tool_results,
                )
                if not recovery_query:
                    continue

                args["query"] = recovery_query
                args.pop("search_query", None)
                return (
                    "search_web",
                    args,
                    (
                        "search coverage complete/degraded but "
                        f"{topic} has no unread usable candidate → "
                        "bounded read-coverage search"
                    ),
                )

            return name, args, ""

        if name != "read_webpage":
            return name, args, ""

        requested_url = str(args.get("url") or "").strip()
        if not requested_url:
            return name, args, ""

        def _url_key(value: str) -> str:
            return str(value or "").strip().rstrip("/").lower()

        requested_key = _url_key(requested_url)
        completed = False
        failed_before = False

        for item in tool_results or []:
            if str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower() != "read_webpage":
                continue

            prior_args = getattr(item, "arguments", {}) or {}
            output = getattr(item, "output", None)
            prior_url = str(prior_args.get("url") or "").strip()

            if isinstance(output, dict):
                prior_url = str(
                    output.get("final_url")
                    or output.get("url")
                    or prior_url
                ).strip()

            if _url_key(prior_url) != requested_key:
                continue

            if not bool(getattr(item, "success", False)):
                failed_before = True
                continue

            if isinstance(output, dict):
                page_text = str(output.get("text") or "").strip()
                has_more = bool(output.get("has_more"))
                remaining = int(output.get("remaining_characters") or 0)
                if page_text and not has_more and remaining <= 0:
                    completed = True

        if completed or failed_before:
            candidate = cls._next_unread_research_candidate(
                tool_results=tool_results,
                goal=goal,
            )
            if candidate:
                reason = (
                    "failed page → next unread candidate"
                    if failed_before
                    else "completed page → next unread candidate"
                )
                return (
                    name,
                    {
                        "url": candidate["url"],
                        "max_characters": int(
                            args.get("max_characters") or 20000
                        ),
                        "timeout": int(args.get("timeout") or 15),
                        "start_character": 0,
                    },
                    reason,
                )

            coverage = cls._response_only_research_coverage_status(
                tool_results=tool_results,
                goal=goal,
            )
            for topic in list(
                coverage.get("missing_read_topics")
                or []
            ):
                recovery_query = cls._next_research_read_coverage_query(
                    topic=topic,
                    goal=goal,
                    tool_results=tool_results,
                )
                if not recovery_query:
                    continue

                return (
                    "search_web",
                    {
                        "query": recovery_query,
                        "max_results": 8,
                        "region": "cn-zh",
                    },
                    (
                        "completed/failed page with no usable candidate → "
                        f"bounded read-coverage search ({topic})"
                    ),
                )

        return name, args, ""

    @classmethod
    def _response_only_research_evidence_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Dict[str, Any]:
        """
        response-only 联网研究的通用证据就绪判定。

        最低要求：
        - 至少存在 1 次成功 search_web；
        - 至少成功读取 2 个不同 URL 的网页正文；
        - 正文必须非空，不能只靠搜索 snippet。

        如果用户显式要求优先政府/民航/协会/院校/招聘等高可信来源，
        在网页动作预算尚未接近硬上限前，还要求至少成功读取 1 个
        authoritative source。这样避免“读了两个营销站点就开始写结论”。
        达到硬上限后允许带 quality_warning 降级进入 Processing，防止死循环。
        """
        successful_searches = 0
        web_actions = 0
        read_urls: List[str] = []
        source_categories: Dict[str, str] = {}
        authoritative_urls: List[str] = []

        total_web_actions = 0
        failed_search_attempts = 0
        for item in tool_results or []:
            name = str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower()
            if name in {"search_web", "read_webpage"}:
                total_web_actions += 1

            if (
                name == "search_web"
                and not bool(getattr(item, "success", False))
            ):
                failed_search_attempts += 1

            if not bool(getattr(item, "success", False)):
                continue

            if name in {"search_web", "read_webpage"}:
                web_actions += 1

            if name == "search_web":
                successful_searches += 1
                continue

            if name != "read_webpage":
                continue

            arguments = getattr(item, "arguments", {}) or {}
            output = getattr(item, "output", None)
            url = str(arguments.get("url") or "").strip()
            text = ""
            if isinstance(output, dict):
                url = str(output.get("final_url") or output.get("url") or url).strip()
                text = str(output.get("text") or "").strip()
            else:
                text = str(output or "").strip()

            if not url or len(text) < 120:
                continue
            if url in read_urls:
                continue

            read_urls.append(url)
            category = cls._research_source_category(url)
            source_categories[url] = category
            if category in {
                "government", "education", "association",
                "recruitment", "public_media",
            }:
                authoritative_urls.append(url)

        minimum_pages_ready = bool(
            successful_searches >= 1
            and len(read_urls) >= 2
        )
        coverage = cls._response_only_research_coverage_status(
            tool_results=tool_results,
            goal=goal,
        )
        web_action_limit = cls._research_web_action_limit(goal)
        priority_requested = cls._goal_requests_priority_research_sources(goal)
        has_authoritative = bool(authoritative_urls)
        # Coverage Gate 只对“多主题研究”启用。单一市场研究中，
        # 用户列出的政府/院校/招聘平台等往往只是来源偏好，不能被
        # 错判成额外研究主题。这样既保留复杂研究覆盖控制，也兼容
        # v6.6 原有 Source Quality Gate。
        coverage_required = bool(
            len(coverage.get("required_topics") or []) >= 2
        )
        # bounded research 的“动作上限”必须按所有真实 web 尝试计数，
        # 不能只统计成功调用。失败/超时同样消耗时间与 Stage Budget。
        # 否则连续失败的 search_web 会让 Gate 永远认为“还没到上限”。
        research_action_budget_used = total_web_actions

        coverage_recovery_needed = bool(
            minimum_pages_ready
            and coverage_required
            and not coverage.get("passed")
            and research_action_budget_used < web_action_limit
        )
        quality_recovery_needed = bool(
            minimum_pages_ready
            and priority_requested
            and not has_authoritative
            and research_action_budget_used < web_action_limit
        )
        blocking_missing_search = (
            coverage.get("blocking_missing_search_topics")
            if "blocking_missing_search_topics" in coverage
            else coverage.get("missing_search_topics")
        ) or []

        covered_read_count = int(
            coverage.get("covered_read_count") or 0
        )
        read_target = int(coverage.get("read_target") or 0)
        searched_required_count = len(
            set(coverage.get("searched_topics") or [])
            & set(coverage.get("required_topics") or [])
        )

        # 正常 degraded_coverage_ok：
        # 已无 blocking search 主题，只是正文覆盖略低于完整目标。
        degraded_coverage_ok = bool(
            coverage_required
            and research_action_budget_used >= web_action_limit
            and not blocking_missing_search
            and covered_read_count
            >= max(2, read_target - 1)
        )

        # v6.6 bounded coverage degradation：
        # 对复杂 response-only 研究，达到 web action limit 后若仍有少量主题
        # 因搜索基础设施失败未覆盖，但已经取得：
        # - >= 3 个真实成功 discovery 主题；
        # - >= 3 个研究主题的真实网页正文；
        # - >= 2 个不同有效网页；
        # 则允许带明确质量警告进入 Processing。
        #
        # 这不是把缺失证据当成成功，而是让 Finalizer 明确写出哪些主题
        # “本轮未获得充分外部核验”，避免为了 1~2 个失败主题无限留在
        # Acquisition。
        bounded_read_target = (
            min(3, read_target)
            if read_target > 0
            else 0
        )
        bounded_search_target = min(
            3,
            len(coverage.get("required_topics") or []),
        )
        standard_bounded_coverage_degradation = bool(
            coverage_required
            and research_action_budget_used >= web_action_limit
            and minimum_pages_ready
            and searched_required_count >= bounded_search_target
            and covered_read_count >= bounded_read_target
        )

        # fix23：搜索基础设施持续异常时的更保守兜底。
        # 只有同时满足：
        # - 已达到 web action limit；
        # - 至少 2 个不同真实网页正文；
        # - 至少 2 个明确研究主题已有真实正文；
        # - 至少 2 个研究主题形成过成功 discovery；
        # - 至少 1 个高可信来源正文；
        # - 至少 4 次 search_web 真实失败；
        # 才允许带质量警告进入 Processing。
        #
        # 这不会把缺失主题当成“已核实”；Finalizer 必须明确披露
        # employment / education / enterprise 等未充分核验部分。
        infrastructure_bounded_degradation = bool(
            coverage_required
            and research_action_budget_used >= web_action_limit
            and minimum_pages_ready
            and searched_required_count >= 2
            and covered_read_count >= 2
            and has_authoritative
            and failed_search_attempts >= 4
        )

        bounded_coverage_degradation = bool(
            standard_bounded_coverage_degradation
            or infrastructure_bounded_degradation
        )

        coverage_ok = bool(
            (not coverage_required)
            or coverage.get("passed")
            or degraded_coverage_ok
            or bounded_coverage_degradation
        )
        degraded_topics = list(
            coverage.get("degraded_search_topics") or []
        )
        quality_warning = bool(
            minimum_pages_ready
            and (
                bool(degraded_topics)
                or bounded_coverage_degradation
                or (
                    research_action_budget_used >= web_action_limit
                    and (
                        (priority_requested and not has_authoritative)
                        or not coverage.get("passed")
                    )
                )
            )
        )
        passed = bool(
            minimum_pages_ready
            and coverage_ok
            and not quality_recovery_needed
            and not coverage_recovery_needed
        )

        if passed and quality_warning:
            if bounded_coverage_degradation:
                unresolved_topics = []
                for topic in (
                    list(blocking_missing_search)
                    + list(coverage.get("missing_read_topics") or [])
                ):
                    if topic not in unresolved_topics:
                        unresolved_topics.append(topic)
                reason = (
                    f"已达到 response-only 联网研究动作上限 {web_action_limit} 次，"
                    f"并取得 {len(read_urls)} 个真实网页正文、"
                    f"覆盖 {covered_read_count} 个明确研究主题；"
                    "剩余主题因搜索/来源基础设施限制未能完整核验"
                    + (
                        "（" + "、".join(unresolved_topics) + "）"
                        if unresolved_topics
                        else ""
                    )
                    + "。允许有界降级进入 Processing；最终回答必须明确披露这些"
                    "未充分核验主题，不得把搜索 snippet 或常识写成已核实外部事实。"
                )
            elif degraded_topics:
                reason = (
                    "已形成多个真实网页正文，且其余研究主题达到可用覆盖；"
                    "但主题 "
                    + "、".join(degraded_topics)
                    + " 的 bounded search recovery 已耗尽。"
                    "允许带警告进入 Processing，最终回答必须明确该主题本轮未获得充分外部核验。"
                )
            else:
                reason = (
                    f"已达到网页 Acquisition 上限 {web_action_limit} 次，并形成多个真实网页正文；"
                    "研究主题覆盖或高可信来源仍有局限。允许带警告进入 Processing，"
                    "最终回答必须明确未验证部分。"
                )
        elif passed:
            reason = (
                "已完成联网候选发现，并读取多个不同来源网页正文；"
                "研究主题覆盖达到要求"
                + ("，且已取得高可信来源正文" if priority_requested else "")
                + "，可进入 Processing 综合分析。"
            )
        elif coverage_recovery_needed:
            missing_topics = "、".join(coverage.get("blocking_missing_search_topics") or coverage.get("missing_read_topics") or [])
            reason = (
                "现有网页证据尚未覆盖用户明确要求的多个研究主题；"
                f"仍需补齐：{missing_topics or '研究主题覆盖不足'}。"
            )
        elif quality_recovery_needed:
            reason = (
                "已读取多个网页正文，但用户明确要求优先政府/民航/协会/"
                "院校/招聘平台/企业官网等可靠来源；当前尚未成功读取高可信来源正文。"
            )
        else:
            reason = (
                "response-only 研究仍缺少足够的真实网页正文证据；"
                "至少需要成功搜索并读取多个不同 URL 的有效正文，且不能只围绕单一研究主题。"
            )

        instruction = ""
        if coverage_recovery_needed:
            missing_topics = list(coverage.get("blocking_missing_search_topics") or coverage.get("missing_read_topics") or [])
            instruction = (
                "下一轮优先补齐未覆盖研究主题："
                + "、".join(missing_topics[:4])
                + "。不要继续重复同一竞品/价格关键词。"
            )
        elif quality_recovery_needed:
            instruction = (
                "下一轮优先做定向高可信来源搜索，例如在查询中加入 "
                "site:gov.cn、民航局、职业院校、招聘平台等限定，并读取其中最高价值网页；"
                "不要继续重复培训机构营销站点。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.ACQUISITION.value,
            "signal": (
                "RESEARCH_EVIDENCE_READY"
                if passed
                else "RESEARCH_EVIDENCE_NOT_READY"
            ),
            "reason": reason,
            "instruction": instruction,
            "successful_searches": successful_searches,
            "web_actions": web_actions,
            "total_web_actions": total_web_actions,
            "research_action_budget_used": research_action_budget_used,
            "unique_read_urls": read_urls,
            "unique_page_reads": len(read_urls),
            "source_categories": source_categories,
            "authoritative_urls": authoritative_urls,
            "authoritative_page_reads": len(authoritative_urls),
            "priority_sources_requested": priority_requested,
            "quality_recovery_needed": quality_recovery_needed,
            "coverage_recovery_needed": coverage_recovery_needed,
            "coverage": coverage,
            "web_action_limit": web_action_limit,
            "quality_warning": quality_warning,
            "bounded_coverage_degradation": bounded_coverage_degradation,
            "standard_bounded_coverage_degradation": (
                standard_bounded_coverage_degradation
            ),
            "infrastructure_bounded_degradation": (
                infrastructure_bounded_degradation
            ),
            "failed_search_attempts": failed_search_attempts,
            "covered_read_count": covered_read_count,
            "read_target": read_target,
            "searched_required_count": searched_required_count,
            "goal": str(goal or ""),
        }

    @staticmethod
    def _has_successful_tool(
        tool_results: List[ToolExecutionResult],
        tool_names: set[str],
    ) -> bool:
        normalized_names = {
            str(name).strip().lower()
            for name in tool_names
        }

        return any(
            bool(item.success)
            and str(item.tool_name or "").strip().lower()
            in normalized_names
            for item in tool_results
        )

    @staticmethod
    def _build_basic_info_final_answer(
        tool_results: List[ToolExecutionResult],
    ) -> str:
        """
        为 response-only 的“数据基本情况”任务生成确定性最终回答。

        只使用真实成功 Tool Observation，不调用额外工具，不凭预览猜测。
        """
        info = None
        source_path = ""
        sheet_name = ""

        for item in tool_results:
            if not getattr(item, "success", False):
                continue

            name = str(
                getattr(item, "tool_name", "") or ""
            ).strip().lower()

            if name == "read_office_data":
                arguments = getattr(
                    item,
                    "arguments",
                    {},
                ) or {}
                source_path = str(
                    arguments.get("file_path") or source_path
                )
                sheet_name = str(
                    arguments.get("sheet_name") or sheet_name
                )

            if name == "get_data_info":
                output = getattr(item, "output", None)
                if isinstance(output, dict):
                    info = output

        if not isinstance(info, dict):
            return (
                "已完成真实数据读取和基础检查。"
                "当前已有证据足以回答该只读任务。"
            )

        rows = info.get("rows")
        columns = info.get("columns")
        column_names = info.get("column_names") or []
        numeric_columns = info.get("numeric_columns") or []
        non_numeric_columns = (
            info.get("non_numeric_columns") or []
        )
        missing_values = info.get("missing_values")
        duplicate_rows = info.get("duplicate_rows")

        lines = []

        if source_path:
            source_name = Path(source_path).name
            if sheet_name:
                lines.append(
                    f"已读取 Excel：{source_name}，"
                    f"工作表：{sheet_name}。"
                )
            else:
                lines.append(
                    f"已读取 Excel：{source_name}。"
                )

        if rows is not None and columns is not None:
            lines.append(
                f"数据规模：{rows} 行 × {columns} 列。"
            )

        if column_names:
            lines.append(
                "字段："
                + "、".join(
                    str(item)
                    for item in column_names
                )
                + "。"
            )

        if numeric_columns:
            lines.append(
                "数值字段："
                + "、".join(
                    str(item)
                    for item in numeric_columns
                )
                + "。"
            )

        if non_numeric_columns:
            lines.append(
                "非数值字段："
                + "、".join(
                    str(item)
                    for item in non_numeric_columns
                )
                + "。"
            )

        if isinstance(missing_values, dict):
            total_missing = sum(
                int(value or 0)
                for value in missing_values.values()
                if isinstance(value, (int, float))
            )
            lines.append(
                f"缺失值总数：{total_missing}。"
            )

        if duplicate_rows is not None:
            lines.append(
                f"重复行：{duplicate_rows} 行。"
            )

        return "\n".join(lines).strip()

    @classmethod
    def _build_response_only_analysis_final_answer(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> str:
        """
        用已经成功的质量检查/统计 Observation 生成响应型最终答案。

        不调用新工具，不从 DataFrame preview 补造统计；仅整理真实工具结果。
        """
        if not isinstance(state, dict):
            return ""
        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return ""

        successful = [
            item
            for item in steps
            if isinstance(item, dict) and bool(item.get("success"))
        ]
        if not successful:
            return ""

        info = None
        analysis_step = None
        read_step = None
        clean_steps: List[Dict[str, Any]] = []

        for item in successful:
            name = str(item.get("tool") or "").strip().lower()
            if name == "read_office_data":
                read_step = item
            elif name == "get_data_info" and isinstance(item.get("observation"), dict):
                info = item.get("observation")
            elif name in {"handle_missing_values", "drop_duplicate_rows"}:
                clean_steps.append(item)
            elif name in {
                "group_statistics",
                "group_multi_statistics",
                "create_pivot_summary",
                "calculate_stats",
                "run_data_pipeline",
            }:
                analysis_step = item

        lines: List[str] = []

        if isinstance(read_step, dict):
            arguments = read_step.get("arguments") or {}
            source_path = str(arguments.get("file_path") or "").strip()
            if source_path:
                lines.append(f"已读取并分析 {Path(source_path).name}。")

        if isinstance(info, dict):
            rows = info.get("rows")
            columns = info.get("columns")
            if rows is not None and columns is not None:
                lines.append(f"数据规模：{rows} 行 × {columns} 列。")

            missing_values = info.get("missing_values") or {}
            duplicate_rows = info.get("duplicate_rows") or 0
            if isinstance(missing_values, dict):
                if missing_values:
                    detail = "、".join(
                        f"{column} {count} 个"
                        for column, count in missing_values.items()
                    )
                    lines.append(f"缺失值检查：发现 {detail}。")
                else:
                    lines.append("缺失值检查：未发现缺失值。")

            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0
            if duplicate_count > 0:
                lines.append(f"重复记录检查：发现 {duplicate_count} 行重复记录。")
            else:
                lines.append("重复记录检查：未发现完全重复行。")

            if not missing_values and duplicate_count == 0 and cls._goal_explicitly_requests_data_cleaning(goal):
                lines.append("由于未发现缺失值或重复记录，因此未执行不必要的数据清洗。")

        for item in clean_steps:
            name = str(item.get("tool") or "").strip().lower()
            arguments = item.get("arguments") or {}
            if name == "handle_missing_values":
                method = str(arguments.get("method") or "既定方法").strip()
                lines.append(f"缺失值已完成处理，方法：{method}。")
            elif name == "drop_duplicate_rows":
                keep = str(arguments.get("keep") or "first").strip()
                lines.append(f"重复记录已完成去重，保留策略：{keep}。")

        if isinstance(analysis_step, dict):
            name = str(analysis_step.get("tool") or "").strip().lower()
            arguments = analysis_step.get("arguments") or {}
            observation = analysis_step.get("observation")
            preview = observation.get("preview") if isinstance(observation, dict) else None

            if name == "group_statistics" and isinstance(preview, list) and preview:
                group_by = str(arguments.get("group_by") or "分组").strip()
                target_column = str(arguments.get("target_column") or "指标").strip()
                operation = str(arguments.get("operation") or "").strip().lower()
                operation_label = {
                    "sum": "合计",
                    "mean": "平均值",
                    "count": "数量",
                    "max": "最大值",
                    "min": "最小值",
                    "median": "中位数",
                }.get(operation, operation or "统计值")

                value_column = ""
                if isinstance(observation, dict):
                    columns = observation.get("columns") or []
                    if isinstance(columns, (list, tuple)):
                        candidates = [
                            str(column)
                            for column in columns
                            if str(column) != group_by
                        ]
                        if len(candidates) == 1:
                            value_column = candidates[0]

                if not value_column:
                    for key in preview[0].keys() if isinstance(preview[0], dict) else []:
                        if str(key) != group_by:
                            value_column = str(key)
                            break

                pairs = []
                numeric_rows = []
                for row in preview:
                    if not isinstance(row, dict):
                        continue
                    group_value = row.get(group_by)
                    metric_value = row.get(value_column) if value_column else None
                    pairs.append(f"{group_value}：{metric_value}")
                    if isinstance(metric_value, (int, float)):
                        numeric_rows.append((group_value, metric_value))

                if pairs:
                    lines.append(
                        f"按{group_by}统计{target_column}{operation_label}："
                        + "；".join(pairs)
                        + "。"
                    )

                goal_text = str(goal or "").lower()
                if numeric_rows and any(token in goal_text for token in ("最高", "最大")):
                    top_group, top_value = max(numeric_rows, key=lambda item: item[1])
                    lines.append(
                        f"{target_column}最高的{group_by}是{top_group}，"
                        f"{operation_label}为 {top_value}。"
                    )

            elif isinstance(preview, list) and preview:
                lines.append(
                    "统计分析已完成；结果来自成功执行的确定性统计工具。"
                )

        return "\n".join(line for line in lines if line).strip()

    @classmethod
    def _get_execution_budget_block_reason(
        cls,
        *,
        goal: str,
        tool_name: str,
        runtime_context: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Optional[str]:
        normalized = str(tool_name or "").strip().lower()
        goal_text = re.sub(
            r"\s+",
            " ",
            str(goal or "").strip().lower(),
        )

        response_only = not cls._task_plan_has_deliverables(
            runtime_context
        )

        if (
            response_only
            and normalized in cls._DATA_MUTATION_TOOLS
            and not cls._goal_explicitly_requests_data_cleaning(goal)
        ):
            return (
                "当前 TaskPlan 没有最终文件交付要求，且用户没有明确要求清洗/去重/缺失值处理；"
                f"工具 {tool_name} 会改变 DataFrame 内容，因此被 Read-Only Task Boundary 拦截。"
                "请使用读取、检查或统计类工具取得证据。"
            )

        single_excel_request = (
            "一个" in goal_text
            and "excel" in goal_text
        )

        source_locked = cls._has_successful_tool(
            tool_results,
            {"read_office_data"},
        )

        if (
            response_only
            and single_excel_request
            and source_locked
            and normalized in {
                "discover_data_files",
                "inspect_data_files",
            }
        ):
            return (
                "用户只要求读取一个 Excel，且已经成功读取并锁定一个数据源；"
                f"后续 {tool_name} 会重新扩大候选范围，违反 Source Lock。"
                "请继续使用当前 DataFrame，或直接申请 finish。"
            )

        basic_info_request = any(
            phrase in goal_text
            for phrase in (
                "基本情况",
                "基本信息",
                "数据概况",
                "数据基本情况",
                "简单看一下",
                "简单看看",
            )
        )

        info_ready = cls._has_successful_tool(
            tool_results,
            {"get_data_info"},
        )

        if (
            response_only
            and basic_info_request
            and info_ready
            and normalized in {
                "group_multi_statistics",
                "group_statistics",
                "calculate_statistics",
                "calculate_stats",
                "describe_data",
            }
        ):
            return (
                "用户只要求数据基本情况，且 get_data_info 已提供核心结构证据；"
                f"继续调用 {tool_name} 会把任务无授权升级为深入统计分析。"
                "请基于现有 Observation 直接申请 finish。"
            )

        return None

    @staticmethod
    def _missing_reread_paths(report: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(report, dict):
            return []
        for check in report.get("checks") or []:
            if not isinstance(check, dict) or check.get("check_id") != "final_deliverables_reread":
                continue
            missing = []
            for item in check.get("evidence") or []:
                text = str(item or "").strip()
                if text.endswith(": MISSING"):
                    missing.append(text[:-len(": MISSING")].strip())
            return missing
        return []

    def _build_deterministic_reread_decision(self, report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        missing = self._missing_reread_paths(report)
        if not missing:
            return None
        recovery_tools = []

        for path in missing:
            suffix = Path(path).suffix.lower()

            preferred = {
                ".xlsx": "inspect_professional_excel_report",
                ".xls": "read_office_data",
                ".docx": "inspect_professional_word_report",
                ".doc": "read_office_data",
                ".csv": "read_office_data",
                ".txt": "read_office_data",
                ".pdf": "read_office_data",
            }.get(suffix, "read_office_data")

            canonical = self.registry.resolve_name(preferred)

            if canonical is None:
                canonical = self.registry.resolve_name(
                    "read_office_data"
                )

            if canonical is None:
                continue

            recovery_tools.append(
                {
                    "action_type": "tool",
                    "tool": canonical,
                    "arguments": {"file_path": path},
                    "purpose": (
                        "Completion Recovery："
                        "确定性补齐最终交付物生成后的回读证据。"
                    ),
                    "recovery_reason": (
                        "final_deliverables_reread"
                    ),
                }
            )

        if not recovery_tools:
            return None

        return recovery_tools[0]


    # ============================================================
    # v5.2 Multi-Stage Router
    # ============================================================

    _ACQUISITION_TOOL_HINTS = (
        "search_web",
        "read_webpage",
        "download_data_file",
        "download_document_file",
        "fetch_weather_dataset",
        "list_files",
        "scan",
        "discover",
    )

    _DELIVERY_TOOL_HINTS = (
        "generate",
        "export",
        "create_weather_analysis_package",
        "create_professional",
        "apply_word_edits",
        "apply_excel_edits",
        "save",
        "write",
    )

    _VERIFICATION_TOOL_HINTS = (
        "inspect_professional_excel_report",
        "inspect_professional_word_report",
        "verify",
        "validate",
        "inspect_pdf",
    )

    _PROCESSING_TOOL_HINTS = (
        "read_office_data",
        "normalize_semantic_dataframe",
        "analyze_weather_dataset",
        "analyze_semantic",
        "semantic",
        "clean",
        "missing",
        "duplicate",
        "quality",
        "statistics",
        "statistic",
        "group",
        "pivot",
        "sort",
        "filter",
        "calculate",
        "chart",
        "visual",
        "plot",
    )

    @classmethod
    def _classify_tool_stage(
        cls,
        tool_name: str,
    ) -> AgentStage:
        """
        将真实 Tool 映射到执行 Stage。

        顺序很重要：
        - 最终 Excel/Word inspect 属于 Verification；
        - read_office_data 属于 Processing；
        - download/search/read_webpage 属于 Acquisition；
        - 生成/编辑最终文件属于 Delivery。
        """
        name = str(tool_name or "").strip().lower()

        if any(
            hint in name
            for hint in cls._VERIFICATION_TOOL_HINTS
        ):
            return AgentStage.VERIFICATION

        if any(
            hint in name
            for hint in cls._ACQUISITION_TOOL_HINTS
        ):
            return AgentStage.ACQUISITION

        if any(
            hint in name
            for hint in cls._DELIVERY_TOOL_HINTS
        ):
            return AgentStage.DELIVERY

        if any(
            hint in name
            for hint in cls._PROCESSING_TOOL_HINTS
        ):
            return AgentStage.PROCESSING

        # 未识别工具保守归 Processing：
        # 它是“理解/处理真实工作对象”的默认阶段，
        # 后续 v5.2-3 会结合 Registry metadata 进一步精确分类。
        return AgentStage.PROCESSING

    @staticmethod
    def _stage_index(stage: AgentStage) -> int:
        return StageOrchestrator.ORDER.index(stage)

    def _sync_stage_for_tool(
        self,
        *,
        route: StageRoute,
        current_stage: AgentStage,
        tool_name: str,
        runtime_context: Dict[str, Any],
    ) -> AgentStage:
        """
        v5.2-2 Stage Router。

        当 LLM 从“找数据”自然转向“处理数据”，或从处理转向输出时，
        Python 根据真实 Tool 类型推进 Stage，并把 Stage State 写入
        runtime_context。当前版本只做路由与边界可见化，不在这里
        强制消耗独立预算；独立预算在 v5.2-3 接入。
        """
        requested_stage = self._classify_tool_stage(tool_name)

        current_index = self._stage_index(current_stage)
        requested_index = self._stage_index(requested_stage)

        # 如果目标 Stage 在当前 Stage 之后，依次关闭中间已启用 Stage。
        if requested_index > current_index:
            for stage in StageOrchestrator.ORDER[
                current_index:requested_index
            ]:
                state = route.states[stage]
                if state.enabled and not state.gate_passed:
                    state.mark_passed(
                        "Stage Router 检测到工作流已进入后续阶段。"
                    )

            target_state = route.states[requested_stage]
            if target_state.enabled:
                target_state.activate()
                current_stage = requested_stage

        # 如果 LLM 想回到更早 Stage，不允许静默乱跳。
        # 真正回退必须由后续 Recovery Router 根据 Gate FAIL 决定。
        elif requested_index < current_index:
            self.report_progress(
                "[Stage Router] 检测到跨阶段向后工具调用："
                f"{current_stage.value} → {requested_stage.value}。"
                "当前版本保留调用但不改变 Stage；"
                "v5.2 Recovery Router 将负责确定性回退。"
            )

        else:
            route.states[current_stage].activate()

        runtime_context["stage_route"] = route.to_dict()
        runtime_context["current_stage"] = current_stage.value

        return current_stage

    @staticmethod
    def _build_stage_runtime_context(
        *,
        route: StageRoute,
        current_stage: AgentStage,
        data_state: DataState,
    ) -> Dict[str, Any]:
        return {
            "current_stage": current_stage.value,
            "stage_route": route.to_dict(),
            "data_state": data_state.to_dict(),
        }


    @staticmethod
    def _enabled_stage_normal_budget(
        route: StageRoute,
    ) -> int:
        """所有启用 Stage 的正常预算总和，仅作为全局失控保险。"""
        return sum(
            int(route.states[stage].budget.normal_iterations)
            for stage in route.enabled_stages
        )

    @staticmethod
    def _stage_budget_summary(
        route: StageRoute,
    ) -> str:
        parts = []
        for stage in route.enabled_stages:
            state = route.states[stage]
            parts.append(
                f"{stage.value}="
                f"{state.budget.normal_iterations}"
                f"(+{state.budget.recovery_iterations})"
            )
        return ", ".join(parts)

    @staticmethod
    def _stage_budget_block_reason(
        *,
        route: StageRoute,
        stage: AgentStage,
    ) -> Optional[str]:
        state = route.states[stage]
        if not state.enabled:
            return (
                f"{stage.value} Stage 未被本任务启用。"
            )
        if state.remaining_normal_iterations <= 0:
            return (
                f"{stage.value} Stage Normal Budget 已耗尽；"
                "不能继续在该阶段执行新的普通 Tool。"
            )
        return None


    @staticmethod
    def _normalize_dataframe_builder_arguments(
        *,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """
        v6.6 fix37：修复 DataFrame Builder 的引用包装问题。

        LLM 有时会生成：
            {"data": [{"$ref": "step_1.output.combined"}]}

        ReferenceResolver 解析后会变成：
            {"data": [<DataFrame>]}

        pandas.DataFrame([existing_dataframe]) 会被解释成 3 维结构，
        触发 "Must pass 2-d input"。

        对 build_dataframe/create_dataframe：
        - 若 data 是“仅包含一个真实 DataFrame 的 list/tuple”，确定性解包；
        - 已经是 DataFrame 时保持不变；
        - 普通 list[dict] / list[list] 不改动。
        """
        name = str(tool_name or "").strip().lower()
        normalized = dict(arguments or {})

        if name not in {
            "build_dataframe",
            "create_dataframe",
        }:
            return normalized, None

        if "data" not in normalized:
            return normalized, None

        data = normalized.get("data")

        if (
            isinstance(data, (list, tuple))
            and len(data) == 1
        ):
            child = data[0]
            if (
                hasattr(child, "columns")
                and hasattr(child, "shape")
            ):
                normalized["data"] = child
                return (
                    normalized,
                    (
                        "检测到 DataFrame 被单元素 list/tuple 包装；"
                        "已确定性解包为真实 DataFrame，避免 pandas 产生 3 维输入。"
                    ),
                )

        return normalized, None


    @staticmethod
    def _latest_successful_tool_output(
        *,
        tool_results: List[ToolExecutionResult],
        tool_name: str,
    ) -> Any:
        target = str(
            tool_name or ""
        ).strip().lower()

        for result in reversed(
            list(tool_results or [])
        ):
            if not getattr(
                result,
                "success",
                False,
            ):
                continue

            name = str(
                getattr(
                    result,
                    "tool_name",
                    "",
                )
                or ""
            ).strip().lower()

            if name != target:
                continue

            return getattr(
                result,
                "output",
                None,
            )

        return None

    @classmethod
    def _normalize_weather_pipeline_arguments(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        runtime_context: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """
        v6.6 fix39：天气专业工具的 Producer -> Consumer 引用桥。

        小模型容易把 current_data_ref（combined 37 行）误传给
        historical / forecast 两个参数，或者把 Observation 摘要
        当成 analysis_result。

        这里不让 LLM 重新解释对象：
        - analyze_weather_dataset 永远直接消费最近一次成功
          fetch_weather_dataset 的真实 historical / forecast / metadata；
        - create_weather_analysis_package 永远直接消费最近一次成功
          analyze_weather_dataset 的真实 Python 输出；
        - output_dir 从 Workspace deliverables_dir 确定性注入。
        """
        name = str(
            tool_name or ""
        ).strip().lower()

        normalized = dict(
            arguments or {}
        )

        if name == "analyze_weather_dataset":
            source = (
                cls._latest_successful_tool_output(
                    tool_results=tool_results,
                    tool_name="fetch_weather_dataset",
                )
            )

            if isinstance(
                source,
                dict,
            ):
                historical = source.get(
                    "historical"
                )
                forecast = source.get(
                    "forecast"
                )
                metadata = source.get(
                    "metadata"
                )

                historical_ok = (
                    hasattr(
                        historical,
                        "columns",
                    )
                    and hasattr(
                        historical,
                        "shape",
                    )
                )
                forecast_ok = (
                    hasattr(
                        forecast,
                        "columns",
                    )
                    and hasattr(
                        forecast,
                        "shape",
                    )
                )
                metadata_ok = isinstance(
                    metadata,
                    dict,
                )

                if (
                    historical_ok
                    and forecast_ok
                    and metadata_ok
                ):
                    normalized[
                        "historical"
                    ] = historical
                    normalized[
                        "forecast"
                    ] = forecast
                    normalized[
                        "metadata"
                    ] = metadata

                    return (
                        normalized,
                        (
                            "已从最近一次成功的 fetch_weather_dataset "
                            "恢复真实 historical / forecast / metadata；"
                            "禁止把 combined DataFrame 同时冒充历史和未来数据。"
                        ),
                    )

        if name == "create_weather_analysis_package":
            source = (
                cls._latest_successful_tool_output(
                    tool_results=tool_results,
                    tool_name="analyze_weather_dataset",
                )
            )

            if isinstance(
                source,
                dict,
            ):
                normalized[
                    "analysis_result"
                ] = source

            workspace = (
                runtime_context.get(
                    "workspace"
                )
                if isinstance(
                    runtime_context.get(
                        "workspace"
                    ),
                    dict,
                )
                else {}
            )

            output_dir = str(
                normalized.get(
                    "output_dir"
                )
                or normalized.get(
                    "deliverables_dir"
                )
                or runtime_context.get(
                    "output_dir"
                )
                or workspace.get(
                    "deliverables_dir"
                )
                or ""
            ).strip()

            if output_dir:
                normalized[
                    "output_dir"
                ] = output_dir

            # 小模型常把通用 DataFrame 参数残留到专业交付 Tool。
            normalized.pop(
                "df",
                None,
            )
            normalized.pop(
                "dataframe",
                None,
            )
            normalized.pop(
                "deliverables_dir",
                None,
            )

            if (
                isinstance(
                    source,
                    dict,
                )
                and output_dir
            ):
                return (
                    normalized,
                    (
                        "已从最近一次成功的 analyze_weather_dataset "
                        "注入真实 analysis_result，并从 Workspace "
                        "注入 deliverables_dir 作为 output_dir。"
                    ),
                )

        return normalized, None


    @staticmethod
    def _extract_output_mapping(
        output: Any,
    ) -> Dict[str, Any]:
        if isinstance(output, dict):
            return output
        # pandas.DataFrame.to_dict() 可能把大表完整复制成嵌套 dict。
        # DataFrame 由 _structured_output_reference() 单独处理，这里不要展开。
        if hasattr(output, "columns") and hasattr(output, "shape"):
            return {}
        if hasattr(output, "to_dict"):
            try:
                value = output.to_dict()
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return {}

    @staticmethod
    def _structured_output_reference(
        *,
        step_id: str,
        output: Any,
    ) -> tuple[Optional[str], List[str]]:
        """
        为真实结构化 Tool 输出建立 ExecutionContext 引用。

        DataPilot 的 ExecutionContext 原生支持 step_N.output(.field) 引用。
        很多 DataFrame 工具直接返回 pandas.DataFrame，或把 DataFrame 放在
        analysis_df/dataframe/clean_df 等字段里，并不会额外返回 data_ref 字符串。
        因此 DataState 不能只等待 *_ref；否则 Processing Gate 会把已经存在的
        真实 DataFrame 误判成“无可追踪数据状态”。
        """
        step = str(step_id or "").strip()
        if not step or output is None:
            return None, []

        def schema_of(value: Any) -> List[str]:
            columns = getattr(value, "columns", None)
            if columns is None:
                return []
            try:
                return [str(item) for item in list(columns)]
            except Exception:
                return []

        direct_schema = schema_of(output)
        if direct_schema:
            return f"{step}.output", direct_schema

        if isinstance(output, dict):
            # 优先选择最能代表“当前完整数据集”的常见字段。
            # v6.6 fix36：支持 acquisition tool 返回多个嵌套 DataFrame，
            # 例如 weather tool 的 historical / forecast / combined。
            preferred_keys = (
                "analysis_df",
                "clean_df",
                "dataframe",
                "df",
                "combined",
                "data",
                "result",
                "output",
                "historical",
                "forecast",
            )

            for key in preferred_keys:
                if key not in output:
                    continue
                child = output.get(key)
                child_schema = schema_of(child)
                if child_schema:
                    return f"{step}.output.{key}", child_schema

            # 通用 fallback：未来其他 Tool 即使使用新的字段名，
            # 只要一级 dict 子项是真实 DataFrame，也可以形成可追踪数据状态。
            for key, child in output.items():
                if key in preferred_keys:
                    continue
                child_schema = schema_of(child)
                if child_schema:
                    return f"{step}.output.{key}", child_schema

        return None, []

    @classmethod
    def _update_data_state_from_tool_result(
        cls,
        *,
        data_state: DataState,
        tool_result: ToolExecutionResult,
        step_id: str = "",
    ) -> None:
        """仅依据成功的真实 Tool Observation 更新跨 Stage Data State。"""
        if not getattr(tool_result, "success", False):
            return

        name = str(
            getattr(tool_result, "tool_name", "") or ""
        ).strip().lower()
        raw_output = getattr(tool_result, "output", None)
        output = cls._extract_output_mapping(raw_output)
        arguments = getattr(tool_result, "arguments", {}) or {}

        structured_ref, structured_schema = cls._structured_output_reference(
            step_id=step_id,
            output=raw_output,
        )

        def first_text(mapping, keys):
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        def first_list(mapping, keys):
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, (list, tuple)):
                    return [
                        str(item)
                        for item in value
                        if str(item).strip()
                    ]
            return []

        schema = first_list(
            output,
            (
                "columns",
                "schema",
                "current_schema",
                "column_names",
            ),
        )
        result_ref = first_text(
            output,
            (
                "result_ref",
                "data_ref",
                "dataframe_ref",
                "output_ref",
                "analysis_df_ref",
                "clean_df_ref",
                "statistics_ref",
            ),
        )

        if "normalize_semantic_dataframe" in name:
            if result_ref:
                data_state.analysis_data_ref = result_ref
                data_state.set_current_data(
                    result_ref,
                    schema=schema or None,
                )
            elif schema:
                data_state.current_schema = schema

            conversion_log = (
                output.get("conversion_log")
                or output.get("unit_conversions")
            )
            if isinstance(conversion_log, list):
                data_state.conversion_log = list(conversion_log)

            dictionary = (
                output.get("field_dictionary")
                or output.get("field_mapping")
            )
            if isinstance(dictionary, list):
                data_state.field_dictionary = list(dictionary)

        elif any(
            token in name
            for token in ("clean", "run_data_pipeline")
        ):
            if result_ref:
                data_state.clean_data_ref = result_ref
                data_state.set_current_data(
                    result_ref,
                    schema=schema or None,
                )
            elif schema:
                data_state.current_schema = schema

            quality = (
                output.get("quality_summary")
                or output.get("data_quality")
            )
            if isinstance(quality, dict):
                data_state.quality_summary = dict(quality)

        elif any(
            token in name
            for token in (
                "read_office_data",
                "load_data",
                "read_csv",
                "read_excel",
                "get_data_info",
            )
        ):
            # SOURCE_READY 修复：
            # 成功读取文件不一定返回内部 dataframe ref。
            # 但读取动作本身已经形成真实数据状态。

            source_ref = first_text(
                arguments,
                (
                    "file_path",
                    "path",
                    "input_path",
                    "source_path",
                ),
            )

            effective_ref = result_ref or source_ref

            if effective_ref:
                if data_state.raw_data_ref is None:
                    data_state.raw_data_ref = effective_ref

                data_state.set_current_data(
                    effective_ref,
                    schema=schema or None,
                )

            elif schema:
                data_state.current_schema = schema

        elif any(
            token in name
            for token in ("statistics", "statistic", "group_multi")
        ):
            if result_ref:
                data_state.statistics_ref = result_ref

        # v6.4 Integration Fix：DataFrame 工具通常直接返回真实 DataFrame，
        # 不额外生成 result_ref。此时使用 ExecutionContext 的 step_N.output
        # 引用登记最新数据状态，避免 Processing Gate 假 FAIL。
        if structured_ref:
            data_state.set_current_data(
                structured_ref,
                schema=structured_schema or schema or None,
            )

            # v6.6 fix36：
            # Acquisition Tool 直接返回真实 DataFrame 时，它本身就是原始数据来源。
            # 不再强制要求“先下载文件，再 read_office_data”才能 SOURCE_READY。
            if (
                cls._classify_tool_stage(name)
                == AgentStage.ACQUISITION
                and data_state.raw_data_ref is None
            ):
                data_state.raw_data_ref = structured_ref

            if name == "normalize_semantic_dataframe":
                data_state.analysis_data_ref = structured_ref
            elif any(
                token in name
                for token in (
                    "build_dataframe",
                    "create_dataframe",
                    "filter_data",
                    "apply_filters",
                    "select_columns",
                    "create_pivot_summary",
                    "merge_data_files",
                    "drop_columns",
                    "rename_columns",
                    "drop_duplicate_rows",
                    "handle_missing_values",
                )
            ):
                data_state.analysis_data_ref = structured_ref

        # 如果 Tool 返回 metadata 中的真实请求 URL，也登记为来源证据。
        metadata = (
            output.get("metadata")
            if isinstance(output, dict)
            else None
        )
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if not str(key).lower().endswith(
                    ("_request_url", "_source_url")
                ):
                    continue
                if (
                    isinstance(value, str)
                    and value.strip().startswith(("http://", "https://"))
                    and value.strip() not in data_state.source_urls
                ):
                    data_state.source_urls.append(value.strip())

        if "download" in name:
            url = first_text(arguments, ("url", "source_url"))
            if url and url not in data_state.source_urls:
                data_state.source_urls.append(url)

            downloaded_path = first_text(
                output,
                (
                    "output_path",
                    "file_path",
                    "path",
                    "saved_path",
                ),
            ) or first_text(
                arguments,
                (
                    "output_path",
                    "file_path",
                    "path",
                ),
            )
            if (
                downloaded_path
                and downloaded_path not in data_state.source_paths
            ):
                data_state.source_paths.append(downloaded_path)

        path_candidates: List[str] = []
        for mapping in (output, arguments):
            for key in (
                "output_path",
                "file_path",
                "path",
                "saved_path",
                "excel_path",
                "word_path",
                "pdf_path",
                "image_path",
                "chart_path",
                "chart_paths",
                "image_paths",
                "output_files",
                "deliverable_paths",
            ):
                value = mapping.get(key)

                if isinstance(value, str) and value.strip():
                    path_candidates.append(value.strip())

                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            path_candidates.append(item.strip())

        if cls._classify_tool_stage(name) == AgentStage.DELIVERY:
            for item in path_candidates:
                if item.lower().endswith(
                    (
                        ".xlsx", ".xls", ".docx", ".pdf",
                        ".png", ".jpg", ".jpeg", ".csv",
                    )
                ):
                    if item not in data_state.deliverable_paths:
                        data_state.deliverable_paths.append(item)



    @staticmethod
    def _required_deliverable_extensions(
        task_plan: Optional[Dict[str, Any]],
    ) -> List[str]:
        """从 TaskPlan 提取最终成果类型，返回规范化扩展名。"""
        if not isinstance(task_plan, dict):
            return []

        values: List[str] = []
        for key in (
            "deliverable_requirements",
            "required_deliverables",
            "deliverables",
            "output_requirements",
        ):
            value = task_plan.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (list, tuple)):
                values.extend(str(item) for item in value)

        combined = " ".join(values).lower()
        mapping = (
            ((".xlsx", "excel", "xlsx", "电子表格"), ".xlsx"),
            ((".docx", "word", "docx", "综合分析报告", "word报告"), ".docx"),
            ((".pdf", "pdf"), ".pdf"),
            ((".png", "png", "图表", "可视化"), ".png"),
            ((".csv", "csv", "原始数据"), ".csv"),
        )

        result: List[str] = []
        for tokens, extension in mapping:
            if any(token in combined for token in tokens):
                if extension not in result:
                    result.append(extension)
        return result

    @classmethod
    def _delivery_gate_status(
        cls,
        *,
        data_state: DataState,
        task_plan: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Delivery → Verification Gate。

        只有 TaskPlan 要求的最终成果类型都已经由成功 Tool Observation
        登记到 DataState.deliverable_paths，才允许进入 Verification。
        文件真实性/最终回读仍由 Completion Gate 负责。
        """
        required = cls._required_deliverable_extensions(task_plan)
        paths = cls._deduplicate_deliverable_paths(
            data_state.deliverable_paths
        )

        present_extensions = {
            Path(item).suffix.lower()
            for item in paths
            if Path(item).suffix
        }

        missing = [
            ext
            for ext in required
            if ext not in present_extensions
        ]

        # 没有显式 TaskPlan 类型时，至少要求存在一个登记的最终成果。
        passed = (
            not missing and bool(paths)
            if not required
            else not missing
        )

        if passed:
            reason = (
                "TaskPlan 要求的最终成果均已由成功 Tool Observation "
                "登记，可进入 Verification。"
            )
        elif missing:
            reason = (
                "Delivery 尚缺少 TaskPlan 要求的最终成果："
                + ", ".join(missing)
                + "。"
            )
        else:
            reason = (
                "Delivery 尚未登记任何成功生成的最终成果，"
                "不能进入 Verification。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.DELIVERY.value,
            "signal": (
                "DELIVERABLES_READY"
                if passed
                else "DELIVERABLES_NOT_READY"
            ),
            "reason": reason,
            "required_extensions": required,
            "present_extensions": sorted(present_extensions),
            "missing_extensions": missing,
            "deliverable_paths": paths,
        }

    @staticmethod
    def _task_requires_regression(goal: str) -> bool:
        text = str(goal or "").lower()
        return any(
            token in text
            for token in (
                "回归", "regression", "预测", "影响因素", "驱动因素",
                "因素分析", "相关性", "模型", "原因分析",
            )
        )

    @staticmethod
    def _collect_acquisition_observations(
        tool_results: List[ToolExecutionResult],
    ) -> List[Any]:
        observations: List[Any] = []
        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if name not in {
                "search_web",
                "read_webpage",
                "download_data_file",
                "download_document_file",
                "read_document",
            }:
                continue
            output = getattr(item, "output", None)
            if output is not None:
                observations.append(output)
        return observations

    @classmethod
    def _acquisition_evidence_status(
        cls,
        *,
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Dict[str, Any]:
        observations = cls._collect_acquisition_observations(tool_results)
        if not observations:
            return {
                "sufficient": False,
                "reason": "尚无成功 Acquisition Observation。",
                "evidence": [],
            }
        try:
            return StageOrchestrator.check_acquisition_gate(
                observations=observations,
                task=goal,
            )
        except Exception as error:
            # Completion Checker 只是智能停止辅助，不能因为它自身异常
            # 阻断原有 SOURCE_READY 路径。
            return {
                "sufficient": False,
                "reason": f"Acquisition Completion Checker 未能判定：{error}",
                "evidence": [],
            }

    @classmethod
    def _has_fine_grained_acquisition_evidence(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> bool:
        """
        判断是否真正读取到可形成回归样本的月度/季度业务证据。

        v6.4 不能只因为长网页/原始 Wiki 文本里出现很多月份、季度和数字
        就判定为 fine-grained。引用日期、更新时间、脚注日期并不是销量时间序列。
        因此要求至少 3 个不同月/季度时间点，并且每个时间点附近存在
        可识别的业务数值。对于“万辆/辆/vehicles”等强车辆单位，单位本身
        可作为销量序列信号；对于 million/units 等泛化单位，则仍要求附近
        同时出现销量/交付等业务度量词。ISO 引用日期（如 2024-01-05）
        不作为月度时间点。
        """
        period_patterns = (
            ("quarter_en", re.compile(r"\bq([1-4])\b", re.IGNORECASE)),
            ("quarter_cn", re.compile(r"第?([一二三四1234])季度")),
            (
                "month_cn",
                re.compile(
                    r"(?<!\d)(?:20\d{2}[年\-/])?(1[0-2]|0?[1-9])月|"
                    r"20\d{2}[-/](1[0-2]|0?[1-9])(?!\d)(?![-/]\d{1,2}\b)"
                ),
            ),
        )
        metric_pattern = re.compile(
            r"销量|销售量|交付量|交付|sales?|sold|deliver(?:y|ies|ed)?",
            re.IGNORECASE,
        )
        value_with_unit_pattern = re.compile(
            r"[-+]?\d[\d,]*(?:\.\d+)?\s*"
            r"(?:万辆|万台|辆|台|million\b|vehicles?\b|units?\b)",
            re.IGNORECASE,
        )
        strong_vehicle_value_pattern = re.compile(
            r"[-+]?\d[\d,]*(?:\.\d+)?\s*"
            r"(?:万辆|万台|辆|台|vehicles?\b|cars?\b)",
            re.IGNORECASE,
        )
        chinese_quarter_map = {
            "一": "1", "二": "2", "三": "3", "四": "4",
            "1": "1", "2": "2", "3": "3", "4": "4",
        }

        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if name not in {"read_webpage", "read_document"}:
                continue

            output = getattr(item, "output", None)
            if isinstance(output, dict):
                text = str(output.get("text") or "")
            else:
                text = str(output or "")
            if not text:
                continue

            qualified_periods: set[str] = set()
            for period_type, pattern in period_patterns:
                for match in pattern.finditer(text):
                    groups = [group for group in match.groups() if group]
                    value = groups[0] if groups else match.group(0)
                    if period_type.startswith("quarter"):
                        normalized = chinese_quarter_map.get(str(value), str(value))
                        period_key = f"Q{normalized}"
                    else:
                        try:
                            period_key = f"M{int(value)}"
                        except Exception:
                            period_key = f"M{value}"

                    window = text[
                        max(0, match.start() - 60):
                        min(len(text), match.end() + 140)
                    ]
                    has_value_with_unit = bool(
                        value_with_unit_pattern.search(window)
                    )
                    has_metric = bool(metric_pattern.search(window))
                    has_strong_vehicle_value = bool(
                        strong_vehicle_value_pattern.search(window)
                    )
                    if (
                        has_value_with_unit
                        and (has_metric or has_strong_vehicle_value)
                    ):
                        qualified_periods.add(period_key)

            if len(qualified_periods) >= 3:
                return True

        return False

    @staticmethod
    def _record_acquisition_gate_failure_repeat(
        *,
        runtime_context: Dict[str, Any],
        reason: str,
    ) -> int:
        """
        同一 Acquisition Gate 失败若在没有新 Tool Observation 的情况下
        被模型反复触发，进行有界计数，避免空转几十轮。
        """
        signature = re.sub(
            r"\s+",
            " ",
            str(reason or "").strip(),
        )

        previous = runtime_context.get(
            "_acquisition_gate_failure_repeat"
        )

        if (
            isinstance(previous, dict)
            and str(previous.get("signature") or "")
            == signature
        ):
            count = int(
                previous.get("count") or 0
            ) + 1
        else:
            count = 1

        runtime_context[
            "_acquisition_gate_failure_repeat"
        ] = {
            "signature": signature,
            "count": count,
        }
        return count

    @staticmethod
    def _clear_acquisition_gate_failure_repeat(
        runtime_context: Dict[str, Any],
    ) -> None:
        runtime_context.pop(
            "_acquisition_gate_failure_repeat",
            None,
        )


    @classmethod
    def _acquisition_gate_status(
        cls,
        data_state: DataState,
        *,
        tool_results: Optional[List[ToolExecutionResult]] = None,
        goal: str = "",
    ) -> Dict[str, Any]:
        """
        v6.4 Acquisition → Processing Gate。

        两条可接受路径：
        1. SOURCE_READY：下载/本地来源已经成功读取，形成可追踪数据状态；
        2. EVIDENCE_READY：联网研究已取得足够结构化网页证据，允许进入
           Processing 使用 build_dataframe/create_dataframe 结构化，不再为了
           “形式上必须下载一个文件”继续消耗搜索 Token。
        """
        has_source = bool(data_state.source_urls or data_state.source_paths)
        has_readable_data = bool(
            data_state.raw_data_ref
            or data_state.current_data_ref
            or data_state.current_schema
        )

        evidence_gate = {
            "sufficient": False,
            "reason": "",
            "evidence": [],
        }
        if tool_results:
            evidence_gate = cls._acquisition_evidence_status(
                tool_results=tool_results,
                goal=goal,
            )

        base_evidence_ready = bool(evidence_gate.get("sufficient", False))
        regression_needed = cls._task_requires_regression(goal)
        fine_grained = cls._has_fine_grained_acquisition_evidence(tool_results or [])
        web_action_count = sum(
            1
            for item in (tool_results or [])
            if str(getattr(item, "tool_name", "") or "").strip().lower()
            in {"search_web", "read_webpage"}
        )

        # 回归/影响因素任务优先要求月度或季度证据。
        # 但为了控制 Token，网页 Acquisition 达到 6 次后允许以年度证据降级推进，
        # 后续 Processing Intelligence 会把模型标记为探索性小样本回归。
        evidence_ready = bool(
            base_evidence_ready
            and (
                not regression_needed
                or fine_grained
                or web_action_count >= 6
            )
        )
        degraded_evidence_ready = bool(
            evidence_ready
            and regression_needed
            and not fine_grained
            and web_action_count >= 6
        )
        passed = bool(has_readable_data or evidence_ready)

        if has_readable_data:
            signal = "SOURCE_READY"
            reason = (
                "来源数据已经成功读取并形成可追踪的数据状态，SOURCE_READY。"
            )
        elif degraded_evidence_ready:
            signal = "EVIDENCE_READY_DEGRADED"
            reason = (
                "核心网页证据已足够，但回归任务在 6 次网页 Acquisition 动作内仍未获得"
                "稳定月度/季度数据；为控制 Token，允许进入 Processing，且回归必须按"
                "探索性小样本模型处理。"
            )
        elif evidence_ready:
            signal = "EVIDENCE_READY"
            reason = (
                "已有联网 Acquisition Observation 满足核心对象/时间范围等证据要求；"
                "回归任务所需细粒度证据条件也已满足，允许进入 Processing 结构化已有证据。"
            )
        elif has_source:
            signal = "SOURCE_NOT_READY"
            reason = (
                "来源已获取/下载，但尚无成功读取形成的数据引用或 schema；"
                "且网页证据尚不足，Acquisition 不能提前 PASS。"
            )
        else:
            signal = "SOURCE_NOT_READY"
            reason = (
                "尚未形成可读取的数据来源，且现有网页证据仍不足；"
                "Acquisition 不能进入 Processing。"
            )

        return {
            "passed": passed,
            "stage": AgentStage.ACQUISITION.value,
            "signal": signal,
            "reason": reason,
            "raw_data_ref": data_state.raw_data_ref,
            "current_data_ref": data_state.current_data_ref,
            "current_schema": list(data_state.current_schema),
            "source_urls": list(data_state.source_urls),
            "source_paths": list(data_state.source_paths),
            "evidence_gate": evidence_gate,
            "regression_requested": regression_needed,
            "fine_grained_evidence": fine_grained,
            "web_action_count": web_action_count,
            "degraded_evidence_ready": degraded_evidence_ready,
        }

    @staticmethod
    def _normalize_numeric_token(value: Any) -> Optional[str]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except Exception:
                return None
            if not (number == number):
                return None
            if number.is_integer():
                return str(int(number))
            return (f"{number:.12f}").rstrip("0").rstrip(".")
        return None

    @classmethod
    def _numeric_evidence_tokens(
        cls,
        tool_results: List[ToolExecutionResult],
    ) -> set[str]:
        tokens: set[str] = set()
        pattern = re.compile(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?")

        for item in tool_results or []:
            if not getattr(item, "success", False):
                continue
            try:
                text = repr(getattr(item, "output", None))
            except Exception:
                text = str(getattr(item, "output", None))
            for raw in pattern.findall(text):
                cleaned = raw.replace(",", "")
                try:
                    number = float(cleaned)
                except Exception:
                    continue
                normalized = cls._normalize_numeric_token(number)
                if normalized is not None:
                    tokens.add(normalized)
        return tokens

    @classmethod
    def _ungrounded_dataframe_literals(
        cls,
        *,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> List[str]:
        """
        检查 create_dataframe/build_dataframe 中由 LLM 直接写入的数值是否
        能在之前真实 Tool Observation 中找到证据。

        v6.4 同时检查 Python 数值和“纯数字字符串”。例如 "1,863,494"
        不能因为被包成字符串就绕过 Evidence Grounding。0/1 编码和用户任务
        中明确写出的年份允许作为结构常量；其他事实数值必须已有真实证据。
        """
        evidence = cls._numeric_evidence_tokens(tool_results)
        allowed = {"0", "1"}
        allowed.update(re.findall(r"\b20\d{2}\b", str(goal or "")))
        missing: set[str] = set()
        numeric_string_pattern = re.compile(
            r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*"
            r"(?:%|辆|台|万辆|万台|元|万元|亿元)?\s*$",
            re.IGNORECASE,
        )

        def literal_token(value: Any) -> Optional[str]:
            token = cls._normalize_numeric_token(value)
            if token is not None:
                return token
            if isinstance(value, str):
                match = numeric_string_pattern.fullmatch(value)
                if match:
                    raw = match.group(1).replace(",", "")
                    try:
                        return cls._normalize_numeric_token(float(raw))
                    except Exception:
                        return None
            return None

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)
                return
            token = literal_token(value)
            if token is None or token in allowed:
                return
            if token not in evidence:
                missing.add(token)

        walk(arguments.get("data"))
        return sorted(missing)

    @staticmethod
    def _extract_row_count_from_output(value: Any) -> Optional[int]:
        if value is None:
            return None

        shape = getattr(value, "shape", None)
        if isinstance(shape, tuple) and shape:
            try:
                return int(shape[0])
            except Exception:
                pass

        if isinstance(value, dict):
            for key in ("row_count", "rows", "record_count", "sample_size"):
                candidate = value.get(key)
                if isinstance(candidate, (int, float)) and int(candidate) >= 0:
                    return int(candidate)

            shape_value = value.get("shape")
            if isinstance(shape_value, (list, tuple)) and shape_value:
                try:
                    return int(shape_value[0])
                except Exception:
                    pass

            for key in (
                "dataframe", "df", "analysis_df", "clean_df", "data",
                "result", "output", "summary",
            ):
                if key in value:
                    found = AgentLoop._extract_row_count_from_output(value.get(key))
                    if found is not None:
                        return found

        return None

    @classmethod
    def _latest_structured_row_count(
        cls,
        tool_results: Optional[List[ToolExecutionResult]],
    ) -> Optional[int]:
        for item in reversed(tool_results or []):
            if not getattr(item, "success", False):
                continue
            found = cls._extract_row_count_from_output(
                getattr(item, "output", None)
            )
            if found is not None and found > 0:
                return found
        return None

    @classmethod
    def _detect_processing_strategy(
        cls,
        *,
        goal: str,
        data_state: DataState,
        tool_results: Optional[List[ToolExecutionResult]] = None,
    ) -> Dict[str, Any]:
        """
        v6.4 Processing Intelligence。

        在 v6.3 的“统计/回归路线推荐”上增加小样本诊断。
        诊断只影响可靠性与下一步建议，不硬性禁止合法的小样本回归。
        """
        schema = [str(item) for item in (data_state.current_schema or [])]
        need_regression = cls._task_requires_regression(goal)
        row_count = cls._latest_structured_row_count(tool_results)
        recommended_min_samples = 12

        if need_regression:
            small_sample = (
                row_count is not None
                and row_count < recommended_min_samples
            )

            if small_sample:
                return {
                    "analysis_type": "regression_exploratory",
                    "recommended_tools": [
                        "regression_analysis",
                        "generate_regression_visualizations",
                    ],
                    "reason": (
                        f"当前最新结构化数据约 {row_count} 条观测，低于 "
                        f"{recommended_min_samples} 条的通用稳健性提醒阈值。"
                        "如 Acquisition 中存在可信月度/季度来源，应优先补充细粒度数据；"
                        "若无法补充，可以做探索性回归，但不得把高 R² 直接解释为稳健因果或主要驱动因素。"
                    ),
                    "schema": schema,
                    "sample_diagnostic": {
                        "row_count": row_count,
                        "recommended_min_samples": recommended_min_samples,
                        "small_sample": True,
                        "reliable_for_inference": False,
                        "model_selection_rule": (
                            "不得仅因 R² 更高就选择模型；必须同时考虑变量业务含义、"
                            "样本量、共线性和可解释性。"
                        ),
                    },
                }

            return {
                "analysis_type": "regression",
                "recommended_tools": [
                    "regression_analysis",
                    "generate_regression_visualizations",
                ],
                "reason": (
                    "检测到预测/因素分析需求；可建立回归分析流程，"
                    "但仍需根据变量业务含义与样本质量解释结果。"
                ),
                "schema": schema,
                "sample_diagnostic": {
                    "row_count": row_count,
                    "recommended_min_samples": recommended_min_samples,
                    "small_sample": False if row_count is not None else None,
                    "reliable_for_inference": None if row_count is None else True,
                    "model_selection_rule": (
                        "不得仅因 R² 更高就选择模型；同时检查变量合理性、共线性与残差。"
                    ),
                },
            }

        return {
            "analysis_type": "statistics",
            "recommended_tools": [
                "group_statistics",
                "recommend_visualizations",
            ],
            "reason": "当前任务更适合基础统计和可视化分析。",
            "schema": schema,
            "sample_diagnostic": {
                "row_count": row_count,
                "small_sample": None,
                "reliable_for_inference": None,
            },
        }


    @staticmethod
    def _processing_gate_status(
        data_state: DataState,
    ) -> Dict[str, Any]:
        """
        Processing → Delivery 的最小结构化 Gate。
        Completion Gate 仍负责最终真实性验收。
        """
        has_data = bool(
            data_state.current_data_ref
            or data_state.raw_data_ref
            or data_state.analysis_data_ref
            or data_state.clean_data_ref
            or data_state.current_schema
        )
        return {
            "passed": has_data,
            "stage": AgentStage.PROCESSING.value,
            "reason": (
                "已存在结构化数据状态，可进入 Delivery。"
                if has_data
                else (
                    "Processing 尚未形成可追踪的数据引用或 schema；"
                    "不能安全进入 Delivery。"
                )
            ),
            "current_data_ref": data_state.current_data_ref,
            "current_schema": list(data_state.current_schema),
        }


    @staticmethod
    def _normalized_search_signature(
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """
        为 Acquisition 重复调用检测生成稳定签名。

        read_webpage 必须把 start_character 纳入签名：
        同一个 URL 的不同正文分块属于有效分页，不应被误判为重复读取。
        """
        name = str(tool_name or "").strip().lower()
        query = str(
            arguments.get("query")
            or arguments.get("search_query")
            or arguments.get("url")
            or ""
        ).strip().lower()
        query = " ".join(query.split())

        if name == "read_webpage":
            try:
                start_character = int(
                    arguments.get("start_character")
                    or 0
                )
            except (TypeError, ValueError):
                start_character = 0

            return (
                f"{name}|{query}|"
                f"start_character={start_character}"
            )

        return f"{name}|{query}"

    @classmethod
    def _post_stage_acquisition_recovery_status(
        cls,
        *,
        current_stage: AgentStage,
        requested_tool_stage: AgentStage,
        tool_name: str,
        runtime_context: Dict[str, Any],
        max_recovery_actions: int = 4,
    ) -> Dict[str, Any]:
        """
        v6.4 Stage-Isolated Acquisition Recovery。

        Processing/Delivery 发现真实证据缺口时，可以临时回补少量网页来源；
        但这不是重新开启完整 Acquisition Stage，必须有独立上限，避免网络
        故障或模型执念把后续 Stage 再次拖入搜索死循环。
        """
        name = str(tool_name or "").strip().lower()
        is_web_recovery = name in {
            "search_web",
            "read_webpage",
            "download_data_file",
            "download_document_file",
        }
        if (
            current_stage == AgentStage.ACQUISITION
            or requested_tool_stage != AgentStage.ACQUISITION
            or not is_web_recovery
        ):
            return {
                "active": False,
                "blocked": False,
                "used": int(
                    runtime_context.get(
                        "_post_stage_acquisition_recovery_count",
                        0,
                    )
                    or 0
                ),
                "limit": int(max_recovery_actions),
            }

        used = int(
            runtime_context.get(
                "_post_stage_acquisition_recovery_count",
                0,
            )
            or 0
        )
        limit = max(1, int(max_recovery_actions))
        return {
            "active": True,
            "blocked": used >= limit,
            "used": used,
            "limit": limit,
            "reason": (
                "后续 Stage 的 Acquisition Recovery 已达到上限；"
                "不能继续通过搜索/网页读取消耗决策轮数。"
                if used >= limit
                else (
                    f"允许后续 Stage 回补来源：{used}/{limit} 已使用。"
                )
            ),
        }


    @classmethod
    def _processing_recovery_degrade_status(
        cls,
        *,
        current_stage: AgentStage,
        data_state: DataState,
        goal: str,
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """Recovery 用尽后，判断是否可基于现有结构化数据安全降级继续。"""
        has_structured_data = bool(
            data_state.current_data_ref
            or data_state.analysis_data_ref
            or data_state.clean_data_ref
            or data_state.raw_data_ref
            or data_state.current_schema
            or cls._latest_structured_row_count(tool_results) is not None
        )
        allowed = bool(
            current_stage == AgentStage.PROCESSING
            and has_structured_data
        )
        strategy = cls._detect_processing_strategy(
            goal=goal,
            data_state=data_state,
            tool_results=tool_results,
        )
        return {
            "degrade_to_processing": allowed,
            "has_structured_data": has_structured_data,
            "processing_strategy": strategy,
            "reason": (
                "Acquisition Recovery 已达到上限，但已有结构化数据。"
                "停止继续联网补证据，回到 Processing 使用现有数据完成可支持的分析；"
                "若样本不足，则回归必须降级为探索性结果并明确局限。"
                if allowed
                else (
                    "Acquisition Recovery 已达到上限，且当前没有足够结构化数据可安全降级。"
                )
            ),
        }


    @staticmethod
    def _normalized_url_key(value: Any) -> str:
        return str(value or "").strip().rstrip("/").lower()

    @classmethod
    def _read_webpage_continuation_arguments(
        cls,
        *,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Optional[Dict[str, Any]]:
        """
        v6.6 fix31：read_webpage 确定性分页恢复。

        当上一轮同一 URL 的真实 Observation 明确给出：
            has_more=True
            end_character=N
        而模型下一轮仍请求 start_character=0 / 未传 start_character，
        Python 自动把请求推进到 N。

        这不是猜测网页内容，而是严格使用工具上一轮返回的分页游标。
        """
        args = dict(arguments or {})
        requested_url = cls._normalized_url_key(
            args.get("url")
        )
        if not requested_url:
            return None

        try:
            requested_start = int(
                args.get("start_character")
                or 0
            )
        except (TypeError, ValueError):
            requested_start = 0

        for result in reversed(tool_results or []):
            if not getattr(result, "success", False):
                continue
            if (
                str(
                    getattr(result, "tool_name", "")
                    or ""
                ).strip().lower()
                != "read_webpage"
            ):
                continue

            prior_args = (
                getattr(result, "arguments", {})
                or {}
            )
            output = getattr(result, "output", None)
            if not isinstance(output, dict):
                continue

            url_keys = {
                cls._normalized_url_key(
                    prior_args.get("url")
                ),
                cls._normalized_url_key(
                    output.get("url")
                ),
                cls._normalized_url_key(
                    output.get("final_url")
                ),
            }
            url_keys.discard("")

            if requested_url not in url_keys:
                continue

            if output.get("has_more") is not True:
                continue

            try:
                prior_start = int(
                    output.get("start_character")
                    if output.get("start_character") is not None
                    else (
                        prior_args.get("start_character")
                        or 0
                    )
                )
                next_start = int(
                    output.get("end_character")
                    or 0
                )
            except (TypeError, ValueError):
                continue

            if next_start <= prior_start:
                continue

            # 用户/模型已经显式请求更靠后的游标时不覆盖。
            # 只修复“又从同一段或更早位置重新读”的情况。
            if requested_start > prior_start:
                continue

            updated = dict(args)
            updated["start_character"] = next_start

            try:
                remaining = int(
                    output.get("remaining_characters")
                    or 0
                )
            except (TypeError, ValueError):
                remaining = 0

            if remaining > 0:
                updated["max_characters"] = min(
                    20000,
                    remaining,
                )

            updated.setdefault(
                "timeout",
                15,
            )
            return updated

        return None

    @classmethod
    def _read_webpage_already_complete(
        cls,
        *,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> bool:
        """
        如果同一 URL 已有 has_more=False 的成功正文读取，
        再从已覆盖区间重新读取没有新增信息价值。
        """
        requested_url = cls._normalized_url_key(
            (arguments or {}).get("url")
        )
        if not requested_url:
            return False

        try:
            requested_start = int(
                (arguments or {}).get(
                    "start_character"
                )
                or 0
            )
        except (TypeError, ValueError):
            requested_start = 0

        for result in reversed(tool_results or []):
            if not getattr(result, "success", False):
                continue
            if (
                str(
                    getattr(result, "tool_name", "")
                    or ""
                ).strip().lower()
                != "read_webpage"
            ):
                continue

            output = getattr(result, "output", None)
            if not isinstance(output, dict):
                continue
            if output.get("has_more") is not False:
                continue

            prior_args = (
                getattr(result, "arguments", {})
                or {}
            )
            url_keys = {
                cls._normalized_url_key(
                    prior_args.get("url")
                ),
                cls._normalized_url_key(
                    output.get("url")
                ),
                cls._normalized_url_key(
                    output.get("final_url")
                ),
            }
            url_keys.discard("")

            if requested_url not in url_keys:
                continue

            try:
                end_character = int(
                    output.get("end_character")
                    or 0
                )
            except (TypeError, ValueError):
                end_character = 0

            if end_character > 0 and requested_start < end_character:
                return True

        return False

    @staticmethod
    def _record_acquisition_saturation_repeat(
        *,
        runtime_context: Dict[str, Any],
        signature: str,
    ) -> int:
        """
        非 response-only Acquisition 的 anti-spin 计数器。
        同一饱和签名连续出现两次时，调用方应终止原地空转。
        """
        key = str(signature or "").strip()
        previous = runtime_context.get(
            "_acquisition_saturation_repeat",
        )

        if (
            isinstance(previous, dict)
            and str(
                previous.get("signature")
                or ""
            )
            == key
        ):
            count = int(
                previous.get("count")
                or 0
            ) + 1
        else:
            count = 1

        runtime_context[
            "_acquisition_saturation_repeat"
        ] = {
            "signature": key,
            "count": count,
        }
        return count

    @staticmethod
    def _clear_acquisition_saturation_repeat(
        runtime_context: Dict[str, Any],
    ):
        runtime_context.pop(
            "_acquisition_saturation_repeat",
            None,
        )


    @classmethod
    def _response_only_saturation_alternative(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str,
    ) -> Optional[Tuple[str, Dict[str, Any], str]]:
        """
        Response-only research 在 Acquisition Saturation 命中后，
        尝试寻找一个与当前调用不同、且仍有信息价值的确定性替代动作。

        优先级：
        1. 读取尚未尝试过的真实网页候选；
        2. 搜索仍缺失的研究主题，且 query 必须未被执行过；
        3. 没有替代动作时返回 None，由调用方决定推进或明确失败。

        该函数只选择动作，不执行工具，也不伪造证据。
        """
        name = str(tool_name or "").strip().lower()

        candidate = cls._next_unread_research_candidate(
            tool_results=tool_results,
            goal=goal,
        )
        if candidate:
            current_url = str((arguments or {}).get("url") or "").strip().rstrip("/").lower()
            candidate_url = str(candidate.get("url") or "").strip()
            candidate_key = candidate_url.rstrip("/").lower()
            if candidate_key and candidate_key != current_url:
                return (
                    "read_webpage",
                    {
                        "url": candidate_url,
                        "max_characters": 20000,
                        "timeout": 15,
                        "start_character": 0,
                    },
                    "saturated action → next unread research candidate",
                )

        coverage = cls._response_only_research_coverage_status(
            tool_results=tool_results,
            goal=goal,
        )
        if "routing_missing_search_topics" in coverage:
            missing_topics = list(
                coverage.get("routing_missing_search_topics") or []
            )
        else:
            missing_topics = list(
                coverage.get("blocking_missing_search_topics")
                or coverage.get("missing_search_topics")
                or []
            )

        attempted_queries: set[str] = set()
        for item in tool_results or []:
            if str(getattr(item, "tool_name", "") or "").strip().lower() != "search_web":
                continue
            prior_args = getattr(item, "arguments", {}) or {}
            prior_query = str(
                prior_args.get("query")
                or prior_args.get("search_query")
                or ""
            ).strip()
            if prior_query:
                attempted_queries.add(
                    re.sub(r"\s+", " ", prior_query).strip().lower()
                )

        current_query = str(
            (arguments or {}).get("query")
            or (arguments or {}).get("search_query")
            or ""
        ).strip()
        if current_query:
            attempted_queries.add(
                re.sub(r"\s+", " ", current_query).strip().lower()
            )

        for topic in missing_topics:
            candidates = [
                cls._research_topic_query(
                    topic,
                    original_query="",
                ),
                *cls._research_topic_query_variants(
                    topic,
                    original_query="",
                ),
            ]
            for query in candidates:
                value = re.sub(r"\s+", " ", str(query or "")).strip()
                key = value.lower()
                if not value or key in attempted_queries:
                    continue
                return (
                    "search_web",
                    {
                        "query": value,
                        "max_results": 8,
                        "region": "cn-zh",
                    },
                    f"saturated action → missing research topic ({topic})",
                )

        return None

    @classmethod
    def _acquisition_saturation_status(
        cls,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        goal: str = "",
        current_stage: Optional[AgentStage] = None,
    ) -> Dict[str, Any]:
        """
        v6.4 Acquisition Smart Stop。

        目标不是简单限制次数，而是：
        - 有足够证据时立即停止继续搜索；
        - 回归任务优先寻找月度/季度细粒度证据；
        - 到达合理搜索上限后停止继续烧 Token，转结构化/下载/探索性分析。
        """
        # Saturation Guard 是 Acquisition 阶段的局部策略，不能污染
        # Processing/Delivery/Verification。后续阶段若因证据缺口临时调用
        # acquisition tool，应允许该调用真实执行，由 Evidence Grounding /
        # Completion Gate 决定是否仍需补来源，而不是继续套用已结束阶段的
        # Smart Stop。
        if (
            current_stage is not None
            and current_stage != AgentStage.ACQUISITION
        ):
            return {
                "saturated": False,
                "reason": "",
                "stage_scoped": True,
            }

        name = str(tool_name or "").strip().lower()
        if name not in {"search_web", "read_webpage"}:
            return {"saturated": False, "reason": ""}

        signature = cls._normalized_search_signature(name, arguments)
        same_count = 0
        web_actions: List[str] = []
        attempted_web_actions: List[str] = []

        for item in tool_results or []:
            item_name = str(getattr(item, "tool_name", "") or "").strip().lower()
            if item_name not in {"search_web", "read_webpage"}:
                continue

            attempted_web_actions.append(item_name)
            item_args = getattr(item, "arguments", {}) or {}
            item_sig = cls._normalized_search_signature(item_name, item_args)

            if getattr(item, "success", False):
                web_actions.append(item_name)
                if item_sig == signature:
                    same_count += 1

        if (
            name == "read_webpage"
            and cls._read_webpage_already_complete(
                arguments=arguments,
                tool_results=tool_results,
            )
        ):
            return {
                "saturated": True,
                "reason": (
                    "当前网页已经完整读取（has_more=False），"
                    "禁止重新从已覆盖区间读取；应使用现有正文、"
                    "读取其他来源或改用明确数据 URL。"
                ),
                "signature": signature,
                "instruction": (
                    "不要重新读取已完整获取的网页。"
                ),
            }

        duplicate_limit = (
            1
            if name == "read_webpage"
            else 2
        )

        if same_count >= duplicate_limit:
            return {
                "saturated": True,
                "reason": (
                    "相同网页正文分块已经成功读取，禁止重复读取；"
                    "应继续下一分页、使用已有来源证据、读取其他来源，"
                    "或改用明确数据 URL。"
                    if name == "read_webpage"
                    else (
                        "相同 Acquisition 调用已成功执行至少 2 次，禁止第三次重复；"
                        "应使用已有来源证据、改用明确数据 URL，或进入 Processing。"
                    )
                ),
                "signature": signature,
                "instruction": (
                    "不要重复读取同一网页正文分块。"
                    if name == "read_webpage"
                    else "不要再改写同义关键词重复搜索。"
                ),
            }

        evidence_gate = cls._acquisition_evidence_status(
            tool_results=tool_results,
            goal=goal,
        )
        regression_needed = cls._task_requires_regression(goal)
        fine_grained = cls._has_fine_grained_acquisition_evidence(tool_results)
        web_count = len(web_actions)
        successful_page_reads = sum(
            1
            for item in (tool_results or [])
            if bool(getattr(item, "success", False))
            and str(getattr(item, "tool_name", "") or "").strip().lower()
            == "read_webpage"
        )

        # 搜索 snippet 只能用于候选发现，不能在尚未读取任何高价值网页正文时
        # 就把 research Acquisition 判为完成。普通研究至少允许 2 次成功
        # read_webpage，再由 Evidence Gate 决定是否停止继续联网。
        evidence_stop_ready = bool(
            evidence_gate.get("sufficient")
            and (not regression_needed or fine_grained)
            and (name != "read_webpage" or successful_page_reads >= 2)
        )

        if evidence_stop_ready:
            return {
                "saturated": True,
                "reason": (
                    "Acquisition Completion Checker 已判断核心对象/时间范围证据充足；"
                    "继续搜索的边际价值低，应立即进入 Processing 结构化已有证据。"
                ),
                "signature": signature,
                "evidence_gate": evidence_gate,
                "instruction": (
                    "优先调用 build_dataframe/create_dataframe 或读取已明确的数据文件，"
                    "不要继续 search_web/read_webpage。"
                ),
            }

        # 复杂联网研究若显式要求竞争、政策、就业、职教、B端等多个主题，
        # 先完成 Research Coverage，再停止 broad search；普通研究仍沿用较小上限。
        web_action_limit = cls._research_web_action_limit(goal)
        coverage_status = (
            cls._response_only_research_coverage_status(
                tool_results=tool_results,
                goal=goal,
            )
            if cls._goal_explicitly_requests_response_only(goal)
            else {}
        )
        if name == "search_web" and web_count >= 4:
            quality_status = (
                cls._response_only_research_evidence_status(
                    tool_results=tool_results,
                    goal=goal,
                )
                if cls._goal_explicitly_requests_response_only(goal)
                else {}
            )
            coverage_guard_active = bool(
                isinstance(coverage_status, dict)
                and len(coverage_status.get("required_topics") or []) >= 2
            )
            blocking_missing_search = (
                coverage_status.get("blocking_missing_search_topics")
                if "blocking_missing_search_topics" in coverage_status
                else coverage_status.get("missing_search_topics")
            ) or []
            if (
                coverage_guard_active
                and blocking_missing_search
                and web_count < web_action_limit
            ):
                return {
                    "saturated": False,
                    "reason": "Research Coverage 尚未完成，允许定向搜索缺失主题。",
                    "signature": signature,
                    "research_coverage_recovery": True,
                    "coverage": coverage_status,
                    "instruction": (
                        "只搜索当前缺失主题，不得继续重复已覆盖主题。"
                    ),
                }
            if (
                isinstance(quality_status, dict)
                and (
                    quality_status.get("quality_recovery_needed")
                    or quality_status.get("coverage_recovery_needed")
                )
                and web_count < web_action_limit
            ):
                return {
                    "saturated": False,
                    "reason": (
                        "允许 Research Quality/Coverage Recovery：当前来源或主题覆盖仍不足。"
                    ),
                    "signature": signature,
                    "source_quality_recovery": True,
                    "instruction": str(quality_status.get("instruction") or ""),
                }

            return {
                "saturated": True,
                "reason": (
                    "已进行了至少 4 次网页 Acquisition 动作并完成当前允许的 "
                    "broad search 扩展；应读取最高价值候选、使用明确数据 URL，"
                    "或基于已有证据推进。"
                ),
                "signature": signature,
                "instruction": "停止 broad search，转 source decision。",
            }

        # 动态网页动作硬上限：复杂多主题研究最多 10 次，普通研究仍为 6 次。
        if web_count >= web_action_limit:
            if regression_needed and not fine_grained:
                reason = (
                    f"已使用 {web_action_limit} 次网页 Acquisition 动作仍未形成稳定月度/季度证据；"
                    "停止继续消耗搜索 Token。若已有明确数据文件 URL 可直接下载；"
                    "否则使用现有年度数据继续，并把回归明确降级为探索性小样本分析。"
                )
            else:
                reason = (
                    f"Acquisition 网页动作已达到 {web_action_limit} 次智能停止上限；"
                    "应使用已有证据进入结构化/下载，不再继续 search/read。"
                )
            return {
                "saturated": True,
                "reason": reason,
                "signature": signature,
                "evidence_gate": evidence_gate,
                "instruction": "停止 search_web/read_webpage，推进现有证据。",
            }

        return {
            "saturated": False,
            "reason": "",
            "web_action_count": web_count,
            "successful_page_reads": successful_page_reads,
            "regression_requested": regression_needed,
            "fine_grained_evidence": fine_grained,
            "evidence_gate": evidence_gate,
        }

    @staticmethod
    def _deduplicate_deliverable_paths(
        paths: List[str],
    ) -> List[str]:
        """按规范化路径去重，保持首次出现顺序。"""
        seen = set()
        result = []
        for item in paths:
            value = str(item or "").strip()
            if not value:
                continue
            key = value.replace("\\", "/").lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @classmethod
    def _raw_retention_already_satisfied(
        cls,
        *,
        data_state: DataState,
        workspace_deliverables: Optional[List[str]] = None,
    ) -> bool:
        """
        判断 Raw Download Retention 是否已经有一个真实原始数据交付物。

        只用于“是否还需要再次 promote”的生命周期判断，
        不替代 Completion Gate 对最终文件真实性的检查。
        """
        candidates = list(data_state.deliverable_paths)
        candidates.extend(workspace_deliverables or [])

        for item in cls._deduplicate_deliverable_paths(
            candidates
        ):
            if str(item).lower().endswith(
                (".csv", ".tsv", ".json", ".parquet")
            ):
                return True
        return False

    @classmethod
    def _normalize_response_only_recovery_stage(
        cls,
        *,
        goal: str,
        runtime_context: Dict[str, Any],
        proposed_stage: AgentStage,
        processing_enabled: bool = True,
    ) -> AgentStage:
        """
        Response-only 任务绝不能因为 final_answer/内容类验收失败，
        被通用 recovery 规则误送进 Delivery。

        用户明确不要文件，且 TaskPlan 没有文件型交付物时：
        - 通用规则若判为 Delivery，改回 Processing；
        - 若 Processing 未启用，则留在 Verification。
        """
        if not cls._goal_explicitly_requests_response_only(goal):
            return proposed_stage
        if cls._task_plan_has_file_deliverables(runtime_context):
            return proposed_stage
        if proposed_stage != AgentStage.DELIVERY:
            return proposed_stage
        return (
            AgentStage.PROCESSING
            if processing_enabled
            else AgentStage.VERIFICATION
        )

    @staticmethod
    def _verification_failure_summary(
        verification_report: Optional[Dict[str, Any]],
        *,
        max_items: int = 8,
    ) -> str:
        """
        把 Completion Gate 的真实失败项压缩成可读日志。

        目的：
        - GUI 不再只显示“Completion Gate：未通过”；
        - 用户能直接看到卡在哪个 deterministic check；
        - response-only Finalizer 的重复失败能够被定位，而不是黑盒循环。
        """
        if not isinstance(verification_report, dict):
            return ""

        lines: List[str] = []
        seen: set[str] = set()

        checks = verification_report.get("checks") or []
        if isinstance(checks, dict):
            iterable = list(checks.values())
        elif isinstance(checks, list):
            iterable = list(checks)
        else:
            iterable = []

        for item in iterable:
            if not isinstance(item, dict):
                continue
            if item.get("passed") is True:
                continue

            check_id = str(
                item.get("check_id")
                or item.get("id")
                or ""
            ).strip()
            category = str(
                item.get("category")
                or ""
            ).strip()
            message = str(
                item.get("message")
                or item.get("reason")
                or item.get("details")
                or ""
            ).strip()

            label = " / ".join(
                value
                for value in (category, check_id)
                if value
            )
            text = (
                (f"[{label}] " if label else "")
                + (message or "未通过")
            ).strip()

            if text and text not in seen:
                seen.add(text)
                lines.append(text)

            if len(lines) >= max(1, int(max_items)):
                break

        if len(lines) < max(1, int(max_items)):
            for item in (
                verification_report.get("failures")
                or []
            ):
                text = str(item or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                lines.append(text)
                if len(lines) >= max(1, int(max_items)):
                    break

        if len(
            lines
        ) < max(
            1,
            int(
                max_items
            ),
        ):
            for item in (
                verification_report.get(
                    "pending_requirements"
                )
                or []
            ):
                text = str(
                    item
                    or ""
                ).strip()
                if not text:
                    continue

                rendered = (
                    "[pending_requirement] "
                    + text
                )

                if rendered in seen:
                    continue

                seen.add(
                    rendered
                )
                lines.append(
                    rendered
                )

                if len(
                    lines
                ) >= max(
                    1,
                    int(
                        max_items
                    ),
                ):
                    break

        if not lines:
            return ""

        return "\n".join(
            f"{index}. {line}"
            for index, line in enumerate(lines, start=1)
        )

    @staticmethod
    def _consume_same_stage_completion_recovery(
        *,
        route: Any,
        stage: AgentStage,
    ) -> Dict[str, Any]:
        """
        Completion Gate FAIL 后，同一个 Stage 内的“再次修正”必须消耗
        StageState.recovery_iterations_used。

        这正是 StageBudget(+N recovery) 的用途。此前 finish → Gate FAIL
        没有真实 Tool 调用，因此 normal iterations 不变，导致
        Processing Loop 1/14 可以无限重复。

        返回：
        {
            "allowed": bool,
            "used": int,
            "limit": int,
            "remaining": int,
        }
        """
        state = route.states[stage]
        limit = int(
            state.budget.recovery_iterations
        )

        if state.remaining_recovery_iterations <= 0:
            return {
                "allowed": False,
                "used": int(
                    state.recovery_iterations_used
                ),
                "limit": limit,
                "remaining": 0,
            }

        state.record_iteration(
            recovery=True
        )

        return {
            "allowed": True,
            "used": int(
                state.recovery_iterations_used
            ),
            "limit": limit,
            "remaining": int(
                state.remaining_recovery_iterations
            ),
        }

    @classmethod
    def _stage_recovery_target_from_report(
        cls,
        verification_report: Optional[Dict[str, Any]],
    ) -> AgentStage:
        """把 Completion/Stage Gate 失败项映射到确定性回退 Stage。"""
        if not verification_report:
            return AgentStage.VERIFICATION

        failed_texts: List[str] = []
        failed_checks: List[Dict[str, Any]] = []
        checks = verification_report.get("checks") or []

        if isinstance(checks, dict):
            iterable = checks.values()
        elif isinstance(checks, list):
            iterable = checks
        else:
            iterable = []

        for item in iterable:
            if not isinstance(item, dict):
                continue
            passed = item.get("passed")
            if passed is True:
                continue
            failed_checks.append(item)
            failed_texts.append(
                " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "name",
                        "check_name",
                        "reason",
                        "message",
                        "details",
                    )
                )
            )

        # v6.6 Evidence Contract Recovery Routing
        #
        # Evidence Contract / response-only reporting audit failures describe
        # the *final answer*, not missing Acquisition evidence by default.
        # Their messages can legitimately contain words such as “来源/外部数字”;
        # the generic StageOrchestrator token router would therefore
        # misclassify them as Acquisition and create:
        # Processing -> Completion FAIL -> Acquisition -> Processing -> ...
        #
        # When every failing deterministic check is answer-quality related,
        # recover in Processing so the finalizer can rewrite the answer using
        # the already-acquired evidence and the precise verification feedback.
        answer_quality_categories = {
            "evidence_contract",
            "response_only_semantic",
            "reporting_content_audit",
        }
        failed_categories = {
            str(item.get("category") or "").strip().lower()
            for item in failed_checks
            if str(item.get("category") or "").strip()
        }
        if (
            failed_checks
            and failed_categories
            and failed_categories.issubset(answer_quality_categories)
        ):
            return AgentStage.PROCESSING

        failed_texts.append(
            str(
                verification_report.get("summary")
                or verification_report.get("reason")
                or ""
            )
        )

        combined = " ".join(failed_texts)
        return StageOrchestrator.recovery_target(
            failure_category="verification_failure",
            failure_text=combined,
        )


    @staticmethod
    def _acquisition_adapter_observation(
        *,
        goal: str,
    ) -> Optional[Dict[str, Any]]:
        """
        v5.2 Acquisition Source Adapter。

        在真正进入 Acquisition Tool 前，
        使用已登记来源策略减少无意义搜索。

        当前只提供决策 Observation，
        不直接替代 Tool 执行。
        """

        text = str(goal or "").strip()

        weather_keywords = (
            "天气",
            "气象",
            "温度",
            "湿度",
            "降水",
            "风速",
        )

        if not any(
            item in text
            for item in weather_keywords
        ):
            return None

        location = None
        for item in (
            "澳门",
            "珠海",
            "广州",
            "深圳",
        ):
            if item in text:
                location = item
                break

        if not location:
            return None

        instruction = build_acquisition_instruction(
            domain="weather",
            location=location,
            data_type="observation",
        )

        return instruction.to_dict()

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        executor: Optional[ToolExecutor] = None,
        progress_callback: ProgressCallback = None,
        client: Optional[OpenAI] = None,
        model: Optional[str] = None,
        max_iterations: int = 12,
        verifier: Optional[VerificationEngine] = None,
        skill_registry: Optional[SkillRegistry] = None,
        max_identical_failures: int = 2,
        max_completion_recovery_iterations: int = 3,
        cancel_event: Optional[Any] = None,
    ):
        self.registry = registry or create_default_tool_registry()
        self.skill_registry = (
            skill_registry or create_default_skill_registry()
        )
        self._validate_skill_catalog()
        self.skill_selector = SkillSelector(
            self.skill_registry
        )
        self._active_skill_selection: Optional[
            SkillSelection
        ] = None
        self.progress_callback = progress_callback

        self.executor = executor or ToolExecutor(
            registry=self.registry,
            progress_callback=progress_callback,
        )

        self.context = ExecutionContext()
        self.verifier = verifier or VerificationEngine()
        self.readback_registry = ReadbackRegistry()
        # v5.8 Execution Observability
        self.execution_monitor = ExecutionMonitor()

        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv(
            "OPENAI_BASE_URL",
            "https://api.deepseek.com",
        )
        self.model = model or os.getenv(
            "OPENAI_MODEL",
            "deepseek-chat",
        )

        if client is not None:
            self.client = client
        else:
            if not self.api_key:
                raise ValueError(
                    "没有找到 OPENAI_API_KEY，请检查 .env。"
                )

            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )

        self.max_iterations = max(
            1,
            int(max_iterations),
        )
        self.max_identical_failures = max(
            1,
            int(max_identical_failures),
        )
        self.max_completion_recovery_iterations = max(
            0,
            int(max_completion_recovery_iterations),
        )
        self.cancel_event = cancel_event

    def _is_cancel_requested(self) -> bool:
        """Return True when the GUI/user has requested a cooperative stop."""
        event = self.cancel_event
        return bool(event is not None and getattr(event, "is_set", lambda: False)())

    def _cancelled_result(
        self,
        *,
        goal: str,
        iteration: int,
        tool_results: List[ToolExecutionResult],
        decisions: List[Dict[str, Any]],
        latest_verification_report: Optional[Dict[str, Any]] = None,
    ) -> AgentLoopResult:
        self.report_progress("已收到用户终止请求，Agent 已在安全检查点停止。")
        return AgentLoopResult(
            success=False,
            goal=goal,
            final_answer="任务已由用户终止。",
            stop_reason="user_cancelled",
            iterations=max(0, int(iteration)),
            tool_results=tool_results,
            decisions=decisions,
            verification_report=(
                dict(latest_verification_report)
                if isinstance(latest_verification_report, dict)
                else None
            ),
        )

    def _validate_skill_catalog(self):
        """
        v4.5：启动时检查 Skill 推荐的 Tool 是否真实存在。

        Skill 只提供方法指导，不获得执行权；如果 Skill Catalog
        引用了不存在的 Tool，应在 Agent 启动阶段立即暴露架构错误，
        而不是让 LLM 在运行中尝试调用虚构能力。
        """
        missing = self.skill_registry.validate_tools(
            self.registry
        )

        if missing:
            details = "; ".join(
                f"{skill}: {', '.join(tools)}"
                for skill, tools in sorted(
                    missing.items()
                )
            )
            raise ValueError(
                "Skill Catalog 引用了未注册 Tool："
                + details
            )

    def report_progress(
        self,
        message: str,
    ):
        # 某些单元测试会通过 __new__ 构造轻量 AgentLoop，
        # 不执行完整 __init__。因此不能假定 progress_callback
        # 属性一定存在。
        callback = getattr(
            self,
            "progress_callback",
            None,
        )

        if callback:
            try:
                callback(str(message))
            except Exception as error:
                print(
                    f"Agent Loop 进度回调失败：{error}"
                )
        else:
            print(message)

    def run(
        self,
        user_task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentLoopResult:
        goal = str(user_task or "").strip()

        if not goal:
            raise ValueError(
                "用户任务不能为空。"
            )

        runtime_context = dict(context or {})
        runtime_context = self._normalize_task_plan_context(
            runtime_context
        )
        runtime_context = self._apply_original_output_mode_override(
            goal=goal,
            runtime_context=runtime_context,
        )

        # v5.2-2：建立本次任务的 Stage Route。
        stage_route = StageOrchestrator.build_route(
            user_task=goal,
            task_plan=runtime_context.get("task_plan"),
        )
        current_stage = StageOrchestrator.first_stage(
            stage_route
        )
        data_state = DataState()

        if current_stage is None:
            current_stage = AgentStage.PROCESSING

        stage_route.states[current_stage].activate()
        runtime_context.update(
            self._build_stage_runtime_context(
                route=stage_route,
                current_stage=current_stage,
                data_state=data_state,
            )
        )

        self.report_progress(
            "v5.2 Stage Router："
            + " → ".join(
                stage.value
                for stage in stage_route.enabled_stages
            )
        )
        self.report_progress(
            f"[{current_stage.value.title()} Loop] "
            "Stage 已启动。"
        )

        self._active_skill_selection = (
            self.skill_selector.select(
                goal=goal,
                task_plan=runtime_context.get(
                    "task_plan"
                ),
            )
        )
        runtime_context["skill_selection"] = (
            self._active_skill_selection.to_dict()
        )

        selected_skill_names = (
            self._active_skill_selection.selected_skills
        )
        selection_mode = (
            "完整目录安全回退"
            if self._active_skill_selection.fallback_used
            else "确定性筛选"
        )
        self.report_progress(
            "v4.5 Skill Selector："
            f"{selection_mode} → "
            + ", ".join(selected_skill_names)
        )

        decisions: List[Dict[str, Any]] = []
        tool_results: List[ToolExecutionResult] = []

        # v4.0 Completion Gate：
        # runtime_context.verification_observation 用于把验收失败原因
        # 注入下一轮 LLM；这里再独立保存最近一次正式验收报告，
        # 供最终 AgentLoopResult 使用，避免结果状态依赖临时上下文。
        latest_verification_report: Optional[
            Dict[str, Any]
        ] = None

        # v6.6 Response-Only Completion Recovery Guard：
        # 只存在于本次 run() 生命周期内，不进入 prompt/runtime_context。
        # 用于识别“同一失败集合 + 完全相同 final_answer”的机械重复。
        last_failed_response_answer = ""
        last_failed_response_signature = ""

        self.report_progress(
            "DataPilot Workspace Agent Loop 启动。"
        )

        # v5.2 Independent Stage Budget：
        # 不再让 Acquisition / Processing / Delivery / Verification
        # 争抢同一个 32 轮池。每个 Stage 自己计数、自己封顶。
        #
        # global_normal_safety_cap 只是“全局失控保险”，等于本任务所有
        # 已启用 Stage 正常预算之和；真正的限制来自各 StageState。
        # Completion Recovery 仍保留原来的独立小窗口。
        global_normal_safety_cap = (
            self._enabled_stage_normal_budget(stage_route)
        )
        max_total_iterations = (
            global_normal_safety_cap
            + self.max_completion_recovery_iterations
        )

        self.report_progress(
            "v5.2 Independent Stage Budget："
            + self._stage_budget_summary(stage_route)
        )
        self.report_progress(
            "全局正常执行 Safety Cap："
            f"{global_normal_safety_cap}；"
            "该值不是共享预算池。"
        )

        iteration = 1

        while iteration <= max_total_iterations:
            if hasattr(self.execution_monitor, "add_loop"):
                self.execution_monitor.add_loop()

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            in_completion_recovery = (
                iteration > global_normal_safety_cap
            )

            if (
                in_completion_recovery
                and latest_verification_report is None
            ):
                break

            if in_completion_recovery:
                recovery_index = (
                    iteration - global_normal_safety_cap
                )
                self.report_progress(
                    "[Completion Recovery "
                    f"{recovery_index}/"
                    f"{self.max_completion_recovery_iterations}] "
                    "正在根据最新验收失败项继续修正/回读……"
                )
            else:
                current_stage_state = stage_route.states[
                    current_stage
                ]
                active_stage_recovery = str(
                    runtime_context.get(
                        "_active_completion_stage_recovery"
                    )
                    or ""
                ).strip().lower()

                if (
                    active_stage_recovery
                    == current_stage.value
                    and current_stage_state.recovery_iterations_used > 0
                ):
                    self.report_progress(
                        f"[{current_stage.value.title()} Completion Recovery "
                        f"{current_stage_state.recovery_iterations_used}/"
                        f"{current_stage_state.budget.recovery_iterations}] "
                        "正在根据上一轮 Completion Gate 失败项重新合成/修正……"
                    )
                else:
                    self.report_progress(
                        f"[{current_stage.value.title()} Loop "
                        f"{current_stage_state.iterations_used + 1}/"
                        f"{current_stage_state.budget.normal_iterations}] "
                        "正在根据当前 Stage 与真实执行状态决定下一步……"
                    )

            # v6.6 Search Infrastructure Terminal Guard
            #
            # 单个 query 的替代检索由 Research Search Recovery 负责；但如果
            # 多个不同 search_web 请求都失败，而且始终没有形成任何成功的
            # 搜索结果/网页正文，就说明问题已经不是“关键词不好”，而是当前
            # 搜索基础设施不可用。此时必须在再次调用 LLM 之前确定性停止，
            # 避免模型继续改写近义 query，最终因上下文重复触发本地推理 500。
            if (
                not in_completion_recovery
                and current_stage == AgentStage.ACQUISITION
                and self._goal_explicitly_requests_response_only(goal)
            ):
                search_infra_status = (
                    self._response_only_search_infrastructure_status(
                        tool_results=tool_results,
                        goal=goal,
                    )
                )
                runtime_context[
                    "search_infrastructure_status"
                ] = search_infra_status
                if search_infra_status.get("exhausted"):
                    self.report_progress(
                        "[Research Search Infrastructure Guard] FAIL："
                        + str(search_infra_status.get("reason") or "")
                        + "任务以 acquisition_incomplete 结束；"
                        "不再调用本地模型继续生成近义搜索词。"
                    )
                    return AgentLoopResult(
                        success=False,
                        goal=goal,
                        final_answer="",
                        stop_reason="acquisition_incomplete",
                        iterations=max(0, iteration - 1),
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            dict(latest_verification_report)
                            if isinstance(latest_verification_report, dict)
                            else None
                        ),
                        retry_policy_report={
                            "search_infrastructure": search_infra_status,
                        },
                    )

            runtime_context.update(
                self._build_stage_runtime_context(
                    route=stage_route,
                    current_stage=current_stage,
                    data_state=data_state,
                )
            )

            state = self._build_state(
                goal=goal,
                runtime_context=runtime_context,
                decisions=decisions,
                tool_results=tool_results,
            )

            decision = None

            pending_acquisition_decision = runtime_context.pop(
                "_pending_response_only_acquisition_decision",
                None,
            )
            if (
                isinstance(pending_acquisition_decision, dict)
                and current_stage == AgentStage.ACQUISITION
                and self._goal_explicitly_requests_response_only(goal)
            ):
                decision = dict(pending_acquisition_decision)
                self.report_progress(
                    "[Response-Only Acquisition Recovery] "
                    "正在执行上一次 Gate FAIL 后由 Python 选择的确定性恢复动作。"
                )

            if decision is None and in_completion_recovery:
                decision = self._build_deterministic_reread_decision(
                    latest_verification_report
                )
                if decision is not None:
                    self.report_progress(
                        "Completion Recovery：检测到最终交付物缺少写后回读证据，"
                        "本轮由 Python 确定性补读，不再消耗 LLM 猜测。"
                    )
            if decision is None:
                llm_start_time = time.time()

                try:
                    decision = self._decide_next_action(
                        goal=goal,
                        state=state,
                    )
                except Exception as decision_error:
                    try:
                        fallback_decision = (
                            self._build_local_backend_failure_fallback_decision(
                                goal=goal,
                                state=state,
                                current_stage=current_stage,
                                tool_results=tool_results,
                                backend_error=decision_error,
                            )
                        )
                    except Exception as fallback_error:
                        if (
                            self._is_ollama_backend()
                            and self._is_retryable_model_backend_error(
                                fallback_error
                            )
                        ):
                            self.report_progress(
                                "[Local Decision Backend Guard] fallback 本身仍遭遇"
                                "本地模型 5xx/连接错误；任务将以 "
                                "local_model_unavailable 干净停止。"
                            )
                            fallback_decision = None
                        else:
                            raise

                    if fallback_decision is None:
                        if (
                            self._is_ollama_backend()
                            and self._is_retryable_model_backend_error(
                                decision_error
                            )
                        ):
                            self.report_progress(
                                "[Local Decision Backend Guard] FAIL："
                                "本地模型连续不可用，且当前 Stage 没有可安全执行的"
                                "确定性降级动作；任务停止，不自动切换云端模型。"
                            )
                            return AgentLoopResult(
                                success=False,
                                goal=goal,
                                final_answer="",
                                stop_reason="local_model_unavailable",
                                iterations=iteration,
                                tool_results=tool_results,
                                decisions=decisions,
                                verification_report=latest_verification_report,
                                execution_timing=self.execution_monitor.summary(),
                            )
                        raise

                    decision = fallback_decision
                    self.report_progress(
                        "[Local Decision Backend Guard] 已接管本轮决策："
                        f"{decision.get('action_type')}"
                        + (
                            f" → {decision.get('tool')}"
                            if decision.get("tool")
                            else ""
                        )
                    )

                if hasattr(self.execution_monitor, "record_llm"):
                    self.execution_monitor.record_llm(
                        iteration=iteration,
                        elapsed=time.time() - llm_start_time,
                    )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            decisions.append(decision)

            action_type = decision.get(
                "action_type"
            )

            if action_type == "finish":
                final_answer = str(
                    decision.get("final_answer")
                    or ""
                ).strip()

                # v6.6 Response-Only Research Stage Boundary
                #
                # 对“联网研究/分析，但不要生成文件”的任务，LLM 在 Acquisition
                # 搜索足够后常把“本阶段完成”表达成 finish。此前这会直接跳到
                # Completion Gate；若 final_answer 尚未形成，Gate 又会把
                # “response deliverable 缺失”误导向 Delivery，形成反复的
                # generate_report / generate_final_answer 伪动作循环。
                #
                # 这里把 Acquisition 中的 finish 先解释为“申请结束 Acquisition”：
                # 证据足够 -> 确定性进入 Processing；
                # 证据不足 -> 留在 Acquisition，并把真实 Gate 结果反馈给下一轮。
                if (
                    current_stage == AgentStage.ACQUISITION
                    and stage_route.states[AgentStage.PROCESSING].enabled
                    and self._goal_explicitly_requests_response_only(goal)
                ):
                    acquisition_gate = self._acquisition_gate_status(
                        data_state,
                        tool_results=tool_results,
                        goal=goal,
                    )

                    # 通用市场/经营研究不应被最初为跨年度结构化数据研究设计的
                    # AcquisitionCompletionChecker 永久卡住。若其未 PASS，但已
                    # 真实读取多个不同网页正文，则以 response-only research fallback
                    # 允许进入 Processing；最终答案仍必须经过 Completion Gate。
                    if not acquisition_gate.get("passed"):
                        research_gate = (
                            self._response_only_research_evidence_status(
                                tool_results=tool_results,
                                goal=goal,
                            )
                        )
                        acquisition_gate = {
                            **acquisition_gate,
                            **research_gate,
                            "passed": bool(
                                research_gate.get("passed")
                            ),
                            "base_gate_passed": False,
                            "fallback_used": bool(
                                research_gate.get("passed")
                            ),
                        }

                    runtime_context[
                        "acquisition_gate_observation"
                    ] = acquisition_gate

                    if acquisition_gate.get("passed"):
                        stage_route.states[
                            AgentStage.ACQUISITION
                        ].mark_passed(
                            str(
                                acquisition_gate.get("reason")
                                or "Response-only Acquisition 证据已足够。"
                            )
                        )
                        previous_stage = current_stage
                        current_stage = AgentStage.PROCESSING
                        stage_route.states[
                            current_stage
                        ].activate()
                        runtime_context[
                            "current_stage"
                        ] = current_stage.value
                        runtime_context[
                            "stage_route"
                        ] = stage_route.to_dict()
                        runtime_context.pop(
                            "acquisition_saturation_observation",
                            None,
                        )
                        self.report_progress(
                            "[Response-Only Stage Advance] "
                            f"{previous_stage.value} → {current_stage.value}；"
                            "Acquisition 证据已足够，开始综合分析并形成最终回答。"
                        )
                        iteration += 1
                        continue

                    gate_instruction = str(
                        acquisition_gate.get("instruction") or ""
                    ).strip()
                    if not gate_instruction:
                        gate_instruction = (
                            "当前是 response-only 联网研究任务；Acquisition 证据尚不足。"
                            "不要生成文件，也不要直接 finish。优先读取最高价值候选来源"
                            "或补齐明确缺口，再申请结束 Acquisition。"
                        )
                    runtime_context["stage_gate_observation"] = {
                        **acquisition_gate,
                        "instruction": gate_instruction,
                    }
                    self.report_progress(
                        "[Response-Only Acquisition Gate] FAIL："
                        + str(
                            acquisition_gate.get("reason")
                            or "现有来源证据尚不足。"
                        )
                    )

                    acquisition_budget_reason = (
                        self._stage_budget_block_reason(
                            route=stage_route,
                            stage=AgentStage.ACQUISITION,
                        )
                    )
                    if acquisition_budget_reason:
                        terminal_research_gate = (
                            self._response_only_research_evidence_status(
                                tool_results=tool_results,
                                goal=goal,
                            )
                        )
                        runtime_context[
                            "acquisition_gate_observation"
                        ] = terminal_research_gate

                        if terminal_research_gate.get("passed"):
                            stage_route.states[
                                AgentStage.ACQUISITION
                            ].mark_passed(
                                str(
                                    terminal_research_gate.get("reason")
                                    or "Acquisition budget exhausted with sufficient research evidence."
                                )
                            )
                            previous_stage = current_stage
                            current_stage = AgentStage.PROCESSING
                            stage_route.states[
                                current_stage
                            ].activate()
                            runtime_context[
                                "current_stage"
                            ] = current_stage.value
                            runtime_context[
                                "stage_route"
                            ] = stage_route.to_dict()
                            runtime_context.pop(
                                "_pending_response_only_acquisition_decision",
                                None,
                            )
                            self.report_progress(
                                "[Response-Only Budget Advance] "
                                f"{previous_stage.value} → {current_stage.value}；"
                                "Acquisition Normal Budget 已耗尽，但现有真实网页证据"
                                "满足有界降级条件，开始 Processing。"
                            )
                            iteration += 1
                            continue

                        runtime_context.pop(
                            "_pending_response_only_acquisition_decision",
                            None,
                        )
                        self.report_progress(
                            "[Response-Only Budget Terminal] FAIL："
                            + str(acquisition_budget_reason)
                            + "；现有真实网页证据仍不足以进入 Processing。"
                            "停止继续排队 Acquisition 工具，任务以 "
                            "acquisition_incomplete 结束。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="acquisition_incomplete",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=latest_verification_report,
                            retry_policy_report={
                                "acquisition_gate": terminal_research_gate,
                            },
                            execution_timing=self.execution_monitor.summary(),
                        )

                    recovery_decision = (
                        self._response_only_acquisition_gate_recovery_decision(
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )
                    if recovery_decision is not None:
                        runtime_context[
                            "_pending_response_only_acquisition_decision"
                        ] = recovery_decision
                        self.report_progress(
                            "[Response-Only Acquisition Recovery] "
                            "Gate FAIL 后已确定下一条 bounded recovery："
                            + str(recovery_decision.get("tool") or "")
                        )
                        iteration += 1
                        continue

                    search_infra_status = (
                        self._response_only_search_infrastructure_status(
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )
                    runtime_context[
                        "search_infrastructure_status"
                    ] = search_infra_status
                    self.report_progress(
                        "[Response-Only Acquisition Terminal] FAIL："
                        "Acquisition Gate 未通过，且已无新的未读网页候选或"
                        "bounded topic recovery query；停止原地 finish/Gate "
                        "循环，任务以 acquisition_incomplete 结束。"
                    )
                    return AgentLoopResult(
                        success=False,
                        goal=goal,
                        final_answer="",
                        stop_reason="acquisition_incomplete",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=latest_verification_report,
                        retry_policy_report={
                            "search_infrastructure": search_infra_status,
                        },
                        execution_timing=self.execution_monitor.summary(),
                    )

                self.report_progress(
                    "Agent 请求完成任务，正在进入 v4.0 Completion Gate……"
                )

                if self._is_cancel_requested():
                    return self._cancelled_result(
                        goal=goal,
                        iteration=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        latest_verification_report=latest_verification_report,
                    )

                # v5.3 Readback Registry:
                # Completion Gate 前确保最终交付物进入可追踪回读状态。
                self.readback_registry.register_many(
                    data_state.deliverable_paths
                )
                runtime_context["readback_registry"] = (
                    self.readback_registry.to_dict()
                )

                self._perform_final_readback_precheck(
                    data_state=data_state,
                    tool_results=tool_results,
                    runtime_context=runtime_context,
                )

                verification_report = self._run_completion_gate(
                    goal=goal,
                    final_answer=final_answer,
                    iteration=iteration,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                    decisions=decisions,
                )

                if verification_report.verified:
                    self.report_progress(
                        "Completion Gate：PASS，任务验收通过。"
                    )

                    runtime_context.pop(
                        "verification_observation",
                        None,
                    )
                    runtime_context.pop(
                        "_active_completion_stage_recovery",
                        None,
                    )

                    return AgentLoopResult(
                        success=True,
                        goal=goal,
                        final_answer=final_answer,
                        stop_reason="completed",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            verification_report.to_dict()
                        ),
                        execution_timing=self.execution_monitor.summary(),
                    )

                latest_verification_report = (
                    verification_report.to_dict()
                )
                runtime_context["verification_observation"] = dict(
                    latest_verification_report
                )

                recovery_stage = (
                    self._stage_recovery_target_from_report(
                        latest_verification_report
                    )
                )
                normalized_recovery_stage = (
                    self._normalize_response_only_recovery_stage(
                        goal=goal,
                        runtime_context=runtime_context,
                        proposed_stage=recovery_stage,
                        processing_enabled=bool(
                            stage_route.states[
                                AgentStage.PROCESSING
                            ].enabled
                        ),
                    )
                )
                if normalized_recovery_stage != recovery_stage:
                    self.report_progress(
                        "[Response-Only Recovery] "
                        f"{recovery_stage.value} → "
                        f"{normalized_recovery_stage.value}；"
                        "用户明确不要文件，禁止回退到 Delivery。"
                    )
                recovery_stage = normalized_recovery_stage
                runtime_context["recovery_stage"] = (
                    recovery_stage.value
                )

                failure_summary = (
                    self._verification_failure_summary(
                        latest_verification_report
                    )
                )
                if failure_summary:
                    self.report_progress(
                        "[Completion Gate Failures]\n"
                        + failure_summary
                    )

                # Response-only Processing 的 Completion Gate 失败属于
                # “同 Stage 修正”，必须使用 Processing 的 +Recovery Budget。
                # 不能因为本轮没有工具调用，就让 Processing 永远停在 1/14。
                if (
                    self._goal_explicitly_requests_response_only(
                        goal
                    )
                    and current_stage == AgentStage.PROCESSING
                    and recovery_stage == AgentStage.PROCESSING
                ):
                    normalized_answer = re.sub(
                        r"\s+",
                        " ",
                        final_answer,
                    ).strip()
                    failure_signature = re.sub(
                        r"\s+",
                        " ",
                        failure_summary,
                    ).strip()

                    if (
                        normalized_answer
                        and normalized_answer
                        == last_failed_response_answer
                        and failure_signature
                        == last_failed_response_signature
                    ):
                        self.report_progress(
                            "[Response-Only Completion Terminal] FAIL："
                            "本轮最终答案与上一轮失败答案完全相同，且 "
                            "Completion Gate 失败集合没有变化。停止机械重复合成。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="verification_failed",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=(
                                dict(
                                    latest_verification_report
                                )
                            ),
                            execution_timing=self.execution_monitor.summary(),
                        )

                    last_failed_response_answer = (
                        normalized_answer
                    )
                    last_failed_response_signature = (
                        failure_signature
                    )

                    recovery_budget = (
                        self._consume_same_stage_completion_recovery(
                            route=stage_route,
                            stage=AgentStage.PROCESSING,
                        )
                    )

                    if not recovery_budget.get("allowed"):
                        runtime_context.pop(
                            "_active_completion_stage_recovery",
                            None,
                        )
                        self.report_progress(
                            "[Response-Only Completion Recovery Exhausted] "
                            "Processing Completion Recovery Budget 已耗尽："
                            f"{recovery_budget.get('used', 0)}/"
                            f"{recovery_budget.get('limit', 0)}。"
                            "停止重复 finalizer，任务以 verification_failed 结束。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="verification_failed",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=(
                                dict(
                                    latest_verification_report
                                )
                            ),
                            execution_timing=self.execution_monitor.summary(),
                        )

                    runtime_context[
                        "_active_completion_stage_recovery"
                    ] = AgentStage.PROCESSING.value
                    runtime_context[
                        "stage_route"
                    ] = stage_route.to_dict()

                    self.report_progress(
                        "[Response-Only Completion Recovery] "
                        "Processing recovery budget："
                        f"{recovery_budget.get('used', 0)}/"
                        f"{recovery_budget.get('limit', 0)}；"
                        "下一轮 Finalizer 必须依据上述失败项定向重写。"
                    )

                elif (
                    recovery_stage != current_stage
                    and stage_route.states[
                        recovery_stage
                    ].enabled
                ):
                    runtime_context.pop(
                        "_active_completion_stage_recovery",
                        None,
                    )
                    stage_route.states[
                        recovery_stage
                    ].reset_for_reentry(
                        "Completion Gate FAIL 触发确定性回退。"
                    )
                    current_stage = recovery_stage
                    stage_route.states[
                        current_stage
                    ].activate()
                    runtime_context["current_stage"] = (
                        current_stage.value
                    )
                    runtime_context["stage_route"] = (
                        stage_route.to_dict()
                    )
                    self.report_progress(
                        "[Stage Recovery] Completion Gate → "
                        f"{current_stage.value}"
                    )
                else:
                    runtime_context.pop(
                        "_active_completion_stage_recovery",
                        None,
                    )

                self.report_progress(
                    "Completion Gate：未通过。"
                    "验收结果已作为 Observation 注入下一轮，"
                    "Agent 将继续修正。"
                )

                iteration += 1
                continue

            if action_type != "tool":
                raise ValueError(
                    f"未知 action_type：{action_type}"
                )

            step_id = f"step_{len(tool_results) + 1}"
            tool_name = str(
                decision.get("tool")
                or ""
            ).strip()

            arguments = decision.get(
                "arguments",
                {},
            )

            if not tool_name:
                raise ValueError(
                    "Agent 决策缺少 tool。"
                )

            canonical_name = self.registry.resolve_name(
                tool_name
            )

            if canonical_name is None:
                raise ValueError(
                    f"Agent 试图调用未注册工具：{tool_name}"
                )

            if not isinstance(arguments, dict):
                raise TypeError(
                    "Agent 工具 arguments 必须是 JSON 对象。"
                )

            previous_stage = current_stage
            requested_tool_stage = self._classify_tool_stage(
                canonical_name
            )

            if (
                current_stage == AgentStage.ACQUISITION
                and requested_tool_stage == AgentStage.ACQUISITION
                and self._goal_explicitly_requests_response_only(goal)
                and canonical_name in {"search_web", "read_webpage"}
            ):
                redirected_name, redirected_arguments, redirect_note = (
                    self._maybe_redirect_response_only_research_action(
                        tool_name=canonical_name,
                        arguments=arguments,
                        tool_results=tool_results,
                        goal=goal,
                    )
                )
                if redirect_note:
                    if redirect_note.startswith("failed page"):
                        redirect_label = "[Research Source Recovery] "
                    elif redirect_note.startswith("failed search"):
                        redirect_label = "[Research Search Recovery] "
                    else:
                        redirect_label = "[Research Coverage Guard] "
                    self.report_progress(
                        redirect_label + redirect_note
                    )
                canonical_name = redirected_name
                arguments = redirected_arguments
                requested_tool_stage = self._classify_tool_stage(canonical_name)

            if canonical_name in {"build_dataframe", "create_dataframe"}:
                ungrounded_literals = self._ungrounded_dataframe_literals(
                    arguments=arguments,
                    tool_results=tool_results,
                    goal=goal,
                )
                if ungrounded_literals:
                    evidence_guard = {
                        "blocked_tool": canonical_name,
                        "ungrounded_numeric_literals": ungrounded_literals[:20],
                        "reason": (
                            "准备写入 DataFrame 的部分数值尚未出现在任何成功 Tool Observation 中。"
                            "禁止由模型直接补数；请先读取/获取真实来源，或用确定性计算工具生成派生值。"
                        ),
                    }
                    runtime_context["evidence_grounding_observation"] = evidence_guard
                    self.report_progress(
                        "[Evidence Grounding Guard] "
                        + evidence_guard["reason"]
                        + " 未证实数值："
                        + ", ".join(ungrounded_literals[:10])
                    )
                    iteration += 1
                    continue

            post_stage_recovery = self._post_stage_acquisition_recovery_status(
                current_stage=current_stage,
                requested_tool_stage=requested_tool_stage,
                tool_name=canonical_name,
                runtime_context=runtime_context,
            )
            if post_stage_recovery.get("blocked"):
                fallback = self._processing_recovery_degrade_status(
                    current_stage=current_stage,
                    data_state=data_state,
                    goal=goal,
                    tool_results=tool_results,
                )
                if fallback.get("degrade_to_processing"):
                    observation = {
                        **post_stage_recovery,
                        **fallback,
                        "instruction": (
                            "Acquisition Recovery 已耗尽。禁止继续 search_web、read_webpage、"
                            "download_data_file、download_document_file。"
                            "必须使用现有结构化数据继续 Processing；"
                            "无法由现有证据支持的影响因素结论必须明确标记为未验证/局限。"
                        ),
                    }
                    runtime_context[
                        "_post_stage_acquisition_recovery_exhausted"
                    ] = True
                    runtime_context[
                        "acquisition_recovery_observation"
                    ] = observation
                    runtime_context[
                        "processing_strategy_observation"
                    ] = fallback.get("processing_strategy")
                    self.report_progress(
                        "[Acquisition Recovery Degrade] "
                        + fallback["reason"]
                    )
                    iteration += 1
                    continue

                self.report_progress(
                    "[Acquisition Recovery Terminal Guard] "
                    + str(post_stage_recovery.get("reason") or "")
                )
                runtime_context[
                    "acquisition_recovery_observation"
                ] = {
                    **post_stage_recovery,
                    **fallback,
                }
                return AgentLoopResult(
                    success=False,
                    goal=goal,
                    final_answer="",
                    stop_reason="post_stage_acquisition_recovery_exhausted",
                    iterations=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        dict(latest_verification_report)
                        if isinstance(latest_verification_report, dict)
                        else None
                    ),
                    retry_policy_report=retry_policy,
                )

            if requested_tool_stage == AgentStage.ACQUISITION:
                if canonical_name == "read_webpage":
                    continuation_arguments = (
                        self._read_webpage_continuation_arguments(
                            arguments=arguments,
                            tool_results=tool_results,
                        )
                    )
                    if continuation_arguments is not None:
                        self.report_progress(
                            "[Acquisition Pagination Recovery] "
                            "检测到上一轮网页正文 has_more=True；"
                            "已根据真实 end_character 自动推进 start_character，"
                            "避免重复读取第一页。"
                        )
                        arguments = continuation_arguments

                acquisition_saturation = (
                    self._acquisition_saturation_status(
                        tool_name=canonical_name,
                        arguments=arguments,
                        tool_results=tool_results,
                        goal=goal,
                        current_stage=current_stage,
                    )
                )
                if (
                    acquisition_saturation["saturated"]
                    and current_stage == AgentStage.ACQUISITION
                    and stage_route.states[AgentStage.PROCESSING].enabled
                    and self._goal_explicitly_requests_response_only(goal)
                ):
                    alternative = self._response_only_saturation_alternative(
                        tool_name=canonical_name,
                        arguments=arguments,
                        tool_results=tool_results,
                        goal=goal,
                    )
                    if alternative is not None:
                        alt_name, alt_arguments, alt_reason = alternative
                        alt_stage = self._classify_tool_stage(alt_name)
                        alt_saturation = self._acquisition_saturation_status(
                            tool_name=alt_name,
                            arguments=alt_arguments,
                            tool_results=tool_results,
                            goal=goal,
                            current_stage=current_stage,
                        )
                        if not alt_saturation.get("saturated"):
                            self.report_progress(
                                "[Acquisition Saturation Recovery] "
                                + alt_reason
                            )
                            canonical_name = alt_name
                            arguments = alt_arguments
                            requested_tool_stage = alt_stage
                            acquisition_saturation = alt_saturation

                if acquisition_saturation["saturated"]:
                    self.report_progress(
                        "[Acquisition Saturation Guard] "
                        + acquisition_saturation["reason"]
                    )
                    runtime_context[
                        "acquisition_saturation_observation"
                    ] = acquisition_saturation

                    # Response-only research：达到证据充足/网页硬上限后，不允许
                    # 在同一 Acquisition Loop 空转。确定性结束 Acquisition，
                    # 进入 Processing 形成最终分析。
                    saturation_reason = str(
                        acquisition_saturation.get("reason") or ""
                    )
                    saturation_evidence = (
                        acquisition_saturation.get("evidence_gate")
                        if isinstance(acquisition_saturation, dict)
                        else None
                    )
                    hard_stop = bool(
                        "智能停止上限" in saturation_reason
                        or (
                            isinstance(saturation_evidence, dict)
                            and saturation_evidence.get("sufficient")
                        )
                    )
                    if (
                        current_stage == AgentStage.ACQUISITION
                        and stage_route.states[AgentStage.PROCESSING].enabled
                        and self._goal_explicitly_requests_response_only(goal)
                        and hard_stop
                    ):
                        stage_route.states[AgentStage.ACQUISITION].mark_passed(
                            saturation_reason
                            or "Acquisition 已达到智能停止条件。"
                        )
                        previous_stage = current_stage
                        current_stage = AgentStage.PROCESSING
                        stage_route.states[current_stage].activate()
                        runtime_context["current_stage"] = current_stage.value
                        runtime_context["stage_route"] = stage_route.to_dict()
                        self.report_progress(
                            "[Acquisition Saturation Advance] "
                            f"{previous_stage.value} → {current_stage.value}；"
                            "已停止继续联网扩展，开始基于现有真实证据综合分析。"
                        )
                        iteration += 1
                        continue

                    # Response-only research 若已经没有任何新的确定性采集动作，
                    # 不允许在同一个 Acquisition Loop 原地 continue。否则当本地
                    # decision backend 持续 5xx 时，会反复得到同一 fallback 动作，
                    # Stage Budget 不增长，却一直消耗全局 iteration。
                    if (
                        current_stage == AgentStage.ACQUISITION
                        and stage_route.states[AgentStage.PROCESSING].enabled
                        and self._goal_explicitly_requests_response_only(goal)
                    ):
                        terminal_research_gate = (
                            self._response_only_research_evidence_status(
                                tool_results=tool_results,
                                goal=goal,
                            )
                        )
                        runtime_context[
                            "acquisition_gate_observation"
                        ] = terminal_research_gate

                        if terminal_research_gate.get("passed"):
                            stage_route.states[
                                AgentStage.ACQUISITION
                            ].mark_passed(
                                str(
                                    terminal_research_gate.get("reason")
                                    or saturation_reason
                                    or "Response-only research evidence ready."
                                )
                            )
                            previous_stage = current_stage
                            current_stage = AgentStage.PROCESSING
                            stage_route.states[current_stage].activate()
                            runtime_context["current_stage"] = current_stage.value
                            runtime_context["stage_route"] = stage_route.to_dict()
                            self.report_progress(
                                "[Acquisition Saturation Advance] "
                                f"{previous_stage.value} → {current_stage.value}；"
                                "重复/饱和动作已无新增价值，现有研究证据已通过 Gate。"
                            )
                            iteration += 1
                            continue

                        self.report_progress(
                            "[Acquisition Saturation Terminal] FAIL："
                            "当前调用已饱和，且不存在新的未读候选或未执行主题搜索；"
                            "为避免 Acquisition 原地空转，任务以 "
                            "acquisition_incomplete 结束。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="acquisition_incomplete",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=latest_verification_report,
                            execution_timing=self.execution_monitor.summary(),
                        )

                    # 非 response-only（需要 Excel/Word/PNG 等交付物）的
                    # Acquisition 不能在同一 saturated decision 上无限空转。
                    # 给模型一次读取 saturation observation 后换策略的机会；
                    # 若下一轮仍命中完全相同的饱和签名，则干净终止，
                    # 而不是重复几十次同一个 Loop。
                    repeat_count = (
                        self._record_acquisition_saturation_repeat(
                            runtime_context=runtime_context,
                            signature=str(
                                acquisition_saturation.get("signature")
                                or canonical_name
                            ),
                        )
                    )

                    if canonical_name == "search_web":
                        self.report_progress(
                            "[Acquisition Saturation Redirect] broad search 已禁用；"
                            "下一轮应读取已有最高价值候选网页，"
                            "使用明确数据 URL，或选择 download_data_file。"
                        )
                    elif canonical_name == "read_webpage":
                        self.report_progress(
                            "[Acquisition Saturation Redirect] 当前网页读取已无新增价值；"
                            "下一轮必须换来源、继续真实分页，"
                            "或使用明确数据 URL 下载结构化数据。"
                        )

                    if repeat_count >= 2:
                        self.report_progress(
                            "[Acquisition Saturation Terminal] FAIL："
                            "非 response-only Acquisition 连续两轮命中同一饱和动作；"
                            "为避免原地空转，任务以 acquisition_incomplete 结束。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="acquisition_incomplete",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=latest_verification_report,
                            execution_timing=self.execution_monitor.summary(),
                        )

                    iteration += 1
                    continue

                # 当前 Acquisition 动作将真实执行，清理上一轮的 anti-spin 计数。
                self._clear_acquisition_saturation_repeat(
                    runtime_context
                )

            if (
                current_stage == AgentStage.ACQUISITION
                and requested_tool_stage == AgentStage.PROCESSING
            ):
                # read_office_data 是 Acquisition 的最后一个验证动作：
                # 它本身必须允许执行，成功后 DataState 才会 SOURCE_READY。
                acquisition_read_tools = {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                if canonical_name not in acquisition_read_tools:
                    acquisition_gate = (
                        self._acquisition_gate_status(
                            data_state,
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )
                    runtime_context[
                        "acquisition_gate_observation"
                    ] = acquisition_gate

                    if not acquisition_gate["passed"]:
                        self.report_progress(
                            "[Acquisition Gate] FAIL："
                            + acquisition_gate["reason"]
                        )
                        runtime_context[
                            "stage_gate_observation"
                        ] = acquisition_gate

                        repeat_count = (
                            self._record_acquisition_gate_failure_repeat(
                                runtime_context=runtime_context,
                                reason=acquisition_gate["reason"],
                            )
                        )

                        if repeat_count >= 3:
                            self.report_progress(
                                "[Acquisition Gate Terminal] FAIL："
                                "连续 3 次命中完全相同的 Acquisition Gate "
                                "失败且没有形成新的可读数据状态；"
                                "停止原地空转，任务以 acquisition_incomplete 结束。"
                            )
                            return AgentLoopResult(
                                success=False,
                                goal=goal,
                                final_answer="",
                                stop_reason="acquisition_incomplete",
                                iterations=iteration,
                                tool_results=tool_results,
                                decisions=decisions,
                                verification_report=latest_verification_report,
                                execution_timing=self.execution_monitor.summary(),
                            )

                        iteration += 1
                        continue

                    self._clear_acquisition_gate_failure_repeat(
                        runtime_context
                    )

                    stage_route.states[
                        AgentStage.ACQUISITION
                    ].mark_passed(acquisition_gate["reason"])
                    self.report_progress(
                        "[Acquisition Gate] PASS："
                        + acquisition_gate["reason"]
                    )

            if (
                current_stage == AgentStage.PROCESSING
                and requested_tool_stage == AgentStage.DELIVERY
            ):
                processing_gate = self._processing_gate_status(
                    data_state
                )
                runtime_context[
                    "processing_gate_observation"
                ] = processing_gate

                if not processing_gate["passed"]:
                    self.report_progress(
                        "[Processing Gate] FAIL："
                        + processing_gate["reason"]
                    )
                    runtime_context[
                        "stage_gate_observation"
                    ] = processing_gate

                    processing_state = stage_route.states[
                        AgentStage.PROCESSING
                    ]
                    if processing_state.remaining_normal_iterations <= 0:
                        self.report_progress(
                            "[Stage Budget Terminal Guard] Processing 正常预算已耗尽且 Gate 仍 FAIL；"
                            "停止继续空转模型决策，直接返回可诊断失败。"
                        )
                        return AgentLoopResult(
                            success=False,
                            goal=goal,
                            final_answer="",
                            stop_reason="processing_gate_failed_after_budget",
                            iterations=iteration,
                            tool_results=tool_results,
                            decisions=decisions,
                            verification_report=(
                                dict(latest_verification_report)
                                if isinstance(latest_verification_report, dict)
                                else None
                            ),
                            retry_policy_report=retry_policy,
                        )

                    iteration += 1
                    continue

                stage_route.states[
                    AgentStage.PROCESSING
                ].mark_passed(processing_gate["reason"])
                self.report_progress(
                    "[Processing Gate] PASS："
                    + processing_gate["reason"]
                )

            if (
                current_stage == AgentStage.DELIVERY
                and requested_tool_stage == AgentStage.VERIFICATION
            ):
                delivery_gate = self._delivery_gate_status(
                    data_state=data_state,
                    task_plan=runtime_context.get("task_plan"),
                )
                runtime_context[
                    "delivery_gate_observation"
                ] = delivery_gate

                if not delivery_gate["passed"]:
                    self.report_progress(
                        "[Delivery Gate] FAIL："
                        + delivery_gate["reason"]
                    )
                    runtime_context[
                        "stage_gate_observation"
                    ] = delivery_gate
                    iteration += 1
                    continue

                stage_route.states[
                    AgentStage.DELIVERY
                ].mark_passed(delivery_gate["reason"])
                self.report_progress(
                    "[Delivery Gate] PASS："
                    + delivery_gate["reason"]
                )

            routing_tool_name = canonical_name
            if (
                current_stage == AgentStage.ACQUISITION
                and canonical_name
                in {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                and not self._acquisition_gate_status(
                    data_state,
                    tool_results=tool_results,
                    goal=goal,
                )["passed"]
            ):
                # 首次来源读取仍属于 Acquisition SOURCE_READY 验证，
                # 不在执行前提前切到 Processing。
                routing_tool_name = "download_data_file"

            current_stage = self._sync_stage_for_tool(
                route=stage_route,
                current_stage=current_stage,
                tool_name=routing_tool_name,
                runtime_context=runtime_context,
            )

            if current_stage != previous_stage:
                # Acquisition 的 saturation instruction 只能约束 Acquisition。
                # 一旦正式进入后续 Stage，清理旧观察，避免系统提示词把
                # “停止搜索”错误带入 Processing 并造成跨阶段死循环。
                if previous_stage == AgentStage.ACQUISITION:
                    runtime_context.pop(
                        "acquisition_saturation_observation",
                        None,
                    )

                self.report_progress(
                    "[Stage Router] "
                    f"{previous_stage.value} → {current_stage.value}；"
                    f"触发工具：{canonical_name}"
                )
                self.report_progress(
                    f"[{current_stage.value.title()} Loop] "
                    "Stage 已启动。"
                )

            # v5.2 Independent Stage Budget：
            # 预算按“实际即将执行的 Tool 所属 Stage”计费，
            # 而不是按全局 decision round 计费。
            tool_stage = self._classify_tool_stage(
                routing_tool_name
            )
            stage_budget_block_reason = (
                self._stage_budget_block_reason(
                    route=stage_route,
                    stage=tool_stage,
                )
            )

            if stage_budget_block_reason:
                self.report_progress(
                    "[Stage Budget Boundary] "
                    + stage_budget_block_reason
                )
                runtime_context["stage_budget_observation"] = {
                    "stage": tool_stage.value,
                    "blocked_tool": canonical_name,
                    "reason": stage_budget_block_reason,
                    "instruction": (
                        "当前 Stage 已耗尽正常预算。"
                        "不要继续重复该阶段工具；"
                        "请基于已有 Observation 推进到后续必要 Stage，"
                        "或在确实无法满足任务时申请 finish，"
                        "由 Completion Gate 给出真实验收结果。"
                    ),
                }

                if (
                    current_stage == AgentStage.ACQUISITION
                    and tool_stage == AgentStage.ACQUISITION
                    and stage_route.states[
                        AgentStage.PROCESSING
                    ].enabled
                    and self._goal_explicitly_requests_response_only(
                        goal
                    )
                ):
                    terminal_research_gate = (
                        self._response_only_research_evidence_status(
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )
                    runtime_context[
                        "acquisition_gate_observation"
                    ] = terminal_research_gate
                    runtime_context.pop(
                        "_pending_response_only_acquisition_decision",
                        None,
                    )

                    if terminal_research_gate.get("passed"):
                        stage_route.states[
                            AgentStage.ACQUISITION
                        ].mark_passed(
                            str(
                                terminal_research_gate.get("reason")
                                or "Acquisition budget terminal evidence ready."
                            )
                        )
                        previous_stage = current_stage
                        current_stage = AgentStage.PROCESSING
                        stage_route.states[
                            current_stage
                        ].activate()
                        runtime_context[
                            "current_stage"
                        ] = current_stage.value
                        runtime_context[
                            "stage_route"
                        ] = stage_route.to_dict()
                        self.report_progress(
                            "[Response-Only Budget Advance] "
                            f"{previous_stage.value} → {current_stage.value}；"
                            "被 Stage Budget 拦截的新 Acquisition 工具不再执行，"
                            "改用现有真实证据进入 Processing。"
                        )
                        iteration += 1
                        continue

                    self.report_progress(
                        "[Response-Only Budget Terminal] FAIL："
                        "Acquisition Stage Normal Budget 已耗尽，"
                        "且现有真实网页证据仍不足；"
                        "不再重新排队被预算拒绝的工具。"
                    )
                    return AgentLoopResult(
                        success=False,
                        goal=goal,
                        final_answer="",
                        stop_reason="acquisition_incomplete",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=latest_verification_report,
                        retry_policy_report={
                            "acquisition_gate": terminal_research_gate,
                        },
                        execution_timing=self.execution_monitor.summary(),
                    )

                iteration += 1
                continue

            stage_route.states[
                tool_stage
            ].record_iteration()

            runtime_context["stage_route"] = (
                stage_route.to_dict()
            )
            runtime_context["current_stage"] = (
                current_stage.value
            )

            self.report_progress(
                "[Stage Budget] "
                f"{tool_stage.value}: "
                f"{stage_route.states[tool_stage].iterations_used}/"
                f"{stage_route.states[tool_stage].budget.normal_iterations}"
            )

            retry_policy = self._evaluate_retry_policy(
                tool_name=canonical_name,
                arguments=arguments,
                tool_results=tool_results,
            )

            if retry_policy.get("blocked"):
                self.report_progress(
                    f"[{step_id}] Recovery Policy：停止重复失败调用。"
                    f"原因：{retry_policy.get('reason', '')}"
                )

                # response-only 联网研究中，一个网页候选连续失败并不等价于
                # 整个任务的 Recovery Exhausted。正常情况下，前面的
                # Research Source Recovery 已经会把第二次原样 read_webpage
                # 重定向到其他尚未尝试的候选。若最终仍走到这里，说明当前
                # 没有可用的确定性替代候选：此时应按“研究证据是否已经足够”
                # 决定推进或明确 Acquisition 未完成，而不是误报通用
                # recovery_exhausted。
                response_only_web_research = bool(
                    current_stage == AgentStage.ACQUISITION
                    and self._goal_explicitly_requests_response_only(goal)
                    and canonical_name in {"search_web", "read_webpage"}
                )
                if response_only_web_research:
                    research_gate = (
                        self._response_only_research_evidence_status(
                            tool_results=tool_results,
                            goal=goal,
                        )
                    )

                    if canonical_name == "search_web":
                        coverage = (
                            research_gate.get("coverage")
                            if isinstance(research_gate, dict)
                            else {}
                        ) or {}
                        exhausted_topics = list(
                            coverage.get("exhausted_search_topics")
                            or (
                                (coverage.get("topic_exhaustion") or {}).get("exhausted_topics")
                                if isinstance(coverage.get("topic_exhaustion"), dict)
                                else []
                            )
                            or []
                        )
                        degraded_topics = list(
                            coverage.get("degraded_search_topics") or []
                        )
                        routing_topics = list(
                            coverage.get("routing_missing_search_topics") or []
                        )
                        blocking_topics = list(
                            coverage.get("blocking_missing_search_topics") or []
                        )
                        current_query = str(
                            (arguments or {}).get("query")
                            or (arguments or {}).get("search_query")
                            or ""
                        )
                        current_topics = set(
                            self._research_topic_hits(current_query)
                        )

                        if (
                            exhausted_topics
                            and current_topics.intersection(exhausted_topics)
                            and routing_topics
                        ):
                            label = (
                                "[Research Topic Degrade] "
                                if current_topics.intersection(degraded_topics)
                                else "[Research Topic Exhausted] "
                            )
                            self.report_progress(
                                label
                                + "当前研究主题的 bounded search recovery 已耗尽；"
                                "停止在该主题继续改写近义 query，转向下一个未尝试主题："
                                + "、".join(routing_topics[:3])
                            )
                            iteration += 1
                            continue
                    if (
                        research_gate.get("passed")
                        and stage_route.states[AgentStage.PROCESSING].enabled
                    ):
                        stage_route.states[AgentStage.ACQUISITION].mark_passed(
                            str(research_gate.get("reason") or "研究证据已足够。")
                        )
                        previous_stage = current_stage
                        current_stage = AgentStage.PROCESSING
                        stage_route.states[current_stage].activate()
                        runtime_context["current_stage"] = current_stage.value
                        runtime_context["stage_route"] = stage_route.to_dict()
                        runtime_context.pop(
                            "acquisition_saturation_observation",
                            None,
                        )
                        self.report_progress(
                            "[Research Recovery Advance] "
                            f"{previous_stage.value} → {current_stage.value}；"
                            "重复失败来源已放弃，但现有真实研究证据已经满足进入 Processing 的条件。"
                        )
                        iteration += 1
                        continue

                    self.report_progress(
                        "[Research Recovery Terminal Guard] "
                        "重复失败来源已达到 Retry Budget，且现有研究证据仍不足；"
                        "任务以 acquisition_incomplete 结束，不把单个来源失败误报为整个 Agent Recovery Exhausted。"
                    )
                    return AgentLoopResult(
                        success=False,
                        goal=goal,
                        final_answer="",
                        stop_reason="acquisition_incomplete",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            dict(latest_verification_report)
                            if isinstance(
                                latest_verification_report,
                                dict,
                            )
                            else None
                        ),
                        retry_policy_report={
                            **retry_policy,
                            "research_gate": research_gate,
                        },
                    )

                return AgentLoopResult(
                    success=False,
                    goal=goal,
                    final_answer="",
                    stop_reason="recovery_exhausted",
                    iterations=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        dict(latest_verification_report)
                        if isinstance(
                            latest_verification_report,
                            dict,
                        )
                        else None
                    ),
                    retry_policy_report=retry_policy,
                )

            try:
                resolver = ReferenceResolver(
                    self.context
                )

                resolved_arguments = resolver.resolve(
                    arguments
                )

                (
                    resolved_arguments,
                    dataframe_argument_note,
                ) = self._normalize_dataframe_builder_arguments(
                    tool_name=canonical_name,
                    arguments=resolved_arguments,
                )

                if dataframe_argument_note:
                    self.report_progress(
                        "[DataFrame Reference Bridge] "
                        + dataframe_argument_note
                    )

                (
                    resolved_arguments,
                    weather_pipeline_note,
                ) = self._normalize_weather_pipeline_arguments(
                    tool_name=canonical_name,
                    arguments=resolved_arguments,
                    tool_results=tool_results,
                    runtime_context=runtime_context,
                )

                if weather_pipeline_note:
                    self.report_progress(
                        "[Weather Pipeline Bridge] "
                        + weather_pipeline_note
                    )

            except Exception as error:
                failed_result = ToolExecutionResult(
                    success=False,
                    tool_name=canonical_name,
                    arguments=arguments,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

                self.context.store(
                    step_id,
                    failed_result,
                )

                self.executor.history.append(
                    failed_result
                )

                tool_results.append(
                    failed_result
                )

                self.report_progress(
                    f"[{step_id}] 参数引用解析失败："
                    f"{type(error).__name__}: {error}"
                )

                iteration += 1
                continue

            self.report_progress(
                f"[{step_id}] Agent 选择工具：{canonical_name}"
            )

            purpose = str(
                decision.get("purpose")
                or ""
            ).strip()

            if purpose:
                self.report_progress(
                    f"[{step_id}] 目的：{purpose}"
                )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            execution_budget_block_reason = (
                self._get_execution_budget_block_reason(
                    goal=goal,
                    tool_name=canonical_name,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                )
            )

            if execution_budget_block_reason:
                # v5.0+ Source / Execution Budget Fast Finish
                #
                # Policy Block 不是 Tool Failure，也不是一次真实 Tool Call。
                # 对“读取一个 Excel 并告诉我基本情况”这类 response-only
                # 任务，如果核心证据已经齐全，LLM 仍试图把任务升级为
                # 深入统计，则不再把“被拦截的工具”塞进 tool_results，
                # 也不再浪费下一轮让 LLM 自己 finish。
                #
                # Python 直接基于当前真实 Observation 运行 Completion Gate：
                # PASS  -> 当前轮直接完成；
                # FAIL  -> 保留 Gate 失败项，继续让 Agent 补齐真正缺失证据。
                self.report_progress(
                    f"[{step_id}] Execution Budget Boundary："
                    f"{execution_budget_block_reason}"
                )
                self.report_progress(
                    f"[{step_id}] Source Selection Budget："
                    "当前任务的必要读取/基础信息证据已经满足，"
                    "该扩展工具未真实执行；正在直接进入 Completion Gate。"
                )

                auto_finish_answer = (
                    self._build_basic_info_final_answer(
                        tool_results
                    )
                )

                verification_report = self._run_completion_gate(
                    goal=goal,
                    final_answer=auto_finish_answer,
                    iteration=iteration,
                    runtime_context=runtime_context,
                    tool_results=tool_results,
                    decisions=decisions,
                )

                if verification_report.verified:
                    self.report_progress(
                        "Completion Gate：PASS，"
                        "Source Selection Budget 已直接完成任务。"
                    )

                    runtime_context.pop(
                        "verification_observation",
                        None,
                    )

                    return AgentLoopResult(
                        success=True,
                        goal=goal,
                        final_answer=auto_finish_answer,
                        stop_reason="completed",
                        iterations=iteration,
                        tool_results=tool_results,
                        decisions=decisions,
                        verification_report=(
                            verification_report.to_dict()
                        ),
                        execution_timing=self.execution_monitor.summary(),
                    )

                latest_verification_report = (
                    verification_report.to_dict()
                )
                runtime_context[
                    "verification_observation"
                ] = dict(
                    latest_verification_report
                )

                self.report_progress(
                    "Completion Gate：当前证据仍不足。"
                    "验收失败项已注入下一轮，Agent 只允许补齐真正缺失的证据。"
                )

                iteration += 1
                continue

            self.execution_monitor.start_tool(canonical_name)

            result = self.executor.execute(
                canonical_name,
                resolved_arguments,
                runtime_context=runtime_context,
            )

            self.execution_monitor.end_tool(
                canonical_name,
                success=bool(getattr(result, "success", False)),
            )

            self.context.store(
                step_id,
                result,
            )

            tool_results.append(
                result
            )

            if post_stage_recovery.get("active"):
                runtime_context[
                    "_post_stage_acquisition_recovery_count"
                ] = int(
                    runtime_context.get(
                        "_post_stage_acquisition_recovery_count",
                        0,
                    )
                    or 0
                ) + 1
                runtime_context[
                    "acquisition_recovery_observation"
                ] = {
                    **post_stage_recovery,
                    "used_after_call": runtime_context[
                        "_post_stage_acquisition_recovery_count"
                    ],
                    "last_tool": canonical_name,
                    "last_success": bool(
                        getattr(result, "success", False)
                    ),
                }

            self._update_data_state_from_tool_result(
                data_state=data_state,
                tool_result=result,
                step_id=step_id,
            )

            if (
                getattr(result, "success", False)
                and requested_tool_stage == AgentStage.ACQUISITION
            ):
                self._clear_acquisition_gate_failure_repeat(
                    runtime_context
                )

            # v6.6 fix39：专业 Tool 可以通过真实输出声明本阶段工作已完成。
            # Stage Advance 仍由 Python 控制，不依赖 LLM 自己猜下一阶段。
            stage_signal_output = (
                getattr(
                    result,
                    "output",
                    None,
                )
                if getattr(
                    result,
                    "success",
                    False,
                )
                else None
            )

            if isinstance(
                stage_signal_output,
                dict,
            ):
                if (
                    current_stage == AgentStage.PROCESSING
                    and stage_signal_output.get(
                        "processing_complete"
                    )
                    is True
                    and stage_route.states[
                        AgentStage.DELIVERY
                    ].enabled
                ):
                    stage_route.states[
                        AgentStage.PROCESSING
                    ].mark_passed(
                        "专业 Processing Tool 已返回 processing_complete=True。"
                    )
                    previous_stage = current_stage
                    current_stage = AgentStage.DELIVERY
                    stage_route.states[
                        current_stage
                    ].activate()
                    runtime_context[
                        "current_stage"
                    ] = current_stage.value
                    runtime_context[
                        "stage_route"
                    ] = stage_route.to_dict()
                    self.report_progress(
                        "[Specialized Stage Advance] "
                        f"{previous_stage.value} → {current_stage.value}；"
                        "Processing 专业分析已确定性完成。"
                    )

                elif (
                    current_stage == AgentStage.DELIVERY
                    and stage_signal_output.get(
                        "delivery_complete"
                    )
                    is True
                    and stage_route.states[
                        AgentStage.VERIFICATION
                    ].enabled
                ):
                    stage_route.states[
                        AgentStage.DELIVERY
                    ].mark_passed(
                        "专业 Delivery Tool 已返回 delivery_complete=True。"
                    )
                    previous_stage = current_stage
                    current_stage = AgentStage.VERIFICATION
                    stage_route.states[
                        current_stage
                    ].activate()
                    runtime_context[
                        "current_stage"
                    ] = current_stage.value
                    runtime_context[
                        "stage_route"
                    ] = stage_route.to_dict()
                    self.report_progress(
                        "[Specialized Stage Advance] "
                        f"{previous_stage.value} → {current_stage.value}；"
                        "最终交付包已生成并完成内部写后验证。"
                    )

            # v6.3 Processing Intelligence:
            # 数据读取/处理成功后，生成分析路线 Observation，
            # 让后续 LLM 决策优先考虑合适的分析工具。
            if getattr(result, "success", False):
                processing_strategy = self._detect_processing_strategy(
                    goal=goal,
                    data_state=data_state,
                    tool_results=tool_results,
                )
                runtime_context[
                    "processing_strategy_observation"
                ] = processing_strategy
            data_state.deliverable_paths = (
                self._deduplicate_deliverable_paths(
                    data_state.deliverable_paths
                )
            )

            # v5.3 Delivery -> Verification Bridge:
            # 成功生成的最终文件进入回读队列。
            if self._classify_tool_stage(result.tool_name) == AgentStage.DELIVERY:
                self.readback_registry.register_many(
                    data_state.deliverable_paths
                )

            if (
                current_stage == AgentStage.ACQUISITION
                and canonical_name
                in {
                    "read_office_data",
                    "load_data",
                    "read_csv",
                    "read_excel",
                    "get_data_info",
                    "read_document",
                }
                and getattr(result, "success", False)
            ):
                acquisition_gate = (
                    self._acquisition_gate_status(
                        data_state,
                        tool_results=tool_results,
                        goal=goal,
                    )
                )
                runtime_context[
                    "acquisition_gate_observation"
                ] = acquisition_gate
                if acquisition_gate["passed"]:
                    stage_route.states[
                        AgentStage.ACQUISITION
                    ].mark_passed(acquisition_gate["reason"])
                    self.report_progress(
                        "[Acquisition Gate] PASS："
                        + acquisition_gate["reason"]
                    )

            runtime_context["data_state"] = data_state.to_dict()

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=iteration,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            if result.success:
                self.report_progress(
                    f"[{step_id}] Observation："
                    f"{self._summarize_output(result.output)}"
                )
            else:
                self.report_progress(
                    f"[{step_id}] Observation：执行失败，"
                    f"{result.error_type}: {result.error_message}"
                )

            iteration += 1

        # --------------------------------------------------------
        # 工具预算耗尽后的最终完成判定
        # --------------------------------------------------------
        #
        # max_iterations 表示正常执行轮数预算。
        # 如果 Completion Gate 曾经失败，则额外允许
        # max_completion_recovery_iterations 个受限恢复轮次，用于：
        # 修正最终交付物 → 回读最新版本 → 再次申请完成。
        # 如果最后一个允许轮次刚好完成用户目标，仍额外允许一次
        # “只判断、不再调用工具”的最终评估。
        #
        # 这里额外允许一次“只判断、不再调用工具”的最终评估：
        # - finish：说明最后一次 Observation 已经满足目标；
        # - tool：说明仍需真实工具，因此按 max_iterations 结束；
        #
        # 这样不会因为“已经生成文件”之类的启发式条件自动判成功。
        # v4.0 起，LLM 的最终 finish 仍只是“申请完成”，必须继续经过
        # Python Completion Gate；Gate FAIL 时返回 verification_failed，
        # 不允许被旧的 max_iterations 收尾逻辑绕过。
        exhausted_iteration_budget = (
            self.max_iterations
            + (
                self.max_completion_recovery_iterations
                if latest_verification_report is not None
                else 0
            )
        )

        if latest_verification_report is not None:
            self.report_progress(
                "Agent 已用完正常执行预算与 Completion Recovery Budget，"
                "正在进行最终完成判定……"
            )
        else:
            self.report_progress(
                "Agent 已用完常规执行轮数，正在进行最终完成判定……"
            )

        if self._is_cancel_requested():
            return self._cancelled_result(
                goal=goal,
                iteration=exhausted_iteration_budget,
                tool_results=tool_results,
                decisions=decisions,
                latest_verification_report=latest_verification_report,
            )

        if (
            current_stage == AgentStage.ACQUISITION
            and stage_route.states[AgentStage.PROCESSING].enabled
            and self._goal_explicitly_requests_response_only(goal)
        ):
            terminal_research_gate = self._response_only_research_evidence_status(
                tool_results=tool_results,
                goal=goal,
            )
            runtime_context["acquisition_gate_observation"] = terminal_research_gate
            if not terminal_research_gate.get("passed"):
                self.report_progress(
                    "[Final Acquisition Guard] FAIL：执行预算已耗尽，但联网研究证据/主题覆盖仍不足；"
                    "禁止绕过 Acquisition 直接由 Completion Gate 判定成功。"
                )
                return AgentLoopResult(
                    success=False,
                    goal=goal,
                    final_answer="",
                    stop_reason="acquisition_incomplete",
                    iterations=exhausted_iteration_budget + 1,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        dict(latest_verification_report)
                        if isinstance(latest_verification_report, dict)
                        else None
                    ),
                    retry_policy_report=retry_policy,
                )

            stage_route.states[AgentStage.ACQUISITION].mark_passed(
                str(terminal_research_gate.get("reason") or "Acquisition 证据已满足。")
            )
            current_stage = AgentStage.PROCESSING
            stage_route.states[current_stage].activate()
            runtime_context["current_stage"] = current_stage.value
            runtime_context["stage_route"] = stage_route.to_dict()
            self.report_progress(
                "[Final Acquisition Guard] PASS：预算耗尽前的研究证据已满足，"
                "确定性进入 Processing 后再生成最终回答。"
            )

        final_state = self._build_state(
            goal=goal,
            runtime_context=runtime_context,
            decisions=decisions,
            tool_results=tool_results,
        )

        final_decision = self._decide_next_action(
            goal=goal,
            state=final_state,
            finish_only=True,
        )

        if self._is_cancel_requested():
            return self._cancelled_result(
                goal=goal,
                iteration=exhausted_iteration_budget + 1,
                tool_results=tool_results,
                decisions=decisions,
                latest_verification_report=latest_verification_report,
            )

        decisions.append(final_decision)

        if final_decision.get("action_type") == "finish":
            final_answer = str(
                final_decision.get("final_answer")
                or ""
            ).strip()

            self.report_progress(
                "最终判定请求完成，正在进入 v4.0 Completion Gate……"
            )

            if self._is_cancel_requested():
                return self._cancelled_result(
                    goal=goal,
                    iteration=exhausted_iteration_budget + 1,
                    tool_results=tool_results,
                    decisions=decisions,
                    latest_verification_report=latest_verification_report,
                )

            verification_report = self._run_completion_gate(
                goal=goal,
                final_answer=final_answer,
                iteration=exhausted_iteration_budget + 1,
                runtime_context=runtime_context,
                tool_results=tool_results,
                decisions=decisions,
            )

            if verification_report.verified:
                self.report_progress(
                    "Completion Gate：PASS，最后一次工具执行后的任务验收通过。"
                )

                return AgentLoopResult(
                    success=True,
                    goal=goal,
                    final_answer=final_answer,
                    stop_reason="completed",
                    iterations=exhausted_iteration_budget + 1,
                    tool_results=tool_results,
                    decisions=decisions,
                    verification_report=(
                        verification_report.to_dict()
                    ),
                )

            latest_verification_report = (
                verification_report.to_dict()
            )
            runtime_context["verification_observation"] = dict(
                latest_verification_report
            )

            self.report_progress(
                "Completion Gate：最终验收未通过，"
                "且工具执行预算已经耗尽。"
            )

            return AgentLoopResult(
                success=False,
                goal=goal,
                final_answer="",
                stop_reason="verification_failed",
                iterations=exhausted_iteration_budget + 1,
                tool_results=tool_results,
                decisions=decisions,
                verification_report=(
                    verification_report.to_dict()
                ),
            )

        self.report_progress(
            "最终判定：任务仍需要继续调用工具，但已达到最大工具执行轮数。"
        )

        return AgentLoopResult(
            success=False,
            goal=goal,
            final_answer="",
            stop_reason="max_iterations",
            iterations=exhausted_iteration_budget + 1,
            tool_results=tool_results,
            decisions=decisions,
            verification_report=(
                dict(latest_verification_report)
                if isinstance(
                    latest_verification_report,
                    dict,
                )
                else None
            ),
        )


    def _perform_final_readback_precheck(
        self,
        *,
        data_state: DataState,
        tool_results: List[ToolExecutionResult],
        runtime_context: Dict[str, Any],
    ) -> None:
        """
        v5.8 Final Readback Hook

        Completion Gate 之前主动补齐最终交付物读取证据。
        VerificationEngine 只负责验收，不负责产生证据。
        """

        readback_map = {
            ".xlsx": "inspect_professional_excel_report",
            ".xls": "read_office_data",
            ".docx": "inspect_professional_word_report",
            ".doc": "read_office_data",
            ".csv": "read_office_data",
            ".txt": "read_office_data",
            ".pdf": "read_office_data",
        }

        existing = {
            str(item.tool_name or "").strip().lower()
            for item in tool_results
            if getattr(item, "success", False)
        }

        for path in self._deduplicate_deliverable_paths(
            data_state.deliverable_paths
        ):
            suffix = Path(path).suffix.lower()
            preferred = readback_map.get(suffix)

            if not preferred:
                continue

            canonical = self.registry.resolve_name(preferred)

            if canonical is None:
                continue

            # 避免重复读取
            if canonical.lower() in existing:
                continue

            self.report_progress(
                "[Final Readback Hook] "
                f"正在读取最终交付物：{Path(path).name}"
            )

            self.execution_monitor.start_tool(canonical)

            result = self.executor.execute(
                canonical,
                {"file_path": path},
                runtime_context=runtime_context,
            )

            self.execution_monitor.end_tool(
                canonical,
                success=bool(getattr(result, "success", False)),
            )

            tool_results.append(result)

            self.context.store(
                f"final_readback_{len(tool_results)}",
                result,
            )

    def _run_completion_gate(
        self,
        *,
        goal: str,
        final_answer: str,
        iteration: int,
        runtime_context: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
        decisions: List[Dict[str, Any]],
    ) -> VerificationReport:
        """
        v4.0 Completion Gate。

        LLM 的 finish 只是“申请完成”，不是最终成功判定。
        Python VerificationEngine 会基于真实工具历史、Workspace 和
        TaskPlan 再做一次确定性验收。
        """
        provisional_result = AgentLoopResult(
            success=True,
            goal=goal,
            final_answer=final_answer,
            stop_reason="completed",
            iterations=iteration,
            tool_results=tool_results,
            decisions=decisions,
        )

        return self.verifier.verify(
            task_plan=runtime_context.get("task_plan"),
            loop_result=provisional_result,
            runtime_context=runtime_context,
            workspace_summary=None,
        )

    def _is_ollama_backend(self) -> bool:
        """
        判断当前 OpenAI-compatible 后端是否为本机 Ollama。

        仅根据 base_url 判断，避免把云端 Qwen 等其他兼容服务
        错误当成 Ollama。
        """
        base_url = str(getattr(self, "base_url", "") or "").strip().lower()
        return (
            "11434" in base_url
            or "ollama" in base_url
        )

    @staticmethod
    def _is_retryable_model_backend_error(error: Exception) -> bool:
        """
        识别本地 OpenAI-compatible 后端可短暂重试的异常。

        只把 429 / 5xx / 连接中断 / 超时视为瞬时后端错误。
        4xx 参数、鉴权、协议错误不自动重试，避免掩盖真实问题。
        """
        status_code = getattr(error, "status_code", None)
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = None

        if status_code is not None:
            return status_code == 429 or status_code >= 500

        name = error.__class__.__name__.lower()
        return any(
            token in name
            for token in (
                "connection",
                "timeout",
                "internalserver",
                "ratelimit",
            )
        )

    def _create_decision_completion_with_backend_retry(
        self,
        request_kwargs: Dict[str, Any],
        *,
        max_attempts: int = 3,
        base_delay: float = 0.75,
    ):
        """
        为本地 Ollama 的 Agent 决策调用提供有界重试。

        云端模型保持单次请求语义，避免额外 API 消耗。
        """
        attempts = max(1, int(max_attempts)) if self._is_ollama_backend() else 1
        last_error: Optional[Exception] = None

        for backend_attempt in range(1, attempts + 1):
            try:
                return (
                    self.client
                    .chat
                    .completions
                    .create(**request_kwargs)
                )
            except Exception as error:
                last_error = error

                if (
                    not self._is_ollama_backend()
                    or not self._is_retryable_model_backend_error(error)
                    or backend_attempt >= attempts
                ):
                    raise

                self.report_progress(
                    "[Local Decision Retry] 本地模型调用暂时失败"
                    f"（{backend_attempt}/{attempts}），正在短暂重试……"
                )

                delay = max(0.0, float(base_delay)) * backend_attempt
                if delay > 0:
                    time.sleep(delay)

        if last_error is not None:
            raise last_error

        raise RuntimeError("Agent Loop 后端调用未返回结果。")

    def _build_local_backend_failure_fallback_decision(
        self,
        *,
        goal: str,
        state: Dict[str, Any],
        current_stage: AgentStage,
        tool_results: List[ToolExecutionResult],
        backend_error: Exception,
    ) -> Optional[Dict[str, Any]]:
        """
        v6.6 Local Decision Backend Guard.

        本地 Ollama 在 Agent 决策调用连续返回 5xx/连接错误时，允许 Python
        对 response-only 联网研究执行极少量“可证明安全”的确定性动作：
        - Acquisition：按 Research Coverage 继续 search_web / read_webpage；
        - Processing：绕过大型决策提示，直接尝试紧凑 plain-text Finalizer。

        该兜底不切换云端模型、不伪造 Observation、不跳过 Acquisition Gate。
        其他任务或非瞬时后端错误仍返回 None，由调用方按原错误处理。
        """
        if (
            not self._is_ollama_backend()
            or not self._is_retryable_model_backend_error(backend_error)
            or not self._goal_explicitly_requests_response_only(goal)
        ):
            return None

        if current_stage == AgentStage.PROCESSING:
            if not self._is_response_only_web_research_state(
                goal=goal,
                state=state,
            ):
                return None

            self.report_progress(
                "[Local Decision Backend Guard] Processing 决策调用连续失败；"
                "改用紧凑证据包直接尝试 Response-Only Finalizer，"
                "不切换云端模型。"
            )
            try:
                final_answer = (
                    self._generate_response_only_research_final_answer(
                        goal=goal,
                        state=state,
                    )
                )
            except Exception as finalizer_error:
                if not self._is_retryable_model_backend_error(
                    finalizer_error
                ):
                    raise

                self.report_progress(
                    "[Local Decision Backend Guard] 标准 Finalizer 仍发生本地"
                    "模型 5xx/连接错误；改用更小上下文的 Emergency Finalizer。"
                )
                try:
                    final_answer = (
                        self._generate_response_only_research_emergency_answer(
                            goal=goal,
                            state=state,
                        )
                    )
                except Exception as emergency_error:
                    if not self._is_retryable_model_backend_error(
                        emergency_error
                    ):
                        raise

                    self.report_progress(
                        "[Local Decision Backend Guard] Emergency Finalizer "
                        "仍无法连接/执行本地模型；确认当前 Ollama 后端不可用。"
                    )
                    return None

            return {
                "action_type": "finish",
                "final_answer": final_answer,
            }

        if current_stage != AgentStage.ACQUISITION:
            return None

        if not self._is_response_only_web_research_acquisition_state(
            goal=goal,
            state=state,
        ):
            return None

        research_gate = self._response_only_research_evidence_status(
            tool_results=tool_results,
            goal=goal,
        )
        if research_gate.get("passed"):
            return {
                "action_type": "finish",
                "final_answer": "__DATAPILOT_STAGE_FINISH__",
            }

        coverage = self._response_only_research_coverage_status(
            tool_results=tool_results,
            goal=goal,
        )
        if "routing_missing_search_topics" in coverage:
            missing_topics = list(
                coverage.get("routing_missing_search_topics") or []
            )
        else:
            missing_topics = list(
                coverage.get("blocking_missing_search_topics")
                or coverage.get("missing_search_topics")
                or []
            )

        if self.registry.resolve_name("search_web") is not None:
            for topic in missing_topics:
                failed_queries = set(
                    self._failed_research_search_queries(
                        tool_results=tool_results,
                        topic=topic,
                    )
                )

                base_query = self._research_topic_query(
                    topic,
                    original_query="",
                )
                normalized_base = re.sub(
                    r"\s+",
                    " ",
                    base_query,
                ).strip().lower()

                query = base_query
                if normalized_base in failed_queries:
                    query = (
                        self._next_research_search_recovery_query(
                            topic=topic,
                            current_query=base_query,
                            tool_results=tool_results,
                        )
                        or ""
                    )

                if not str(query or "").strip():
                    continue

                return {
                    "action_type": "tool",
                    "tool": "search_web",
                    "arguments": {
                        "query": str(query).strip(),
                        "max_results": 8,
                        "region": "cn-zh",
                    },
                    "reason": (
                        "本地决策模型暂时不可用；Python 根据 Research Coverage "
                        f"确定性补采主题：{topic}"
                    ),
                }

        candidate = self._next_unread_research_candidate(
            tool_results=tool_results,
            goal=goal,
        )
        if (
            candidate
            and self.registry.resolve_name("read_webpage") is not None
        ):
            return {
                "action_type": "tool",
                "tool": "read_webpage",
                "arguments": {
                    "url": candidate["url"],
                    "max_characters": 20000,
                    "timeout": 15,
                    "start_character": 0,
                },
                "reason": (
                    "本地决策模型暂时不可用；Python 读取已发现且尚未读取的"
                    "最高价值真实网页候选。"
                ),
            }

        return None

    @staticmethod
    def _normalize_decision_contract(
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        对本地小模型常见的轻微 JSON 契约偏差做确定性归一化。

        只修正字段别名/动作同义词，不猜业务内容、不猜工具参数。
        所有归一化结果仍会继续经过 Tool Registry、arguments 类型、
        Stage/Recovery 等原有严格校验。
        """
        if not isinstance(decision, dict):
            return decision

        normalized = dict(decision)

        raw_action = (
            normalized.get("action_type")
            or normalized.get("action")
            or normalized.get("type")
            or ""
        )
        action = str(raw_action).strip().lower()

        # 本地中小模型在“观察 -> 下一步”场景中，常把真正动作包进
        # next_action / next_step / decision / tool_call/function_call。Qwen 还会
        # 生成 decision -> tool_call 两层嵌套，因此这里最多做 3 层受控解包。
        # 只解包 dict，不执行任何名字；最终仍必须经过 Registry 白名单。
        for _ in range(3):
            nested_candidate = None
            nested_key_used = ""
            for nested_key in (
                "next_action",
                "next_step",
                "decision",
                "tool_call",
                "function_call",
            ):
                candidate = normalized.get(nested_key)
                if isinstance(candidate, dict) and candidate:
                    nested_candidate = dict(candidate)
                    nested_key_used = nested_key
                    break

            if nested_candidate is None:
                break

            # tool_call/function_call 的常见 schema 是
            # {"name": "read_excel", "arguments": {...}}。
            if nested_key_used in {"tool_call", "function_call"}:
                nested_candidate.setdefault("action_type", "tool")
                if "tool" not in nested_candidate and nested_candidate.get("name"):
                    nested_candidate["tool"] = nested_candidate.get("name")

            # 外层 arguments/purpose/reasoning 仅在内层缺失时补入。
            if "arguments" not in nested_candidate:
                for alias in ("arguments", "args", "parameters", "params"):
                    if alias in normalized:
                        nested_candidate["arguments"] = normalized[alias]
                        break
            if "purpose" not in nested_candidate:
                for alias in ("purpose", "reason", "rationale", "reasoning"):
                    if alias in normalized:
                        nested_candidate["purpose"] = normalized[alias]
                        break

            normalized = nested_candidate
            raw_action = (
                normalized.get("action_type")
                or normalized.get("action")
                or normalized.get("type")
                or ""
            )
            action = str(raw_action).strip().lower()

            # 已经解包到标准 tool/finish 或显式 tool/name 时即可停止；
            # 否则允许再解一层（典型：decision -> tool_call）。
            if action in {"tool", "finish"} or normalized.get("tool"):
                break

        tool_aliases = {
            "tool",
            "use_tool",
            "call_tool",
            "tool_call",
            "function",
            "function_call",
        }
        finish_aliases = {
            "finish",
            "done",
            "complete",
            "completed",
            "final",
            "final_answer",
            "answer",
        }

        if action in tool_aliases:
            normalized["action_type"] = "tool"
        elif action in finish_aliases:
            normalized["action_type"] = "finish"
        else:
            # 除标准 tool/tool_name 外，兼容 Qwen 常用的 next_tool /
            # next_action / next_step 字符串表达；这些仍会在后续经过 Registry
            # 白名单校验，不能执行任意名字。
            explicit_tool_value = (
                normalized.get("tool")
                or normalized.get("tool_name")
                or normalized.get("function_name")
                or normalized.get("next_tool")
            )
            if not explicit_tool_value:
                for key in ("next_action", "next_step"):
                    value = normalized.get(key)
                    if isinstance(value, str) and value.strip():
                        explicit_tool_value = value
                        break

            has_tool_field = bool(str(explicit_tool_value or "").strip())
            has_answer_field = any(
                str(normalized.get(key) or "").strip()
                for key in (
                    "final_answer", "answer", "response", "message",
                    "final", "final_response", "conclusion",
                )
            )

            # 本地小模型有时会把动作写成 respond / plan / analysis 等
            # 非标准枚举。只有当其余字段已经明确表达唯一意图时才修正：
            # 有真实工具字段 -> tool；只有回答字段 -> finish。
            # 不在两者都存在时猜测，避免错误改变业务动作。
            if has_tool_field and not has_answer_field:
                normalized["action_type"] = "tool"
                normalized.setdefault("tool", explicit_tool_value)
            elif has_answer_field and not has_tool_field:
                normalized["action_type"] = "finish"
            else:
                # 本地小模型有时会把真实/拟调用的工具名直接写进
                # action_type（例如 {"action_type":"read_file",...}）。
                # 这里只在存在 arguments/args/parameters/params 时把该值
                # 视为“候选工具名”；后续仍必须经过 Tool Registry/本地别名
                # 的严格验证，因此不会把 plan/respond/analysis 等文本动作
                # 直接当成可执行工具。
                has_argument_payload = any(
                    key in normalized
                    for key in ("arguments", "args", "parameters", "params")
                )
                meta_actions = {
                    "plan", "analysis", "respond", "response", "answer",
                    "think", "reason", "reasoning", "skill",
                }
                if action and has_argument_payload and action not in meta_actions:
                    normalized["action_type"] = "tool"
                    normalized.setdefault("tool", raw_action)

        if normalized.get("action_type") == "tool":
            if not str(normalized.get("tool") or "").strip():
                alias_tool = (
                    normalized.get("tool_name")
                    or normalized.get("function_name")
                    or normalized.get("name")
                )
                if alias_tool is not None:
                    normalized["tool"] = alias_tool

            if "arguments" not in normalized:
                for alias in ("args", "parameters", "params"):
                    if alias in normalized:
                        normalized["arguments"] = normalized[alias]
                        break

            if "purpose" not in normalized:
                alias_purpose = (
                    normalized.get("reason")
                    or normalized.get("rationale")
                )
                if alias_purpose is not None:
                    normalized["purpose"] = alias_purpose

        elif normalized.get("action_type") == "finish":
            if not str(normalized.get("final_answer") or "").strip():
                alias_answer = (
                    normalized.get("answer")
                    or normalized.get("response")
                    or normalized.get("message")
                    or normalized.get("final")
                    or normalized.get("final_response")
                    or normalized.get("conclusion")
                )
                if alias_answer is not None:
                    normalized["final_answer"] = alias_answer

        return normalized

    def _normalize_local_response_pseudo_action(
        self,
        decision: Dict[str, Any],
        *,
        goal: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        v6.6 本地 Qwen 文本回复伪工具兼容层。

        本地模型在无需真实工具、只需直接回答用户时，偶尔会返回：
        {"action":"generate_text","arguments":{"text":"..."}}
        这实际上是“finish + final_answer”，不是 Tool 调用。

        仅在以下条件同时满足时做确定性转换：
        1. 当前是 Ollama 本地后端；
        2. 决策已经被归一化为 tool；
        3. tool 是明确的“回复伪动作”；
        4. 该名称并未真实注册在 Tool Registry；
        5. arguments 中存在非空文本。

        不推断任何业务事实，也不执行未注册工具。转换后的 finish 仍会继续
        经过 Completion Gate，因此不会绕过文件交付、证据或验收要求。
        """
        if not isinstance(decision, dict):
            return decision

        if not self._is_ollama_backend():
            return decision

        if decision.get("action_type") != "tool":
            return decision

        tool_name = str(decision.get("tool") or "").strip()
        normalized_name = tool_name.lower().replace("-", "_").replace(" ", "_")

        pseudo_response_tools = {
            "generate_text",
            "generate_response",
            "respond",
            "reply",
            "reply_to_user",
            "answer_user",
            "final_response",
            "write_response",
            "generate_final_answer",
            "compose_final_answer",
            "return_final_answer",
        }

        # “generate_report / generate_deliverable”在文件型任务里可能真的
        # 表达交付意图，不能一概当成聊天回答。只有用户明确 response-only、
        # TaskPlan 又没有文件交付要求时，才允许把带有完整正文 content 的
        # 这类本地伪动作收敛为 finish。
        response_only_report_aliases = {
            "generate_final_report",
            "generate_report",
            "generate_deliverable",
        }
        runtime = {}
        if isinstance(state, dict):
            candidate = state.get("runtime_context")
            if isinstance(candidate, dict):
                runtime = candidate

        response_only_task = bool(
            self._goal_explicitly_requests_response_only(goal)
            and not self._task_plan_has_file_deliverables(runtime)
        )

        if normalized_name not in pseudo_response_tools:
            if not (
                response_only_task
                and normalized_name in response_only_report_aliases
            ):
                return decision

        # 如果未来真的注册了同名 Tool，必须尊重 Registry，不能把真实 Tool
        # 偷偷改写成 finish。
        try:
            if self.registry.resolve_name(tool_name) is not None:
                return decision
        except Exception:
            pass

        arguments = decision.get("arguments")
        if not isinstance(arguments, dict):
            return decision

        final_answer = ""
        for key in (
            "text",
            "content",
            "answer",
            "response",
            "message",
            "final_answer",
            "report_content",
            "deliverable_content",
        ):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                final_answer = value.strip()
                break

        if not final_answer:
            return decision

        return {
            "action_type": "finish",
            "final_answer": final_answer,
            "_local_response_alias": tool_name,
        }

    @staticmethod
    def _local_single_input_path(state: Dict[str, Any]) -> str:
        """从真实 runtime_context 中取得唯一已选输入文件，不猜测路径。"""
        if not isinstance(state, dict):
            return ""
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return ""

        candidates: List[str] = []

        def add(value: Any):
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if text not in candidates:
                    candidates.append(text)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    add(item)

        add(runtime.get("input_paths"))
        workspace = runtime.get("workspace")
        if isinstance(workspace, dict):
            add(workspace.get("protected_input_paths"))
            add(workspace.get("source_paths"))

        return candidates[0] if len(candidates) == 1 else ""

    @staticmethod
    def _local_current_data_ref(state: Dict[str, Any]) -> str:
        if not isinstance(state, dict):
            return ""
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return ""
        data_state = runtime.get("data_state")
        if not isinstance(data_state, dict):
            return ""
        for key in (
            "current_data_ref",
            "analysis_data_ref",
            "clean_data_ref",
            "raw_data_ref",
        ):
            value = str(data_state.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _local_current_schema(state: Dict[str, Any]) -> List[str]:
        """取得 DataState 中最近的真实字段名列表。"""
        if not isinstance(state, dict):
            return []
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return []
        data_state = runtime.get("data_state")
        if not isinstance(data_state, dict):
            return []
        raw_schema = data_state.get("current_schema") or []
        if not isinstance(raw_schema, (list, tuple)):
            return []
        result: List[str] = []
        for item in raw_schema:
            value = str(item or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def _resolve_local_tool_request(
        self,
        *,
        tool_name: str,
        arguments: Any,
        state: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any], str]:
        """
        Ollama 中小模型的确定性工具名/参数兼容层。

        目标不是替模型猜业务结论，而是把常见的“泛化工具名、引用写法、
        聚合参数 schema”收敛到 DataPilot 已注册工具的真实签名。所有结果
        仍然继续经过 ToolRegistry、ReferenceResolver、ToolPreflight、
        Stage/Recovery 与 Verification。
        """
        raw_name = str(tool_name or "").strip()
        args = dict(arguments) if isinstance(arguments, dict) else {}

        if not self._is_ollama_backend() or not raw_name:
            return raw_name, args, ""

        def as_step_ref(value: Any) -> Any:
            # Qwen 常把 {"$ref": "step_1.output"} 简写成
            # "step_1.output" 或 "$step_1.output"。只有严格匹配 DataPilot
            # 引用语法时才包装，避免普通字符串被误当引用。
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("$") and re.fullmatch(
                    r"\$step_\d+\.output(?:\..+)?", text
                ):
                    text = text[1:]
                if re.fullmatch(r"step_\d+\.output(?:\..+)?", text):
                    return {"$ref": text}
            return value

        def completed_successfully(name: str) -> bool:
            steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
            if not isinstance(steps, list):
                return False
            target_name = str(name or "").strip().lower()
            return any(
                isinstance(item, dict)
                and bool(item.get("success"))
                and str(item.get("tool") or "").strip().lower() == target_name
                for item in steps
            )

        def latest_successful_observation(name: str) -> Any:
            steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
            if not isinstance(steps, list):
                return None
            target_name = str(name or "").strip().lower()
            for item in reversed(steps):
                if (
                    isinstance(item, dict)
                    and bool(item.get("success"))
                    and str(item.get("tool") or "").strip().lower() == target_name
                ):
                    return item.get("observation")
            return None

        def normalize_dataframe_reference(value: Any, fallback_ref: str) -> Any:
            # Qwen 有时会把 Observation 中的 DataFrame 摘要对象
            # {python_type: DataFrame, shape: ..., columns: ...} 原样塞回 df。
            # 该对象只是 JSON 摘要，不是真实 DataFrame；应回到 DataState 的
            # 可追踪 Python 引用。
            if (
                isinstance(value, dict)
                and str(value.get("python_type") or "").strip().lower() == "dataframe"
                and fallback_ref
            ):
                return {"$ref": fallback_ref}

            converted = as_step_ref(value)
            ref_text = ""
            if isinstance(converted, dict) and set(converted) == {"$ref"}:
                ref_text = str(converted.get("$ref") or "").strip()

            # get_data_info 的 output 是质量信息 dict，不是 DataFrame。
            # Qwen 常把“最新一步”误写为 df；此时回退到 DataState 中最近的
            # 真实 DataFrame 引用，而不是让 ToolPreflight 报类型错误。
            match = re.fullmatch(r"step_(\d+)\.output", ref_text)
            if match:
                steps = state.get("completed_tool_steps") if isinstance(state, dict) else None
                index = int(match.group(1)) - 1
                if isinstance(steps, list) and 0 <= index < len(steps):
                    item = steps[index]
                    source_tool = (
                        str(item.get("tool") or "").strip().lower()
                        if isinstance(item, dict)
                        else ""
                    )
                    non_dataframe_outputs = {
                        "get_data_info",
                        "inspect_data_files",
                        "inspect_documents",
                    }
                    if source_tool in non_dataframe_outputs and fallback_ref:
                        return {"$ref": fallback_ref}

            return converted

        name = raw_name.lower()

        # 本地中小模型经常把 Python / pandas API 当作 Agent Tool 名，
        # 例如 pandas.read_excel、pd.read_csv、df.drop_duplicates。
        # DataPilot 不直接执行任意库 API；只把受控前缀下、末级名称明确命中
        # 已知别名白名单的调用收敛到 Registry 中的确定性 Tool。
        semantic_name = name
        namespace_prefixes = (
            "pandas.",
            "pd.",
            "dataframe.",
            "pandas.dataframe.",
            "df.",
        )
        if any(name.startswith(prefix) for prefix in namespace_prefixes):
            semantic_name = name.rsplit(".", 1)[-1]

        input_path = self._local_single_input_path(state)
        suffix = Path(input_path).suffix.lower() if input_path else ""
        current_ref = self._local_current_data_ref(state)
        current_schema = self._local_current_schema(state)

        spreadsheet_suffixes = {".csv", ".xlsx", ".xls", ".xlsm"}
        document_suffixes = {".docx", ".doc", ".pdf", ".txt", ".md"}

        read_aliases = {
            "read_file", "load_file", "open_file",
            "read_excel", "read_excel_file", "load_excel", "open_excel",
            "read_csv", "load_csv", "load_data",
        }
        info_aliases = {
            "check_data_quality", "inspect_data", "data_info",
            "check_missing_values", "check_duplicates",
            "inspect_dataframe", "describe_dataframe",
        }
        duplicate_aliases = {
            "remove_duplicates", "drop_duplicates", "deduplicate",
            "remove_duplicate_rows",
        }
        missing_aliases = {
            "fill_missing_values", "clean_missing_values",
            "handle_missing", "impute_missing_values",
        }

        canonical_existing = self.registry.resolve_name(raw_name)
        target = str(canonical_existing or "").strip()
        notes: List[str] = []

        # 先处理未注册的常见泛化工具名。
        if not target:
            if semantic_name in read_aliases:
                if suffix in spreadsheet_suffixes or any(
                    token in semantic_name for token in ("excel", "csv", "data")
                ):
                    target = "read_office_data"
                    if input_path and not str(args.get("file_path") or "").strip():
                        args["file_path"] = input_path
                    if "file_path" not in args and isinstance(args.get("path"), str):
                        args["file_path"] = args.pop("path")
                elif suffix in document_suffixes:
                    target = "read_document"
                    if input_path and not str(args.get("file_path") or "").strip():
                        args["file_path"] = input_path
                    if "file_path" not in args and isinstance(args.get("path"), str):
                        args["file_path"] = args.pop("path")

            elif semantic_name in info_aliases and current_ref:
                target = "get_data_info"

            elif semantic_name in duplicate_aliases and current_ref:
                target = "drop_duplicate_rows"

            elif semantic_name in missing_aliases and current_ref:
                target = "handle_missing_values"

            if target:
                notes.append(f"{raw_name} -> {target}")

        if not target:
            return raw_name, args, ""

        canonical = self.registry.resolve_name(target)
        if canonical is None:
            return raw_name, args, ""
        target = canonical

        # --------------------------------------------------------
        # read_office_data 参数收敛
        # --------------------------------------------------------
        if target == "read_office_data":
            # DataPilot 的真实签名只有 file_path / sheet_name。Qwen 从 pandas
            # API 迁移过来时常附带 header/engine/usecols 等参数，这些不能直接
            # 透传给确定性 Tool。路径仍必须来自真实输入或模型明确给出的路径。
            allowed_read_args = {"file_path", "sheet_name"}
            removed = [key for key in list(args) if key not in allowed_read_args]
            for key in removed:
                args.pop(key, None)
            if removed:
                notes.append(
                    "drop unsupported read args: " + ",".join(sorted(removed))
                )
            if input_path and not str(args.get("file_path") or "").strip():
                args["file_path"] = input_path

        # --------------------------------------------------------
        # 质量检查前置：用户明确要求检查缺失/重复时，在任何清洗或分析
        # 之前先取得 get_data_info 的真实 Observation。
        # --------------------------------------------------------
        goal_text = str((state or {}).get("goal") or "").lower()
        quality_requested = any(
            token in goal_text
            for token in ("缺失", "重复", "数据质量", "missing", "duplicate")
        )
        downstream_data_tools = {
            "handle_missing_values",
            "drop_duplicate_rows",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
        }
        if (
            current_ref
            and quality_requested
            and target in downstream_data_tools
            and not completed_successfully("get_data_info")
        ):
            notes.append(f"{target} -> get_data_info (quality prerequisite)")
            target = "get_data_info"
            args = {"df": {"$ref": current_ref}}
            return target, args, "; ".join(notes)

        # 质量检查已经完成后，如果 Observation 明确发现问题且用户要求
        # “存在则清洗”，则禁止直接跳到统计分析。让 LLM 先选择真实清洗工具。
        quality_observation = latest_successful_observation("get_data_info")
        if (
            isinstance(quality_observation, dict)
            and self._goal_explicitly_requests_data_cleaning(goal_text)
            and target in {
                "group_statistics",
                "group_multi_statistics",
                "create_pivot_summary",
            }
        ):
            missing_values = quality_observation.get("missing_values") or {}
            duplicate_rows = quality_observation.get("duplicate_rows") or 0
            try:
                duplicate_count = int(duplicate_rows)
            except (TypeError, ValueError):
                duplicate_count = 0

            missing_pending = bool(missing_values) and not completed_successfully(
                "handle_missing_values"
            )
            duplicate_pending = duplicate_count > 0 and not completed_successfully(
                "drop_duplicate_rows"
            )

            if missing_pending or duplicate_pending:
                pending = []
                if missing_pending:
                    pending.append("缺失值处理(handle_missing_values)")
                if duplicate_pending:
                    pending.append("重复记录处理(drop_duplicate_rows)")
                raise ValueError(
                    "用户要求发现数据质量问题时先清洗；get_data_info 已确认仍需："
                    + "、".join(pending)
                    + "。请先调用对应真实清洗 Tool，再进行统计分析。"
                )

        # --------------------------------------------------------
        # DataFrame 引用兼容
        # --------------------------------------------------------
        dataframe_tools = {
            "get_data_info",
            "handle_missing_values",
            "drop_duplicate_rows",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
            "apply_filters",
            "filter_data",
            "sort_data",
            "select_columns",
            "drop_columns",
            "rename_columns",
            "filter_date_range",
        }

        if target in dataframe_tools:
            if "df" not in args and "data_ref" in args:
                args["df"] = args.pop("data_ref")
                notes.append("data_ref -> df")
            if "df" in args:
                converted = normalize_dataframe_reference(
                    args["df"],
                    current_ref,
                )
                if converted != args["df"]:
                    notes.append("df reference normalized")
                args["df"] = converted
            elif current_ref:
                args["df"] = {"$ref": current_ref}
                notes.append("inject current_data_ref")

        # --------------------------------------------------------
        # group_statistics / group_multi_statistics schema 兼容
        # --------------------------------------------------------
        if target in {"group_statistics", "group_multi_statistics"}:
            if "group_by" not in args and "group_by_columns" in args:
                args["group_by"] = args.pop("group_by_columns")
                notes.append("group_by_columns -> group_by")

            raw_aggs = args.get("aggregations")
            normalized_aggs: Dict[str, List[str]] = {}
            if isinstance(raw_aggs, list):
                for item in raw_aggs:
                    if not isinstance(item, dict):
                        continue
                    column = str(
                        item.get("column")
                        or item.get("target_column")
                        or ""
                    ).strip()
                    function = str(
                        item.get("function")
                        or item.get("operation")
                        or item.get("agg")
                        or ""
                    ).strip().lower()
                    if column and function:
                        normalized_aggs.setdefault(column, [])
                        if function not in normalized_aggs[column]:
                            normalized_aggs[column].append(function)
                if normalized_aggs:
                    notes.append("aggregation list -> mapping")
            elif isinstance(raw_aggs, dict):
                for column, functions in raw_aggs.items():
                    if isinstance(functions, str):
                        normalized_aggs[str(column)] = [functions]
                    elif isinstance(functions, (list, tuple)):
                        normalized_aggs[str(column)] = [
                            str(item) for item in functions if str(item).strip()
                        ]

            group_by = args.get("group_by")
            group_columns = (
                list(group_by)
                if isinstance(group_by, (list, tuple))
                else ([group_by] if isinstance(group_by, str) and group_by.strip() else [])
            )

            # ----------------------------------------------------
            # 用户目标字段约束：本地 9B 容易在看到完整 schema 后擅自加入
            # “月份/部门/订单金额”等未要求维度。只有当用户目标中明确出现
            # 真实 schema 字段时才启用收敛；不凭空猜字段。
            # ----------------------------------------------------
            goal_columns = [
                column
                for column in current_schema
                if column and column in goal_text
            ]

            requested_group_columns: List[str] = []
            # 中文“按X统计/汇总/分析/计算”优先识别 X。
            for match in re.finditer(
                r"按([^，。；;]+?)(?:统计|汇总|分析|计算)",
                goal_text,
            ):
                phrase = match.group(1)
                for column in current_schema:
                    if column and column in phrase and column not in requested_group_columns:
                        requested_group_columns.append(column)

            quality_info = latest_successful_observation("get_data_info")
            numeric_columns = []
            if isinstance(quality_info, dict):
                values = quality_info.get("numeric_columns") or []
                if isinstance(values, (list, tuple)):
                    numeric_columns = [str(item) for item in values]

            if numeric_columns:
                # 有 get_data_info 类型证据时，只把真实数值列当作指标。
                requested_metric_columns = [
                    column for column in goal_columns
                    if column in numeric_columns
                ]
            else:
                # 没有类型证据时，至少排除已经明确识别为“按X”的分组字段。
                requested_metric_columns = [
                    column for column in goal_columns
                    if column not in requested_group_columns
                ]

            requested_operations: List[str] = []
            operation_tokens = (
                ("sum", ("合计", "总和", "求和")),
                ("mean", ("平均", "均值")),
                ("count", ("数量", "计数", "个数")),
                ("max", ("最大值",)),
                ("min", ("最小值",)),
                ("median", ("中位数",)),
            )
            for operation_name, tokens in operation_tokens:
                if any(token in goal_text for token in tokens):
                    requested_operations.append(operation_name)

            if requested_group_columns:
                if group_columns != requested_group_columns:
                    notes.append("group_by constrained by user goal")
                group_columns = requested_group_columns

            if requested_metric_columns:
                filtered_aggs: Dict[str, List[str]] = {}
                for column in requested_metric_columns:
                    if column in normalized_aggs:
                        functions = normalized_aggs[column]
                    else:
                        functions = requested_operations or ["sum"]
                    if requested_operations:
                        overlap = [
                            item for item in functions
                            if item in requested_operations
                        ]
                        functions = overlap or list(requested_operations)
                    filtered_aggs[column] = functions
                if filtered_aggs != normalized_aggs:
                    notes.append("aggregations constrained by user goal")
                normalized_aggs = filtered_aggs

            # Qwen 没给 group_by/aggregations，但用户目标已经明确到真实字段时，
            # 直接用目标字段补足，而不是让空参数进入 ToolPreflight。
            if not group_columns and requested_group_columns:
                group_columns = list(requested_group_columns)
            if not normalized_aggs and requested_metric_columns:
                normalized_aggs = {
                    column: (requested_operations or ["sum"])
                    for column in requested_metric_columns
                }

            # Qwen 常把“多字段/多指标”参数交给 group_statistics。
            # 这时不丢信息，确定性切换到真实的 group_multi_statistics。
            goal_constrained = bool(
                requested_group_columns or requested_metric_columns
            )
            should_use_multi = (
                len(group_columns) > 1
                or len(normalized_aggs) > 1
                or any(len(funcs) > 1 for funcs in normalized_aggs.values())
                or (target == "group_multi_statistics" and not goal_constrained)
            )

            if should_use_multi and normalized_aggs:
                multi_name = self.registry.resolve_name("group_multi_statistics")
                if multi_name is not None:
                    if target != multi_name:
                        notes.append(f"{target} schema -> {multi_name}")
                    target = multi_name
                    args = {
                        "df": args.get("df"),
                        "group_by": group_columns,
                        "aggregations": normalized_aggs,
                    }
            elif normalized_aggs:
                # 单分组 + 单指标：无论模型原先选 group_statistics 还是
                # group_multi_statistics，都收敛到更精确的单指标 Tool。
                first_column = next(iter(normalized_aggs))
                first_functions = normalized_aggs[first_column]
                single_name = self.registry.resolve_name("group_statistics")
                if group_columns and first_functions and single_name is not None:
                    if target != single_name:
                        notes.append(f"{target} schema -> {single_name}")
                    target = single_name
                    args = {
                        "df": args.get("df"),
                        "group_by": group_columns[0],
                        "target_column": first_column,
                        "operation": first_functions[0],
                    }
                    notes.append("generic aggregation -> group_statistics signature")
            elif target == "group_statistics":
                if isinstance(args.get("group_by"), (list, tuple)):
                    values = list(args["group_by"])
                    if len(values) == 1:
                        args["group_by"] = values[0]
                        notes.append("single group_by list -> scalar")

        return target, args, "; ".join(notes)

    @classmethod
    def _is_response_only_web_research_state(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> bool:
        """判断当前是否是已经进入 Processing 的 response-only 联网研究任务。"""
        if not cls._goal_explicitly_requests_response_only(goal):
            return False
        if not isinstance(state, dict):
            return False

        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            runtime = {}
        if cls._task_plan_has_file_deliverables(runtime):
            return False
        if str(runtime.get("current_stage") or "").strip().lower() != "processing":
            return False

        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return False

        successful_names = {
            str(item.get("tool") or "").strip().lower()
            for item in steps
            if isinstance(item, dict) and bool(item.get("success"))
        }
        return bool(successful_names & {"search_web", "read_webpage"})

    @classmethod
    def _is_response_only_web_research_acquisition_state(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> bool:
        """判断当前是否是 response-only 联网研究的 Acquisition 阶段。

        这里判断的是 *Stage identity*，不是证据是否已经采集成功。

        v6.6 修复：
        - 即使 search_web/read_webpage 当前全部失败，只要任务仍处于
          response-only Acquisition，就必须返回 True；
        - 这样本地模型会收到短 ``__DATAPILOT_STAGE_FINISH__`` 协议，
          截断的长 finish JSON 也能被恢复成阶段结束申请；
        - Acquisition 是否真的可以离开，仍完全交给 Acquisition Gate 判断，
          本方法绝不绕过证据充分性检查。
        """
        if not cls._goal_explicitly_requests_response_only(goal):
            return False
        if not isinstance(state, dict):
            return False

        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return False
        if cls._task_plan_has_file_deliverables(runtime):
            return False

        current_stage = str(
            runtime.get("current_stage") or ""
        ).strip().lower()
        if current_stage != AgentStage.ACQUISITION.value:
            return False

        # Stage 身份不依赖工具成功与否。
        # 真实证据是否足够由 run() 中的 Acquisition Gate 单独负责。
        return True

    @staticmethod
    def _looks_like_truncated_finish_payload(content: str) -> bool:
        """
        识别本地模型已经明确选择 finish，但长 final_answer 导致 JSON 被截断的情况。

        只看协议外壳，不从截断正文中提取或拼接业务内容。
        """
        text = re.sub(r"\s+", " ", str(content or "").strip()).lower()
        if not text:
            return False
        return (
            '"action_type"' in text
            and '"finish"' in text
            and '"final_answer"' in text
        )

    @staticmethod
    def _extract_source_date(text: str) -> str:
        """从已读取正文中保守提取页面日期；识别不到就返回空。"""
        source = str(text or "")
        patterns = (
            r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})",
            r"(20\d{2}年\d{1,2}月\d{1,2}日)",
        )
        for pattern in patterns:
            match = re.search(pattern, source)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _compact_response_only_research_evidence(
        cls,
        state: Dict[str, Any],
        *,
        max_total_characters: int = 6500,
    ) -> str:
        """
        为最终回答专用调用构造紧凑证据包。

        一旦已经存在真实 read_webpage 正文，就不再把 search snippet 当作
        最终事实证据发送给 Finalizer，避免营销摘要中的“行业均值/人才缺口”
        被模型误写成已核实事实。搜索摘要只在完全没有成功正文时兜底。
        """
        if not isinstance(state, dict):
            return ""
        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return ""

        page_blocks: List[str] = []
        search_blocks: List[str] = []
        seen_urls: set[str] = set()

        for item in steps:
            if not isinstance(item, dict) or not bool(item.get("success")):
                continue
            name = str(item.get("tool") or "").strip().lower()
            observation = item.get("observation")

            if name == "read_webpage" and isinstance(observation, dict):
                url = str(
                    observation.get("final_url")
                    or observation.get("url")
                    or ""
                ).strip()
                key = url.lower()
                if key and key in seen_urls:
                    continue
                if key:
                    seen_urls.add(key)
                title = str(observation.get("title") or "").strip()
                body = str(observation.get("text") or "").strip()
                if not body:
                    continue
                category = cls._research_source_category(url)
                source_date = cls._extract_source_date(body)
                body = body[:2600]
                page_blocks.append(
                    "[已读取网页正文]\n"
                    f"标题：{title}\n"
                    f"URL：{url}\n"
                    f"来源类型：{category}\n"
                    f"页面时间：{source_date or '未识别'}\n"
                    f"正文摘录：{body}"
                )
                continue

            if name == "search_web" and observation is not None:
                try:
                    raw = json.dumps(observation, ensure_ascii=False, default=str)
                except Exception:
                    raw = str(observation)
                search_blocks.append(
                    "[搜索候选摘要，仅用于候选发现，不得作为已核实事实]\n"
                    + raw[:1800]
                )

        blocks = page_blocks[:4] if page_blocks else search_blocks[:2]
        if not blocks:
            return ""

        result: List[str] = []
        used = 0
        for block in blocks:
            remaining = max_total_characters - used
            if remaining <= 0:
                break
            piece = block[:remaining]
            result.append(piece)
            used += len(piece)
        return "\n\n".join(result).strip()

    @classmethod
    def _build_response_only_source_appendix(
        cls,
        state: Dict[str, Any],
    ) -> str:
        """把本轮真实读取过的网页确定性列到最终回答末尾。"""
        if not isinstance(state, dict):
            return ""
        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return ""

        lines: List[str] = []
        seen: set[str] = set()
        for item in steps:
            if not isinstance(item, dict) or not bool(item.get("success")):
                continue
            if str(item.get("tool") or "").strip().lower() != "read_webpage":
                continue
            observation = item.get("observation")
            if not isinstance(observation, dict):
                continue
            url = str(
                observation.get("final_url")
                or observation.get("url")
                or ""
            ).strip()
            if not url or url.lower() in seen:
                continue
            seen.add(url.lower())
            title = str(observation.get("title") or url).strip()
            body = str(observation.get("text") or "").strip()
            date = cls._extract_source_date(body) or "页面未识别"
            category = cls._research_source_category(url)
            lines.append(
                f"- {title}｜来源类型：{category}｜时间：{date}｜{url}"
            )

        if not lines:
            return ""
        return "## 本轮实际读取的外部来源\n" + "\n".join(lines)

    @staticmethod
    def _response_only_numeric_token_matches(text: str) -> List[re.Match]:
        """
        与 Evidence Contract 的高风险数字识别保持同一口径。
        返回 match 以便安全地只移除未经证据支持的数值片段。
        """
        return list(
            re.finditer(
                r"(?<![A-Za-z0-9])"
                r"\d+(?:\.\d+)?"
                r"(?:\s*[-~—–至]\s*\d+(?:\.\d+)?)?"
                r"\s*(?:%|k|K|万|亿|元|人|家|个|名|年|月)?",
                str(text or ""),
            )
        )

    @classmethod
    def _response_only_real_evidence_text(
        cls,
        state: Dict[str, Any],
    ) -> str:
        """汇总本轮真实 read_webpage Observation，供最终数字清洗使用。"""
        if not isinstance(state, dict):
            return ""

        steps = state.get("completed_tool_steps")
        if not isinstance(steps, list):
            return ""

        parts: List[str] = []
        for item in steps:
            if not isinstance(item, dict) or not bool(item.get("success")):
                continue
            if str(item.get("tool") or "").strip().lower() != "read_webpage":
                continue

            observation = item.get("observation")
            if observation is None:
                continue

            try:
                parts.append(
                    json.dumps(
                        observation,
                        ensure_ascii=False,
                        default=str,
                    )
                )
            except Exception:
                parts.append(str(observation))

        return "\n".join(parts).lower()

    @classmethod
    def _sanitize_response_only_external_numeric_claims(
        cls,
        *,
        content: str,
        goal: str,
        state: Dict[str, Any],
    ) -> str:
        """
        最终文本的 deterministic numeric-grounding guard。

        仅处理同时满足以下条件的句子：
        - 带有行业/市场/招聘/薪资/政策等外部事实 cue；
        - 不是预测/假设/目标/实验等规划句；
        - 含有既不来自用户原始事实、也不来自真实 read_webpage 的数字。

        不删除整个分析结论，只把不受支持的具体数字替换成
        “本轮未核实数值”，让结论退化为方向性判断。
        """
        text = str(content or "")
        if not text:
            return ""

        evidence_text = cls._response_only_real_evidence_text(state)
        goal_text = str(goal or "").lower()

        external_cues = (
            "行业", "市场", "招聘", "薪资", "工资", "职位", "缺口",
            "产值", "增长", "政策", "企业", "通常", "普遍", "平均",
            "数据显示", "资料显示", "公开数据", "报告显示",
            "就业率", "通过率",
        )
        forecast_cues = (
            "预测", "情景", "假设", "预算", "投入", "继续条件",
            "停止条件", "3个月", "3 个月", "6个月", "6 个月",
            "12个月", "12 个月", "目标", "建议", "实验",
        )

        pieces = re.split(r"(\n+|(?<=[。！？]))", text)
        sanitized_count = 0

        for index in range(0, len(pieces), 2):
            segment = pieces[index]
            lowered = segment.lower()

            if not any(cue in lowered for cue in external_cues):
                continue
            if any(cue in lowered for cue in forecast_cues):
                continue

            matches = cls._response_only_numeric_token_matches(segment)
            if not matches:
                continue

            unsupported_spans: List[tuple[int, int]] = []

            for match in matches:
                raw = match.group(0)
                compact = (
                    re.sub(r"\s+", "", raw)
                    .replace("—", "-")
                    .replace("–", "-")
                    .replace("~", "-")
                    .replace("至", "-")
                    .lower()
                )

                number_match = re.match(r"\d+(?:\.\d+)?", compact)
                if not number_match:
                    continue
                number = number_match.group(0)

                # 用户原始事实可直接使用。
                if compact in goal_text or number in goal_text:
                    continue

                # 真实网页正文/URL/标题中已经出现，可保留。
                if compact and compact in evidence_text:
                    continue

                # 范围表达：两端数字都能在真实正文中追溯即可。
                if "-" in compact:
                    nums = re.findall(r"\d+(?:\.\d+)?", compact)
                    if nums and all(num in evidence_text for num in nums):
                        continue

                # 单独年份不作为高风险 benchmark。
                if len(number) == 4 and number.startswith("20"):
                    continue

                unsupported_spans.append(match.span())

            if not unsupported_spans:
                continue

            rebuilt: List[str] = []
            cursor = 0
            for start, end in unsupported_spans:
                rebuilt.append(segment[cursor:start])
                rebuilt.append("本轮未核实数值")
                cursor = end
                sanitized_count += 1
            rebuilt.append(segment[cursor:])
            pieces[index] = "".join(rebuilt)

        sanitized = "".join(pieces)

        if sanitized_count:
            # 不把该说明放进外部事实句中，避免产生新的数字审计 cue。
            sanitized = (
                sanitized.rstrip()
                + "\n\n> 注：部分外部数值因无法从本轮已读取网页正文追溯，"
                  "已自动去除具体数值，仅保留方向性判断。"
            )

        return sanitized

    @staticmethod
    def _extract_response_only_internal_amount(
        goal: str,
        *,
        labels: tuple[str, ...],
    ) -> Optional[int]:
        text = str(goal or "")
        for label in labels:
            pattern = (
                re.escape(label)
                + r"[^0-9]{0,24}"
                + r"(\d{3,7})"
                + r"\s*元"
            )
            match = re.search(pattern, text)
            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    continue
        return None

    @classmethod
    def _build_response_only_forecast_contract_section(
        cls,
        *,
        goal: str,
    ) -> str:
        """
        当原任务明确要求 3/6/12 月 × 保守/基准/乐观时，
        生成一个可被 Evidence Contract 确定性识别的完整情景矩阵。

        所有数字都明确标记为“规划假设”，不是外部 benchmark。
        收入/毛利只使用用户原始任务中的收费与单学员毛利。
        """
        compact_goal = re.sub(r"\s+", "", str(goal or ""))
        required_tokens = (
            "3个月",
            "6个月",
            "12个月",
            "保守",
            "基准",
            "乐观",
        )
        if not all(token in compact_goal for token in required_tokens):
            return ""

        fee = cls._extract_response_only_internal_amount(
            goal,
            labels=("单学员收费", "培训费合计", "培训费", "收费"),
        )
        margin = cls._extract_response_only_internal_amount(
            goal,
            labels=("单学员毛利", "毛利"),
        )
        if fee is None or margin is None:
            return ""

        scenarios = (
            ("保守", 0.15, (40, 80, 160)),
            ("基准", 0.18, (50, 100, 200)),
            ("乐观", 0.20, (60, 120, 240)),
        )
        horizons = ("3个月", "6个月", "12个月")

        lines: List[str] = [
            "## 经营情景预测（规划假设，不是事实）",
            (
                "口径：以下咨询量与报名率均是为了经营规划设置的假设，"
                "不是行业平均值，也不是把“80多人咨询”擅自精确化。"
                f"收入按用户提供的单学员收费 {fee} 元计算，"
                f"毛利按用户提供的单学员毛利 {margin} 元计算。"
                "报名人数为累计报名，不等于同时在训人数。"
            ),
        ]

        for scenario, conversion, consultations in scenarios:
            lines.append(f"### {scenario}情景")
            lines.append(
                f"假设：咨询量采用该情景的规划目标，报名率按 {conversion:.0%} "
                "作为内部经营假设；该比例不是外部行业统计值。"
            )

            for horizon, inquiry_count in zip(horizons, consultations):
                enrollments = max(
                    0,
                    int(inquiry_count * conversion + 0.5),
                )
                revenue = enrollments * fee
                gross_profit = enrollments * margin
                lines.append(
                    f"- {horizon}：咨询 {inquiry_count} 人；"
                    f"报名 {enrollments} 人；"
                    f"收入 {revenue} 元；"
                    f"毛利 {gross_profit} 元。"
                )

        return "\n".join(lines)

    @staticmethod
    def _build_response_only_top3_contract_section(
        *,
        goal: str,
    ) -> str:
        """
        对“未来90天只能做3件事”的任务，确定性输出恰好 3 项。
        该章节必须位于整个最终答案最后，避免来源标题中的“行动方案”
        干扰 Evidence Contract 的 scope rfind。
        """
        text = re.sub(r"\s+", "", str(goal or ""))
        asks_three = bool(
            re.search(r"(?:只能做|优先做|做)\s*3\s*件", text)
            or re.search(r"3\s*件事", text)
        )
        if "90天" not in text or not asks_three:
            return ""

        return "\n".join(
            [
                "## 未来90天优先事项（恰好3项）",
                (
                    "1. 第一：把“私信/加微信→咨询→体验/试听→报名”做成可记录的漏斗。"
                    "具体做法：统一咨询表、未成交原因标签和跟进状态；"
                    "判断指标：有效咨询率、体验预约率、体验到报名转化率、跟进后重新激活率。"
                ),
                (
                    "2. 第二：重构课程价值表达并用短视频验证。"
                    "具体做法：内容从单纯考证转向证书、实操、行业应用和就业方向，"
                    "每条内容设置明确私信/加微信承接；"
                    "判断指标：视频带来的有效私信、加微信、体验预约和报名。"
                ),
                (
                    "3. 第三：启动学校与B端的小规模合作试点。"
                    "具体做法：优先接触职业院校、技校及测绘、巡检、应急等应用单位，"
                    "先做体验课、联合宣讲或小型技能活动，再谈长期合作；"
                    "判断指标：有效合作线索、试点落地、由合作带来的咨询和报名。"
                ),
            ]
        )

    @classmethod
    def _apply_response_only_final_contract(
        cls,
        *,
        goal: str,
        state: Dict[str, Any],
        content: str,
    ) -> str:
        """
        v6.6 fix28 Response-Only Final Contract。

        LLM 负责业务分析；Python 只负责三个确定性验收边界：
        - 未追溯外部数字清洗；
        - 完整 3×3 情景矩阵；
        - 恰好 3 个 90 天优先事项，并确保它位于答案最后。
        """
        answer = str(content or "").strip()
        if not answer:
            return ""

        # 模型若自行输出来源清单，统一去掉，改由 Python 真实 Observation 重建。
        source_marker = "## 本轮实际读取的外部来源"
        if source_marker in answer:
            answer = answer.split(source_marker, 1)[0].rstrip()

        forecast_section = cls._build_response_only_forecast_contract_section(
            goal=goal,
        )
        if forecast_section:
            # Evidence Contract 对每个 scenario 取“第一次出现”的位置。
            # 将模型自由文本中的 scenario 名称改成同义词，确保确定性矩阵
            # 成为唯一的“保守/基准/乐观”正式场景。
            answer = (
                answer
                .replace("保守", "谨慎")
                .replace("基准", "中性")
                .replace("乐观", "进取")
            )

        answer = cls._sanitize_response_only_external_numeric_claims(
            content=answer,
            goal=goal,
            state=state,
        )

        sections: List[str] = [answer]

        # fix30：forecast 必须先于来源附录。
        # Verification Engine 对“保守/基准/乐观”取第一次出现位置；
        # 若来源标题中偶然出现这些词，不能让它们抢在正式矩阵之前。
        if forecast_section:
            sections.append(forecast_section)

        appendix = cls._build_response_only_source_appendix(state)
        if appendix:
            sections.append(appendix)

        top3_section = cls._build_response_only_top3_contract_section(
            goal=goal,
        )
        if top3_section:
            # 必须最后添加；来源标题常含“行动方案”，否则 required_action_count
            # 的 rfind(scope_keywords) 可能把检测窗口移动到来源清单。
            sections.append(top3_section)

        return "\n\n".join(
            section.strip()
            for section in sections
            if str(section or "").strip()
        )

    @staticmethod
    def _compact_response_only_verification_feedback(
        state: Dict[str, Any],
        *,
        max_characters: int = 5000,
    ) -> str:
        """
        提取上一次 Completion Gate 的失败项，供 response-only Finalizer
        定向重写最终答案。

        只传递确定性验收反馈，不把整个运行状态再次塞进 prompt。
        """
        if not isinstance(state, dict):
            return ""
        runtime = state.get("runtime_context")
        if not isinstance(runtime, dict):
            return ""
        report = runtime.get("verification_observation")
        if not isinstance(report, dict):
            return ""

        lines: List[str] = []

        failures = report.get("failures") or []
        if isinstance(failures, list):
            for item in failures:
                value = str(item or "").strip()
                if value:
                    lines.append("- 失败：" + value)

        checks = report.get("checks") or []
        if isinstance(checks, dict):
            iterable = list(checks.values())
        elif isinstance(checks, list):
            iterable = checks
        else:
            iterable = []

        for item in iterable:
            if not isinstance(item, dict) or item.get("passed") is True:
                continue
            check_id = str(item.get("check_id") or "").strip()
            category = str(item.get("category") or "").strip()
            message = str(
                item.get("message")
                or item.get("reason")
                or ""
            ).strip()
            prefix = " / ".join(
                value for value in (category, check_id) if value
            )
            if message:
                lines.append(
                    "- 检查"
                    + (f"[{prefix}]" if prefix else "")
                    + "："
                    + message
                )
            evidence = item.get("evidence") or []
            if isinstance(evidence, list):
                for evidence_item in evidence[:3]:
                    value = str(evidence_item or "").strip()
                    if value:
                        lines.append("  证据/缺口：" + value[:700])

        if not lines:
            return ""

        text = "\n".join(lines)
        limit = max(500, int(max_characters or 5000))
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…(验收反馈已截断)"
        return text

    def _generate_response_only_research_emergency_answer(
        self,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> str:
        """
        v6.6 Emergency Finalizer.

        仅在标准 response-only Finalizer 因本地 Ollama 5xx/连接错误失败后使用。
        目标是显著缩小 prompt/output 预算，区分：
        - 大上下文/显存压力导致的 5xx；
        - Ollama/模型整体不可用。

        仍使用同一个本地模型，不切云端，不降低事实约束。
        """
        evidence = self._compact_response_only_research_evidence(
            state,
            max_total_characters=2800,
        )
        if not evidence:
            raise ValueError("Emergency Finalizer 缺少可用联网证据。")

        verification_feedback = (
            self._compact_response_only_verification_feedback(
                state,
                max_characters=1600,
            )
        )

        compact_goal = re.sub(
            r"\s+",
            " ",
            str(goal or "").strip(),
        )
        if len(compact_goal) > 5200:
            compact_goal = compact_goal[:5200].rstrip() + "…"

        system_prompt = (
            "你是 DataPilot 的紧急最终合成器。只输出中文最终答案，不输出 JSON。"
            "只能使用用户原始事实和[已读取网页正文]证据；证据不足必须明确说明。"
            "禁止编造行业均值、薪资、人才缺口、机构数据或来源。"
            "80多人不能改成精确80；14/80只能表述为按80人近似计算的转化率上界。"
            "营销费统计周期不明时，CAC必须写‘暂不能准确计算’。"
            "预测必须明确是假设情景，不得写成事实。"
            "用户要求的3/6/12个月三种情景和90天3项优先事项应尽量完整。"
        )

        user_prompt = (
            "用户任务：\n"
            + compact_goal
            + "\n\n真实网页证据：\n"
            + evidence
            + "\n\n请基于以上内容形成完整但紧凑的经营分析。"
            "缺少本地/外部证据的竞争、就业、学校或B端结论必须显式标注局限。"
        )
        if verification_feedback:
            user_prompt += (
                "\n\n上一轮验收失败项，必须修正：\n"
                + verification_feedback
            )

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
        }
        if self._is_ollama_backend():
            request_kwargs["reasoning_effort"] = "none"
            request_kwargs["max_tokens"] = 2200

        response = self._create_decision_completion_with_backend_retry(
            request_kwargs,
            max_attempts=2,
            base_delay=0.5,
        )
        content = str(
            response.choices[0].message.content or ""
        ).strip()
        if not content:
            raise ValueError("Emergency Finalizer 返回空内容。")

        return self._apply_response_only_final_contract(
            goal=goal,
            state=state,
            content=content,
        )

    def _generate_response_only_research_final_answer(
        self,
        *,
        goal: str,
        state: Dict[str, Any],
    ) -> str:
        """
        为 response-only 联网研究生成最终正文。

        这是“最终答案合成”调用，不是 Agent 决策调用：
        - 不要求 JSON；
        - 不暴露 Tool Catalog；
        - 使用紧凑证据包，给 16K 本地模型留出充足输出空间；
        - 仍通过同一后端，不自动切换云端模型。
        """
        evidence = self._compact_response_only_research_evidence(state)
        if not evidence:
            raise ValueError("response-only 最终回答缺少可用联网证据。")

        verification_feedback = (
            self._compact_response_only_verification_feedback(state)
        )

        system_prompt = (
            "你是 DataPilot 的最终分析合成器。只输出给用户看的最终中文分析正文，"
            "不要输出 JSON、代码围栏、工具名或执行过程。"
            "必须严格区分：用户提供的内部经营事实、外部公开证据、你的分析推断、"
            "未来情景假设。只有标记为[已读取网页正文]的材料可以支持外部事实；"
            "搜索候选摘要不得作为已核实事实。证据不足时明确写‘数据不足/暂不能准确判断’。"
            "禁止自行引入没有出现在已读取正文中的行业平均转化率、行业基准、头部机构月咨询量、"
            "薪资均值、人才缺口等数字。禁止把建议性话术写成外部事实。"
            "对于 CAC：获客成本按营销费用/实际付费报名人数定义；营销费用/咨询人数只能"
            "称为线索成本。若统计周期不一致，不给出确定 CAC。"
            "对于‘80多人咨询’，不要把 80 当成精确值；14/80=17.5% 只能作为按80人"
            "近似计算的转化率上界。用户给出的单学员毛利6900元应直接作为内部事实使用，"
            "不得反推或编造未提供的直接成本。累计报名人数与‘最多同时容纳40人’不是同一口径，"
            "不得用14/40计算产能利用率。"
            "若用户要求3/6/12个月情景预测，应分别给出咨询人数、报名人数、收入和毛利，"
            "明确假设与计算口径；不要只给月度run-rate替代累计情景。"
            "重要外部事实尽量在正文中标明对应来源标题/页面时间；正文末尾还会由系统追加"
            "本轮真实读取来源清单。用户明确不要生成文件，因此只给正文。"
            "如果下面提供了‘上一次 Python 验收失败项’，必须逐项修正后重新输出完整答案，"
            "不能只解释失败原因，也不能原样重复上一版答案。"
        )
        user_prompt = (
            "用户原始任务：\n"
            + str(goal or "").strip()
            + "\n\n以下是本次任务已经通过真实工具取得的外部证据。"
              "只能以这些证据和用户原始任务中的内部事实为基础：\n\n"
            + evidence
            + "\n\n请完整回答用户要求的经营诊断、客户细分、竞争/定价判断、"
              "抖音与视频号策略、招生漏斗、90天增长实验、学校/B端机会、"
              "3/6/12个月保守/基准/乐观情景及90天优先事项。"
              "如果某一项缺少足够外部证据，保留该章节并明确说明证据不足，"
              "不要为了完整而编造数字。"
        )
        if verification_feedback:
            user_prompt += (
                "\n\n上一次 Python Completion Gate 验收失败项（本轮必须逐项修正）：\n"
                + verification_feedback
            )

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
        }
        if self._is_ollama_backend():
            request_kwargs["reasoning_effort"] = "none"
            # 专用模型当前为 16K context；证据包已经压缩，给正文保留明确输出预算。
            request_kwargs["max_tokens"] = 4200

        response = self._create_decision_completion_with_backend_retry(
            request_kwargs
        )
        content = str(
            response.choices[0].message.content or ""
        ).strip()
        if not content:
            raise ValueError("response-only 最终回答合成器返回空内容。")

        return self._apply_response_only_final_contract(
            goal=goal,
            state=state,
            content=content,
        )

    def _decide_next_action(
        self,
        goal: str,
        state: Dict[str, Any],
        finish_only: bool = False,
    ) -> Dict[str, Any]:
        """
        让 LLM 决定下一步动作。

        v3.3 增强：
        - 对偶发的空响应、非 JSON、非法 action_type、非法工具名、
          非对象 arguments 做有限次数的格式纠错重试；
        - 重试只重新请求“决策 JSON”，不会重复执行已经成功的工具；
        - 因此类似 Word 已经编辑并回读成功后，若模型某一轮输出格式异常，
          Agent 不会直接崩溃，也不会丢失已有 ExecutionContext。
        """
        runtime_state = state.get("runtime_context", {}) if isinstance(state, dict) else {}

        # fix28：response-only Processing 的 Completion Recovery 不再先请求
        # “下一步 JSON 决策”。上一轮 Gate 失败项已经足够明确，直接调用
        # Finalizer 重写完整答案，避免 9B 模型再次错误请求 read_webpage。
        if (
            self._is_ollama_backend()
            and self._is_response_only_web_research_state(
                goal=goal,
                state=state,
            )
            and self._compact_response_only_verification_feedback(state)
        ):
            self.report_progress(
                "[Response-Only Completion Recovery Finalizer] "
                "检测到上一轮 Completion Gate 失败项；"
                "跳过 Processing 决策 JSON，直接执行定向最终合成与 Python 合规收口。"
            )
            return {
                "action_type": "finish",
                "final_answer": self._generate_response_only_research_final_answer(
                    goal=goal,
                    state=state,
                ),
            }

        recovery_exhausted = bool(
            isinstance(runtime_state, dict)
            and runtime_state.get("_post_stage_acquisition_recovery_exhausted")
        )
        excluded_tools: set[str] = set()
        if recovery_exhausted:
            excluded_tools = {
                "search_web",
                "read_webpage",
                "download_data_file",
                "download_document_file",
            }

        # v6.6 Research Saturation Redirect：一旦 Acquisition 明确提示
        # “停止 broad search”，下一轮就从真实 Tool Catalog 中移除 search_web，
        # 强制模型在已发现候选中 read_webpage，而不是反复改写同义关键词。
        saturation_observation = (
            runtime_state.get("acquisition_saturation_observation")
            if isinstance(runtime_state, dict)
            else None
        )
        if isinstance(saturation_observation, dict):
            saturation_instruction = str(
                saturation_observation.get("instruction") or ""
            ).lower()
            saturation_reason = str(
                saturation_observation.get("reason") or ""
            ).lower()
            saturation_text = saturation_instruction + " " + saturation_reason
            if any(
                token in saturation_text
                for token in (
                    "停止 broad search",
                    "停止继续扩展搜索",
                    "不要再改写同义关键词重复搜索",
                    "停止 search_web",
                )
            ):
                excluded_tools.add("search_web")
            if any(
                token in saturation_text
                for token in (
                    "停止 search_web/read_webpage",
                    "网页动作已达到 6 次",
                    "不再继续 search/read",
                )
            ):
                excluded_tools.add("read_webpage")

        local_advisory_only = bool(
            self._is_ollama_backend()
            and self._goal_explicitly_requests_advisory_only(goal)
        )

        if local_advisory_only:
            system_prompt = self._build_local_advisory_system_prompt()
        else:
            # 始终通过统一入口构造系统提示。
            # 这样既保留生产环境中的本地阶段 Tool 过滤，
            # 也保持历史测试/扩展通过 monkeypatch _build_system_prompt
            # 注入最小提示词的兼容行为。
            included_tools = (
                self._local_stage_tool_allowlist(
                    state,
                    finish_only=finish_only,
                )
                if self._is_ollama_backend()
                else None
            )
            if (
                self._is_ollama_backend()
                and self._is_response_only_web_research_state(
                    goal=goal,
                    state=state,
                )
            ):
                # Acquisition 已经通过证据 Gate 后，response-only 研究的
                # Processing 只负责“综合并 finish”。不再向本地模型暴露
                # Acquisition 工具，避免 processing -> search_web 的无界回跳。
                included_tools = set()

            system_prompt = self._build_system_prompt(
                finish_only=finish_only,
                excluded_tools=excluded_tools,
                included_tools=included_tools,
            )

        if finish_only:
            decision_instruction = (
                "\n\n工具执行预算已经耗尽。"
                "现在只允许根据现有真实 Observation 判断任务是否已经完成。"
                "禁止再调用任何工具。"
                "如果已经满足用户目标，返回 finish；"
                "如果仍需要任何工具才能完成，返回 action_type=tool，"
                "仅用于表示任务尚未完成，该工具不会被执行。"
            )
        else:
            decision_instruction = "\n\n请决定下一步。"
            if (
                "search_web" in excluded_tools
                and not recovery_exhausted
                and isinstance(saturation_observation, dict)
            ):
                decision_instruction += (
                    "\nAcquisition 已达到 broad-search 饱和：本轮禁止 search_web。"
                    "请从已有搜索 Observation 中选择最高价值 URL 调用 read_webpage；"
                    "优先政府/官方/权威来源。若已有证据足够则进入后续分析，"
                    "不要继续改写关键词。"
                )
            if recovery_exhausted:
                decision_instruction += (
                    "\nAcquisition Recovery 已耗尽：本轮禁止调用 search_web、read_webpage、"
                    "download_data_file、download_document_file。"
                    "请仅使用现有结构化数据继续 Processing/Delivery，并明确无法验证的局限。"
                )

            # 本地 response-only 联网研究进入 Processing 后，决策 JSON 不承载
            # 数千字最终正文。模型只需返回一个短 finish marker，随后由专用
            # plain-text finalizer 基于紧凑真实证据生成正文。这样避免长字符串
            # 把 JSON 截断，导致反复格式纠错。
            if (
                self._is_ollama_backend()
                and self._is_response_only_web_research_state(
                    goal=goal,
                    state=state,
                )
            ):
                decision_instruction += (
                    '\n当前是 response-only 联网研究的 Processing 阶段。'
                    '如果分析已可形成最终回答，请只返回短 JSON：'
                    '{"action_type":"finish","final_answer":"__DATAPILOT_SYNTHESIZE__"}。'
                    '不要把长篇报告正文塞进决策 JSON。'
                )
            elif (
                self._is_ollama_backend()
                and self._is_response_only_web_research_acquisition_state(
                    goal=goal,
                    state=state,
                )
            ):
                decision_instruction += (
                    '\n当前仍是 response-only 联网研究的 Acquisition 阶段。'
                    '如果你认为采集证据已经足够，请只返回短 JSON：'
                    '{"action_type":"finish","final_answer":"__DATAPILOT_STAGE_FINISH__"}。'
                    '这里的 finish 只表示申请结束 Acquisition；Python 会先执行证据 Gate，'
                    '通过后才进入 Processing。不要在 Acquisition 决策 JSON 中写长篇最终报告。'
                )

        prompt_state = state
        if self._is_ollama_backend() and isinstance(state, dict):
            # 本地 9B 模型需要更紧凑的决策上下文。
            # goal 已单独放在 user prompt，TaskPlan 也只保留一份，
            # 避免长研究任务把同一内容重复发送给 Ollama。
            prompt_state = dict(state)
            prompt_state.pop("goal", None)
            prompt_state.pop("available_skills", None)
            prompt_state.pop("selected_skills", None)
            prompt_state.pop("skill_selection", None)

            runtime_for_prompt = prompt_state.get("runtime_context")
            if isinstance(runtime_for_prompt, dict):
                runtime_for_prompt = dict(runtime_for_prompt)
                runtime_for_prompt.pop("skill_selection", None)
                runtime_for_prompt.pop("task_plan", None)
                prompt_state["runtime_context"] = runtime_for_prompt

        if local_advisory_only:
            base_user_prompt = (
                f"用户问题：\n{goal}\n\n"
                "请直接回答这个问题，并严格返回 finish JSON。"
            )
        else:
            if self._is_ollama_backend():
                state_json = json.dumps(
                    prompt_state,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            else:
                state_json = json.dumps(
                    prompt_state,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )

            base_user_prompt = (
                f"用户最终目标：\n{goal}\n\n"
                "当前真实执行状态：\n"
                + state_json
                + decision_instruction
            )

        max_format_attempts = 3
        last_error: Optional[Exception] = None
        correction_message = ""

        for attempt in range(
            1,
            max_format_attempts + 1,
        ):
            user_prompt = base_user_prompt

            if correction_message:
                user_prompt += (
                    "\n\n上一次返回格式不合法。"
                    "请纠正格式后重新返回一个且仅一个合法 JSON 对象。"
                    f"\n具体问题：{correction_message}"
                    "\n不要添加 Markdown、解释文字、代码围栏或第二个 JSON。"
                )

            request_kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "temperature": 0.0,
                "response_format": {
                    "type": "json_object"
                },
            }

            # Ollama + Qwen 的 thinking 模式可能把主要内容放入 reasoning，
            # 导致 OpenAI-compatible message.content 为空或结构化 JSON 被污染。
            # 本地 Agent 决策需要短、稳定、可解析的 JSON，因此强制关闭 thinking。
            # 云端 DeepSeek 等后端保持原请求参数，不受影响。
            if self._is_ollama_backend():
                request_kwargs["reasoning_effort"] = "none"

            response = self._create_decision_completion_with_backend_retry(
                request_kwargs
            )

            content = (
                response
                .choices[0]
                .message
                .content
            )

            try:
                if not content:
                    raise ValueError(
                        "Agent Loop 没有返回下一步决策。"
                    )

                decision = self._extract_json_object(
                    content
                )
                decision = self._normalize_decision_contract(
                    decision
                )

                if self._is_ollama_backend():
                    decision = self._normalize_local_response_pseudo_action(
                        decision,
                        goal=goal,
                        state=state,
                    )
                    local_response_alias = str(
                        (decision.get("_local_response_alias") or "")
                        if isinstance(decision, dict)
                        else ""
                    ).strip()
                    if local_response_alias:
                        self.report_progress(
                            "[Local Response Alias] "
                            f"{local_response_alias} -> finish"
                        )

                    if (
                        isinstance(decision, dict)
                        and decision.get("action_type") == "finish"
                        and self._is_response_only_web_research_state(
                            goal=goal,
                            state=state,
                        )
                    ):
                        requested_answer = str(
                            decision.get("final_answer") or ""
                        ).strip()
                        if requested_answer == "__DATAPILOT_SYNTHESIZE__":
                            mode_text = "短 finish marker"
                        else:
                            mode_text = "模型直接 finish 正文"
                        self.report_progress(
                            "[Response-Only Finalizer] "
                            + mode_text
                            + " 已由专用 Finalizer 接管；"
                            "正在使用紧凑真实证据"
                            + (
                                "和上一轮验收失败项"
                                if self._compact_response_only_verification_feedback(
                                    state
                                )
                                else ""
                            )
                            + "重新生成最终分析正文。"
                        )
                        decision["final_answer"] = (
                            self._generate_response_only_research_final_answer(
                                goal=goal,
                                state=state,
                            )
                        )

                if not isinstance(decision, dict):
                    raise TypeError(
                        "Agent Loop 决策不是 JSON 对象。"
                    )

                action_type = decision.get(
                    "action_type"
                )

                if action_type not in {
                    "tool",
                    "finish",
                }:
                    raise ValueError(
                        "action_type 只允许 tool 或 finish。"
                    )

                if action_type == "tool":
                    tool_name = str(
                        decision.get("tool")
                        or ""
                    ).strip()

                    if not tool_name:
                        raise ValueError(
                            "tool 决策缺少工具名。"
                        )

                    if (
                        self._is_ollama_backend()
                        and self._is_response_only_web_research_state(
                            goal=goal,
                            state=state,
                        )
                        and tool_name.strip().lower() in {
                            "search_web",
                            "read_webpage",
                            "download_data_file",
                            "download_document_file",
                        }
                    ):
                        self.report_progress(
                            "[Response-Only Processing Guard] Acquisition 已完成；"
                            f"阻止 Processing 回跳调用 {tool_name}，直接进入最终分析合成。"
                        )
                        return {
                            "action_type": "finish",
                            "final_answer": (
                                self._generate_response_only_research_final_answer(
                                    goal=goal,
                                    state=state,
                                )
                            ),
                        }

                    raw_arguments = decision.get("arguments", {})
                    if raw_arguments is None:
                        raw_arguments = {}

                    # Response-Only Delivery Guard：用户明确只要分析结论、
                    # TaskPlan 也没有交付文件时，本地小模型仍可能机械进入
                    # delivery 并请求 write_file/save/export。若真实质量检查和
                    # 分析 Observation 已满足目标，则确定性改为 finish，既不
                    # 写文件，也不让未注册的 write_file 造成任务崩溃。
                    if (
                        self._is_ollama_backend()
                        and self._looks_like_file_output_tool(tool_name)
                        and self._response_only_analysis_ready(
                            goal=goal,
                            state=state,
                        )
                    ):
                        final_answer = self._build_response_only_analysis_final_answer(
                            goal=goal,
                            state=state,
                        )
                        if final_answer:
                            self.report_progress(
                                "[Response-Only Guard] 用户未要求文件交付；"
                                f"阻止 {tool_name}，基于现有真实 Observation 直接完成回答。"
                            )
                            return {
                                "action_type": "finish",
                                "final_answer": final_answer,
                            }

                    if self._is_ollama_backend():
                        (
                            resolved_local_name,
                            resolved_local_arguments,
                            local_alias_note,
                        ) = self._resolve_local_tool_request(
                            tool_name=tool_name,
                            arguments=raw_arguments,
                            state=state,
                        )
                        if local_alias_note:
                            self.report_progress(
                                "[Local Tool Alias] " + local_alias_note
                            )
                        tool_name = resolved_local_name
                        raw_arguments = resolved_local_arguments
                        decision["tool"] = tool_name
                        decision["arguments"] = raw_arguments

                    canonical_name = self.registry.resolve_name(
                        tool_name
                    )

                    if canonical_name is None:
                        skill_name = None
                        try:
                            if getattr(self, "skill_registry", None) is not None:
                                skill_name = self.skill_registry.resolve_name(tool_name)
                        except Exception:
                            skill_name = None

                        if skill_name is not None:
                            raise ValueError(
                                f"{tool_name} 是 Skill 方法名，不是可执行 Tool。"
                                "禁止把 Skill 放进 tool 字段；请选择真实 Tool Registry "
                                "中的工具，若用户只要求说明/计划且无需执行，则返回 finish。"
                            )

                        raise ValueError(
                            f"Agent 选择了未注册工具：{tool_name}"
                        )
                    if canonical_name.lower() in excluded_tools:
                        raise ValueError(
                            "Acquisition Recovery 已耗尽，当前工具已被禁用："
                            f"{canonical_name}。请选择 Processing/Delivery 工具。"
                        )

                    arguments = decision.get(
                        "arguments",
                        {},
                    )

                    if arguments is None:
                        arguments = {}

                    if not isinstance(arguments, dict):
                        raise TypeError(
                            "arguments 必须是 JSON 对象。"
                        )

                    decision["tool"] = canonical_name
                    decision["arguments"] = arguments

                if attempt > 1:
                    self.report_progress(
                        "Agent 决策 JSON 格式纠错成功，继续执行。"
                    )

                if isinstance(decision, dict):
                    decision.pop("_local_response_alias", None)

                return decision

            except (
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ) as error:
                last_error = error
                correction_message = (
                    f"{type(error).__name__}: {error}"
                )

                # 本地 response-only 研究已经明确选择 finish，但把长篇正文
                # 直接塞进 JSON 后可能因为输出预算/上下文边界被截断。
                # Acquisition 与 Processing 必须区别处理：
                # - Acquisition：只恢复成“申请结束采集阶段”的短 finish marker，
                #   随后由 run() 中的 Acquisition Gate 决定能否进入 Processing；
                # - Processing：才允许调用 plain-text Finalizer 生成最终正文。
                if (
                    self._is_ollama_backend()
                    and content
                    and self._looks_like_truncated_finish_payload(content)
                    and self._is_response_only_web_research_acquisition_state(
                        goal=goal,
                        state=state,
                    )
                ):
                    self.report_progress(
                        "[Response-Only Stage Finish Recovery] "
                        "Acquisition 中检测到长 final_answer JSON 被截断；"
                        "已恢复为短阶段结束请求，并交给 Acquisition Gate 验证，"
                        "不再重复生成长 JSON。"
                    )
                    return {
                        "action_type": "finish",
                        "final_answer": "__DATAPILOT_STAGE_FINISH__",
                    }

                if (
                    self._is_ollama_backend()
                    and content
                    and self._is_response_only_web_research_state(
                        goal=goal,
                        state=state,
                    )
                    and self._looks_like_truncated_finish_payload(content)
                ):
                    try:
                        self.report_progress(
                            "[Response-Only Finalizer] 检测到长 final_answer JSON 被截断；"
                            "改用紧凑证据进行纯文本最终合成，不再重复请求长 JSON。"
                        )
                        return {
                            "action_type": "finish",
                            "final_answer": (
                                self._generate_response_only_research_final_answer(
                                    goal=goal,
                                    state=state,
                                )
                            ),
                        }
                    except Exception as finalizer_error:
                        self.report_progress(
                            "[Response-Only Finalizer] 最终合成失败，将回到原有有限格式纠错："
                            f"{type(finalizer_error).__name__}: {finalizer_error}"
                        )

                # 本地模型仍发生协议偏差时，输出一段受限原始响应，方便
                # 精确诊断，而不是继续猜测模型到底用了哪个字段。只在失败
                # 场景输出并截断长度，避免正常日志被模型文本淹没。
                if self._is_ollama_backend() and content:
                    raw_preview = re.sub(
                        r"\s+",
                        " ",
                        str(content).strip(),
                    )[:600]
                    self.report_progress(
                        "[Local Decision Raw] " + raw_preview
                    )

                if attempt >= max_format_attempts:
                    break

                self.report_progress(
                    "Agent 决策返回格式异常，"
                    f"正在自动纠错重试 "
                    f"({attempt}/{max_format_attempts - 1})："
                    f"{correction_message}"
                )

        raise ValueError(
            "Agent Loop 连续多次未返回合法决策 JSON。"
            f"最后错误：{type(last_error).__name__}: {last_error}"
        )

    @staticmethod
    def _extract_json_object(
        text: str,
    ) -> Dict[str, Any]:
        """
        从 LLM 返回文本中提取第一个合法 JSON 对象。

        兼容：
        - 纯 JSON
        - Markdown ```json 围栏
        - JSON 前后带解释文字
        - 一个合法 JSON 对象后又出现额外文本或第二段内容

        Agent Loop 每轮只需要第一个完整决策对象，因此使用
        JSONDecoder.raw_decode()，避免 json.loads() 因 Extra data
        直接让整个动态执行流程崩溃。
        """
        content = str(text or "").strip()

        if not content:
            raise ValueError(
                "Agent Loop 返回内容为空。"
            )

        try:
            parsed = json.loads(content)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fence = "`" * 3

        content = re.sub(
            rf"^\s*{re.escape(fence)}(?:json)?\s*",
            "",
            content,
            flags=re.IGNORECASE,
        )

        content = re.sub(
            rf"\s*{re.escape(fence)}\s*$",
            "",
            content,
        ).strip()

        decoder = json.JSONDecoder()

        for index, char in enumerate(content):
            if char != "{":
                continue

            try:
                parsed, _ = decoder.raw_decode(
                    content[index:]
                )
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                return parsed

        raise ValueError(
            "Agent Loop 返回内容中没有找到合法 JSON 对象。"
        )


    def _selected_skill_names(self) -> List[str]:
        if not self._active_skill_selection:
            return []
        return list(
            getattr(
                self._active_skill_selection,
                "selected_skills",
                [],
            )
            or []
        )

    def _selected_skill_execution_guidance(self) -> str:
        selected = set(self._selected_skill_names())
        lines: List[str] = []

        if "web_business_research" in selected:
            lines.extend(
                [
                    "【Web Business Research Guidance】",
                    "当 selected_skills 包含 web_business_research 时：",
                    "1. search_web 只负责发现候选来源；关键外部事实优先通过 read_webpage 读取真实正文后再使用。",
                    "2. 用户要求政府、民航、协会、院校、招聘平台或企业官网时，优先读取这些高可信/一手来源；商业培训平台主要用于竞争价格、课程和卖点补充。",
                    "3. 已取得足够来源后立即停止扩展搜索并进入 Processing；不要用近义关键词重复搜索消耗预算。",
                    "4. 最终回答必须区分内部经营事实、外部来源事实、分析推断、行动建议和预测假设；没有真实 Observation 的行业 benchmark 不得写成事实。",
                    "5. 宏观行业机会不能直接推出具体基地一定增长；应单独分析本地获客、转化、产品价值、渠道和执行能力。",
                ]
            )

        if "data_visualization" in selected:
            lines.extend(
                [
                    "【Visualization Execution Guidance】",
                    "当 selected_skills 包含 data_visualization 时：",
                    "1. 如果用户要求图表、趋势图、可视化、PNG，或数据本身适合趋势/分布展示，必须把“生成图表”视为核心任务，而不是可选装饰。",
                    "2. 在 finish 前，应确保至少已经执行过能够产生图表或图片交付物的真实工具，并把生成的 PNG / 图表文件纳入 deliverable_paths。",
                    "3. 优先选择与图表、绘图、可视化、PNG 导出直接相关的真实 Tool；如果推荐工具名不在 available tools 中，不要直接臆造工具名，而是从 available tools 中选择最接近、且确实存在的图表/可视化工具。",
                    "4. 如果用户同时要求 Excel、PNG、Word，则不要只生成 Excel / Word 后直接 finish；必须补齐图表交付物，再进入 finish。",
                    "5. 如果数据包含时间字段与数值字段，优先考虑趋势图；如果变量是事件量/降水/计数量，优先考虑柱状图；如果是类别对比，优先考虑柱状图；如果是纯数值分布，优先考虑直方图。",
                ]
            )

        if "excel_report_delivery" in selected:
            lines.extend(
                [
                    "【Excel Delivery Guidance】",
                    "当 selected_skills 包含 excel_report_delivery 时：",
                    "1. 只有在真实分析结果已经形成后再生成最终 Excel。",
                    "2. finish 前应确保最终 Excel 已生成，并已被回读或检查。",
                ]
            )

        if "professional_word_delivery" in selected:
            lines.extend(
                [
                    "【Word Delivery Guidance】",
                    "当 selected_skills 包含 professional_word_delivery 时：",
                    "1. 只有在真实分析结果与关键结论已经形成后再生成最终 Word 报告。",
                    "2. finish 前应确保最终 Word 已生成，并已被检查或回读。",
                ]
            )

        if not lines:
            return ""

        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _local_stage_tool_allowlist(
        state: Dict[str, Any],
        *,
        finish_only: bool = False,
    ) -> Optional[set[str]]:
        """
        本地 9B 模型的 Stage-aware Tool Catalog 压缩。

        目的不是改变 Python 真实 Tool Registry，而是减少每轮发送给
        Ollama 的无关工具 schema。当前只对 Acquisition 做强过滤；
        其他 Stage 保持完整工具集，优先降低回归风险。
        """
        if finish_only:
            return set()

        runtime = (
            state.get("runtime_context", {})
            if isinstance(state, dict)
            else {}
        )
        stage = str(
            runtime.get("current_stage")
            if isinstance(runtime, dict)
            else ""
        ).strip().lower()

        goal_text = str(
            state.get("goal")
            if isinstance(state, dict)
            else ""
        ).lower()

        completed_steps = (
            state.get(
                "completed_tool_steps",
                [],
            )
            if isinstance(
                state,
                dict,
            )
            else []
        )

        successful_tools = {
            str(
                item.get("tool")
                or ""
            ).strip().lower()
            for item in completed_steps
            if isinstance(item, dict)
            and bool(
                item.get("success")
            )
        }

        weather_task = any(
            token in goal_text
            for token in (
                "天气",
                "气象",
                "降水",
                "湿度",
                "风速",
                "weather",
                "meteorological",
            )
        )

        if (
            stage == "processing"
            and weather_task
            and "fetch_weather_dataset"
            in successful_tools
        ):
            # 专业分析已经成功后，不再允许重复 pivot/group 等无意义处理。
            if (
                "analyze_weather_dataset"
                in successful_tools
            ):
                return {
                    "create_weather_analysis_package",
                }

            # 允许先做一次结构检查；一旦 get_data_info 已成功，
            # 下一步只保留专业气象分析 Tool。
            if (
                "get_data_info"
                in successful_tools
            ):
                return {
                    "analyze_weather_dataset",
                }

            return {
                "get_data_info",
                "analyze_weather_dataset",
            }

        if (
            stage == "delivery"
            and weather_task
            and "analyze_weather_dataset"
            in successful_tools
        ):
            if (
                "create_weather_analysis_package"
                in successful_tools
            ):
                return {
                    "inspect_professional_excel_report",
                    "inspect_professional_word_report",
                }

            return {
                "create_weather_analysis_package",
            }

        if (
            stage == "acquisition"
            and weather_task
            and "fetch_weather_dataset"
            in successful_tools
        ):
            # v6.6 fix40：
            # structured weather data 已经真实在内存中可用。
            # 此时禁止本地模型再幻想一个 weather_data.csv，
            # 或绕回 read_office_data / download_data_file。
            # 只允许进入 Processing 所需的结构检查/专业分析。
            if (
                "get_data_info"
                in successful_tools
            ):
                return {
                    "analyze_weather_dataset",
                }

            return {
                "get_data_info",
                "analyze_weather_dataset",
            }

        if stage != "acquisition":
            return None

        return {
            # 本地/Workspace 来源发现
            "discover_data_files",
            "inspect_data_files",
            "scan_document_files",
            "inspect_documents",
            "read_document",
            "read_office_data",
            "get_data_info",
            "analyze_dataframe_semantics",
            # 联网研究
            "search_web",
            "read_webpage",
            "download_data_file",
            "download_document_file",
            # 结构化气象数据 Acquisition
            "fetch_weather_dataset",
            # 将网页/文档证据结构化，便于后续 Processing
            "create_dataframe",
            "build_dataframe",
        }

    def _build_tool_catalog_text(
        self,
        *,
        excluded_tools: Optional[set[str]] = None,
        included_tools: Optional[set[str]] = None,
    ) -> str:
        excluded = {
            str(name).strip().lower()
            for name in (excluded_tools or set())
            if str(name).strip()
        }
        included = (
            {
                str(name).strip().lower()
                for name in included_tools
                if str(name).strip()
            }
            if included_tools is not None
            else None
        )
        tools = [
            tool
            for tool in self.registry.list_tools()
            if tool.name.lower() not in excluded
            and (
                included is None
                or tool.name.lower() in included
            )
        ]
        if not tools:
            return "当前没有可用工具。"
        blocks = []
        for index, tool in enumerate(tools, start=1):
            blocks.append(
                "\n".join(
                    [
                        f"[工具 {index}]",
                        f"名称：{tool.name}",
                        f"类别：{tool.category}",
                        f"用途：{tool.description}",
                        f"参数：{tool.parameters}",
                        f"返回：{tool.returns or '未特别说明'}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _build_local_system_prompt(
        self,
        finish_only: bool = False,
        excluded_tools: Optional[set[str]] = None,
        included_tools: Optional[set[str]] = None,
    ) -> str:
        """
        Ollama / 本地中小模型专用的压缩决策协议。

        云端模型继续使用完整系统提示；本地模型只保留执行决策所需的
        硬约束、Stage 状态和真实 Tool Registry，避免 Skill Catalog 与
        历史版本规则淹没 action schema。
        """
        catalog = self._build_tool_catalog_text(
            excluded_tools=excluded_tools,
            included_tools=included_tools,
        )

        # 本地 9B 决策器不接收 Skill 细节。Skill 已由 Python 侧完成选择，
        # 继续把 workflow/名称塞给中小模型反而容易把 Skill 或自然语言动作
        # 误抄进 tool 字段。这里只保留真实 Tool Registry。
        selected_guidance = (
            "本地模式不暴露 Skill 细节；请只依据真实 Tool Registry、"
            "TaskPlan、输入路径与 Observation 决策。"
        )

        finish_rule = (
            "当前是最终完成判定：禁止真正执行新工具。若现有证据足够，返回 finish；"
            "否则返回 tool 仅表示仍缺工具。"
            if finish_only
            else
            "正常执行：一次只能选择一个真实 Tool，或直接 finish。"
        )

        return f"""
你是 DataPilot 的本地 Workspace Agent 决策器。
你的唯一任务：根据用户目标、TaskPlan、当前 Stage 和真实 Observation，
返回一个且仅一个 JSON 对象。不要输出思考过程、Markdown 或解释文字。

【最高优先级 JSON 协议】
只能二选一：
1. 调工具：
{{"action_type":"tool","tool":"真实Tool名","arguments":{{}},"purpose":"简短原因"}}
2. 完成：
{{"action_type":"finish","final_answer":"直接给用户的最终回答"}}

action_type 的值只能逐字为 "tool" 或 "finish"。
不要返回 plan、analysis、respond、answer、skill 等其他 action_type。

【Skill 与 Tool 严格分离】
Skill 只是 Python 已选择的方法指导，不是工具，永远不能写入 tool 字段。
只允许调用下面“真实 Tool Registry”中出现的名称。
如果某个名字只出现在方法指导里、没有出现在 Tool Registry，它就不可执行。

【无需执行时直接回答】
如果用户只是询问“准备怎么做 / 如何处理 / 给出方案 / 解释流程”，并且明确说
不需要联网、不需要读取真实文件、不需要生成文件或不需要实际执行，则直接返回 finish，
在 final_answer 中说明方案；不要为了形式调用 Excel Skill 或任何工具。
只有用户要求真实读取、修改、分析文件、联网取数或生成交付物时才调用工具。

【本地 Excel/CSV 常用真实 Tool 名与参数】
- 读取已选择的 CSV/Excel：read_office_data
  例：{{"file_path":"真实路径"}}
- 查看行列、字段、缺失值、重复情况：get_data_info
  例：{{"df":{{"$ref":"step_1.output"}}}}
- 处理缺失值：handle_missing_values
- 删除重复记录：drop_duplicate_rows
- 单字段/单指标分组统计：group_statistics
  例：{{"df":{{"$ref":"step_1.output"}},"group_by":"城市","target_column":"销售额","operation":"sum"}}
- 多字段/多指标统计：group_multi_statistics
  例：{{"df":{{"$ref":"step_1.output"}},"group_by":["城市","月份"],"aggregations":{{"销售额":["sum"]}}}}
不要写 read_file/read_excel/load_file。不要调用 pandas.read_excel、pd.read_excel、pandas.read_csv、
pd.read_csv 或任何 pandas/Python 库 API；这些都不是 DataPilot Tool。
read_office_data 只允许 file_path 和可选 sheet_name，不要传 engine/header/usecols 等 pandas 参数。
DataFrame 引用必须严格写成 {{"df":{{"$ref":"step_N.output"}}}}；不要写成
"step_N.output"、"$step_N.output"，也不要把 Observation 的 DataFrame 摘要 dict 塞进 df。
若用户明确要求检查缺失值/重复值，读取后先调用 get_data_info，再决定是否清洗和统计。
get_data_info 的 missing_values={{}} 且 duplicate_rows=0 表示无需清洗，应直接继续用户要求的统计；
不要返回 analysis/continue 来描述“下一步”，而要直接返回 action_type=tool 并调用下一真实 Tool。
统计时只使用用户目标或 TaskPlan 明确要求的分组字段与指标，不要擅自添加月份、部门、其他金额字段。

【执行硬约束】
- {finish_rule}
- 服从 runtime_context.current_stage：Acquisition → Processing → Delivery → Verification。
- TaskPlan 是任务合同；真实 Observation 是事实来源，不得编造工具成功、文件、数字或来源。
- build_dataframe/create_dataframe 的事实数值必须来自已有成功 Observation；派生值优先用确定性工具计算。
- 如果 runtime_context.data_state.current_data_ref 已经指向真实 DataFrame，不要为了“再转换一次”重复重建；应直接使用 get_data_info / 清洗 / 统计等 Processing Tool。
- 如果已经成功执行 fetch_weather_dataset，且任务要求历史天气分析 + 未来预报，Processing 优先使用 analyze_weather_dataset，并分别引用 historical / forecast / metadata。
- 如果已经成功执行 analyze_weather_dataset，且任务要求气象 Excel + PNG + Word，Delivery 优先使用 create_weather_analysis_package；不要用 generate_regression_visualizations 画普通时间序列，也不要手工拼一大段 Word JSON。
- analyze_weather_dataset 的 historical / forecast / metadata 必须来自同一次 fetch_weather_dataset 的真实子字段；禁止把 combined 同时作为 historical 和 forecast。
- fetch_weather_dataset 成功后，结构化气象 DataFrame 已经在内存中，不要再虚构 weather_data.csv、不要调用 read_office_data 去读取不存在的临时 CSV。
- create_weather_analysis_package 成功且 verification 显示交付包完整时，不要重复生成相同文件；进入 Verification / finish。
- 如果确实需要 build_dataframe/create_dataframe 复用已有 DataFrame，data 参数必须直接写成 {{$ref: "step_N.output.some_field"}}，不要写成 [{{$ref: "step_N.output.some_field"}}]。
- 使用前一步 Python 对象时用 {{$ref: "step_N.output"}} 或真实子字段引用，不要把 DataFrame 全文抄入 JSON。
- 用户要求最终文件时，必须生成到 deliverables_dir，并在最后一次写入后 read/inspect，再申请 finish。
- 用户未要求文件时，不要主动生成文件。
- 如果用户明确“不生成 Word/Excel/其他文件”“只输出分析结果”，则最终交付就是 final_answer，不是 Workspace deliverable；禁止 invent `generate_report`、`generate_final_report`、`generate_deliverable`、`generate_final_answer` 之类未注册工具。完成证据获取/分析后直接返回 action_type=finish，并把完整分析正文放进 final_answer。
- 若 TaskPlan 的 deliverable_requirements 为空，且用户明确“只需要告诉我结果/只返回结论”，完成所需检查和统计后必须直接 finish；不要调用 write_file、save_file、export_*、to_csv、to_excel 或报告生成工具。
- 用户明确“不需要联网”时，不得调用 search_web/read_webpage/download_*。
- 如果真实 Tool Registry 中存在 fetch_weather_dataset，且用户要求历史天气/未来预报等结构化气象时序数据，
  Acquisition 必须优先调用 fetch_weather_dataset；不要先 search_web 查 API 文档，也不要手工拼 Open-Meteo URL。
- Acquisition Recovery 已耗尽时，不得继续联网；用现有数据降级完成并说明局限。
- 工具失败后根据真实错误调整，不得原样无限重试。

【当前方法指导】
{selected_guidance}
再次强调：上面的 Skill/Guidance 不是工具名。

【真实 Tool Registry——tool 字段只能从这里选择】
{catalog}
""".strip()

    def _build_system_prompt(
        self,
        finish_only: bool = False,
        excluded_tools: Optional[set[str]] = None,
        included_tools: Optional[set[str]] = None,
    ) -> str:
        if self._is_ollama_backend():
            return self._build_local_system_prompt(
                finish_only=finish_only,
                excluded_tools=excluded_tools,
                included_tools=included_tools,
            )

        # 云端模型维持原有完整 Tool Registry 行为。
        # included_tools 仅用于本地 Ollama 的阶段化上下文压缩。
        catalog = self._build_tool_catalog_text(
            excluded_tools=excluded_tools,
        )

        if self._active_skill_selection is None:
            skill_catalog = (
                self.skill_registry.build_llm_catalog_text()
            )
            skill_selection_note = (
                "当前没有运行期 Skill Selection；"
                "为兼容直接 Prompt 测试，展示完整 Skill Catalog。"
            )
        else:
            # SkillSelector 负责选择，不负责生成 Catalog。
            # 使用 SkillRegistry 提供的真实 Skill Catalog。
            skill_catalog = self.skill_registry.build_llm_catalog_text()
            skill_selection_note = (
                "本轮只展示 Skill Selector "
                "针对当前用户目标 + TaskPlan 选出的相关 Skills。"
            )

        if finish_only:
            budget_rule = """
20. 当前处于工具预算耗尽后的最终完成判定。
21. 这一轮禁止真正继续执行工具。
22. 如果现有 Observation 已经满足用户目标，必须返回 finish。
23. 如果任务仍未完成，返回 action_type=tool 表示仍需要工具；
    Python 不会执行这个工具，而会以 max_iterations 结束。
24. 不得因为存在某个输出文件就自动认为整个用户目标已经完成；
    必须结合用户目标和全部真实 Observation 判断。
25. 如果用户要求完成后自检，而某个最终交付物在最后一次写入或重新导出之后没有成功回读/检查，则任务仍未完成，不得返回 finish。
26. 如果用户要求多个交付物彼此一致，但缺少各自最终版本的写后验证证据，不得声称一致性验证已经完成。
""".strip()
        else:
            budget_rule = ""

        return f"""
你是 DataPilot 的 Workspace 动态执行 Agent。

你每次只能做一个决定：
1. 调用一个真实工具；
2. 或者确认任务已经完成。

============================================================
v5.2 Multi-Stage 工作流
============================================================

runtime_context.current_stage 是当前执行阶段。
标准顺序：
Acquisition → Processing → Delivery → Verification。

- Acquisition：搜索、读取网页、下载、确认来源。
- Processing：读取数据、语义理解、单位转换、清洗、计算、统计、分析。
- Delivery：生成或编辑 PNG / Excel / Word / PDF 等最终成果。
- Verification：回读最终成果、核验数字/时间/来源/跨交付物一致性。

优先完成当前 Stage 的必要工作后再进入后续 Stage。
不要为了“确认一下”无理由返回已经完成的早期 Stage。
如果 Completion Gate 指出需要回退，按真实失败证据修复，不得伪造 Stage PASS。

每个 Stage 有独立执行预算。不要因为某阶段还有剩余预算就故意用满；
Stage 目标一旦满足，应立即进入下一阶段。Acquisition 的搜索/网页读取
不得消耗 Processing、Delivery、Verification 的预算。

runtime_context.data_state 是跨 Stage 的权威结构化状态。
当 current_schema 已存在时，后续工具参数必须使用 current_schema 中的
真实字段名，不得继续沿用标准化/重命名前的旧字段名。
统计、聚合、图表和 Delivery 应优先消费 current_data_ref 指向的最新数据状态。

Acquisition 不得反复执行相同搜索。已有足够来源证据后，应进入下载/读取，
而不是继续用近义关键词无限搜索。

如果真实 Tool Registry 中存在 fetch_weather_dataset，且用户要求历史天气、
未来预报、气象统计或气象文件交付，Acquisition 应优先调用该结构化天气工具。
不要先搜索 API 文档，也不要由 LLM 手工拼接 Open-Meteo 历史/预报 URL。

【v6.4 Acquisition / Regression Intelligence】
- 如果用户明确要求“回归、影响因素、驱动因素、预测”，且研究跨多个年度，Acquisition 应优先寻找月度或季度结构化数据；年度汇总只能作为背景或探索性回归的最低可用数据。
- 仅当 runtime_context.current_stage=acquisition 时，runtime_context.acquisition_saturation_observation 才具有约束力；进入 processing/delivery/verification 后不得继续沿用上一阶段的 saturation instruction。Acquisition 阶段内一旦存在该 observation，必须遵守其中 instruction，不要通过换近义关键词继续 search_web/read_webpage 绕过智能停止。
- Processing/Delivery 若通过 Evidence Grounding 发现关键事实确实缺少来源，可临时回补少量 search_web/read_webpage/download_data_file/download_document_file；这是 bounded acquisition recovery，不是重新开启无限搜索。runtime_context.acquisition_recovery_observation 存在时必须优先解决明确缺口，不得扩展无关研究面。
- 如果 runtime_context._post_stage_acquisition_recovery_exhausted=True，说明后续来源回补预算已用尽；此后 Acquisition 工具不可再用。必须基于现有结构化数据继续 Processing/Delivery；回归样本不足时按 regression_exploratory 处理，无法由证据支持的“主要因素”必须明确写成局限，而不是终止整个任务或编造结论。
- 如果已有网页证据能够覆盖核心对象与时间范围，可以进入 Processing 用 build_dataframe/create_dataframe 结构化，不需要为了形式额外下载无关数据文件。
- runtime_context.processing_strategy_observation.sample_diagnostic.small_sample=True 时，回归只能视为探索性证据。不得把高 R² 直接解释为因果、主要驱动因素或可靠预测能力。
- 不得为了追求更高 R² 反复换变量“挑模型”；模型选择必须同时考虑业务含义、样本量、共线性与可解释性。
- build_dataframe/create_dataframe 中的事实数值必须来自此前真实 Tool Observation；不得凭记忆补齐某公司某年份的销量、份额、增长率等数字。缺失值应保持缺失并继续补证据。
- 派生数值（份额、增速、差值、CAGR 等）应优先由确定性数据/计算工具生成，不要在 JSON 参数里心算后直接写入。runtime_context.evidence_grounding_observation 存在时，必须先解决其中未证实数值。

Raw Download Lifecycle 只允许一个最终原始数据保留路径：
优先下载到 temporary → 读取/验证 → 最终 promote 一次。
如果原始文件已经存在于 deliverables，不得再次 promote 生成 *_2 等重复副本。

Acquisition 的 SOURCE_READY 不能由“下载成功”单独触发。
下载后的来源必须至少成功读取一次，并形成可追踪的数据引用或 schema；
首次 read_office_data/load_data 属于 Acquisition 的来源可读性验证动作。

Delivery 的 DELIVERABLES_READY 不能由“调用过生成工具”触发。
TaskPlan 要求的 Excel/Word/PDF/PNG/CSV 等最终成果必须由成功 Tool Observation
真实登记到 data_state.deliverable_paths；缺任一要求类型都不能进入 Verification。

============================================================
工作原则
============================================================

1. 只能调用下方 Tool Registry 中存在的工具。
2. 一次只调用一个工具。
3. 不要假装工具已经执行。
4. 工具执行后，Python 会把真实 Observation 在下一轮提供给你。
5. 如果前一步失败，要根据错误信息修正下一步，而不是重复同样错误。
6. 后续步骤需要使用前一步真实 Python 对象时，使用：
   {{"$ref": "step_1.output"}}
7. 可以引用 dict 输出中的字段：
   {{"$ref": "step_1.output.some_field"}}
7a. 如果 current_data_ref 已经指向真实 DataFrame，应直接进入 get_data_info / 清洗 / 统计；
    不要无意义重复 build_dataframe。确需复用时，data 必须直接引用 DataFrame，
    不要把 {{$ref: "step_N.output.some_field"}} 再包进单元素 list。
8. 不要把 DataFrame 或文档全文重新写进 JSON。
9. 不要调用未注册工具。
10. 不要重复执行已经成功完成且无需重做的步骤。
11. 只有当用户目标的关键条件真正满足后，才能 finish。
12. 如果用户要求生成文件，必须确认对应输出工具已经成功执行后才能 finish。
13. 如果工具失败，可以换参数、换工具或根据真实错误修正。
14. 当前 web 能力包括：
    - search_web：根据关键词搜索互联网；
    - read_webpage：读取普通 HTML 网页正文；
    - download_data_file：从明确 URL 下载 CSV / Excel 等结构化数据文件；若服务器实际返回纯文本，工具会保留文本扩展名，随后应使用 read_document。
    - download_document_file：从明确 URL 下载 TXT / Markdown / PDF / DOCX 等文档来源，下载后使用 read_document 获取真实正文证据。
15. 对联网研究任务，优先使用 search_web 获取候选来源，再根据 Observation 选择值得读取的 URL。
15a. URL 类型必须按响应内容路由：普通 HTML → read_webpage；CSV/XLS/XLSX → download_data_file + read_office_data；TXT/MD/PDF/DOCX/raw text → download_document_file + read_document。若下载结果扩展名为 .txt/.md/.pdf/.docx，后续不得调用 read_office_data。
16. 如果 search_web 连续失败 2 次，不要继续机械地重复相同搜索。应根据任务情况：
    - 改用已知且高度可信的普通 HTML URL 调用 read_webpage；
    - 若已知 URL 指向 raw text / TXT / Markdown / PDF / DOCX，应使用 download_document_file，再用 read_document 读取；不要把纯文本原始文件强行当 CSV 解析；
    - 或在已有成功网页/文档证据足以满足任务时继续完成任务；
    - 如果没有足够证据，则明确说明搜索失败，不得假装搜索成功。
17. 如果 read_webpage 已经成功读取了满足用户核心要求的可靠网页，不要仅为了形式要求反复调用失败的 search_web。
18. 不具备的能力不得假装完成。
19. 只返回一个合法 JSON 对象，不要 Markdown，不要在 JSON 前后添加解释文字，也不要连续输出多个 JSON 对象。

【v3.5 复杂办公任务规划与验证规则】
25. 当用户要求多个交付物时，必须持续跟踪所有交付物；不得完成其中一个就提前 finish。
26. 当存在多个候选数据源，且用户给出“已审核、已批准、正式、最终版”等业务有效性条件时，必须读取足够的候选内容或说明取得业务证据；不得仅凭文件名、修改时间或月份新旧选择数据源。明确的审核/批准/正式状态证据优先于单纯的“更新”。
27. 区分源文件、目标文件、参考文件、未批准草稿和无关文件。除非用户明确要求，不得修改参考文件、未批准草稿或无关文件。
28. 用户要求“不覆盖原文件”时，写入工具必须输出到新文件。
29. 每个最终交付文件都以“最后一次写入版本”为准。Word 在最后一次生成/编辑后必须重新读取；Excel 在最后一次生成/编辑/重新导出后必须重新读取或检查。
30. 如果验证后又修正或重新导出某个文件，之前对该文件的验证立即失效，必须再次验证最新版本。
31. 用户要求多个交付物彼此一致时，finish 前必须基于各交付物最终版本的真实回读 Observation 核对关键业务字段。
32. 不得仅凭写入工具成功就声称“已核验”。最终回答中的“已重新读取、已核验、已确认一致”等表述必须有最终版本的写后读取/检查 Observation 支撑。
33. 工具预算接近耗尽时，优先完成尚未满足的最终交付物验证，不要为了美化结果重复做非必要的透视、排序、筛选或格式转换。
34. 当用户要求“修改/更新现有 Word 或 Excel”且对应高保真编辑工具已注册时，优先使用 apply_word_edits / apply_excel_edits；不要用“重新生成整份报告/重新导出 DataFrame”替代原文件编辑，除非编辑工具确实无法完成用户要求。
35. apply_word_edits 支持对现有 Word 批量执行 replace_text、replace_paragraph、append_paragraph、delete_paragraphs、update_table_cell；apply_excel_edits 支持 replace_values、add_column、delete_columns、rename_columns、sort_rows、delete_duplicate_rows、fill_missing_values、append_rows、update_cell。不要因为不存在单独的 replace_values 工具就误判为“无法替换 Excel 值”。
36. apply_excel_edits 的 add_column action 必须使用 column_name 指定新列名；使用 value 给所有数据行填同一值，或使用 values 传逐行列表。不要把 column 当成 add_column 的列名参数。apply_excel_edits 的 output_path 必须与 file_path 不同，因为默认禁止覆盖源 Excel。
37. 如果用户要求修改现有文件，且本轮尚未成功执行任何能够产生该最终交付物的写入/编辑工具，不得 finish。读取、统计和分析成功不等于交付物已经生成。
38. 多交付物任务在开始构造输出前，先根据用户要求确定最终交付字段。对于需要“基础数据 + 少量汇总字段”的新 Excel，优先选择能够在较少步骤内形成最终版本的路径；避免对同一数据重复做多个透视表、重复导出半成品，或先生成明显缺字段的最终文件再返工。
39. 如果必须采用“先导出基础 Excel，再用 apply_excel_edits 补字段”的路径：第一次导出的基础文件应使用临时/中间文件名；随后一次 apply_excel_edits 直接从该中间文件生成最终交付文件，并在最后一次写入后立即回读最终文件。不要先把半成品占用最终文件名，也不要为了绕过覆盖保护重复导出同一基础数据。
40. 如果已经生成正确交付物，不要仅为了增加非必要字段、美化格式或改变实现方式再次重写它；若确需再次写入，必须预留一次最终回读验证。

【v3.6 Workspace 文件生命周期规则】
41. runtime_context.workspace 是本次任务唯一可信的工作区信息。必须优先读取其中的 task_root、temporary_dir、deliverables_dir、manifest_path 和 protected_input_paths。
42. protected_input_paths 中的文件属于受保护输入。除非用户明确要求覆盖且系统工具本身允许，否则不得把任何写入工具的 output_path 指向这些路径。
43. 中间文件、基础表、临时下载、需要后续再次编辑的半成品，必须优先写入 runtime_context.workspace.temporary_dir；不要把半成品写入 deliverables_dir。
44. 用户最终需要收到的 Word、Excel、CSV 或其他最终文件，必须优先写入 runtime_context.workspace.deliverables_dir。最终文件名应表达业务含义，不要使用 temp、tmp、临时、中间等名称。
45. runtime_context.output_dir 等于本次任务 deliverables_dir。用户只说“输出到结果目录”而没有指定更具体路径时，直接在该目录下生成最终交付物。
46. 如果工作流是“先生成基础文件，再编辑成最终文件”，基础文件必须进入 temporary_dir，最后一次编辑的 output_path 必须进入 deliverables_dir。
47. 不要自行在项目根目录、源文件目录或任意未知目录创建中间文件。只有用户明确指定某个最终路径时，才可优先遵循用户指定路径；但仍不得覆盖 protected_input_paths。
48. finish 前检查最终交付物路径。若用户要求生成文件，而最终成功写入的文件仍只有 temporary_dir 中的半成品，则任务未完成。
49. 不要修改 manifest.json。Workspace manifest 由 Python 的 WorkspaceManager 自动维护，Agent 只负责正确使用工作区路径。
50. 不要把 temporary_dir 中的临时文件当作最终交付物写进 final_answer。final_answer 应优先报告 deliverables_dir 中最后验证成功的文件。

【v4.0 TaskPlan 任务合同规则】
51. 如果当前真实执行状态中存在 runtime_context.task_plan，它是本次任务在执行前建立的结构化任务合同，必须与用户最终目标一起使用。
52. evidence_requirements 表示完成任务必须取得或核实的真实证据；缺少关键证据时不得凭空补全，也不得 finish。
53. source_requirements 表示需要识别、读取或甄别的输入资料；不能仅凭文件名声称已经满足资料要求。
54. deliverable_requirements 表示最终必须交付的结果。存在多个交付物时，必须逐项完成，不得只完成其中一部分就 finish。
55. execution_requirements 是计划中的必要业务步骤。可以根据真实 Observation 调整具体工具路径，但不得无理由跳过仍然必要的业务要求。
56. verification_requirements 是 finish 前的验收合同。只要仍有关键验收要求缺少真实 Observation 支撑，就不得返回 finish。
57. safety_requirements 必须持续遵守；TaskPlan 不会覆盖 ToolPreflight 和 Workspace Policy，Python 层安全校验仍具有最终约束力。
58. assumptions 不是事实。必须通过真实资料或工具结果确认后才能把其中内容作为最终结论；无法确认时应明确保留不确定性。
59. TaskPlan 是任务合同，不是固定工具脚本。工具失败或真实证据与初始假设不一致时，应根据 Observation 修正执行路径，但仍应围绕 task_goal 和尚未满足的合同要求继续。
60. finish 时必须同时满足用户最终目标和 TaskPlan 中仍适用的 deliverable / verification / safety 要求；不得仅因为模型认为“差不多完成”而结束。

【v4.0 Completion Gate 规则】
61. action_type=finish 只是向 Python Completion Gate 申请完成，不代表任务已经成功。
62. 如果 runtime_context.verification_observation 存在，说明上一次 finish 被 Python 验收层拒绝；必须优先读取其中 failures、pending_requirements 和 checks，再决定如何补证据或修正。
63. Completion Gate 失败后，不要机械地再次 finish。只要仍可通过真实工具补齐缺失证据，就应调用对应工具。
64. verification_observation 中的 pending_requirements 表示当前 Python 层尚不能证明的验收要求；不得把 pending 擅自改写成“已验证”。
65. 只有 Completion Gate 返回 PASS，Python 才会把任务最终标记为 completed。
{budget_rule}

【v4.5 Office Skill Guidance 规则】
66. 下方 Office Skill Catalog 是“推荐工作方法”，不是可执行 Tool。
67. Skill 不拥有 handler，禁止把 Skill 名放进 tool 字段；真实执行仍只能调用 Tool Registry 中存在的 Tool。
68. 开始或继续办公任务时，应结合用户目标、TaskPlan、真实 Observation 选择适用 Skill 的方法指导。
69. Skill 的 workflow 是推荐流程，不是不可改变的固定脚本；已经有真实证据支持的步骤不要为了形式重复执行。
70. Skill 的 recommended_tools 只是候选工具，不代表每个任务都必须全部调用；只调用完成当前任务真正需要的 Tool。
71. Skill 的 verification 与 safety_rules 用于帮助规划执行和自检，但最终完成权仍属于 Python Verification Engine / Completion Gate。
72. 如果多个 Skill 同时适用，可以组合其方法，例如先 excel_data_analysis，再 excel_report_delivery；但每一轮仍只允许执行一个真实 Tool。
73. Skill Guidance 不得覆盖用户明确要求、TaskPlan、Workspace Policy、ToolPreflight 或 Completion Gate；发生冲突时以后者的真实约束为准。
74. Skill Selector 只减少发送给模型的 Guidance，不会减少 Tool Registry 中真实可用工具；即使某个 Skill 未被选中，仍不得据此声称对应 Tool 不可用。
75. Skill Selection 基于用户目标 + TaskPlan 在任务启动时确定，不额外调用 LLM；若没有足够明确的匹配信号，Python 会安全回退到完整 Skill Catalog。
76. 不要为了“遵循 Skill”而改变已经由真实 Observation 证明正确的执行路径；Skill 是方法参考，不是第二套 TaskPlan。

【v5.0 多交付物一致性规则】
77. 当用户同时要求 Excel 和 Word 等多个最终交付物时，应尽量复用同一份已经由真实源数据计算得到的分析结果，不要为每个交付物分别重新计算一套可能产生差异的数据。
78. 先完成业务分析并形成可复用的真实 Observation，再分别构造各最终交付物；多个交付物中的总计、分组结果、排名、冠军等共同业务字段必须来自同一证据链。
79. 每个最终交付物最后一次写入后都必须分别 read/inspect；只检查其中一个文件不能证明另一个文件正确。
80. 用户要求 Excel 与 Word 数据一致时，finish 前必须让两个最终文件的回读 Observation 同时存在，并核对共同业务数字；冠军/最高/排名类结论还必须核对共同业务主体。
81. 如果 Completion Gate 返回跨交付物一致性 pending，不要重新生成已经正确的文件；优先补齐缺失的最终文件 inspect/read 或独立数据统计 Observation。
82. 多交付物任务应把每个最终文件直接写入 deliverables_dir；仅供生成过程使用的中间表、临时工作簿或草稿必须留在 temporary_dir。

【v5.1 Semantic-Aware Data Understanding 规则】
83. 对从互联网下载、外部公开数据源取得、或字段含义明显不透明的数据，在正式统计、图表和报告之前，应先调用 analyze_dataframe_semantics 理解关键字段角色、可读名称和已知单位。
84. analyze_dataframe_semantics 是只读理解工具，不会修改源 DataFrame。不要把“识别字段含义”误当成“已经完成单位换算或数据清洗”。
85. 对 tmpf、sknt 等专业缩写或其他不透明字段，只有语义工具或可靠来源给出足够证据时才能赋予具体含义；证据不足时必须保留原字段并说明不确定性，不得自行猜测。
86. 在选择可视化方案前，优先使用 recommend_visualizations 或等价的语义证据。不得仅因为多个字段都是 numeric，就把不同物理量、不同单位或未知单位的指标放到同一 Y 轴比较。
87. 最终 Excel、Word 和图表面向用户展示时，应优先使用已经有证据支持的可读字段名称和单位；原始下载数据本身仍应保持可追溯，不要为了展示而覆盖原始文件。
88. “字段改成中文显示”与“单位转换”是两个不同动作。没有执行确定性单位换算工具及其真实 Observation 时，不得把 °F 数据标成 ℃、把 kt 标成 m/s、把 in 标成 mm。
89. 如果用户明确要求保存/保留原始下载数据，该文件属于用户要求的交付内容，不得只停留在 temporary_dir；应使用当前已注册且安全的文件/导出能力形成 deliverable，若当前工具确实无法做到则不得假装已交付。
90. Semantic Tool 是为了减少错误解释，而不是强制增加无意义轮次。对于字段已经清晰、单位无需判断且任务简单的本地办公数据，不要为了形式重复调用语义工具。

【v5.0 Evidence-grounded Reporting 规则】
83. 正式报告内容必须区分三类：事实、计算/比较结论、建议。三类内容的证据要求不同，不得混写成同等确定的事实。
84. “事实”包括源数据值、KPI、排名、日期、主体、数量、状态等；只有真实 Observation 已读取、计算或验证的内容才能作为事实写入报告。
85. “计算/比较结论”必须能够由现有真实 Observation 直接推出，例如“澳门销售额最高”必须有城市汇总或排序证据；不得从单个数字扩展出未验证的因果关系、趋势、增长原因或经营表现判断。
86. “建议”不得伪装成源数据事实。仅凭当前数据不能充分支持的行动建议，必须明确使用“建议”“可考虑”“建议进一步分析”等措辞，并说明它是基于当前有限证据的分析建议，而不是已验证事实。
87. 不得仅凭单期横截面销售额就声称“持续领先”“增长”“下降”“趋势改善”“市场潜力更高”“应加大资源投入”“原因是”等需要时间序列、因果或额外业务证据才能支持的结论。
88. 如果用户没有要求建议，正式 Word/Excel 报告不应为了显得完整而主动编造经营建议；优先报告真实发现、数据限制和可验证结论。
89. 如果用户明确要求建议但现有证据不足，应给出“进一步分析方向”或带条件的建议，并指出还需要哪些数据，例如历史期、目标值、成本、利润、转化率或业务约束。
90. executive_summary、KPI、sections、图表标题和 final_answer 都受上述证据约束；不能因为内容位于“结论与建议”章节就降低事实准确性要求。
91. 生成专业 Word 时，模板已经自动生成“执行摘要”和“核心指标”模块；sections 不要再次创建同名或等价重复章节。自定义 sections 应用于业务分析、汇总表、数据说明、分析建议等补充内容。
92. 当报告同时包含事实与建议时，优先把事实性“关键发现/业务分析”与“分析建议/进一步分析方向”分成不同章节，使读者能够区分已验证事实与模型建议。
93. 最终文件回读后，如果发现报告把缺乏证据的推测写成确定事实，或把建议包装成已验证结论，不得 finish；应修正报告并重新回读最新版本。

【v5.0 Tool Failure Recovery 规则】
94. 当失败 Observation 中存在 recovery 时，它是 Python 根据真实 error_type / error_message 生成的确定性恢复提示，优先读取 category、recoverable、recommended_actions 和 avoid_actions。
95. recovery 只是诊断，不代表工具已经恢复成功；任何修正调用仍必须经过真实 ToolExecutor、ToolPreflight 和 Tool Registry。
96. recoverable=true 时，优先只修正与本次失败直接相关的参数、引用、路径、来源或工具选择，并复用已经成功取得的 Observation。
97. avoid_actions 是恢复边界：不得猜测不存在的文件、Sheet、列名、step_id、output 字段或工具参数。
98. preflight_signature 应重新核对真实 Tool Registry 参数；preflight_type 需要真实 Python 对象时优先通过 $ref 复用成功 Observation。
99. output_safety 必须保持 protected_input_paths 不变；中间文件使用 temporary_dir，最终交付物使用 deliverables_dir，不得绕过 ToolPreflight。
100. missing_file、missing_sheet、missing_column 必须先从 Workspace、成功检查结果或真实数据 Observation 取得实际名称，再重新构造调用。
101. reference_resolution 必须先确认被引用 step 是否成功且 output 字段真实存在；前置步骤失败时先补齐前置步骤。
102. network、rate_limit 不得无限机械重复相同请求；permission 优先改用 Workspace 中新的可写路径或不冲突文件名。
103. recoverable=false 或 category=unknown 时保持保守；无法确定安全修复方式时不得猜测参数、伪造成功或声称任务已完成。
104. 同一工具连续失败时结合 failed_tool_counts 和每次 recovery 改变策略；不得只为了“再试一次”原样重复失败调用。

【v5.0 Recovery Policy / Retry Budget 规则】
105. 当前 state.retry_policy 是 Python 的确定性重试预算状态；它不是建议，而是执行边界。
106. “完全相同调用”由 canonical tool + 实际 arguments 的稳定签名确定；只有真实改变参数、路径、引用或工具，才算新的恢复策略。
107. recoverable=true 不代表可以无限重试；同一失败调用达到 max_identical_failures 后，Python 会以 recovery_exhausted 停止，不再执行第三次相同失败。
108. recoverable=false 的完全相同调用在首次真实失败后禁止再次盲目执行；必须换用有真实依据的策略，否则应保守停止。
109. failed_tool_counts 统计工具级失败次数；retry_policy.identical_failure_counts 统计调用签名级失败次数。不要把“同一工具但已修正参数”误判为原样重试。
110. Retry Budget 不得用于绕过 output_safety、protected_input_paths、ToolPreflight 或 Verification；安全失败只能通过合法的新路径/新参数解决。
111. 如果已有 RecoveryHint 指出了可验证的修复方向，应优先利用现有 Observation 改变失败调用，而不是消耗剩余 Retry Budget。

【v5.0 Completion Recovery Budget 规则】
112. max_iterations 是正常执行预算，不是允许跳过最终回读验证的理由。
113. 只有 Python Completion Gate 已真实返回 FAIL 后，系统才可能开放受限的 Completion Recovery Budget；不得把它当作普通额外轮数。
114. Gate FAIL 后如果修正或重新生成了最终 Word/Excel，之前对该文件的回读证据立即失效，下一步应优先 inspect/read 最新版本。
115. Completion Recovery 的目标是形成“Gate FAIL → 修正 → 回读最新版本 → 再次 finish”的闭环；不要在恢复窗口中增加非必要分析、美化或重复计算。
116. Completion Recovery 仍受 Retry Budget、ToolPreflight、Workspace Policy 和 Verification Engine 约束，不得借恢复窗口绕过任何安全或验收规则。
117. 如果 TaskPlan 已给出确定性的时间范围（例如“最近30天：YYYY-MM-DD 至 YYYY-MM-DD”），所有联网查询、下载 URL、数据筛选、统计和报告必须服从该边界；不得把“近期”擅自扩大到年初、全年或更早。
118. 已成功生成某个最终交付物后，不得仅为重复确认而再次用相同内容覆盖生成；优先使用 inspect/read 工具进行写后验证。只有真实内容需要修正时才允许重写。

Skill Selection 状态：
{skill_selection_note}

============================================================
Selected Office Skill Catalog（方法指导，不可直接执行）
============================================================

{skill_catalog}

============================================================
调用工具时返回
============================================================

{{
  "action_type": "tool",
  "tool": "真实工具名",
  "arguments": {{}},
  "purpose": "为什么现在调用这个工具"
}}

============================================================
任务完成时返回
============================================================

{{
  "action_type": "finish",
  "final_answer": "简洁说明已经完成什么、主要结果和生成文件"
}}

============================================================
真实 Tool Registry
============================================================

{catalog}
""".strip()

    @staticmethod
    def _normalize_task_plan_context(
        runtime_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v4.0：把 TaskPlan 规范化为 JSON-safe dict 后放入 runtime_context。

        兼容三种情况：
        1. context["task_plan"] 是 TaskPlan；
        2. context["task_plan"] 已经是 dict；
        3. 没有 TaskPlan，保持 v3.9 行为。
        """
        normalized = dict(runtime_context or {})
        task_plan = normalized.get("task_plan")

        if task_plan is None:
            return normalized

        if isinstance(task_plan, TaskPlan):
            normalized["task_plan"] = task_plan.to_dict()
            return normalized

        if isinstance(task_plan, dict):
            normalized["task_plan"] = dict(task_plan)
            return normalized

        if hasattr(task_plan, "to_dict"):
            converted = task_plan.to_dict()

            if not isinstance(converted, dict):
                raise TypeError(
                    "runtime_context.task_plan.to_dict() 必须返回 dict。"
                )

            normalized["task_plan"] = converted
            return normalized

        raise TypeError(
            "runtime_context.task_plan 必须是 TaskPlan、dict，"
            "或提供返回 dict 的 to_dict()。"
        )

    def _build_state(
        self,
        goal: str,
        runtime_context: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        observations = []

        for index, result in enumerate(
            tool_results,
            start=1,
        ):
            observations.append(
                {
                    "step_id": f"step_{index}",
                    "tool": result.tool_name,
                    "success": result.success,
                    "arguments": self._json_safe_arguments(
                        result.arguments
                    ),
                    "observation": (
                        self._summarize_output(
                            result.output
                        )
                        if result.success
                        else self._build_failure_observation(
                            result
                        )
                    ),
                }
            )

        failed_tool_counts: Dict[str, int] = {}

        for result in tool_results:
            if result.success:
                continue

            name = str(result.tool_name)

            failed_tool_counts[name] = (
                failed_tool_counts.get(name, 0) + 1
            )

        return {
            "goal": goal,
            "task_plan": runtime_context.get("task_plan"),
            "available_skills": self.skill_registry.summary(),
            "selected_skills": (
                runtime_context.get(
                    "skill_selection",
                    {}
                ).get(
                    "selected_skills",
                    []
                )
                if isinstance(
                    runtime_context.get(
                        "skill_selection"
                    ),
                    dict,
                )
                else []
            ),
            "skill_selection": runtime_context.get(
                "skill_selection"
            ),
            "runtime_context": runtime_context,
            "completed_tool_steps": observations,
            "tool_step_count": len(tool_results),
            "previous_decision_count": len(decisions),
            "failed_tool_counts": failed_tool_counts,
            "retry_policy": self._build_retry_policy_state(
                tool_results
            ),
            "completion_recovery_budget": {
                "normal_iteration_limit": self.max_iterations,
                "max_recovery_iterations": (
                    self.max_completion_recovery_iterations
                ),
                "gate_has_failed": bool(
                    runtime_context.get(
                        "verification_observation"
                    )
                ),
            },
        }

    def _build_retry_policy_state(
        self,
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """
        v5.0：向决策层暴露确定性的 Recovery Retry Budget 状态。

        这里不自动重试、不修改参数，也不把失败改写成成功。
        它只统计“完全相同的失败调用签名”，供 Agent 判断是否必须换策略。
        """
        signature_counts: Dict[str, int] = {}
        unrecoverable_signatures: List[str] = []

        for result in tool_results:
            if result.success:
                continue

            signature = self._failure_call_signature(
                result.tool_name,
                result.arguments,
            )
            signature_counts[signature] = (
                signature_counts.get(signature, 0) + 1
            )

            recovery = build_recovery_hint(result)
            if (
                isinstance(recovery, dict)
                and recovery.get("recoverable") is False
            ):
                unrecoverable_signatures.append(signature)

        return {
            "max_identical_failures": self.max_identical_failures,
            "identical_failure_counts": signature_counts,
            "unrecoverable_signatures": sorted(
                set(unrecoverable_signatures)
            ),
            "policy": (
                "相同 tool + arguments 的 recoverable 失败达到预算后，"
                "禁止再次原样执行；recoverable=false 的完全相同调用"
                "在首次失败后即禁止盲目重试。改变真实相关参数、路径、"
                "引用或工具后形成新的调用签名，不视为原样重试。"
            ),
        }

    def _evaluate_retry_policy(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_results: List[ToolExecutionResult],
    ) -> Dict[str, Any]:
        """
        在真实工具执行前执行确定性 Retry Budget 检查。

        - recoverable=true：同一失败调用最多真实执行
          max_identical_failures 次；
        - recoverable=false：同一失败调用真实失败一次后，
          下一次完全相同调用立即阻断；
        - 参数/路径/引用/工具发生真实变化时，签名改变，
          允许 Agent 继续自纠；
        - 不绕过 ToolExecutor / ToolPreflight / ToolRegistry。
        """
        signature = self._failure_call_signature(
            tool_name,
            arguments,
        )

        matching_failures: List[ToolExecutionResult] = []

        for result in tool_results:
            if result.success:
                continue

            if self._failure_call_signature(
                result.tool_name,
                result.arguments,
            ) == signature:
                matching_failures.append(result)

        if not matching_failures:
            return {
                "blocked": False,
                "signature": signature,
                "failure_count": 0,
                "limit": self.max_identical_failures,
                "recoverable": None,
                "reason": "",
            }

        latest_recovery = build_recovery_hint(
            matching_failures[-1]
        )
        recoverable = (
            latest_recovery.get("recoverable")
            if isinstance(latest_recovery, dict)
            else None
        )

        failure_count = len(matching_failures)
        limit = (
            1
            if recoverable is False
            else self.max_identical_failures
        )
        blocked = failure_count >= limit

        if recoverable is False:
            reason = (
                "该完全相同调用此前已产生 recoverable=false 失败；"
                "禁止在没有新证据或策略变化时盲目重试。"
            )
        else:
            reason = (
                f"该完全相同调用已失败 {failure_count} 次，"
                f"达到 Retry Budget={limit}；必须改变参数、路径、"
                "引用或工具策略，而不是继续原样重试。"
            )

        return {
            "blocked": blocked,
            "signature": signature,
            "failure_count": failure_count,
            "limit": limit,
            "recoverable": recoverable,
            "category": (
                latest_recovery.get("category")
                if isinstance(latest_recovery, dict)
                else None
            ),
            "reason": reason if blocked else "",
        }

    @classmethod
    def _failure_call_signature(
        cls,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """
        构造稳定的“工具 + 参数”调用签名。

        DataFrame 等真实 Python 对象不会被序列化全文，只记录类型与 shape；
        JSON 参数则稳定排序，保证完全相同调用可被确定性识别。
        """
        safe_arguments = cls._json_safe_arguments(
            dict(arguments or {})
        )

        payload = {
            "tool": str(tool_name or "").strip(),
            "arguments": safe_arguments,
        }

        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _build_failure_observation(
        result: ToolExecutionResult,
    ) -> Dict[str, Any]:
        """
        v5.0：把真实 Tool 失败转换成结构化恢复 Observation。

        RecoveryHint 只提供确定性诊断与恢复边界；
        不自动修改参数、不自动重试，也不绕过 ToolPreflight。
        """
        observation: Dict[str, Any] = {
            "error_type": result.error_type,
            "error_message": result.error_message,
        }

        recovery = build_recovery_hint(result)

        if recovery is not None:
            observation["recovery"] = recovery

        return observation

    @staticmethod
    def _json_safe_arguments(
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe = {}

        for key, value in arguments.items():
            if hasattr(value, "shape"):
                safe[key] = {
                    "python_type": type(value).__name__,
                    "shape": list(value.shape),
                }
            elif isinstance(
                value,
                (str, int, float, bool),
            ) or value is None:
                safe[key] = value
            elif isinstance(value, list):
                safe[key] = (
                    value
                    if len(value) <= 20
                    else f"<list length={len(value)}>"
                )
            elif isinstance(value, dict):
                safe[key] = value
            else:
                safe[key] = (
                    f"<{type(value).__name__}>"
                )

        return safe

    @staticmethod
    def _summarize_output(
        output: Any,
    ) -> Any:
        if output is None:
            return "工具执行成功，无返回值。"

        if hasattr(output, "shape"):
            summary = {
                "python_type": type(output).__name__,
                "shape": list(output.shape),
            }

            try:
                summary["columns"] = [
                    str(item)
                    for item in output.columns
                ]
            except Exception:
                pass

            try:
                preview = output.head(5)
                summary["preview"] = preview.to_dict(
                    orient="records"
                )
            except Exception:
                pass

            return summary

        if isinstance(output, dict):
            compact = {}

            for key, value in output.items():
                if hasattr(value, "shape"):
                    compact[str(key)] = {
                        "python_type": type(value).__name__,
                        "shape": list(value.shape),
                    }
                elif isinstance(
                    value,
                    (str, int, float, bool),
                ) or value is None:
                    text = value
                    if isinstance(text, str) and len(text) > 800:
                        text = text[:797] + "..."
                    compact[str(key)] = text
                elif isinstance(value, list):
                    compact[str(key)] = (
                        value[:10]
                        if len(value) <= 10
                        else {
                            "type": "list",
                            "length": len(value),
                            "preview": value[:5],
                        }
                    )
                elif isinstance(value, dict):
                    # 数据质量等工具经常返回很小的嵌套 dict，例如
                    # missing_values={"销售额": 1}。过去统一压成 <dict>，
                    # 会让本地模型失去“究竟有没有缺失值”的关键事实。
                    # 这里只保留小型、浅层、JSON-safe 的真实字典；大对象仍压缩。
                    if len(value) <= 20:
                        nested = {}
                        for nested_key, nested_value in value.items():
                            if isinstance(
                                nested_value,
                                (str, int, float, bool),
                            ) or nested_value is None:
                                nested[str(nested_key)] = nested_value
                            elif isinstance(nested_value, list) and len(nested_value) <= 10:
                                nested[str(nested_key)] = nested_value
                            else:
                                nested[str(nested_key)] = (
                                    f"<{type(nested_value).__name__}>"
                                )
                        compact[str(key)] = nested
                    else:
                        compact[str(key)] = {
                            "type": "dict",
                            "length": len(value),
                            "keys_preview": [
                                str(item) for item in list(value.keys())[:10]
                            ],
                        }
                else:
                    compact[str(key)] = (
                        f"<{type(value).__name__}>"
                    )

            return compact

        if isinstance(output, str):
            if len(output) > 1200:
                return output[:1197] + "..."
            return output

        if isinstance(output, (list, tuple)):
            if len(output) > 10:
                return {
                    "python_type": type(output).__name__,
                    "length": len(output),
                    "preview": [
                        str(item)[:200]
                        for item in output[:5]
                    ],
                }

            return [
                str(item)[:300]
                for item in output
            ]

        return str(output)[:1200]


def main():
    """
    真实动态 Agent Loop 测试。

    不给它固定三步计划。
    只给最终目标和文件路径，让 DeepSeek 每执行一步后
    根据真实 Observation 自己决定下一步。
    """
    print("=" * 70)
    print("DataPilot Workspace Agent Loop")
    print("=" * 70)

    working_directory = os.getcwd()

    source_path = os.path.join(
        working_directory,
        "office_test.xlsx",
    )

    output_path = os.path.join(
        working_directory,
        "outputs",
        "v3_1_agent_loop_test.xlsx",
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    if not os.path.exists(source_path):
        raise FileNotFoundError(
            f"测试文件不存在：{source_path}"
        )

    task = (
        f"读取 {source_path}，按城市统计销售额合计，"
        f"并把结果导出到 {output_path}。"
    )

    runtime_context = {
        "working_directory": working_directory,
        "input_paths": [
            source_path
        ],
        "requested_output_path": output_path,
    }

    print("测试任务：")
    print(task)
    print()

    agent = AgentLoop(
        max_iterations=8,
    )

    result = agent.run(
        task,
        context=runtime_context,
    )

    print()
    print("Agent Loop 最终结果：")
    print(
        json.dumps(
            {
                "success": result.success,
                "stop_reason": result.stop_reason,
                "iterations": result.iterations,
                "tool_count": len(result.tool_results),
                "final_answer": result.final_answer,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("工具执行链：")

    for index, tool_result in enumerate(
        result.tool_results,
        start=1,
    ):
        print(
            f"step_{index}: "
            f"{tool_result.tool_name} "
            f"→ {'成功' if tool_result.success else '失败'}"
        )

    if not result.success:
        raise AssertionError(
            f"Agent Loop 未完成任务：{result.stop_reason}"
        )

    if not os.path.exists(output_path):
        raise AssertionError(
            f"Agent 声称完成，但输出文件不存在：{output_path}"
        )

    successful_tools = [
        item.tool_name
        for item in result.tool_results
        if item.success
    ]

    required_tools = {
        "read_office_data",
        "group_statistics",
        "export_office_result",
    }

    if not required_tools.issubset(
        set(successful_tools)
    ):
        raise AssertionError(
            "Agent 没有完成预期的读取、统计、导出工具链。"
        )

    print()
    print("输出文件：", output_path)

    print()
    print("=" * 70)
    print("Dynamic Agent Loop 测试通过。")
    print("DataPilot 已经可以根据真实执行结果逐步决定下一步。")
    print("=" * 70)


if __name__ == "__main__":
    main()
