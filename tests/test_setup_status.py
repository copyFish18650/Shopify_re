import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from data_manager import _setup_complete
from store_setup import StoreSetup


class SetupStatusTests(unittest.TestCase):
    def make_setup(self, csv_path=""):
        bot = SimpleNamespace(
            page=SimpleNamespace(url="https://admin.shopify.com/store/test-shop/settings/legal"),
            data={"产品CSV": csv_path}, _log=Mock(), _handle_skip_offer=Mock(),
        )
        setup = StoreSetup(bot)
        setup._dismiss_modals = Mock()
        setup.setup_store_profile = Mock(side_effect=RuntimeError("找不到店铺地址编辑入口"))
        setup.setup_return_rules = Mock()
        setup.setup_written_policies = Mock(return_value=["当前店铺无 Terms of sale 入口，已跳过该项"])
        setup.import_products = Mock()
        return setup

    def test_reported_case_is_success_with_warning_and_can_be_skipped(self):
        result = self.make_setup().run()
        self.assertTrue(result["ok"])
        self.assertIn("店铺资料提示（不影响成功）：找不到店铺地址编辑入口", result["setup_notes"])
        self.assertTrue(_setup_complete({"状态": "成功", "装修状态": result["setup_notes"]}))

    def test_required_policy_failures_still_fail(self):
        for method in ("setup_return_rules", "setup_written_policies"):
            with self.subTest(method=method):
                setup = self.make_setup()
                getattr(setup, method).side_effect = RuntimeError("保存未完成")
                self.assertFalse(setup.run()["ok"])

    def test_configured_product_import_must_complete(self):
        setup = self.make_setup("products.csv")
        setup.import_products.side_effect = RuntimeError("导入未完成")
        self.assertFalse(setup.run()["ok"])
        setup.import_products.side_effect = None
        self.assertTrue(setup.run()["ok"])

    def test_failure_word_in_advisory_does_not_requeue_success(self):
        notes = "店铺资料提示（不影响成功）：加载失败；退货规则已保存；书面政策已发布"
        self.assertTrue(_setup_complete({"状态": "成功", "装修状态": notes}))

    def test_incomplete_or_failed_saved_results_are_not_skipped(self):
        for item in (
            {"状态": "失败", "装修状态": "退货规则已保存；书面政策已发布"},
            {"状态": "成功", "装修状态": "退货规则已保存；书面政策失败：保存超时"},
            {"状态": "成功", "装修状态": "退货规则失败：保存超时；书面政策已发布"},
            {"状态": "成功", "装修状态": "退货规则已保存；书面政策已发布", "产品CSV": "products.csv"},
        ):
            with self.subTest(item=item):
                self.assertFalse(_setup_complete(item))


if __name__ == "__main__":
    unittest.main()
