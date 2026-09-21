from acquisition_adapter import build_acquisition_instruction


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    macau = build_acquisition_instruction(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    print(macau.to_dict())

    check(
        "澳门天气生成验证源指令",
        macau.action == "use_verified_source",
    )

    check(
        "澳门天气跳过搜索",
        macau.skip_search is True,
    )

    check(
        "澳门存在推荐来源",
        len(macau.preferred_sources) > 0,
    )


    unknown = build_acquisition_instruction(
        domain="weather",
        location="未知地区",
        data_type="observation",
    )

    check(
        "未知地区生成搜索指令",
        unknown.action == "search_new_source",
    )

    check(
        "未知地区允许搜索",
        unknown.skip_search is False,
    )


    print("=" * 64)
    print("DataPilot Acquisition Adapter Regression: 5/5 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
