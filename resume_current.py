"""从当前已打开的 AdsPower 窗口继续：已登录就直接装修，未登录才注册。"""
import configparser

from adspower_client import AdsPowerClient
from bot import ShopifyBot
from data_manager import read_registration_data, write_result


def main():
    config = configparser.ConfigParser()
    if not config.read("config.ini", encoding="utf-8"):
        print("找不到 config.ini")
        return
    excel_file = config.get("settings", "excel_path")
    rows = read_registration_data(excel_file)
    if not rows:
        print("没有待处理数据。")
        return
    ads = AdsPowerClient(
        api_base=config.get("adspower", "api_base"),
        api_key=config.get("adspower", "api_key", fallback=""),
    )
    ads.check_ready()
    data = rows[0]
    bot = ShopifyBot(config, data, ads)
    result = bot.register()
    try:
        write_result(excel_file, data["_excel_row"], result)
    except Exception as e:
        print(f"写回 Excel 失败：{e}", flush=True)
    print(result, flush=True)


if __name__ == "__main__":
    main()
