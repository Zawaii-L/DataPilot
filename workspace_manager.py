from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class WorkspaceFile:
    path: str
    role: str
    exists: bool = True
    created_by_agent: bool = False
    registered_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceManifest:
    task_id: str
    workspace_root: str
    source_files: List[WorkspaceFile] = field(default_factory=list)
    reference_files: List[WorkspaceFile] = field(default_factory=list)
    temporary_files: List[WorkspaceFile] = field(default_factory=list)
    deliverables: List[WorkspaceFile] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
            "source_files": [item.to_dict() for item in self.source_files],
            "reference_files": [item.to_dict() for item in self.reference_files],
            "temporary_files": [item.to_dict() for item in self.temporary_files],
            "deliverables": [item.to_dict() for item in self.deliverables],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkspaceManager:
    """
    DataPilot v3.6 工作区与交付物管理器。

    职责：
    1. 为一次 Agent 任务建立独立工作区；
    2. 管理 source / reference / temporary / deliverable 四类文件；
    3. 提供统一的临时文件和最终交付物路径；
    4. 防止 Agent 把最终结果直接写回源文件；
    5. 根据真实工具输出登记 Agent 生成的文件；
    6. 清理临时文件；
    7. 生成 JSON manifest，供 GUI、Agent 和后续恢复机制使用。

    注意：
    ExecutionContext 负责“步骤之间的 Python 对象引用”；
    WorkspaceManager 负责“文件与目录生命周期”。
    两者职责独立。
    """

    SOURCE = "source"
    REFERENCE = "reference"
    TEMPORARY = "temporary"
    DELIVERABLE = "deliverable"

    SUPPORTED_ROLES = {
        SOURCE,
        REFERENCE,
        TEMPORARY,
        DELIVERABLE,
    }

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        task_id: Optional[str] = None,
        create: bool = True,
    ):
        root = Path(workspace_root).expanduser()

        try:
            root = root.resolve()
        except Exception:
            root = Path(os.path.abspath(str(root)))

        self.workspace_root = root
        self.task_id = (
            str(task_id).strip()
            if task_id
            else self._generate_task_id()
        )

        self.task_root = self.workspace_root / self.task_id
        self.temp_dir = self.task_root / "temporary"
        self.deliverables_dir = self.task_root / "deliverables"
        self.manifest_path = self.task_root / "manifest.json"

        now = self._now()

        self.manifest = WorkspaceManifest(
            task_id=self.task_id,
            workspace_root=str(self.workspace_root),
            created_at=now,
            updated_at=now,
        )

        if create:
            self.create_workspace()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _generate_task_id() -> str:
        return datetime.now().strftime(
            "task_%Y%m%d_%H%M%S_%f"
        )

    @staticmethod
    def _normalize_path(
        path: str | Path,
    ) -> Path:
        candidate = Path(path).expanduser()

        try:
            return candidate.resolve()
        except Exception:
            return Path(
                os.path.abspath(str(candidate))
            )

    @staticmethod
    def _path_key(
        path: str | Path,
    ) -> str:
        normalized = WorkspaceManager._normalize_path(
            path
        )
        return os.path.normcase(str(normalized))

    @staticmethod
    def _safe_filename(
        filename: str,
    ) -> str:
        name = Path(str(filename)).name.strip()

        if not name:
            raise ValueError("文件名不能为空。")

        if name in {".", ".."}:
            raise ValueError(
                f"非法文件名：{filename}"
            )

        return name

    @staticmethod
    def _deduplicate_path(
        path: Path,
    ) -> Path:
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix

        counter = 2

        while True:
            candidate = path.with_name(
                f"{stem}_{counter}{suffix}"
            )

            if not candidate.exists():
                return candidate

            counter += 1

    def create_workspace(self) -> Dict[str, str]:
        self.temp_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.deliverables_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.save_manifest()

        return self.get_paths()

    def get_paths(self) -> Dict[str, str]:
        return {
            "workspace_root": str(
                self.workspace_root
            ),
            "task_root": str(
                self.task_root
            ),
            "temporary_dir": str(
                self.temp_dir
            ),
            "deliverables_dir": str(
                self.deliverables_dir
            ),
            "manifest_path": str(
                self.manifest_path
            ),
        }

    def register_file(
        self,
        file_path: str | Path,
        role: str,
        *,
        created_by_agent: bool = False,
        require_exists: bool = True,
    ) -> WorkspaceFile:
        role = str(role or "").strip().lower()

        if role not in self.SUPPORTED_ROLES:
            raise ValueError(
                f"不支持的工作区文件角色：{role}"
            )

        path = self._normalize_path(file_path)
        exists = path.exists()

        if require_exists and not exists:
            raise FileNotFoundError(
                f"要登记的文件不存在：{path}"
            )

        if exists and not path.is_file():
            raise ValueError(
                f"只能登记文件，不能登记目录：{path}"
            )

        if role in {
            self.TEMPORARY,
            self.DELIVERABLE,
        }:
            self.ensure_safe_output_path(path)

        item = WorkspaceFile(
            path=str(path),
            role=role,
            exists=exists,
            created_by_agent=bool(
                created_by_agent
            ),
            registered_at=self._now(),
        )

        target_list = self._get_role_list(role)

        key = self._path_key(path)

        for existing in target_list:
            if self._path_key(existing.path) == key:
                existing.exists = exists
                existing.created_by_agent = (
                    existing.created_by_agent
                    or created_by_agent
                )
                existing.registered_at = item.registered_at
                self._touch()
                self.save_manifest()
                return existing

        self._remove_path_from_other_roles(
            path=path,
            keep_role=role,
        )

        target_list.append(item)

        self._touch()
        self.save_manifest()

        return item

    def register_source(
        self,
        file_path: str | Path,
    ) -> WorkspaceFile:
        return self.register_file(
            file_path,
            self.SOURCE,
            created_by_agent=False,
            require_exists=True,
        )

    def register_reference(
        self,
        file_path: str | Path,
    ) -> WorkspaceFile:
        return self.register_file(
            file_path,
            self.REFERENCE,
            created_by_agent=False,
            require_exists=True,
        )

    def register_temporary(
        self,
        file_path: str | Path,
        *,
        require_exists: bool = True,
    ) -> WorkspaceFile:
        return self.register_file(
            file_path,
            self.TEMPORARY,
            created_by_agent=True,
            require_exists=require_exists,
        )

    def register_deliverable(
        self,
        file_path: str | Path,
        *,
        require_exists: bool = True,
    ) -> WorkspaceFile:
        return self.register_file(
            file_path,
            self.DELIVERABLE,
            created_by_agent=True,
            require_exists=require_exists,
        )

    def _get_role_list(
        self,
        role: str,
    ) -> List[WorkspaceFile]:
        mapping = {
            self.SOURCE: self.manifest.source_files,
            self.REFERENCE: self.manifest.reference_files,
            self.TEMPORARY: self.manifest.temporary_files,
            self.DELIVERABLE: self.manifest.deliverables,
        }
        return mapping[role]

    def _remove_path_from_other_roles(
        self,
        *,
        path: str | Path,
        keep_role: str,
    ):
        key = self._path_key(path)

        for role in self.SUPPORTED_ROLES:
            if role == keep_role:
                continue

            items = self._get_role_list(role)

            items[:] = [
                item
                for item in items
                if self._path_key(item.path) != key
            ]

    def is_source_file(
        self,
        file_path: str | Path,
    ) -> bool:
        key = self._path_key(file_path)

        return any(
            self._path_key(item.path) == key
            for item in self.manifest.source_files
        )

    def is_reference_file(
        self,
        file_path: str | Path,
    ) -> bool:
        key = self._path_key(file_path)

        return any(
            self._path_key(item.path) == key
            for item in self.manifest.reference_files
        )

    def is_protected_input(
        self,
        file_path: str | Path,
    ) -> bool:
        return (
            self.is_source_file(file_path)
            or self.is_reference_file(file_path)
        )

    def ensure_safe_output_path(
        self,
        output_path: str | Path,
    ) -> str:
        path = self._normalize_path(
            output_path
        )

        if self.is_protected_input(path):
            raise ValueError(
                "为保护输入文件，禁止把输出直接写回 "
                f"source/reference 文件：{path}"
            )

        return str(path)

    def build_temporary_path(
        self,
        filename: str,
        *,
        deduplicate: bool = True,
    ) -> str:
        name = self._safe_filename(filename)
        path = self.temp_dir / name

        if deduplicate:
            path = self._deduplicate_path(path)

        self.ensure_safe_output_path(path)

        return str(path)

    def build_deliverable_path(
        self,
        filename: str,
        *,
        deduplicate: bool = True,
    ) -> str:
        name = self._safe_filename(filename)
        path = self.deliverables_dir / name

        if deduplicate:
            path = self._deduplicate_path(path)

        self.ensure_safe_output_path(path)

        return str(path)

    def classify_generated_file(
        self,
        file_path: str | Path,
    ) -> str:
        """
        根据真实文件位置判断 Agent 产物角色。

        v3.9 Artifact Governance：
        1. temporary_dir 内的文件明确属于 temporary；
        2. deliverables_dir 内的文件明确属于 deliverable；
        3. Workspace 外部的历史流程产物继续按 deliverable 兼容处理；
        4. 不再根据“临时 / 中间 / 基础表 / tmp”等文件名猜测角色。

        这样可以避免合法最终文件因为名称中包含“基础表”等词，
        被错误登记为 temporary 并在 cleanup 时删除。
        """
        path = self._normalize_path(file_path)

        if self._is_within(
            path,
            self.temp_dir,
        ):
            return self.TEMPORARY

        if self._is_within(
            path,
            self.deliverables_dir,
        ):
            return self.DELIVERABLE

        # 兼容旧流程：
        # WorkspaceManager 仍允许发现并整理 Workspace 外部已经真实生成的文件。
        # 这类文件不能仅凭文件名推断为 temporary，因此默认视为 deliverable。
        return self.DELIVERABLE

    @staticmethod
    def _is_within(
        path: Path,
        parent: Path,
    ) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def register_generated_file(
        self,
        file_path: str | Path,
    ) -> Optional[WorkspaceFile]:
        path = self._normalize_path(file_path)

        if not path.exists() or not path.is_file():
            return None

        if self.is_protected_input(path):
            return None

        role = self.classify_generated_file(
            path
        )

        return self.register_file(
            path,
            role,
            created_by_agent=True,
            require_exists=True,
        )

    def register_generated_paths(
        self,
        value: Any,
    ) -> List[WorkspaceFile]:
        """
        从真实工具 output 中递归发现已经存在的本地文件。

        支持：
        - str / Path
        - dict
        - list / tuple / set
        - 对象 __dict__

        只登记真实存在的文件，不根据字符串猜测不存在的产物。
        """
        found_paths: List[Path] = []
        seen_objects = set()

        def visit(item: Any):
            if item is None:
                return

            if isinstance(item, Path):
                candidate = self._normalize_path(
                    item
                )
                if candidate.exists() and candidate.is_file():
                    found_paths.append(candidate)
                return

            if isinstance(item, str):
                text = item.strip()

                if not text:
                    return

                try:
                    candidate = self._normalize_path(
                        text
                    )
                except Exception:
                    return

                if candidate.exists() and candidate.is_file():
                    found_paths.append(candidate)

                return

            if isinstance(item, dict):
                object_id = id(item)

                if object_id in seen_objects:
                    return

                seen_objects.add(object_id)

                for nested in item.values():
                    visit(nested)

                return

            if isinstance(
                item,
                (list, tuple, set),
            ):
                object_id = id(item)

                if object_id in seen_objects:
                    return

                seen_objects.add(object_id)

                for nested in item:
                    visit(nested)

                return

            if hasattr(item, "__dict__"):
                object_id = id(item)

                if object_id in seen_objects:
                    return

                seen_objects.add(object_id)

                try:
                    visit(vars(item))
                except Exception:
                    pass

        visit(value)

        registered: List[WorkspaceFile] = []
        seen_paths = set()

        for path in found_paths:
            key = self._path_key(path)

            if key in seen_paths:
                continue

            seen_paths.add(key)

            item = self.register_generated_file(
                path
            )

            if item is not None:
                registered.append(item)

        return registered

    def register_input_paths(
        self,
        input_paths: Optional[
            Iterable[str | Path]
        ],
        *,
        default_role: str = SOURCE,
    ) -> List[WorkspaceFile]:
        if not input_paths:
            return []

        registered: List[WorkspaceFile] = []

        for raw_path in input_paths:
            path = self._normalize_path(
                raw_path
            )

            if not path.exists():
                continue

            if path.is_file():
                registered.append(
                    self.register_file(
                        path,
                        default_role,
                        created_by_agent=False,
                        require_exists=True,
                    )
                )
                continue

            if path.is_dir():
                for child in path.rglob("*"):
                    if not child.is_file():
                        continue

                    # v3.9 Workspace Policy:
                    # 当用户把一个包含当前任务工作区的上级目录作为输入时，
                    # 不能把 task_root 内由 Agent 自己生成的 temporary、
                    # deliverables、manifest 等文件重新登记成 source/reference。
                    if self._is_within(
                        self._normalize_path(child),
                        self.task_root,
                    ):
                        continue

                    if child.name.startswith("~$"):
                        continue

                    if child.name.startswith("."):
                        continue

                    registered.append(
                        self.register_file(
                            child,
                            default_role,
                            created_by_agent=False,
                            require_exists=True,
                        )
                    )

        return registered

    def cleanup_temporary_files(
        self,
        *,
        remove_empty_directory: bool = False,
    ) -> Dict[str, Any]:
        removed: List[str] = []
        failed: List[Dict[str, str]] = []

        for item in list(
            self.manifest.temporary_files
        ):
            path = self._normalize_path(
                item.path
            )

            if not path.exists():
                item.exists = False
                continue

            if not item.created_by_agent:
                failed.append(
                    {
                        "path": str(path),
                        "error": (
                            "该文件不是 Agent 创建的，"
                            "拒绝自动删除。"
                        ),
                    }
                )
                continue

            if self.is_protected_input(path):
                failed.append(
                    {
                        "path": str(path),
                        "error": (
                            "该文件属于受保护输入文件，"
                            "拒绝自动删除。"
                        ),
                    }
                )
                continue

            try:
                path.unlink()
                item.exists = False
                removed.append(str(path))
            except Exception as error:
                failed.append(
                    {
                        "path": str(path),
                        "error": (
                            f"{type(error).__name__}: "
                            f"{error}"
                        ),
                    }
                )

        if remove_empty_directory:
            try:
                if (
                    self.temp_dir.exists()
                    and not any(
                        self.temp_dir.iterdir()
                    )
                ):
                    self.temp_dir.rmdir()
            except Exception:
                pass

        self._touch()
        self.save_manifest()

        return {
            "removed": removed,
            "failed": failed,
        }

    def copy_to_deliverables(
        self,
        source_path: str | Path,
        *,
        filename: Optional[str] = None,
        move: bool = False,
        deduplicate: bool = True,
    ) -> str:
        source = self._normalize_path(
            source_path
        )

        if not source.exists() or not source.is_file():
            raise FileNotFoundError(
                f"待整理的交付文件不存在：{source}"
            )

        target_name = self._safe_filename(
            filename or source.name
        )

        target = self.deliverables_dir / target_name

        if deduplicate:
            target = self._deduplicate_path(
                target
            )

        self.ensure_safe_output_path(
            target
        )

        if self._path_key(source) == self._path_key(target):
            self.register_deliverable(
                target
            )
            return str(target)

        if move:
            shutil.move(
                str(source),
                str(target),
            )
        else:
            shutil.copy2(
                str(source),
                str(target),
            )

        self.register_deliverable(
            target
        )

        return str(target)

    def get_deliverable_paths(
        self,
        *,
        existing_only: bool = True,
    ) -> List[str]:
        paths = []

        for item in self.manifest.deliverables:
            path = self._normalize_path(
                item.path
            )

            exists = (
                path.exists()
                and path.is_file()
            )

            item.exists = exists

            if existing_only and not exists:
                continue

            paths.append(str(path))

        return paths

    def get_temporary_paths(
        self,
        *,
        existing_only: bool = True,
    ) -> List[str]:
        paths = []

        for item in self.manifest.temporary_files:
            path = self._normalize_path(
                item.path
            )

            exists = (
                path.exists()
                and path.is_file()
            )

            item.exists = exists

            if existing_only and not exists:
                continue

            paths.append(str(path))

        return paths

    def to_runtime_context(
        self,
    ) -> Dict[str, Any]:
        """
        生成可直接交给 AgentLoop.run(context=...) 的工作区信息。
        """
        return {
            "workspace": {
                "task_id": self.task_id,
                "task_root": str(
                    self.task_root
                ),
                "temporary_dir": str(
                    self.temp_dir
                ),
                "deliverables_dir": str(
                    self.deliverables_dir
                ),
                "manifest_path": str(
                    self.manifest_path
                ),
                "protected_input_paths": [
                    item.path
                    for item in (
                        self.manifest.source_files
                        + self.manifest.reference_files
                    )
                ],
            }
        }

    def summary(
        self,
    ) -> Dict[str, Any]:
        self.refresh_exists_state()

        return {
            "task_id": self.task_id,
            "task_root": str(
                self.task_root
            ),
            "temporary_dir": str(
                self.temp_dir
            ),
            "deliverables_dir": str(
                self.deliverables_dir
            ),
            "manifest_path": str(
                self.manifest_path
            ),
            "source_files": [
                item.path
                for item in self.manifest.source_files
                if item.exists
            ],
            "reference_files": [
                item.path
                for item in self.manifest.reference_files
                if item.exists
            ],
            "temporary_files": [
                item.path
                for item in self.manifest.temporary_files
                if item.exists
            ],
            "deliverables": [
                item.path
                for item in self.manifest.deliverables
                if item.exists
            ],
        }

    def refresh_exists_state(self):
        for role in self.SUPPORTED_ROLES:
            for item in self._get_role_list(role):
                path = self._normalize_path(
                    item.path
                )
                item.exists = (
                    path.exists()
                    and path.is_file()
                )

        self._touch()

    def _touch(self):
        self.manifest.updated_at = self._now()

    def save_manifest(self) -> str:
        self.task_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._touch()

        payload = self.manifest.to_dict()

        temporary_path = self.manifest_path.with_suffix(
            ".json.tmp"
        )

        with temporary_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            temporary_path,
            self.manifest_path,
        )

        return str(self.manifest_path)

    @classmethod
    def load(
        cls,
        manifest_path: str | Path,
    ) -> "WorkspaceManager":
        path = cls._normalize_path(
            manifest_path
        )

        if not path.exists():
            raise FileNotFoundError(
                f"Workspace manifest 不存在：{path}"
            )

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            payload = json.load(file)

        task_id = str(
            payload.get("task_id")
            or ""
        ).strip()

        workspace_root = str(
            payload.get("workspace_root")
            or ""
        ).strip()

        if not task_id or not workspace_root:
            raise ValueError(
                "Workspace manifest 缺少 "
                "task_id 或 workspace_root。"
            )

        manager = cls(
            workspace_root=workspace_root,
            task_id=task_id,
            create=False,
        )

        manager.manifest = WorkspaceManifest(
            task_id=task_id,
            workspace_root=workspace_root,
            source_files=cls._load_items(
                payload.get("source_files"),
                cls.SOURCE,
            ),
            reference_files=cls._load_items(
                payload.get("reference_files"),
                cls.REFERENCE,
            ),
            temporary_files=cls._load_items(
                payload.get("temporary_files"),
                cls.TEMPORARY,
            ),
            deliverables=cls._load_items(
                payload.get("deliverables"),
                cls.DELIVERABLE,
            ),
            created_at=str(
                payload.get("created_at")
                or cls._now()
            ),
            updated_at=str(
                payload.get("updated_at")
                or cls._now()
            ),
        )

        manager.refresh_exists_state()

        return manager

    @staticmethod
    def _load_items(
        raw_items: Any,
        role: str,
    ) -> List[WorkspaceFile]:
        if not isinstance(raw_items, list):
            return []

        items: List[WorkspaceFile] = []

        for raw in raw_items:
            if not isinstance(raw, dict):
                continue

            path = str(
                raw.get("path")
                or ""
            ).strip()

            if not path:
                continue

            items.append(
                WorkspaceFile(
                    path=path,
                    role=role,
                    exists=bool(
                        raw.get(
                            "exists",
                            True,
                        )
                    ),
                    created_by_agent=bool(
                        raw.get(
                            "created_by_agent",
                            False,
                        )
                    ),
                    registered_at=str(
                        raw.get(
                            "registered_at"
                        )
                        or ""
                    ),
                )
            )

        return items
