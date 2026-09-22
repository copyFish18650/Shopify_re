import configparser

from adspower_client import AdsPowerClient
from bot import ShopifyBot
from data_manager import read_registration_data, write_result


def main():
    print("正在读取配置文件...")
    config = configparser.ConfigParser()
    if not config.read("config.ini", encoding="utf-8"):
        print("找不到 config.ini，脚本退出。")
        return

    excel_file = config.get("settings", "excel_path")
    print(f"正在读取 Excel：{excel_file}")
    register_list = read_registration_data(excel_file)
    if not register_list:
        print("没有待处理的注册数据。请打开 stores.xlsx 的「注册信息」表填写后重试。")
        return

    ads = AdsPowerClient(
        api_base=config.get("adspower", "api_base"),
        api_key=config.get("adspower", "api_key", fallback=""),
    )
    ads.check_ready()
    print("AdsPower 本地 API 已连通。")

    results = []
    total = len(register_list)
    for i, data in enumerate(register_list, 1):
        print(f"\n{'=' * 20} 处理第 {i}/{total} 条数据 {'=' * 20}")
        shop_name = data.get("店铺名", "未知")
        try:
            bot = ShopifyBot(config, data, ads)
            result = bot.register()
        except Exception as e:
            print(f"处理店铺 {shop_name} 时发生未捕获错误：{e}")
            result = {"status": "failed", "shop_name": shop_name, "error": str(e)}
        results.append(result)
        try:
            write_result(excel_file, data["_excel_row"], result)
        except Exception as e:
            print(f"写回 Excel 失败：{e}")

    print("\n" + "=" * 60)
    print("【处理结果汇总】（退货规则、书面政策完成即成功，不做主题）")
    success_count = sum(1 for res in results if res.get("status") == "success")
    print(f"成功：{success_count} 条 | 失败：{len(results) - success_count} 条")
    print("-" * 60)
    for res in results:
        shop_name = res.get("shop_name", "未知")
        if res.get("status") == "success":
            print(f"[成功] 店铺: {shop_name} | {res.get('setup_notes', '')} | 后台: {res.get('admin_url', '')}")
        else:
            print(f"[失败] 店铺: {shop_name} | 原因: {res.get('error', '无详情')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
