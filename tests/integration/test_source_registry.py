from source_registry.loader import build_default_source_registry, query_sources


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    registry = build_default_source_registry()

    sources = registry.all_sources()

    check(
        "Source Registry 可以正常加载",
        len(sources) > 0,
    )


    macau = query_sources(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    check(
        "澳门天气观测源可以查询",
        len(macau) > 0,
    )

    check(
        "澳门天气源状态正确",
        macau[0].status == "verified",
    )

    check(
        "澳门天气源包含核心字段",
        (
            "temperature" in macau[0].fields
            and
            "humidity" in macau[0].fields
            and
            "precipitation" in macau[0].fields
            and
            "wind_speed" in macau[0].fields
        ),
    )


    zhuhai = query_sources(
        domain="weather",
        location="珠海",
        data_type="observation",
    )

    check(
        "珠海候选天气源可以查询",
        len(zhuhai) > 0,
    )


    finance = query_sources(
        domain="finance",
        location="澳门",
    )

    check(
        "不同领域查询不会错误匹配",
        len(finance) == 0,
    )


    print("=" * 64)
    print(
        "DataPilot Source Registry Regression: 6/6 PASS"
    )
    print("=" * 64)


if __name__ == "__main__":
    main()
