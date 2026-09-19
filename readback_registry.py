"""
DataPilot v5.3 Readback Registry

负责 Verification 阶段最终文件回读状态管理。

流程:

Delivery
  |
  v
Readback Registry
  |
  v
Verification
  |
  v
Completion Gate
"""


from pathlib import Path


class ReadbackRegistry:

    def __init__(self):
        self._items = []


    def register(self, path):
        if not path:
            return False

        p = str(Path(path).resolve())

        for item in self._items:
            if item["path"].lower() == p.lower():
                return False

        self._items.append(
            {
                "path": p,
                "read_success": False,
                "read_message": "",
            }
        )

        return True


    def register_many(self, paths):
        count = 0

        for path in paths:
            if self.register(path):
                count += 1

        return count


    def mark_success(self, path, message=""):
        p = str(Path(path).resolve())

        for item in self._items:
            if item["path"].lower() == p.lower():
                item["read_success"] = True
                item["read_message"] = message
                return True

        return False


    def pending_files(self):
        return [
            item["path"]
            for item in self._items
            if not item["read_success"]
        ]


    def all_completed(self):
        return (
            bool(self._items)
            and all(
                item["read_success"]
                for item in self._items
            )
        )


    def to_dict(self):
        return {
            "items": self._items,
            "pending_files": self.pending_files(),
            "all_completed": self.all_completed(),
        }
