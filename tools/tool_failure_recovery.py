from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass
class RecoveryHint:
    """
    DataPilot 工具失败后的确定性恢复建议。

    category:
    - reference_resolution: $ref / ExecutionContext 引用解析失败
    - preflight_signature: 工具参数签名不匹配
    - preflight_type: 参数 Python 类型不符合工具要求
    - output_safety: 输出路径违反 Workspace / 源文件保护
    - missing_file: 输入文件不存在或路径错误
    - missing_sheet: Excel 工作表不存在
    - missing_column: 数据列不存在或列名错误
    - invalid_value: 参数值本身非法
    - network: 网络/HTTP/连接类失败
    - rate_limit: API/服务限流
    - permission: 权限或文件占用
    - unknown: 尚未建立确定性恢复规则
    """

    category: str
    recoverable: bool
    summary: str
    recommended_actions: List[str] = field(default_factory=list)
    avoid_actions: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ToolFailureRecovery:
    """
    ToolExecutionResult -> RecoveryHint 的确定性分类器。

    目标不是自动修改参数，也不是自动重试，而是把原始异常转换成
    AgentLoop 可以稳定消费的结构化恢复 Observation。

    设计边界：
    1. 不调用 LLM；
    2. 不执行 Tool；
    3. 不猜测不存在的文件名、Sheet、列名或路径；
    4. 不自动绕过 ToolPreflight / Workspace Policy；
    5. 不把“可恢复”理解为“原样重试”；
    6. unknown 错误保持保守，不伪造修复方案。
    """

    RATE_LIMIT_TERMS = (
        "429",
        "rate limit",
        "rate_limit",
        "too many requests",
        "限流",
        "请求过多",
    )

    NETWORK_TERMS = (
        "connection",
        "connecterror",
        "connectionerror",
        "timeout",
        "timed out",
        "network",
        "http error",
        "httpstatuserror",
        "dns",
        "proxy",
        "网络",
        "连接失败",
        "连接超时",
        "请求超时",
    )

    PERMISSION_TERMS = (
        "permissionerror",
        "permission denied",
        "access is denied",
        "winerror 32",
        "being used by another process",
        "另一个程序正在使用",
        "权限",
        "拒绝访问",
        "文件被占用",
    )

    MISSING_FILE_TERMS = (
        "filenotfounderror",
        "no such file or directory",
        "file not found",
        "找不到文件",
        "文件不存在",
        "路径不存在",
    )

    MISSING_SHEET_TERMS = (
        "worksheet",
        "sheet",
        "工作表",
    )

    MISSING_COLUMN_TERMS = (
        "keyerror",
        "column",
        "columns",
        "列名",
        "不存在列",
        "找不到列",
        "字段不存在",
        "不存在字段",
        "不存在统计字段",
        "统计字段不存在",
    )

    SIGNATURE_TERMS = (
        "参数签名不匹配",
        "unexpected keyword argument",
        "missing a required argument",
        "required positional argument",
        "got multiple values for argument",
    )

    TYPE_TERMS = (
        "需要 pandas dataframe",
        "必须是 pandas dataframe",
        "参数 sheets 必须是",
        "实际收到",
        "typeerror",
    )

    OUTPUT_SAFETY_TERMS = (
        "受保护输入文件",
        "output_path 与 file_path 相同",
        "可能覆盖源文件",
        "拒绝执行",
    )

    REFERENCE_TERMS = (
        "引用解析",
        "reference",
        "$ref",
        "executioncontext",
        "step_",
    )

    INVALID_VALUE_TERMS = (
        "valueerror",
        "invalid",
        "不合法",
        "无效",
        "必须为",
        "只允许",
    )

    @classmethod
    def from_result(cls, result: Any) -> Optional[RecoveryHint]:
        if result is None or bool(getattr(result, "success", False)):
            return None

        return cls.classify(
            tool_name=str(getattr(result, "tool_name", "") or ""),
            error_type=str(getattr(result, "error_type", "") or ""),
            error_message=str(getattr(result, "error_message", "") or ""),
            arguments=getattr(result, "arguments", {}) or {},
        )

    @classmethod
    def classify(
        cls,
        *,
        tool_name: str,
        error_type: str,
        error_message: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> RecoveryHint:
        tool = str(tool_name or "").strip()
        error_type = str(error_type or "").strip()
        message = str(error_message or "").strip()
        arguments = dict(arguments or {})

        blob = f"{error_type}\n{message}".lower()
        evidence = cls._build_evidence(
            tool=tool,
            error_type=error_type,
            message=message,
        )

        if cls._contains_any(blob, cls.REFERENCE_TERMS):
            return RecoveryHint(
                category="reference_resolution",
                recoverable=True,
                summary="参数引用无法从真实 ExecutionContext 解析。",
                recommended_actions=[
                    "检查失败引用所指向的 step_id 和 output 字段是否真实存在。",
                    "优先复用最近一次成功工具 Observation 中已经存在的对象或字段。",
                    "如果前置步骤未成功，先补做前置步骤，再重新构造引用。",
                ],
                avoid_actions=[
                    "不要猜测不存在的 step_id 或 output 字段。",
                    "不要把 DataFrame/文档全文手工复制进 JSON 来绕过引用机制。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.OUTPUT_SAFETY_TERMS):
            return RecoveryHint(
                category="output_safety",
                recoverable=True,
                summary="输出路径违反源文件保护或 Workspace 安全规则。",
                recommended_actions=[
                    "保留原输入文件不变。",
                    "中间文件改写入 runtime_context.workspace.temporary_dir。",
                    "最终交付物改写入 runtime_context.workspace.deliverables_dir，并使用新的业务文件名。",
                ],
                avoid_actions=[
                    "不要再次把 output_path 指向 protected_input_paths。",
                    "不要让 output_path 与 file_path 相同。",
                    "不要通过关闭或绕过 ToolPreflight 解决此错误。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.SIGNATURE_TERMS):
            return RecoveryHint(
                category="preflight_signature",
                recoverable=True,
                summary="工具参数名、必填参数或参数组合与真实函数签名不匹配。",
                recommended_actions=[
                    "重新查看 Tool Registry 中该工具的参数定义。",
                    "删除不存在的参数名，并补齐真实必填参数。",
                    "保持已经成功得到的上游 Observation，不要重做无关步骤。",
                ],
                avoid_actions=[
                    "不要原样重复同一组失败参数。",
                    "不要凭经验发明 Tool Registry 中不存在的参数名。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.MISSING_FILE_TERMS):
            return RecoveryHint(
                category="missing_file",
                recoverable=True,
                summary="工具需要的输入文件当前不存在或路径无法访问。",
                recommended_actions=[
                    "从 runtime_context.workspace、用户已提供路径或成功文件枚举 Observation 中重新确认真实路径。",
                    "如果文件应由前一步生成，先确认对应写入步骤是否成功。",
                    "使用确认存在的路径后再调用读取/编辑工具。",
                ],
                avoid_actions=[
                    "不要猜测文件名或目录。",
                    "不要在未确认路径前机械重复同一调用。",
                ],
                evidence=evidence,
            )

        if (
            cls._contains_any(blob, cls.MISSING_SHEET_TERMS)
            and cls._looks_missing(blob)
        ):
            return RecoveryHint(
                category="missing_sheet",
                recoverable=True,
                summary="请求的 Excel 工作表不存在或名称不匹配。",
                recommended_actions=[
                    "先读取/检查工作簿结构，取得真实 sheet_names。",
                    "从真实工作表名称中选择符合任务要求的 Sheet 后重试。",
                ],
                avoid_actions=[
                    "不要猜测 Sheet 名称。",
                    "不要原样重复不存在的工作表名。",
                ],
                evidence=evidence,
            )

        if (
            cls._contains_any(blob, cls.MISSING_COLUMN_TERMS)
            and (
                cls._looks_missing(blob)
                or error_type.lower() == "keyerror"
            )
        ):
            return RecoveryHint(
                category="missing_column",
                recoverable=True,
                summary="请求的数据列/字段不存在或列名不匹配。",
                recommended_actions=[
                    "优先使用最近一次成功数据读取/信息检查 Observation 中的真实 columns。",
                    "根据真实列名重新构造 group/sort/filter/rename 等参数。",
                ],
                avoid_actions=[
                    "不要猜测列名。",
                    "不要原样重复同一个不存在的字段。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.RATE_LIMIT_TERMS):
            return RecoveryHint(
                category="rate_limit",
                recoverable=True,
                summary="外部服务当前返回限流信号。",
                recommended_actions=[
                    "不要立即机械重复相同请求。",
                    "若已有足够真实证据，优先继续后续任务。",
                    "若仍必须取得该证据，后续重试应减少不必要请求并遵守调用层已有的等待/重试策略。",
                ],
                avoid_actions=[
                    "不要通过并发或高频重复调用绕过限流。",
                    "不要把限流失败当成成功 Observation。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.NETWORK_TERMS):
            return RecoveryHint(
                category="network",
                recoverable=True,
                summary="工具遇到网络、连接或超时类失败。",
                recommended_actions=[
                    "检查当前任务是否已有其他成功来源足以继续。",
                    "若必须重试，优先调整来源、URL 或请求范围，而不是无限重复。",
                ],
                avoid_actions=[
                    "不要把失败响应当作真实网页/数据证据。",
                    "不要无限重复完全相同的失败网络调用。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.PERMISSION_TERMS):
            return RecoveryHint(
                category="permission",
                recoverable=True,
                summary="文件或目录存在权限、占用或访问冲突。",
                recommended_actions=[
                    "优先改用 Workspace 中可写的新输出路径。",
                    "若是最终文件被占用，使用新的不冲突文件名生成交付物。",
                ],
                avoid_actions=[
                    "不要尝试覆盖受保护输入。",
                    "不要把权限失败视为文件已经成功写入。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.TYPE_TERMS):
            return RecoveryHint(
                category="preflight_type",
                recoverable=True,
                summary="工具参数的 Python 对象类型不符合真实工具要求。",
                recommended_actions=[
                    "检查 Tool Registry 参数定义和最近成功 Observation 的对象类型。",
                    "需要 DataFrame 时优先通过 $ref 传递真实 DataFrame 对象。",
                    "需要 sheets 映射时确保每个值都是真实 pandas DataFrame。",
                ],
                avoid_actions=[
                    "不要把 DataFrame 转成字符串后冒充真实对象。",
                    "不要绕过 ToolPreflight 的类型检查。",
                ],
                evidence=evidence,
            )

        if cls._contains_any(blob, cls.INVALID_VALUE_TERMS):
            return RecoveryHint(
                category="invalid_value",
                recoverable=True,
                summary="工具参数值不符合当前工具约束。",
                recommended_actions=[
                    "根据错误信息和 Tool Registry 参数说明重新构造参数值。",
                    "只修改与本次失败直接相关的参数，保留已经验证成功的上下游结果。",
                ],
                avoid_actions=[
                    "不要原样重复同一非法参数。",
                    "不要为了通过校验而绕过安全规则。",
                ],
                evidence=evidence,
            )

        return RecoveryHint(
            category="unknown",
            recoverable=False,
            summary="当前错误尚未匹配到确定性的恢复规则。",
            recommended_actions=[
                "依据原始 error_type、error_message 和真实 Observation 判断下一步。",
                "若无法从现有证据确定安全修复方式，不要猜测参数或声称已经恢复。",
            ],
            avoid_actions=[
                "不要原样机械重复失败调用。",
                "不要伪造成功 Observation。",
            ],
            evidence=evidence,
        )

    @staticmethod
    def _contains_any(text: str, terms: Sequence[str]) -> bool:
        lowered = str(text or "").lower()
        return any(str(term).lower() in lowered for term in terms)

    @staticmethod
    def _looks_missing(text: str) -> bool:
        lowered = str(text or "").lower()
        markers = (
            "not found",
            "does not exist",
            "不存在",
            "找不到",
            "unknown",
            "invalid",
            "no ",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _build_evidence(
        *,
        tool: str,
        error_type: str,
        message: str,
    ) -> List[str]:
        values = []
        if tool:
            values.append(f"tool={tool}")
        if error_type:
            values.append(f"error_type={error_type}")
        if message:
            compact = " ".join(message.split())
            if len(compact) > 500:
                compact = compact[:497] + "..."
            values.append(f"error_message={compact}")
        return values


def build_recovery_hint(result: Any) -> Optional[Dict[str, Any]]:
    hint = ToolFailureRecovery.from_result(result)
    return hint.to_dict() if hint is not None else None
