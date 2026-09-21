from clarification_gate import ClarificationGate


def check(title, condition):
    if not condition:
        raise AssertionError(title)
    print(f"PASS  {title}")


def main():
    # 天气
    normal_weather = ClarificationGate.evaluate("帮我分析澳门近期天气")
    check("常规天气不打断", not normal_weather.needs_clarification)

    detailed_weather = ClarificationGate.evaluate(
        "帮我做非常详细的澳门近期天气变化分析"
    )
    check("详细天气未给粒度时澄清", detailed_weather.needs_clarification)

    explicit_weather = ClarificationGate.evaluate(
        "帮我做澳门近期逐小时的详细天气变化分析"
    )
    check("详细天气已明确逐小时不再询问", not explicit_weather.needs_clarification)

    # 金融
    normal_finance = ClarificationGate.evaluate(
        "分析一下这只股票最近的价格走势"
    )
    check("常规金融分析不打断", not normal_finance.needs_clarification)

    detailed_finance = ClarificationGate.evaluate(
        "详细分析这只股票最近的价格走势"
    )
    check("详细金融任务未给粒度时澄清", detailed_finance.needs_clarification)
    check(
        "金融澄清使用金融粒度",
        detailed_finance.questions
        and detailed_finance.questions[0].key == "finance_analysis_granularity",
    )

    explicit_finance = ClarificationGate.evaluate(
        "详细分析这只股票最近的日线走势"
    )
    check("金融已明确日线不再询问", not explicit_finance.needs_clarification)

    # 经营/销售
    normal_business = ClarificationGate.evaluate(
        "分析这个销售表并生成Word报告"
    )
    check("常规经营分析不打断", not normal_business.needs_clarification)

    detailed_business = ClarificationGate.evaluate(
        "详细分析这个销售表"
    )
    check("详细经营任务未给维度时澄清", detailed_business.needs_clarification)

    explicit_business = ClarificationGate.evaluate(
        "详细分析这个销售表，按地区和产品拆分"
    )
    check("经营任务已明确维度不再询问", not explicit_business.needs_clarification)

    # 未知领域也不能失效
    generic = ClarificationGate.evaluate(
        "对这个数据集做非常详细的分析"
    )
    check("未知领域详细任务使用通用澄清", generic.needs_clarification)
    check(
        "未知领域使用通用 analysis_granularity",
        generic.questions
        and generic.questions[0].key == "analysis_granularity",
    )

    print("=" * 68)
    print("DataPilot Generic Clarification Gate Regression: 12/12 PASS")
    print("=" * 68)


if __name__ == "__main__":
    main()
