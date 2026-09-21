from data_tools import run_data_pipeline


if __name__ == "__main__":
    result = run_data_pipeline(
        file_path="test_weather.csv",
        output_directory="outputs",
    )

    print("数据处理完成")
    print()

    print("处理前数据质量：")
    print(result["before_quality"])

    print()

    print("清洗过程：")
    print(result["cleaning_log"])

    print()

    print("处理后数据质量：")
    print(result["after_quality"])

    print()

    print("统计结果：")
    print(result["statistics"])

    print()

    print("图表文件：")
    print(result["chart_path"])

    print()

    print("Excel 文件：")
    print(result["excel_path"])