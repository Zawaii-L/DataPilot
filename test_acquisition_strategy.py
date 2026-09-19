from acquisition_strategy import build_acquisition_decision


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    macau = build_acquisition_decision(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    print(macau.to_dict())

    check(
        "澳门天气进入验证源获取模式",
        macau.action == "use_verified_source",
    )

    check(
        "澳门策略包含来源",
        len(macau.sources) > 0,
    )


    unknown = build_acquisition_decision(
        domain="weather",
        location="未知地区",
        data_type="observation",
    )

    check(
        "未知地区进入搜索模式",
        unknown.action == "search_new_source",
    )


    print("=" * 64)
    print("DataPilot Acquisition Strategy Regression: 3/3 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
