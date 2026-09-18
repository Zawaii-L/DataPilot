from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class SkillDefinition:
    """
    DataPilot v4.5 的办公 Skill 定义。

    Skill 不是可直接执行的 Python Tool。
    它描述一类成熟、可复用的办公工作方法：
    - 什么时候适用；
    - 推荐使用哪些已注册 Tool；
    - 推荐执行流程；
    - 完成前应验证什么；
    - 必须遵守哪些安全边界。

    真正的执行仍由 AgentLoop -> ToolExecutor -> ToolPreflight ->
    ToolRegistry 完成，因此 Skill 不绕过现有安全与验收架构。
    """

    name: str
    description: str
    category: str = "general"
    use_when: List[str] = field(default_factory=list)
    recommended_tools: List[str] = field(default_factory=list)
    workflow: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    safety_rules: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_llm_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "use_when": list(self.use_when),
            "recommended_tools": list(self.recommended_tools),
            "workflow": list(self.workflow),
            "verification": list(self.verification),
            "safety_rules": list(self.safety_rules),
            "aliases": list(self.aliases),
        }


class SkillRegistry:
    """
    DataPilot v4.5 Skill Registry。

    与 ToolRegistry 的职责严格分离：

    ToolRegistry
        保存“可执行的原子 Python 能力”。

    SkillRegistry
        保存“如何组合原子能力完成一类办公任务”的方法知识。

    SkillRegistry 本身不执行 handler，也不直接写文件。
    """

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        self._aliases: Dict[str, str] = {}

    def register(
        self,
        name: str,
        description: str,
        *,
        category: str = "general",
        use_when: Optional[Iterable[str]] = None,
        recommended_tools: Optional[Iterable[str]] = None,
        workflow: Optional[Iterable[str]] = None,
        verification: Optional[Iterable[str]] = None,
        safety_rules: Optional[Iterable[str]] = None,
        aliases: Optional[Iterable[str]] = None,
        overwrite: bool = False,
    ) -> SkillDefinition:
        normalized_name = self._normalize_name(name)

        if not normalized_name:
            raise ValueError("Skill 名称不能为空。")

        if normalized_name in self._skills and not overwrite:
            raise ValueError(
                f"Skill 已存在：{normalized_name}"
            )

        alias_list = self._normalize_string_list(
            aliases or []
        )
        alias_list = [
            item
            for item in alias_list
            if item != normalized_name
        ]

        for alias in alias_list:
            existing_skill = self._aliases.get(alias)

            if (
                existing_skill
                and existing_skill != normalized_name
            ):
                raise ValueError(
                    f"Skill 别名冲突：{alias} "
                    f"已指向 {existing_skill}"
                )

            if (
                alias in self._skills
                and alias != normalized_name
            ):
                raise ValueError(
                    f"Skill 别名冲突：{alias} "
                    "与正式 Skill 名重复。"
                )

        definition = SkillDefinition(
            name=normalized_name,
            description=str(
                description or ""
            ).strip(),
            category=(
                str(category or "general").strip()
                or "general"
            ),
            use_when=self._normalize_string_list(
                use_when or []
            ),
            recommended_tools=self._normalize_string_list(
                recommended_tools or []
            ),
            workflow=self._normalize_string_list(
                workflow or []
            ),
            verification=self._normalize_string_list(
                verification or []
            ),
            safety_rules=self._normalize_string_list(
                safety_rules or []
            ),
            aliases=alias_list,
        )

        if overwrite and normalized_name in self._skills:
            self._remove_aliases_for(normalized_name)

        self._skills[normalized_name] = definition

        for alias in alias_list:
            self._aliases[alias] = normalized_name

        return definition

    def unregister(self, name: str) -> bool:
        canonical_name = self.resolve_name(name)

        if canonical_name is None:
            return False

        self._remove_aliases_for(canonical_name)
        self._skills.pop(canonical_name, None)
        return True

    def has(self, name: str) -> bool:
        return self.resolve_name(name) is not None

    def resolve_name(
        self,
        name: str,
    ) -> Optional[str]:
        normalized_name = self._normalize_name(name)

        if normalized_name in self._skills:
            return normalized_name

        return self._aliases.get(normalized_name)

    def get(self, name: str) -> SkillDefinition:
        canonical_name = self.resolve_name(name)

        if canonical_name is None:
            raise KeyError(
                f"未注册 Skill：{name}"
            )

        return self._skills[canonical_name]

    def list_skills(
        self,
        category: Optional[str] = None,
    ) -> List[SkillDefinition]:
        skills = list(self._skills.values())

        if category is not None:
            category_text = (
                str(category).strip().lower()
            )
            skills = [
                skill
                for skill in skills
                if skill.category.lower()
                == category_text
            ]

        return sorted(
            skills,
            key=lambda item: (
                item.category.lower(),
                item.name.lower(),
            ),
        )

    def categories(self) -> List[str]:
        return sorted(
            {
                skill.category
                for skill in self._skills.values()
            }
        )

    def to_llm_catalog(
        self,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return [
            skill.to_llm_dict()
            for skill in self.list_skills(
                category=category
            )
        ]

    def build_llm_catalog_text(
        self,
        category: Optional[str] = None,
    ) -> str:
        skills = self.list_skills(
            category=category
        )

        if not skills:
            return "当前没有可用 Office Skills。"

        blocks: List[str] = []

        for index, skill in enumerate(
            skills,
            start=1,
        ):
            workflow_text = self._numbered_text(
                skill.workflow
            )
            verification_text = self._bulleted_text(
                skill.verification
            )
            safety_text = self._bulleted_text(
                skill.safety_rules
            )

            blocks.append(
                "\n".join(
                    [
                        f"[Skill {index}]",
                        f"名称：{skill.name}",
                        f"类别：{skill.category}",
                        f"用途：{skill.description}",
                        "适用场景："
                        + self._inline_text(
                            skill.use_when
                        ),
                        "推荐工具："
                        + self._inline_text(
                            skill.recommended_tools
                        ),
                        "推荐流程：",
                        workflow_text,
                        "验收重点：",
                        verification_text,
                        "安全规则：",
                        safety_text,
                    ]
                )
            )

        return "\n\n".join(blocks)

    def validate_tools(
        self,
        tool_registry: Any,
    ) -> Dict[str, List[str]]:
        """
        检查每个 Skill 推荐的 Tool 是否真实存在于 ToolRegistry。

        这里只做架构一致性检查，不执行任何 Tool。
        """
        missing: Dict[str, List[str]] = {}

        for skill in self.list_skills():
            absent = [
                tool_name
                for tool_name in skill.recommended_tools
                if not tool_registry.has(tool_name)
            ]

            if absent:
                missing[skill.name] = absent

        return missing

    def summary(self) -> Dict[str, Any]:
        return {
            "skill_count": len(self._skills),
            "categories": self.categories(),
            "skills": [
                skill.name
                for skill in self.list_skills()
            ],
        }

    def _remove_aliases_for(
        self,
        canonical_name: str,
    ):
        aliases_to_remove = [
            alias
            for alias, target
            in self._aliases.items()
            if target == canonical_name
        ]

        for alias in aliases_to_remove:
            self._aliases.pop(alias, None)

    @staticmethod
    def _normalize_name(name: str) -> str:
        return str(name or "").strip()

    @classmethod
    def _normalize_string_list(
        cls,
        values: Iterable[Any],
    ) -> List[str]:
        result: List[str] = []

        for item in values:
            text = str(item or "").strip()

            if text and text not in result:
                result.append(text)

        return result

    @staticmethod
    def _inline_text(
        values: List[str],
    ) -> str:
        if not values:
            return "未特别说明"

        return "；".join(values)

    @staticmethod
    def _numbered_text(
        values: List[str],
    ) -> str:
        if not values:
            return "  未特别说明"

        return "\n".join(
            f"  {index}. {value}"
            for index, value in enumerate(
                values,
                start=1,
            )
        )

    @staticmethod
    def _bulleted_text(
        values: List[str],
    ) -> str:
        if not values:
            return "  - 未特别说明"

        return "\n".join(
            f"  - {value}"
            for value in values
        )


def create_default_skill_registry() -> SkillRegistry:
    """
    创建 DataPilot v4.5 第一批通用 Office Skills。

    这些 Skill 只描述工作方法，不直接执行 Python。
    """

    registry = SkillRegistry()

    registry.register(
        "excel_data_analysis",
        (
            "从真实 CSV / Excel 数据中完成字段识别、筛选、"
            "清洗、汇总、排序、透视等分析，并基于真实结果得出结论。"
        ),
        category="data_analysis",
        use_when=[
            "用户要求分析 CSV / Excel 数据。",
            "任务涉及汇总、排名、筛选、统计、透视或数据质量处理。",
        ],
        recommended_tools=[
            "inspect_data_files",
            "read_office_data",
            "get_data_info",
            "filter_data",
            "apply_filters",
            "sort_data",
            "select_columns",
            "drop_columns",
            "rename_columns",
            "drop_duplicate_rows",
            "handle_missing_values",
            "filter_date_range",
            "group_statistics",
            "group_multi_statistics",
            "create_pivot_summary",
        ],
        workflow=[
            "识别并检查真实数据源、工作表和字段结构。",
            "读取完成任务所需的真实数据，不凭文件名或猜测代替读取。",
            "根据用户目标执行必要的筛选、清洗、汇总、排序或透视。",
            "保留关键中间 Observation，使最终业务结论可追溯。",
            "如果需要交付文件，再交给相应交付 Skill 生成最终 deliverable。",
        ],
        verification=[
            "关键数字、排名和筛选结果应有真实数据处理 Observation 支撑。",
            "如果 TaskPlan 要求与源数据一致，完成前必须保留可审计证据。",
        ],
        safety_rules=[
            "不得覆盖 source/reference 输入文件。",
            "不得把未经读取或验证的业务数字当成事实。",
        ],
        aliases=[
            "data_analysis",
            "excel_analysis",
        ],
    )

    registry.register(
        "excel_report_delivery",
        (
            "把已经完成分析的数据结果整理成最终 Excel 交付物。"
            "普通数据交付可使用基础 Excel 导出工具；"
            "当用户要求正式、专业、汇报型、可直接发送的 Excel 报告时，"
            "优先生成带标题、KPI、专业表格格式和必要原生图表的专业报告，"
            "并在写入后重新检查最终文件。"
        ),
        category="office_delivery",
        use_when=[
            "用户明确要求生成最终 Excel。",
            "数据分析结果需要以 Excel 报表形式交付。",
            "用户要求正式、专业、汇报型、可直接发送给领导或客户的 Excel 报告。",
            "最终 Excel 需要 KPI、图表、专业布局或更强可读性。",
        ],
        recommended_tools=[
            "select_columns",
            "rename_columns",
            "sort_data",
            "export_office_result",
            "export_multi_sheet_excel",
            "create_professional_excel_report",
            "read_office_data",
            "inspect_professional_excel_report",
        ],
        workflow=[
            "在写文件前确定最终 Excel 的字段、顺序、业务含义和交付层级。",
            "必要时整理字段名、字段顺序和排序，确保写入报告的是已经完成分析的真实 DataFrame。",
            "如果用户只要求普通 Excel 数据文件，可使用 export_office_result 或 export_multi_sheet_excel。",
            "如果用户要求正式、专业、汇报型、可直接发送的 Excel 报告，优先使用 create_professional_excel_report，而不是用普通导出冒充专业报告。",
            "专业报告应根据真实分析结果合理组织 Sheet、报告标题、KPI 和必要图表；不得为了美观编造 KPI、业务数字或结论。",
            "将最终 Excel 写入 Workspace deliverables_dir，不得把 temporary 中间文件冒充最终交付物。",
            "普通 Excel 生成后使用 read_office_data 回读关键数据；专业 Excel 报告生成后优先使用 inspect_professional_excel_report 重新检查最终交付文件。",
            "完成前根据 TaskPlan 核对关键字段、关键业务数字、Sheet 结构和要求的专业报告元素，再交给 Completion Gate 做最终验收。",
        ],
        verification=[
            "最终 Excel 必须真实存在并位于 deliverables_dir。",
            "最终文件生成后必须存在成功的重新读取或结构检查证据。",
            "回读或检查结果应满足 TaskPlan 的关键业务验收要求。",
            "专业报告任务应验证要求的 Sheet、标题、关键字段、KPI 或图表等交付结构真实存在。",
            "专业报告中的关键 KPI、排名和图表数据必须来自已经验证的真实分析结果。",
            "生成专业报告时，inspect_professional_excel_report 的最终文件检查结果应作为优先验收证据之一。",
        ],
        safety_rules=[
            "最终输出路径不得与受保护输入路径相同。",
            "需要中间文件时应写入 temporary_dir。",
            "不得把普通 Excel 导出包装成已经完成专业报告设计的交付物。",
            "不得为了填充 KPI、标题或图表而编造源数据中不存在的业务事实。",
            "Skill 只提供工作流指导，真实执行仍必须经过 ToolExecutor、ToolPreflight 和 ToolRegistry。",
        ],
        aliases=[
            "excel_delivery",
            "excel_report",
        ],
    )

    registry.register(
        "existing_excel_edit",
        (
            "在保留现有 Excel 结构与内容的前提下，对已有工作簿进行"
            "高保真修改并另存为新的最终文件。"
        ),
        category="office_edit",
        use_when=[
            "用户要求修改、更新、补充或整理已有 Excel。",
            "任务重点是编辑原工作簿，而不是从 DataFrame 重新生成整个文件。",
        ],
        recommended_tools=[
            "inspect_data_files",
            "read_office_data",
            "apply_excel_edits",
        ],
        workflow=[
            "先检查源 Excel 的工作表、字段和需要修改的位置。",
            "读取与修改决策有关的真实内容。",
            "一次规划清楚必要编辑，尽量减少重复重写。",
            "使用高保真 Excel 编辑工具另存新文件。",
            "重新读取或检查最终文件，确认目标修改真实存在。",
        ],
        verification=[
            "要求的编辑必须出现在最终文件中。",
            "未要求修改的关键结构不应被无意破坏。",
            "最终文件必须与源文件路径不同。",
        ],
        safety_rules=[
            "禁止覆盖受保护源 Excel。",
            "优先编辑现有工作簿，不用重新生成整个工作簿冒充高保真编辑。",
        ],
        aliases=[
            "excel_edit",
        ],
    )

    registry.register(
        "professional_word_delivery",
        (
            "把已经验证的数据分析结果、业务事实或文档研究结果整理成新的"
            "专业 Word 汇报交付物。适合正式、汇报型、可直接发送给领导或客户的"
            "新 Word 报告；优先使用专业 Word 创建工具并在写入后重新检查最终文件。"
        ),
        category="office_delivery",
        use_when=[
            "用户明确要求生成新的最终 Word 报告、汇报、简报或分析报告。",
            "用户要求正式、专业、汇报型、可直接发送给领导或客户的 Word 交付物。",
            "最终 Word 需要执行摘要、KPI、业务表格、来源说明或清晰章节层级。",
        ],
        recommended_tools=[
            "read_office_data",
            "read_document",
            "create_professional_word_report",
            "inspect_professional_word_report",
        ],
        workflow=[
            "先读取完成任务所需的真实数据或文档证据，不能凭文件名、常识或猜测填写报告。",
            "在写文件前确定报告对象、主标题、执行摘要、关键 KPI、章节结构和来源说明。",
            "把拟写内容先区分为事实、可直接计算/比较的结论、分析建议三类；所有 KPI、排名、业务数字和事实性结论必须来自前序已验证 Observation。",
            "计算或比较结论只能写到现有证据能够直接推出的程度；不得把横截面数据扩展成未经验证的趋势、因果、增长原因、市场潜力或资源投入结论。",
            "用户未要求建议时，不为了报告完整主动添加经营建议；用户要求建议但证据不足时，应改写为带条件的分析建议或进一步分析方向，并明确仍需补充的数据。",
            "模板会自动生成“执行摘要”和“核心指标”；sections 不再重复创建同名或等价章节，优先用于业务分析、汇总表、数据说明和必要的分析建议。",
            "事实性关键发现与分析建议应分开组织，避免把模型建议包装成已验证事实。",
            "使用 create_professional_word_report 创建新的正式 Word；已有 Word 修改任务仍交给 existing_word_edit。",
            "将最终 Word 写入 Workspace deliverables_dir，不得覆盖 source/reference 输入。",
            "生成后使用 inspect_professional_word_report 重新打开最终文件，检查标题层级、关键正文、业务表格和报告结构。",
            "根据 TaskPlan 核对关键事实、内容证据边界与交付要求，再交给 Completion Gate 做最终验收。",
        ],
        verification=[
            "最终 Word 必须真实存在并位于 deliverables_dir。",
            "最终文件生成后必须存在 inspect_professional_word_report 的成功检查证据。",
            "报告标题、要求的执行摘要、KPI、关键章节和业务表格应真实存在。",
            "关键 KPI、排名、数字和事实性结论必须与前序真实证据一致。",
            "报告不得把缺乏 Observation 支撑的趋势、因果、经营判断或行动建议写成确定事实。",
            "存在分析建议时，应能从最终回读内容中区分建议与已验证事实；证据不足的建议应使用条件性措辞或明确为进一步分析方向。",
            "TaskPlan 要求来源说明时，最终报告中必须存在相应来源或口径说明。",
        ],
        safety_rules=[
            "不得覆盖 source/reference 输入文件。",
            "不得为了报告完整或美观编造 KPI、业务数字、来源或结论。",
            "不得为了显得专业而自动生成缺乏证据支持的经营建议、因果解释或趋势判断。",
            "创建新报告使用 professional_word_delivery；修改已有 Word 使用 existing_word_edit，二者不得混淆。",
            "Skill 只提供工作流指导，真实执行仍必须经过 ToolExecutor、ToolPreflight 和 ToolRegistry。",
        ],
        aliases=[
            "word_delivery",
            "word_report",
            "professional_word",
        ],
    )

    registry.register(
        "existing_word_edit",
        (
            "在尽量保留原 Word 文档结构与格式的前提下执行"
            "文本、段落或表格修改，并另存最终文档。"
        ),
        category="office_edit",
        use_when=[
            "用户要求修改、更新或同步已有 Word 文档。",
            "任务要求保留原文档而不是重新生成一份全新报告。",
        ],
        recommended_tools=[
            "inspect_documents",
            "read_document",
            "apply_word_edits",
        ],
        workflow=[
            "检查并读取真实源 Word 内容。",
            "确定需要修改的文本、段落或表格位置。",
            "使用高保真 Word 编辑工具执行必要修改并另存。",
            "重新读取最终 Word，确认关键修改已经落盘。",
        ],
        verification=[
            "用户要求的文本或表格修改必须可在最终文件中验证。",
            "最终文件必须真实存在且不覆盖受保护源文件。",
        ],
        safety_rules=[
            "禁止覆盖受保护源 Word。",
            "已有文档编辑优先使用 apply_word_edits，不用整份重生成替代。",
        ],
        aliases=[
            "word_edit",
        ],
    )

    registry.register(
        "document_summary",
        (
            "基于真实 Word / PDF / TXT / Markdown 文档读取结果，"
            "提取事实、归纳内容，并按任务要求形成摘要或报告。"
        ),
        category="document",
        use_when=[
            "用户要求阅读、总结、提取或综合办公文档。",
            "任务结论必须来自真实文档内容。",
        ],
        recommended_tools=[
            "scan_document_files",
            "inspect_documents",
            "read_document",
            "generate_document_summary_report",
        ],
        workflow=[
            "发现或确认任务相关文档。",
            "检查候选文档并读取真正需要的完整内容。",
            "区分源文档事实与模型归纳，不凭文件名猜内容。",
            "如果用户要求正式交付物，再生成文档摘要报告。",
            "生成文件后验证最终交付物真实存在。",
        ],
        verification=[
            "摘要中的关键事实应能追溯到真实文档 Observation。",
            "不得把未在源文档中出现的信息写成源文档事实。",
        ],
        safety_rules=[
            "不得覆盖 source/reference 文档。",
        ],
        aliases=[
            "document_reading",
            "document_summarization",
        ],
    )

    registry.register(
        "cross_file_office_workflow",
        (
            "跨多个数据文件或办公文档甄别正式资料、提取证据、"
            "统一关键事实，并生成一致的最终交付物。"
        ),
        category="cross_file",
        use_when=[
            "任务同时涉及多个候选文件、多个 Sheet 或多种 Office 文档。",
            "需要从不同来源甄别正式版本并保持跨交付物事实一致。",
        ],
        recommended_tools=[
            "discover_data_files",
            "inspect_data_files",
            "scan_document_files",
            "inspect_documents",
            "read_office_data",
            "read_document",
            "merge_data_files",
            "export_office_result",
            "export_multi_sheet_excel",
            "generate_document_summary_report",
            "apply_excel_edits",
            "apply_word_edits",
        ],
        workflow=[
            "先发现并检查所有相关候选资料。",
            "根据真实内容、审批状态、说明或用户要求选择适用数据源。",
            "读取必要来源并建立关键事实证据链。",
            "在生成多个交付物前先确定需要保持一致的关键业务事实。",
            "生成或编辑最终交付物。",
            "回读最终文件，核对跨文件关键事实一致性。",
        ],
        verification=[
            "正式数据源的选择必须有真实内容证据，而不是只看文件名。",
            "多个最终交付物中的关键业务事实应保持一致。",
            "最终交付物应分别存在真实生成与验证证据。",
        ],
        safety_rules=[
            "所有 source/reference 输入均视为受保护资料。",
            "中间产物与最终交付物必须遵守 Workspace 目录治理。",
        ],
        aliases=[
            "cross_file",
            "multi_file_office",
        ],
    )

    return registry
