from pathlib import Path
from tempfile import mkdtemp

from readback_registry import ReadbackRegistry


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    root = Path(mkdtemp())

    xlsx = root / "result.xlsx"
    docx = root / "report.docx"

    xlsx.write_text("test", encoding="utf-8")
    docx.write_text("test", encoding="utf-8")

    r = ReadbackRegistry()

    check(
        "可以登记最终文件",
        r.register_many([xlsx, docx]) == 2,
    )

    check(
        "存在待回读文件",
        len(r.pending_files()) == 2,
    )

    check(
        "单文件回读成功可以标记",
        r.mark_success(xlsx, "xlsx read ok") is True,
    )

    check(
        "仍保留未完成回读",
        len(r.pending_files()) == 1,
    )

    check(
        "全部回读完成状态正确",
        r.mark_success(docx, "docx read ok")
        and r.all_completed(),
    )

    print("=" * 64)
    print("DataPilot Readback Registry Regression: 5/5 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
