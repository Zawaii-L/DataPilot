from acquisition_router import build_acquisition_route


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    macau = build_acquisition_route(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    print(macau.to_dict())

    check(
        "澳门天气进入 verified_source 路由",
        macau.route == "verified_source",
    )

    check(
        "澳门天气禁止盲目搜索",
        macau.should_search_web is False,
    )

    check(
        "澳门路由包含推荐来源",
        len(macau.recommended_sources) > 0,
    )


    unknown = build_acquisition_route(
        domain="weather",
        location="未知地区",
        data_type="observation",
    )

    check(
        "未知地区进入 web_search 路由",
        unknown.route == "web_search",
    )

    check(
        "未知地区允许搜索",
        unknown.should_search_web is True,
    )


    print("=" * 64)
    print("DataPilot Acquisition Router Regression: 5/5 PASS")
    print("=" * 64)


if __name__ == "__main__":
    main()
