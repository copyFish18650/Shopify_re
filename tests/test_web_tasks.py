import configparser
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from adspower_client import AdsPowerAPIError, AdsPowerClient
from profile_source import parse_profile
from web_backend import make_server
from web_tasks import Tasks, WebBot as RealWebBot, public_job, local_api_url


PROFILE = {"country": "es", "country_name": "Spain", "name": "Example Person", "address": "Example road 1",
           "city": "Madrid", "postal_code": "28001", "phone": "+34910000000", "province": "M", "synthetic": True}


class SourceTests(unittest.TestCase):
    def html(self, country="ES"):
        values = {"givenname": "Example", "surname": "Person", "streetaddress": "Example road 1", "city": "Madrid",
                  "state": "M", "statefull": "Madrid", "zipcode": "28001", "country": country,
                  "telephonecountrycode": "34", "telephonenumber": "910 000 000", "ccnumber": "DO-NOT-IMPORT"}
        items = [{key: i + 1 for i, key in enumerate(values)}] + list(values.values())
        return '<script type="application/json" id="__NUXT_DATA__">' + json.dumps(items) + '</script>'

    def test_extract_only_requested_profile_fields(self):
        result = parse_profile(self.html(), "es")
        self.assertEqual(result["phone"], "+34910000000")
        self.assertEqual(result["name"], "Example Person")
        self.assertEqual(result["first_name"], "Example")
        self.assertEqual(result["last_name"], "Person")
        self.assertEqual(result["province"], "M")
        self.assertNotIn("DO-NOT-IMPORT", json.dumps(result))

    def test_reject_country_mismatch_and_changed_markup(self):
        for html in (self.html("FR"), "<html>Unavailable</html>"):
            with self.assertRaises(ValueError):
                parse_profile(html, "es")

    def test_loopback_configuration_only(self):
        self.assertEqual(local_api_url("http://127.0.0.1:50325/"), "http://127.0.0.1:50325")
        for url in ("http://example.com:50325", "file:///config.ini", "http://127.0.0.1:50325@evil.com", "http://localhost:bad"):
            with self.assertRaises(ValueError):
                local_api_url(url)


class TaskTests(unittest.TestCase):
    def setUp(self):
        # Unit fixtures only: production DPAPI is validated separately under the real Windows user.
        self.crypto = patch("web_storage.protect", side_effect=lambda raw, decrypt=False: raw)
        self.crypto.start()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.tasks = Tasks(self.path, start_worker=False)
        config = configparser.ConfigParser()
        config.read_dict({"settings": {"excel_path": "stores.xlsx", "imap_server": "example.com"},
                          "adspower": {"api_base": "http://127.0.0.1:50325", "api_key": "test-api-secret"}})
        self.tasks.config = Mock(return_value=config)
        self.ads = Mock()
        self.ads.create_profile.return_value = "test-profile-id"
        self.ads.find_task_profile.return_value = None
        self.tasks.ads = Mock(return_value=self.ads)
        self.fetch = patch("web_tasks.fetch_profile", return_value=PROFILE.copy())
        self.fetch_mock = self.fetch.start()
        self.bot_patch = patch("web_tasks.WebBot")
        self.bot = self.bot_patch.start()
        self.bot.return_value.register.return_value = {"status": "success", "password": "hidden", "setup_notes": "done"}

    def tearDown(self):
        self.bot_patch.stop(); self.fetch.stop()
        self.tasks.store.close(); self.temp.cleanup(); self.crypto.stop()

    def submit(self, **extra):
        return self.tasks.submit({"email": "owner@example.com", "country": "es", **extra})["id"]

    def test_create_profile_and_persist_id_and_generated_password(self):
        job_id = self.submit()
        self.tasks.execute(job_id)
        job = self.tasks.store.get(job_id)
        self.assertEqual(job["state"], "success")
        self.assertEqual(job["payload"]["profile_id"], "test-profile-id")
        self.assertGreaterEqual(len(job["payload"]["shopify_password"]), 12)
        self.assertNotIn("password", job["result"])
        self.assertNotIn("shopify_password", public_job(job))
        self.ads.create_profile.assert_called_once()

    def test_retry_reuses_profile_and_identity(self):
        self.bot.return_value.register.return_value = {"status": "failed", "error": "temporary"}
        job_id = self.submit()
        self.tasks.execute(job_id)
        saved = self.tasks.store.get(job_id)["payload"].copy()
        self.tasks.retry(job_id)
        self.bot.return_value.register.return_value = {"status": "success"}
        self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "success")
        self.assertEqual(self.tasks.store.get(job_id)["payload"], saved)
        self.ads.create_profile.assert_called_once()
        self.fetch_mock.assert_called_once()

    def test_structured_contact_names_are_passed_to_registration(self):
        self.fetch_mock.return_value = {**PROFILE, "name": "Sofia de la Cruz",
                                       "first_name": "Sofia", "last_name": "de la Cruz"}
        job_id = self.submit()
        self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "success")
        data = self.bot.call_args.args[1]
        self.assertEqual(data["联系人姓名"], "Sofia de la Cruz")
        self.assertEqual(data["联系人名"], "Sofia")
        self.assertEqual(data["联系人姓"], "de la Cruz")

    def test_completed_setup_steps_are_persisted_and_loaded_for_next_run(self):
        job_id = self.submit()
        bot = RealWebBot(self.tasks.config(), {"AdsPower环境ID": "same-env", "邮箱": "owner@example.com",
                                              "店铺名": "test"}, self.ads, self.tasks, job_id)
        bot._checkpoint_setup("return_rules")
        bot._checkpoint_setup("return_rules")
        self.assertEqual(self.tasks.store.get(job_id)["payload"]["setup_completed"], ["return_rules"])
        self.tasks.execute(job_id)
        self.assertEqual(self.bot.call_args.args[1]["_setup_completed"], ["return_rules"])

    def test_store_address_is_persisted_while_admin_is_still_blank(self):
        job_id = self.submit()
        bot = RealWebBot(self.tasks.config(), {"AdsPower环境ID": "same-env", "邮箱": "owner@example.com",
                                              "店铺名": "test"}, self.ads, self.tasks, job_id)
        bot.page = Mock(url="https://admin.shopify.com/store/actual-store?welcome")
        bot._remember_admin()
        self.assertEqual(self.tasks.store.get(job_id)["payload"]["admin_url"],
                         "https://admin.shopify.com/store/actual-store")
        self.assertEqual(bot.admin_url, "")

    def test_ambiguous_create_failure_never_creates_twice(self):
        self.ads.create_profile.side_effect = requests.Timeout("unknown outcome")
        job_id = self.submit()
        self.tasks.execute(job_id)
        self.assertTrue(self.tasks.store.get(job_id)["payload"]["creation_uncertain"])
        self.ads.find_task_profile.return_value = "recovered-id"
        self.tasks.retry(job_id); self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["payload"]["profile_id"], "recovered-id")
        self.ads.create_profile.assert_called_once()

    def test_automatic_retry_recovers_created_environment_without_creating_again(self):
        self.ads.create_profile.side_effect = requests.Timeout("unknown outcome")
        self.ads.find_task_profile.return_value = "recovered-id"
        job_id = self.submit()
        self.tasks.execute(job_id)
        job = self.tasks.store.get(job_id)
        self.assertEqual(job["state"], "success")
        self.assertEqual(job["result"]["auto_retry_count"], 1)
        self.assertEqual(job["payload"]["profile_id"], "recovered-id")
        self.ads.create_profile.assert_called_once()
        self.ads.find_task_profile.assert_called_once_with("shopflow:" + job_id)

    def test_explicit_rejection_allows_retry_without_manual_binding(self):
        self.ads.create_profile.side_effect = AdsPowerAPIError("quota")
        job_id = self.submit(); self.tasks.execute(job_id)
        self.assertFalse(self.tasks.store.get(job_id)["payload"]["creation_uncertain"])
        self.ads.create_profile.side_effect = None
        self.tasks.retry(job_id); self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "success")

    def test_idempotent_submission(self):
        job_id = self.submit()
        same = self.tasks.submit({"email": "owner@example.com", "country": "es", "request_id": job_id})
        self.assertEqual(same["id"], job_id)
        self.assertEqual(len(self.tasks.store.jobs()), 1)

    def test_cancel_queued_job_without_creating_environment(self):
        job_id = self.submit(); self.tasks.cancel(job_id)
        with self.assertRaises(ValueError):
            self.tasks.retry(job_id)
        self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "cancelled")
        self.ads.create_profile.assert_not_called()

    def test_manual_code_handoff_and_redaction(self):
        job_id = self.submit(email_password="private-password")
        answers = []
        thread = threading.Thread(target=lambda: answers.append(self.tasks.ask(job_id, "Enter code", "otp")))
        thread.start()
        deadline = time.monotonic() + 3
        while self.tasks.store.get(job_id)["state"] != "waiting" and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            with self.assertRaises(ValueError):
                self.tasks.resume(job_id, "abc")
            self.tasks.resume(job_id, "123456")
        finally:
            thread.join(timeout=3)
        self.assertEqual(answers, ["123456"])
        self.tasks.log(job_id, "private-password test-api-secret 验证码：123456 ws://localhost:123/test")
        text = self.tasks.store.logs(job_id)[-1]["message"]
        for secret in ("private-password", "test-api-secret", "123456", "ws://"):
            self.assertNotIn(secret, text)

    def test_restart_marks_pending_tasks_interrupted(self):
        job_id = self.submit()
        self.tasks.store.close()
        from web_storage import Store
        self.tasks.store = Store(self.path)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "interrupted")

    def test_http_auth_and_origin_checks(self):
        server = make_server(0, self.tasks)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            self.assertEqual(requests.get(base + "/api/jobs").status_code, 403)
            bootstrap = requests.get(base + "/api/bootstrap").json()
            headers = {"X-ShopFlow-Token": bootstrap["token"]}
            self.assertEqual(requests.get(base + "/api/jobs", headers=headers).status_code, 200)
            self.assertEqual(requests.post(base + "/api/profile", json={"country": "es"},
                                          headers={**headers, "Origin": "https://untrusted.example"}).status_code, 403)
            self.assertEqual(requests.get(base + "/api/bootstrap", headers={"Host": "untrusted.example"}).status_code, 403)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


class AdsPowerTests(unittest.TestCase):
    def test_deleted_default_group_uses_only_matching_shop_group(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads.list_groups = Mock(return_value=[{"group_id": "other", "group_name": "Other workspace"},
                                            {"group_id": "shops", "group_name": "Shopify_register"}])
        seen = []

        def create(_path, payload):
            seen.append(dict(payload))
            if payload["group_id"] == "0":
                raise AdsPowerAPIError("group is deleted or archived")
            return {"profile_id": "new-profile"}

        ads._api_data = Mock(side_effect=create)
        proxy = {"proxy_soft": "other", "proxy_type": "socks5", "proxy_host": "proxy.example", "proxy_port": "443"}
        self.assertEqual(ads.create_profile("Shop", "a@example.com", "marker", proxy_config=proxy), "new-profile")
        self.assertEqual([item["group_id"] for item in seen], ["0", "shops"])
        self.assertEqual(seen[0]["user_proxy_config"], seen[1]["user_proxy_config"])

    def test_unknown_creation_outcome_never_retries_with_another_group(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._api_data = Mock(side_effect=requests.Timeout("unknown"))
        ads.list_groups = Mock()
        with self.assertRaises(requests.Timeout):
            ads.create_profile("Shop", "a@example.com", "marker")
        ads._api_data.assert_called_once()
        ads.list_groups.assert_not_called()

    def test_explicit_group_is_not_replaced_when_rejected(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._api_data = Mock(side_effect=AdsPowerAPIError("group is deleted or archived"))
        ads.list_groups = Mock()
        with self.assertRaises(AdsPowerAPIError):
            ads.create_profile("Shop", "a@example.com", "marker", group_id="selected-group")
        ads._api_data.assert_called_once()
        ads.list_groups.assert_not_called()

    def test_default_group_failure_does_not_assign_unrelated_workspace(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._api_data = Mock(side_effect=AdsPowerAPIError("group is deleted or archived"))
        ads.list_groups = Mock(return_value=[{"group_id": "other", "group_name": "Other workspace"}])
        with self.assertRaises(AdsPowerAPIError):
            ads.create_profile("Shop", "a@example.com", "marker")
        ads._api_data.assert_called_once()

    def test_create_sends_required_fields_and_returns_id(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._post = Mock(return_value=Mock(json=lambda: {"code": 0, "data": {"profile_id": "abc"}}))
        self.assertEqual(ads.create_profile("Example", "owner@example.com", "marker"), "abc")
        body = ads._post.call_args.kwargs["json"]
        self.assertEqual(body["user_proxy_config"], {"proxy_soft": "no_proxy"})
        self.assertEqual(body["remark"], "marker")
        self.assertEqual(body["group_id"], "0")
        self.assertEqual(body["fingerprint_config"]["random_ua"],
                         {"ua_browser": ["chrome"], "ua_system_version": ["Windows 10", "Windows 11"]})
        self.assertEqual(body["fingerprint_config"]["screen_resolution"], "none")

    def test_kernel_download_waits_then_succeeds(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._start_v2 = Mock(side_effect=[
            (None, "SunBrowser is updating, waiting for download."),
            (None, "SunBrowser 151 is not ready,please to download!"),
            ("ws://test", None),
        ])
        on_wait = Mock()
        with patch("adspower_client.time.sleep"):
            self.assertEqual(ads.start("abc", on_wait=on_wait), "ws://test")
        self.assertEqual(on_wait.call_count, 2)
        self.assertEqual(ads._start_v2.call_count, 3)

    def test_kernel_not_ready_still_respects_wait_timeout(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._start_v2 = Mock(return_value=(None, "SunBrowser 151 is not ready,please to download!"))
        ads._start_v1 = Mock()
        with patch("adspower_client.time.sleep"), patch("adspower_client.time.monotonic", side_effect=[0, 301]):
            with self.assertRaisesRegex(RuntimeError, "下载完成后重试"):
                ads.start("abc", wait_timeout=300)
        ads._start_v2.assert_called_once_with("abc")
        ads._start_v1.assert_not_called()

    def test_proxy_failure_does_not_wait_for_kernel(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._start_v2 = Mock(return_value=(None, "proxy not ready"))
        on_wait = Mock()
        with patch("adspower_client.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "proxy not ready"):
                ads.start("abc", on_wait=on_wait)
        on_wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
