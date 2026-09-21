from source_strategy import build_source_strategy


def check(title, condition):
    if not condition:
        raise AssertionError(title)
    print(f"PASS  {title}")


def main():

    macau = build_source_strategy(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    print(macau.to_dict())

    check(
        "澳门天气使用 Registry 优先策略",
        macau.strategy == "registry_first",
    )

    check(
        "澳门存在推荐来源",
        len(macau.recommended_sources) > 0,
    )


    unknown = build_source_strategy(
        domain="weather",
        location="未知城市",
        data_type="observation",
    )

    check(
        "未知来源进入搜索 fallback",
        unknown.strategy == "web_search_fallback",
    )


    print("=" * 64)
    print("DataPilot Source Strategy Regression: 3/3 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
