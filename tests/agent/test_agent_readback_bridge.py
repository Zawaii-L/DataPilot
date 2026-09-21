from pathlib import Path
from tempfile import mkdtemp

from readback_registry import ReadbackRegistry


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    root = Path(mkdtemp())

    files = [
        root / "result.xlsx",
        root / "report.docx",
        root / "chart.png",
    ]

    for f in files:
        f.write_text("test", encoding="utf-8")


    registry = ReadbackRegistry()

    count = registry.register_many(files)

    check(
        "最终交付物可以进入回读队列",
        count == 3,
    )

    check(
        "Verification前存在待回读文件",
        len(registry.pending_files()) == 3,
    )


    for f in files:
        check(
            f"{f.suffix} 回读成功登记",
            registry.mark_success(
                f,
                f"{f.suffix} read ok",
            ),
        )


    check(
        "全部最终文件完成回读",
        registry.all_completed(),
    )


    result = registry.to_dict()

    print(result)

    print("=" * 64)
    print("DataPilot Agent Readback Bridge Regression: 5/5 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
