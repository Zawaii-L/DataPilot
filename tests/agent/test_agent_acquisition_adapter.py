from acquisition_adapter import build_acquisition_instruction
from acquisition_router import build_acquisition_route
from source_strategy import build_source_strategy


def check(title, condition):
    if not condition:
        raise AssertionError(title)

    print(f"PASS  {title}")


def main():

    strategy = build_source_strategy(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    check(
        "澳门天气 Source Strategy 为 Registry 优先",
        strategy.strategy == "registry_first",
    )


    route = build_acquisition_route(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    check(
        "澳门天气 Acquisition Route 正确",
        route.route == "verified_source",
    )

    check(
        "澳门天气不需要搜索",
        route.should_search_web is False,
    )


    instruction = build_acquisition_instruction(
        domain="weather",
        location="澳门",
        data_type="observation",
    )

    print(instruction.to_dict())

    check(
        "Agent 指令动作为验证源获取",
        instruction.action == "use_verified_source",
    )

    check(
        "Agent 指令包含来源",
        len(instruction.preferred_sources) > 0,
    )

    unknown = build_acquisition_instruction(
        domain="weather",
        location="未知地区",
        data_type="observation",
    )

    check(
        "未知地区允许搜索",
        unknown.action == "search_new_source",
    )


    print("=" * 64)
    print(
        "DataPilot Agent Acquisition Adapter Regression: 6/6 PASS"
    )
    print("=" * 64)


if __name__ == "__main__":
    main()
