from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool_registry import ToolRegistry


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
    """DataPilot v3.8 工具执行前确定性校验层。"""

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
            warnings.append(f"无法读取工具签名，跳过签名校验：{error}")

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
        """
        对少数需要 Python 真实对象的参数做确定性语义类型校验。

        v4.5 修正：
        旧逻辑只要参数说明文字中出现 "DataFrame"，就把该参数本身
        当成 DataFrame。例如 default_sheet_name 的说明如果写着
        "当 dataframe 参数存在时..."，会被错误判定为 DataFrame。

        因此 DataFrame 校验必须基于明确的参数角色，而不能从整段
        自然语言说明里做模糊推断。
        """
        dataframe_parameter_names = {
            "df",
            "dataframe",
        }

        for name, value in arguments.items():
            if (
                name in dataframe_parameter_names
                and value is not None
                and not self._is_dataframe(value)
            ):
                errors.append(
                    f"参数 {name} 需要 pandas DataFrame，"
                    f"实际收到 {type(value).__name__}。"
                )

            if name == "sheets":
                if value is None:
                    continue

                if not isinstance(value, dict):
                    errors.append(
                        "参数 sheets 必须是 "
                        "Sheet 名到 pandas DataFrame 的映射，"
                        f"实际收到 {type(value).__name__}。"
                    )
                    continue

                invalid = [
                    str(key)
                    for key, item in value.items()
                    if not self._is_dataframe(item)
                ]

                if invalid:
                    errors.append(
                        "参数 sheets 的每个值都必须是 pandas DataFrame；"
                        f"以下 Sheet 不合法：{', '.join(invalid)}。"
                    )

    def _validate_output_safety(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        runtime_context: Dict[str, Any],
        errors: List[str],
    ) -> None:
        protected = self._protected_paths(runtime_context)

        for key in self.OUTPUT_KEYS:
            value = arguments.get(key)
            if not value or not isinstance(value, (str, os.PathLike)):
                continue
            output_path = self._normalize_path(value)
            if output_path in protected:
                errors.append(
                    f"拒绝执行：{key} 指向受保护输入文件：{value}"
                )

        source_path = arguments.get("file_path")
        output_path = arguments.get("output_path")
        if source_path and output_path:
            if self._normalize_path(source_path) == self._normalize_path(output_path):
                errors.append(
                    "拒绝执行：output_path 与 file_path 相同，"
                    "可能覆盖源文件。"
                )

    @staticmethod
    def _normalize_path(value: Any) -> str:
        try:
            return os.path.normcase(
                os.path.abspath(os.fspath(Path(value).expanduser()))
            )
        except Exception:
            return os.path.normcase(os.path.abspath(str(value)))

    def _protected_paths(self, runtime_context: Dict[str, Any]) -> set[str]:
        workspace = runtime_context.get("workspace") or {}
        values = workspace.get("protected_input_paths") or []
        protected = set()
        for value in values:
            if value:
                protected.add(self._normalize_path(value))
        return protected
