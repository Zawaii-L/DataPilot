"""
DataPilot v5.3 Delivery Registry

记录最终交付物，并为 Verification 提供回读队列。
"""

from pathlib import Path


class DeliveryRegistry:

    def __init__(self):
        self._deliverables = []


    def register(self, path):
        if not path:
            return False

        p = str(Path(path).resolve())

        for item in self._deliverables:
            if item["path"].lower() == p.lower():
                return False

        self._deliverables.append({
            "path": p,
            "extension": Path(p).suffix.lower(),
            "verified_read": False,
        })

        return True


    def register_many(self, paths):
        count = 0
        for path in paths:
            if self.register(path):
                count += 1
        return count


    def exists_type(self, extension):
        extension = extension.lower()
        return any(
            item["extension"] == extension
            for item in self._deliverables
        )


    def mark_read_success(self, path):
        p = str(Path(path).resolve())

        for item in self._deliverables:
            if item["path"].lower() == p.lower():
                item["verified_read"] = True
                return True

        return False


    def pending_readbacks(self):
        return [
            item["path"]
            for item in self._deliverables
            if not item["verified_read"]
        ]


    def all_read_success(self):
        return (
            bool(self._deliverables)
            and all(
                item["verified_read"]
                for item in self._deliverables
            )
        )


    def to_dict(self):
        return {
            "deliverables": self._deliverables,
            "pending_readbacks": self.pending_readbacks(),
            "all_read_success": self.all_read_success(),
        }
