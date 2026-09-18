from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_registry import ToolRegistry


@dataclass
class PreflightResult:
    success: bool
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class ToolPreflight:
    """
    DataPilot 工具执行前确定性校验层。

    v3.9 第一阶段新增：
    - 当 runtime_context 中存在完整 Workspace 路径信息时，
      写文件工具的 output_path 必须位于：
        1. Workspace temporary_dir
        2. Workspace deliverables_dir
    - Workspace 外部输出路径会在真实 handler 执行前被拒绝。
    - 没有 Workspace runtime_context 时保持 v3.8 兼容行为，
      不启用严格目录治理。
    """

    OUTPUT_KEYS = {"output_path"}
    OUTPUT_DIR_KEYS = {"output_dir"}

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def validate(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> PreflightResult:
        errors: List[str] = []
        warnings: List[str] = []
        call_arguments = dict(arguments or {})

        canonical_name = self.registry.resolve_name(tool_name)
        if canonical_name is None:
            return PreflightResult(
                success=False,
                tool_name=str(tool_name),
                arguments=call_arguments,
                errors=[f"工具未注册：{tool_name}"],
            )

        definition = self.registry.get(canonical_name)
        handler = definition.handler

        try:
            signature = inspect.signature(handler)
            signature.bind(**call_arguments)
        except TypeError as error:
            errors.append(f"参数签名不匹配：{error}")
        except (ValueError, RuntimeError) as error:
            warnings.append(
                f"无法读取工具签名，跳过签名校验：{error}"
            )

        self._validate_semantic_types(
            definition.parameters,
            call_arguments,
            errors,
        )

        self._validate_output_safety(
            canonical_name,
            call_arguments,
            runtime_context or {},
            errors,
        )

        return PreflightResult(
            success=not errors,
            tool_name=canonical_name,
            arguments=call_arguments,
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _is_dataframe(value: Any) -> bool:
        return (
            value is not None
            and type(value).__name__ == "DataFrame"
            and hasattr(value, "to_excel")
            and hasattr(value, "columns")
        )

    def _validate_semantic_types(
        self,
        parameter_docs: Dict[str, Any],
        arguments: Dict[str, Any],
        errors: List[str],
    ) -> None:
        for name, value in arguments.items():
            description = str(
                parameter_docs.get(name, "")
            )
            normalized = description.lower()

            if (
                "dataframe" in normalized
                and "映射" not in description
                and "多个" not in description
                and not self._is_dataframe(value)
            ):
                errors.append(
                    f"参数 {name} 需要 pandas DataFrame，"
                    f"实际收到 {type(value).__name__}。"
                )

            if (
                name == "sheets"
                and isinstance(value, dict)
            ):
                invalid = [
                    str(key)
                    for key, item in value.items()
                    if not self._is_dataframe(item)
                ]

                if invalid:
                    errors.append(
                        "参数 sheets 的每个值都必须是 "
                        "pandas DataFrame；"
                        "以下 Sheet 不合法："
                        f"{', '.join(invalid)}。"
                    )

    def _validate_output_safety(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        runtime_context: Dict[str, Any],
        errors: List[str],
    ) -> None:
        protected = self._protected_paths(
            runtime_context
        )

        for key in self.OUTPUT_KEYS:
            value = arguments.get(key)

            if (
                not value
                or not isinstance(
                    value,
                    (str, os.PathLike),
                )
            ):
                continue

            output_path = self._normalize_path(
                value
            )

            if output_path in protected:
                errors.append(
                    f"拒绝执行：{key} 指向受保护输入文件："
                    f"{value}"
                )

            self._validate_workspace_output_path(
                key=key,
                value=value,
                runtime_context=runtime_context,
                errors=errors,
            )

        source_path = arguments.get(
            "file_path"
        )
        output_path = arguments.get(
            "output_path"
        )

        if source_path and output_path:
            if (
                self._normalize_path(source_path)
                == self._normalize_path(
                    output_path
                )
            ):
                errors.append(
                    "拒绝执行：output_path 与 "
                    "file_path 相同，可能覆盖源文件。"
                )

    def _validate_workspace_output_path(
        self,
        *,
        key: str,
        value: str | os.PathLike,
        runtime_context: Dict[str, Any],
        errors: List[str],
    ) -> None:
        """
        v3.9 Workspace 输出路径策略。

        只有 runtime_context 中同时存在：
        - workspace
        - temporary_dir
        - deliverables_dir

        才启用严格目录治理。

        这样可以保证：
        1. Workspace Agent 的写文件行为被限制在任务工作区内；
        2. v3.8 之前的非 Workspace 调用保持兼容。
        """

        workspace = (
            runtime_context.get("workspace")
            or {}
        )

        if not isinstance(workspace, dict):
            return

        temporary_dir = workspace.get(
            "temporary_dir"
        )
        deliverables_dir = workspace.get(
            "deliverables_dir"
        )

        if not temporary_dir or not deliverables_dir:
            return

        output_path = self._normalize_path(
            value
        )
        temporary_path = self._normalize_path(
            temporary_dir
        )
        deliverables_path = self._normalize_path(
            deliverables_dir
        )

        if (
            self._is_within(
                output_path,
                temporary_path,
            )
            or self._is_within(
                output_path,
                deliverables_path,
            )
        ):
            return

        errors.append(
            "拒绝执行："
            f"{key} 位于当前 Workspace 允许目录之外："
            f"{value}。"
            "输出文件必须写入 "
            "temporary_dir 或 deliverables_dir。"
        )

    @staticmethod
    def _normalize_path(
        value: Any,
    ) -> str:
        try:
            return os.path.normcase(
                os.path.abspath(
                    os.fspath(
                        Path(value).expanduser()
                    )
                )
            )
        except Exception:
            return os.path.normcase(
                os.path.abspath(
                    str(value)
                )
            )

    @staticmethod
    def _is_within(
        path: str,
        parent: str,
    ) -> bool:
        """
        判断 path 是否位于 parent 内部，或就是 parent 本身。

        使用 os.path.commonpath，避免简单 startswith 带来的：
        C:\\task\\deliverables_bad
        被误判为
        C:\\task\\deliverables
        的子目录。
        """

        try:
            return (
                os.path.commonpath(
                    [path, parent]
                )
                == parent
            )
        except (
            ValueError,
            TypeError,
            OSError,
        ):
            return False

    def _protected_paths(
        self,
        runtime_context: Dict[str, Any],
    ) -> set[str]:
        workspace = (
            runtime_context.get("workspace")
            or {}
        )

        values = (
            workspace.get(
                "protected_input_paths"
            )
            or []
        )

        protected = set()

        for value in values:
            if value:
                protected.add(
                    self._normalize_path(
                        value
                    )
                )

        return protected
