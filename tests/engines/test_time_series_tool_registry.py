from tool_registry import create_default_tool_registry


def main():

    registry = create_default_tool_registry()

    print("DataPilot v5.7 Time Series Tool Registry Test")
    print("=" * 55)

    exists = registry.has("analyze_time_series")

    print("tool_exists:", exists)

    if exists:
        tool = registry.get("analyze_time_series")

        print("tool:", tool.name)
        print("category:", tool.category)
        print("description:", tool.description)


if __name__ == "__main__":
    main()
