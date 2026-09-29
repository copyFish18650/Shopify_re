import configparser
import copy
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from adspower_client import AdsPowerClient
from cliproxy_client import CliproxyClient, ExtractionRejected, ExtractionUncertain, parse_proxy
from web_tasks import Tasks, WebBot as RealWebBot, public_job


PROFILE = {"country": "es", "country_name": "Spain", "name": "Test Person", "address": "Test 1",
           "city": "Madrid", "postal_code": "28001", "phone": "+34910000000", "province": "M"}
PROXY = {"config": parse_proxy("proxy.example:20001:proxy-user:proxy-password"), "fetched_at": "2026-09-23T00:00:00+00:00", "country": "ES"}


class ProviderTests(unittest.TestCase):
    def test_invalid_starting_ports_are_rejected_before_any_request(self):
        client = CliproxyClient("private-key")
        client.session.get = Mock()
        for port in (0, 442, 3001, 20000, 65535, True, "443"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                client.extract("es", port)
        client.session.get.assert_not_called()

    def response(self, raw, status=200):
        response = Mock(status_code=status)
        response.iter_content.return_value = [raw.encode("utf-8")]
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        return manager

    def test_single_extraction_uses_documented_format_country_and_unique_port(self):
        client = CliproxyClient("private-key")
        client.session.get = Mock(return_value=self.response("proxy.example:443:user:password\n"))
        result = client.extract("uk", 443)
        params = client.session.get.call_args.kwargs
        self.assertEqual(params["params"]["country"], "GB")
        self.assertEqual(params["params"]["num"], 1)
        self.assertEqual(params["params"]["type"], 2)
        self.assertEqual(params["params"]["port"], 443)
        self.assertFalse(params["allow_redirects"])
        self.assertEqual(result["config"]["proxy_type"], "socks5")

    def test_timeout_is_not_retried_or_leaked(self):
        client = CliproxyClient("private-key")
        client.session.get = Mock(side_effect=requests.Timeout("https://example/?key=private-key"))
        with self.assertRaises(ExtractionUncertain) as error:
            client.extract("es", 443)
        self.assertNotIn("private-key", str(error.exception))
        client.session.get.assert_called_once()

    def test_unknown_response_stops_and_explicit_rejection_is_distinct(self):
        client = CliproxyClient("private-key")
        for raw in ("", "<html>error</html>", "a.example:1:u:p\nb.example:2:u:p", '{"data": {"unknown": 1}}'):
            client.session.get = Mock(return_value=self.response(raw))
            with self.assertRaises(ExtractionUncertain):
                client.extract("es", 443)
        client.session.get = Mock(return_value=self.response('{"code": -1, "msg": "private-key"}'))
        with self.assertRaises(ExtractionRejected) as error:
            client.extract("es", 443)
        self.assertNotIn("private-key", str(error.exception))

    def test_ads_custom_proxy_is_used_without_saved_proxy_id(self):
        ads = AdsPowerClient("http://127.0.0.1:50325")
        ads._api_data = Mock(return_value={"profile_id": "new-id"})
        ads.create_profile("store", "a@example.com", "task", "old-proxy", proxy_config=PROXY["config"])
        body = ads._api_data.call_args.args[1]
        self.assertNotIn("proxyid", body)
        self.assertEqual(body["user_proxy_config"], PROXY["config"])
        ads.update_proxy("new-id", PROXY["config"])
        self.assertEqual(ads._api_data.call_args.args[1]["profile_id"], "new-id")

    def test_rejection_preserves_provider_code_and_reason(self):
        client = CliproxyClient("private-key")
        client.session.get = Mock(return_value=self.response('{"code": 1003, "msg": "Invalid port range"}'))
        with self.assertRaises(ExtractionRejected) as error:
            client.extract("es", 443)
        self.assertIn("1003", str(error.exception))
        self.assertIn("Invalid port range", str(error.exception))
        client.session.get.assert_called_once()

    def test_rejection_masks_echoed_auth_and_proxy_credentials(self):
        client = CliproxyClient("private-key")
        body = {"code": -1, "message": "private-key https://example.test/?token=secret "
                "proxy.example:443:account:password password=private-pass"}
        client.session.get = Mock(return_value=self.response(json.dumps(body)))
        with self.assertRaises(ExtractionRejected) as error:
            client.extract("es", 443)
        for value in ("private-key", "secret", "account", "private-pass", "proxy.example"):
            self.assertNotIn(value, str(error.exception))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.crypto = patch("web_storage.protect", side_effect=lambda raw, decrypt=False: raw)
        self.crypto.start()
        self.temp = tempfile.TemporaryDirectory()
        self.tasks = Tasks(Path(self.temp.name) / "test.sqlite3", start_worker=False)
        config = configparser.ConfigParser()
        config.read_dict({"settings": {}, "adspower": {"api_base": "http://127.0.0.1:50325"}})
        self.tasks.config = Mock(return_value=config)
        self.tasks.save_settings({"cliproxy_key": "private-key", "proxy_source": "cliproxy"})
        self.ads = Mock()
        self.ads.create_profile.side_effect = ["env-1", "env-2", "env-3"]
        self.tasks.ads = Mock(return_value=self.ads)
        self.fetch = patch("web_tasks.fetch_profile", side_effect=lambda c: copy.deepcopy(PROFILE))
        self.fetch_mock = self.fetch.start()
        self.provider = patch("web_tasks.CliproxyClient")
        self.client = self.provider.start().return_value
        self.client.extract.side_effect = lambda country, port: {**copy.deepcopy(PROXY), "requested_port": port}
        self.bot_patch = patch("web_tasks.WebBot")
        self.bot = self.bot_patch.start()
        self.bot.return_value.register.side_effect = lambda: {"status": "success"}

    def tearDown(self):
        self.bot_patch.stop()
        self.provider.stop()
        self.fetch.stop()
        self.tasks.store.close()
        self.temp.cleanup()
        self.crypto.stop()

    def batch(self, **kw):
        return self.tasks.submit_batch({"country": "es", "emails": "a@example.com\nb@example.com", **kw})

    def test_validate_entire_batch_before_queueing_anything(self):
        for emails in ("a@example.com\ninvalid", "a@example.com\nA@example.com", "\n", "\n".join("a{}@example.com".format(i) for i in range(101))):
            with self.assertRaises(ValueError):
                self.batch(emails=emails)
            self.assertEqual(self.tasks.store.jobs(), [])
            self.assertTrue(self.tasks.queue.empty())
        self.client.extract.assert_not_called()

    def test_batch_idempotency_and_changed_body_rejected(self):
        request_id = str(uuid.uuid4())
        first = self.batch(request_id=request_id)
        second = self.batch(request_id=request_id)
        self.assertEqual(first, second)
        self.assertEqual(self.tasks.queue.qsize(), 2)
        with self.assertRaises(ValueError):
            self.batch(request_id=request_id, country="fr")
        self.assertEqual(len(self.tasks.store.jobs()), 2)

    def test_separate_contacts_proxies_ports_and_environments_just_in_time(self):
        batch = self.batch(emails="a@example.com----app-pass----store-a\nb@example.com\tother-pass\tstore-b")
        self.client.extract.assert_not_called()
        self.fetch_mock.assert_not_called()
        for job in batch["jobs"]:
            self.tasks.execute(job["id"])
        jobs = [self.tasks.store.get(j["id"]) for j in batch["jobs"]]
        self.assertEqual([j["state"] for j in jobs], ["success", "success"])
        self.assertEqual([j["payload"]["proxy_lease"]["requested_port"] for j in jobs], [443, 444])
        self.assertEqual([j["payload"]["profile_id"] for j in jobs], ["env-1", "env-2"])
        self.assertEqual(jobs[0]["payload"]["email_password"], "app-pass")
        self.assertNotEqual(jobs[0]["payload"]["shopify_password"], jobs[1]["payload"]["shopify_password"])
        self.assertEqual(self.fetch_mock.call_count, 2)
        self.assertEqual(self.ads.create_profile.call_count, 2)
        self.assertEqual(self.ads.create_profile.call_args.kwargs["proxy_config"], PROXY["config"])
        for job in jobs:
            exposed = json.dumps(public_job(job)) + json.dumps(self.tasks.store.logs(job["id"]))
            for secret in ("private-key", "proxy-user", "proxy-password", "app-pass"):
                self.assertNotIn(secret, exposed)

    def test_failed_shop_retry_reuses_saved_proxy(self):
        self.bot.return_value.register.side_effect = lambda: {"status": "failed", "error": "temporary"}
        job_id = self.batch(emails="a@example.com")["jobs"][0]["id"]
        self.tasks.execute(job_id)
        self.tasks.retry(job_id)
        self.tasks.execute(job_id)
        self.client.extract.assert_called_once()
        self.ads.create_profile.assert_called_once()

    def test_legacy_out_of_range_counter_is_migrated_before_extraction(self):
        saved = self.tasks.store.get_settings()
        saved["cliproxy_next_port"] = 20002
        self.tasks.store.save_settings(saved)
        self.assertEqual(self.tasks.settings()["cliproxy_next_port"], 443)
        job_id = self.batch(emails="a@example.com")["jobs"][0]["id"]
        self.tasks.execute(job_id)
        self.client.extract.assert_called_once_with("es", 443)
        self.assertEqual(self.tasks.store.get_settings()["cliproxy_next_port"], 444)

    def test_port_wrap_skips_existing_and_unconfirmed_reservations(self):
        existing, pending, last, next_job = [j["id"] for j in self.batch(
            emails="a@example.com\nb@example.com\nc@example.com\nd@example.com")["jobs"]]
        self.tasks.execute(existing)
        pending_payload = self.tasks.store.get(pending)["payload"]
        pending_payload.update({"extraction_uncertain": True, "extraction_port": 444})
        self.tasks.store.update(pending, payload=pending_payload)
        saved = self.tasks.store.get_settings()
        saved["cliproxy_next_port"] = 3000
        self.tasks.store.save_settings(saved)
        self.tasks.execute(last)
        self.tasks.execute(next_job)
        self.assertEqual([call.args[1] for call in self.client.extract.call_args_list], [443, 3000, 445])

    def test_exhausted_port_pool_makes_no_paid_request(self):
        jobs = [{"payload": {"proxy_lease": {"requested_port": port}}} for port in range(443, 3001)]
        with patch.object(self.tasks.store, "jobs", return_value=jobs):
            with self.assertRaises(ValueError):
                self.tasks._available_proxy_port({})
        self.client.extract.assert_not_called()

    def test_ambiguous_extraction_requires_recovery_and_never_repeats(self):
        self.client.extract.side_effect = ExtractionUncertain("unknown")
        job_id = self.batch(emails="a@example.com")["jobs"][0]["id"]
        self.tasks.execute(job_id)
        self.assertTrue(self.tasks.store.get(job_id)["payload"]["extraction_uncertain"])
        self.tasks.retry(job_id)
        self.tasks.execute(job_id)
        self.client.extract.assert_called_once()
        self.ads.create_profile.assert_not_called()
        self.tasks.change_proxy(job_id, "proxy.example:443:recovered:password")
        self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["state"], "success")
        self.client.extract.assert_called_once()

    def test_explicit_rejection_can_retry_and_replacement_updates_same_environment(self):
        self.client.extract.side_effect = ExtractionRejected("no stock")
        job_id = self.batch(emails="a@example.com")["jobs"][0]["id"]
        self.tasks.execute(job_id)
        self.assertFalse(self.tasks.store.get(job_id)["payload"]["extraction_uncertain"])
        self.client.extract.side_effect = lambda country, port: copy.deepcopy(PROXY)
        self.bot.return_value.register.side_effect = lambda: {"status": "failed"}
        self.tasks.retry(job_id)
        self.tasks.execute(job_id)
        self.tasks.change_proxy(job_id)
        self.tasks.execute(job_id)
        self.assertEqual(self.client.extract.call_count, 4)
        self.ads.create_profile.assert_called_once()
        self.ads.update_proxy.assert_called_once_with("env-1", PROXY["config"])

    def test_batch_stop_does_not_consume_ip_and_can_retry_unfinished(self):
        batch = self.batch()
        result = self.tasks.batch_action(batch["batch_id"], "cancel")
        self.assertEqual(result["changed"], 2)
        for job in batch["jobs"]:
            self.tasks.execute(job["id"])
        self.client.extract.assert_not_called()
        result = self.tasks.batch_action(batch["batch_id"], "retry")
        self.assertEqual(result["changed"], 2)

    def test_settings_hide_key_and_cannot_reset_port_during_batch(self):
        self.assertNotIn("private-key", json.dumps(self.tasks.settings()))
        self.batch()
        with self.assertRaises(ValueError):
            self.tasks.save_settings({"cliproxy_next_port": 30000})

    def test_first_failure_retries_once_before_next_ip_is_extracted(self):
        batch = self.batch()
        self.bot.return_value.register.side_effect = [{"status": "failed"}, {"status": "success"}, {"status": "success"}]
        for job in batch["jobs"]:
            self.tasks.execute(job["id"])
        self.assertEqual([self.tasks.store.get(j["id"])["state"] for j in batch["jobs"]], ["success", "success"])
        self.assertEqual(self.client.extract.call_count, 2)
        self.assertEqual(self.ads.create_profile.call_count, 2)
        self.assertEqual([call.args[1]["AdsPower环境ID"] for call in self.bot.call_args_list], ["env-1", "env-1", "env-2"])
        self.assertEqual(public_job(self.tasks.store.get(batch["jobs"][0]["id"]))["auto_retry_count"], 1)
        self.assertFalse(self.tasks.queue_status()["paused"])

    def test_first_page_failure_is_marked_and_next_job_runs_with_its_own_ip(self):
        first, second = [j['id'] for j in self.batch()['jobs']]
        self.bot.return_value.register.side_effect = [
            {'status': 'failed', 'failure_code': 'first_page_unavailable', 'error': '首屏超时'},
            {'status': 'failed', 'failure_code': 'first_page_unavailable', 'error': '首屏超时'},
            {'status': 'success'},
        ]
        self.tasks.execute(first)
        original = self.tasks.store.get(first)
        self.assertTrue(public_job(original)['auto_skipped'])
        self.assertEqual(original['state'], 'failed')
        self.assertEqual(public_job(original)['auto_retry_count'], 1)
        self.assertEqual(self.bot.return_value.register.call_count, 2)
        self.assertEqual(self.tasks.store.get(second)['state'], 'queued')
        self.assertFalse(self.tasks.queue_status()['paused'])
        self.client.extract.assert_called_once()
        self.tasks.execute(second)
        self.assertEqual(self.tasks.store.get(second)['state'], 'success')
        self.assertEqual(self.client.extract.call_count, 2)
        self.assertEqual(self.tasks.store.get(first)['payload'], original['payload'])

    def test_setup_failure_retries_once_then_continues_with_last_error(self):
        first, second = [j['id'] for j in self.batch()['jobs']]
        self.bot.return_value.register.side_effect = [
            {'status': 'failed', 'error': 'first failure', 'setup_notes': 'policies incomplete'},
            {'status': 'failed', 'error': 'retry failure', 'setup_notes': 'policies incomplete'},
            {'status': 'success'},
        ]
        self.tasks.execute(first)
        failed = self.tasks.store.get(first)
        self.assertEqual(self.bot.return_value.register.call_count, 2)
        self.assertEqual(failed['result']['error'], 'retry failure')
        self.assertTrue(failed['result']['auto_skipped'])
        self.client.extract.assert_called_once()
        self.tasks.execute(second)
        self.assertEqual(self.tasks.store.get(second)['state'], 'success')
        self.assertEqual(self.bot.return_value.register.call_count, 3)

    def test_retry_reloads_saved_setup_progress_without_new_identity(self):
        first, second = [j['id'] for j in self.batch()['jobs']]
        original = self.tasks.store.get(first)['payload']

        def register():
            if self.bot.return_value.register.call_count == 1:
                payload = self.tasks.store.get(first)['payload']
                payload.update(setup_completed=['profile', 'return_rules'],
                               admin_url='https://admin.shopify.com/store/existing-shop')
                self.tasks.store.update(first, payload=payload)
                return {'status': 'failed', 'error': 'policies timeout'}
            data = self.bot.call_args.args[1]
            self.assertEqual(data['_setup_completed'], ['profile', 'return_rules'])
            self.assertEqual(data['店铺后台'], 'https://admin.shopify.com/store/existing-shop')
            self.assertEqual(data['Shopify密码'], original['shopify_password'])
            self.assertEqual(data['邮箱'], original['email'])
            self.assertEqual(data['AdsPower环境ID'], 'env-1')
            self.assertEqual(self.tasks.store.get(second)['state'], 'queued')
            return {'status': 'success'}

        self.bot.return_value.register.side_effect = register
        self.tasks.execute(first)
        self.assertEqual(self.tasks.store.get(first)['state'], 'success')
        self.assertEqual(self.bot.return_value.register.call_count, 2)
        self.client.extract.assert_called_once()
        self.ads.create_profile.assert_called_once()
        self.fetch_mock.assert_called_once()

    def test_browser_recovery_does_not_multiply_task_retry(self):
        from bot import ShopifyBot
        first = self.batch(emails='a@example.com')['jobs'][0]['id']
        with patch('web_tasks.WebBot', RealWebBot), \
                patch.object(ShopifyBot, '_run_browser_attempt', side_effect=RuntimeError('Browser disconnected')) as run:
            self.tasks.execute(first)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(self.tasks.store.get(first)['result']['auto_skipped'])
        self.client.extract.assert_called_once()

    def test_preparation_failure_retries_once_then_skips_without_consuming_ip(self):
        first, second = [j['id'] for j in self.batch()['jobs']]
        self.fetch_mock.side_effect = [RuntimeError('profile unavailable'), RuntimeError('profile unavailable'), copy.deepcopy(PROFILE)]
        self.tasks.execute(first)
        self.assertEqual(self.fetch_mock.call_count, 2)
        self.client.extract.assert_not_called()
        self.assertTrue(self.tasks.store.get(first)['result']['auto_skipped'])
        self.tasks.execute(second)
        self.assertEqual(self.tasks.store.get(second)['state'], 'success')
        self.client.extract.assert_called_once()

    def test_cancel_during_automatic_retry_pauses_without_third_attempt(self):
        first, second = [j['id'] for j in self.batch()['jobs']]

        def register():
            if self.bot.return_value.register.call_count == 2:
                self.tasks.cancel(first)
            return {'status': 'failed', 'error': 'temporary'}

        self.bot.return_value.register.side_effect = register
        self.tasks.execute(first)
        self.assertEqual(self.bot.return_value.register.call_count, 2)
        self.assertEqual(self.tasks.store.get(first)['state'], 'cancelled')
        self.assertFalse(public_job(self.tasks.store.get(first))['auto_skipped'])
        self.assertEqual(self.tasks.store.get(second)['state'], 'paused')
        self.client.extract.assert_called_once()

    def test_retrying_blocker_with_first_page_failure_releases_paused_jobs(self):
        first, second = [j['id'] for j in self.batch()['jobs']]
        # A previously stopped task still owns the durable queue pause.
        self.bot.return_value.register.side_effect = lambda: self.tasks.cancel(first)
        self.tasks.execute(first)
        self.bot.return_value.register.side_effect = [
            {'status': 'failed', 'failure_code': 'first_page_unavailable'},
            {'status': 'failed', 'failure_code': 'first_page_unavailable'},
        ]
        self.tasks.retry(first)
        self.tasks.execute(first)
        self.assertFalse(self.tasks.queue_status()['paused'])
        self.assertEqual(self.tasks.store.get(second)['state'], 'queued')
        self.client.extract.assert_called_once()

    def test_real_worker_continues_after_marking_first_page_failure(self):
        batch = self.batch()
        self.bot.return_value.register.side_effect = [
            {'status': 'failed', 'failure_code': 'first_page_unavailable'},
            {'status': 'failed', 'failure_code': 'first_page_unavailable'},
            {'status': 'success'},
        ]
        worker = threading.Thread(target=self.tasks._worker, daemon=True)
        worker.start()
        try:
            self.tasks.queue.join()
            self.assertEqual([self.tasks.store.get(j['id'])['state'] for j in batch['jobs']], ['failed', 'success'])
            self.assertTrue(self.tasks.store.get(batch['jobs'][0]['id'])['result']['auto_skipped'])
            self.assertFalse(self.tasks.queue_status()['paused'])
            self.assertEqual(self.client.extract.call_count, 2)
        finally:
            self.tasks.queue.put(None)
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_manual_stop_overrides_first_page_skip_and_pauses_queue(self):
        first, second = [j['id'] for j in self.batch()['jobs']]

        def stop():
            self.tasks.cancel(first)
            return {'status': 'failed', 'failure_code': 'first_page_unavailable'}

        self.bot.return_value.register.side_effect = stop
        self.tasks.execute(first)
        self.assertEqual(self.tasks.store.get(first)['state'], 'cancelled')
        self.assertFalse(public_job(self.tasks.store.get(first))['auto_skipped'])
        self.assertEqual(self.tasks.store.get(second)['state'], 'paused')
        self.client.extract.assert_called_once()

    def test_successful_retry_reuses_ip_then_releases_following_jobs(self):
        first, second = [j["id"] for j in self.batch()["jobs"]]
        self.bot.return_value.register.side_effect = lambda: self.tasks.cancel(first)
        self.tasks.execute(first)
        self.bot.return_value.register.side_effect = [{"status": "failed"}, {"status": "success"}, {"status": "success"}]
        self.tasks.retry(first)
        self.assertFalse(self.tasks.queue_status()["can_continue"])
        with self.assertRaises(ValueError):
            self.tasks.continue_queue(first)
        self.tasks.execute(first)
        self.client.extract.assert_called_once()
        self.assertFalse(self.tasks.queue_status()["paused"])
        self.assertEqual(self.tasks.store.get(second)["state"], "queued")
        self.tasks.execute(second)
        self.assertEqual(self.client.extract.call_count, 2)
        self.assertEqual(self.tasks.store.get(second)["state"], "success")

    def test_manual_skip_of_stopped_task_checks_the_expected_blocker(self):
        first, second = [j["id"] for j in self.batch()["jobs"]]
        self.bot.return_value.register.side_effect = lambda: self.tasks.cancel(first)
        self.tasks.execute(first)
        self.bot.return_value.register.side_effect = lambda: {"status": "success"}
        with self.assertRaises(ValueError):
            self.tasks.continue_queue(second)
        self.tasks.continue_queue(first)
        self.client.extract.assert_called_once()
        self.tasks.execute(second)
        self.assertEqual(self.client.extract.call_count, 2)

    def test_new_batch_is_paused_and_stopped_pending_jobs_stay_stopped(self):
        first, second = [j["id"] for j in self.batch()["jobs"]]
        self.bot.return_value.register.side_effect = lambda: self.tasks.cancel(first)
        self.tasks.execute(first)
        new = self.batch(emails="c@example.com\nd@example.com")
        self.assertEqual([j["state"] for j in new["jobs"]], ["paused", "paused"])
        self.tasks.cancel(second)
        self.tasks.continue_queue(first)
        self.assertEqual(self.tasks.store.get(second)["state"], "cancelled")
        self.assertEqual([self.tasks.queue.get_nowait() for _ in range(2)], [j["id"] for j in new["jobs"]])
        self.client.extract.assert_called_once()

    def test_queue_pause_survives_service_restart(self):
        first, second = [j["id"] for j in self.batch()["jobs"]]
        self.bot.return_value.register.side_effect = lambda: self.tasks.cancel(first)
        self.tasks.execute(first)
        self.tasks.store.close()
        from web_storage import Store
        self.tasks.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.assertTrue(self.tasks.queue_status()["paused"])
        self.tasks.execute(second)
        self.client.extract.assert_called_once()

    def test_real_worker_does_not_prefetch_while_registration_is_waiting(self):
        batch = self.batch()
        entered, release = threading.Event(), threading.Event()

        def register():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return {"status": "failed"}

        self.bot.return_value.register.side_effect = register
        worker = threading.Thread(target=self.tasks._worker, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.client.extract.assert_called_once()
            second = self.tasks.store.get(batch["jobs"][1]["id"])
            self.assertEqual(second["state"], "queued")
            self.assertNotIn("proxy_lease", second["payload"])
            release.set()
            self.tasks.queue.join()
            self.assertEqual(self.tasks.store.get(second["id"])["state"], "failed")
            self.assertTrue(self.tasks.store.get(second["id"])["result"]["auto_skipped"])
            self.assertEqual(self.bot.return_value.register.call_count, 4)
            self.assertEqual(self.client.extract.call_count, 2)
        finally:
            release.set()
            self.tasks.queue.put(None)
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
