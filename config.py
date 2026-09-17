import os

from dotenv import load_dotenv


# 加载 .env 文件
load_dotenv()


OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    "",
).strip()

OPENAI_BASE_URL = os.getenv(
    "OPENAI_BASE_URL",
    "https://api.deepseek.com",
).strip()

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "deepseek-chat",
).strip()


def check_config():
    """
    检查大模型 API 配置是否完整。
    """

    if not OPENAI_API_KEY:
        raise ValueError(
            "没有找到 OPENAI_API_KEY。"
            "请检查 F:\\DataPilot\\.env 文件。"
        )

    if not OPENAI_BASE_URL:
        raise ValueError(
            "没有找到 OPENAI_BASE_URL。"
        )

    if not OPENAI_MODEL:
        raise ValueError(
            "没有找到 OPENAI_MODEL。"
        )

    return True


if __name__ == "__main__":
    try:
        check_config()

        print("API 配置读取成功")
        print("API 地址：", OPENAI_BASE_URL)
        print("模型名称：", OPENAI_MODEL)
        print("API Key：已读取")

    except Exception as error:
        print("配置检查失败：", error)