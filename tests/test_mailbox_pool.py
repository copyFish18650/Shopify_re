import configparser
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from mailbox_pool import parse_mailboxes
from web_backend import make_server
from web_storage import Store
from web_tasks import Tasks, public_job


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.crypto = patch("web_storage.protect", side_effect=lambda raw, decrypt=False: raw)
        self.crypto.start()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.tasks = Tasks(self.path, start_worker=False)
        config = configparser.ConfigParser()
        config.read_dict({"settings": {}, "adspower": {"api_base": "http://127.0.0.1:50325"}})
        self.tasks.config = Mock(return_value=config)
        self.ads = Mock()
        self.ads.create_profile.return_value = "test-environment"
        self.tasks.ads = Mock(return_value=self.ads)
        self.profile = patch("web_tasks.fetch_profile", return_value={"name": "Person", "address": "Road 1", "city": "Madrid", "postal_code": "28001", "phone": "+34100000000"})
        self.profile.start()
        self.bot_patch = patch("web_tasks.WebBot")
        self.bot = self.bot_patch.start()
        self.bot.return_value.register.side_effect = lambda: {"status": "success"}

    def tearDown(self):
        self.bot_patch.stop(); self.profile.stop()
        self.tasks.store.close(); self.temp.cleanup(); self.crypto.stop()

    def import_two(self):
        return self.tasks.import_mailboxes("first@example.com----first-password\nsecond@example.com----second-password")["mailboxes"]

    def states(self):
        return [m["state"] for m in self.tasks.mailbox_inventory()["mailboxes"]]

    def batch(self, count=1, **extra):
        return self.tasks.submit_batch({"country": "es", "email_source": "library", "mailbox_count": count, **extra})

    def test_parse_keeps_only_email_password_and_handles_bom_and_duplicates(self):
        rows, summary = parse_mailboxes("\ufefffirst@example.com----password----oauth-client----oauth-token\r\n\nFIRST@example.com----password")
        self.assertEqual(rows, [{"email": "first@example.com", "password": "password"}])
        self.assertEqual(summary, {"duplicates": 1, "ignored_extra": 1})
        self.assertNotIn("oauth", json.dumps(rows))

    def test_invalid_import_is_atomic_and_never_echoes_credentials(self):
        for text in ("a@example.com----secret\nbad----do-not-echo", "a@example.com----secret\nA@example.com----different-secret"):
            with self.assertRaises(ValueError) as error:
                self.tasks.import_mailboxes(text)
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(self.tasks.store.mailboxes(), [])

    def test_single_selection_reserves_and_loads_password_without_exposing_it(self):
        accounts = self.import_two()
        job = self.tasks.submit({"mailbox_id": accounts[1]["id"], "country": "es"})
        self.assertEqual(job["email"], "second@example.com")
        self.assertEqual(self.states(), ["unused", "reserved"])
        self.assertEqual(self.tasks.store.get(job["id"])["payload"]["email_password"], "second-password")
        exposed = json.dumps(self.tasks.mailbox_inventory()) + json.dumps(job)
        self.assertNotIn("second-password", exposed)
        self.ads.create_profile.assert_not_called()
        with self.assertRaises(ValueError):
            self.tasks.submit({"mailbox_id": accounts[1]["id"]})

    def test_single_retry_of_submission_is_idempotent(self):
        account = self.import_two()[0]
        data = {"mailbox_id": account["id"], "request_id": str(uuid.uuid4())}
        first = self.tasks.submit(data)
        self.assertEqual(self.tasks.submit(data)["id"], first["id"])
        self.assertEqual(len(self.tasks.store.jobs()), 1)

    def test_batch_reserves_in_import_order_without_starting_or_getting_ips(self):
        self.import_two()
        data = self.batch(2)
        self.assertEqual([j["email"] for j in data["jobs"]], ["first@example.com", "second@example.com"])
        self.assertEqual(self.states(), ["reserved", "reserved"])
        self.ads.create_profile.assert_not_called()
        self.bot.assert_not_called()

    def test_batch_idempotency_does_not_take_next_unused_accounts(self):
        self.import_two()
        request_id = str(uuid.uuid4())
        first = self.batch(request_id=request_id)
        self.tasks.execute(first["jobs"][0]["id"])
        again = self.batch(request_id=request_id)
        self.assertEqual(again["jobs"][0]["id"], first["jobs"][0]["id"])
        self.assertEqual(self.states(), ["used", "unused"])

    def test_not_enough_accounts_leaves_all_accounts_unused(self):
        self.import_two()
        with self.assertRaises(ValueError):
            self.batch(3)
        self.assertEqual(self.states(), ["unused", "unused"])
        self.assertEqual(self.tasks.store.jobs(), [])

    def test_concurrent_batches_never_receive_the_same_mailbox(self):
        self.import_two()
        results, errors = [], []
        def submit():
            try:
                results.append(self.batch()["jobs"][0]["email"])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ["first@example.com", "second@example.com"])

    def test_failure_stays_used_and_keeps_next_account_reserved_until_execution(self):
        accounts = self.import_two()
        batch = self.batch(2)
        self.bot.return_value.register.side_effect = lambda: {"status": "failed"}
        self.tasks.execute(batch["jobs"][0]["id"])
        self.assertEqual(self.states(), ["used", "reserved"])
        self.assertEqual(self.tasks.store.get(batch["jobs"][1]["id"])["state"], "queued")
        self.tasks.import_mailboxes("FIRST@example.com----replacement-password")
        self.assertEqual(self.states(), ["used", "reserved"])
        self.assertEqual(self.tasks.store.mailbox(mailbox_id=accounts[0]["id"])["password"], "first-password")
        with self.assertRaises(ValueError):
            self.tasks.submit({"email": "first@example.com"})

    def test_cancel_before_execution_releases_mailbox_and_old_retry_cannot_steal_it(self):
        self.import_two()
        old = self.batch()["jobs"][0]["id"]
        self.tasks.cancel(old); self.tasks.execute(old)
        self.assertEqual(self.states(), ["unused", "unused"])
        newer = self.batch()["jobs"][0]["id"]
        self.tasks.execute(newer)
        with self.assertRaises(ValueError):
            self.tasks.retry(old)
        self.assertEqual(self.states(), ["used", "unused"])

    def test_retry_original_task_preserves_use_marker_and_credentials(self):
        self.import_two()
        job_id = self.batch()["jobs"][0]["id"]
        self.bot.return_value.register.side_effect = [{"status": "failed"}, {"status": "failed"}, {"status": "success"}]
        self.tasks.execute(job_id)
        started = self.tasks.store.get(job_id)["payload"]["execution_started"]
        self.tasks.retry(job_id)
        self.assertEqual(self.states(), ["in_use", "unused"])
        self.tasks.execute(job_id)
        self.assertEqual(self.tasks.store.get(job_id)["payload"]["execution_started"], started)
        self.assertEqual(self.states(), ["used", "unused"])

    def test_import_matches_existing_history_and_restart_keeps_reservations(self):
        job_id = self.tasks.submit({"email": "first@example.com"})["id"]
        self.tasks.execute(job_id)
        self.import_two()
        self.assertEqual(self.states(), ["used", "unused"])
        pending = self.batch()["jobs"][0]["id"]
        self.tasks.store.close(); self.tasks.store = Store(self.path)
        self.assertEqual(self.states(), ["used", "reserved"])
        self.tasks.cancel(pending)
        self.assertEqual(self.states(), ["used", "unused"])

    def test_import_endpoint_authenticates_and_returns_no_secrets(self):
        server = make_server(0, self.tasks)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            body = {"text": "first@example.com----secret-password----client-id----refresh-token"}
            self.assertEqual(requests.post(base + "/api/mailboxes/import", json=body).status_code, 403)
            headers = {"X-ShopFlow-Token": server.token}
            response = requests.post(base + "/api/mailboxes/import", json=body, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["added"], 1)
            mailbox_id = response.json()["mailboxes"][0]["id"]
            state_url = base + "/api/mailboxes/" + mailbox_id + "/state"
            self.assertEqual(requests.post(state_url, json={"state": "used"}).status_code, 403)
            changed = requests.post(state_url, json={"state": "used"}, headers=headers)
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["counts"]["used"], 1)
            self.assertEqual(requests.post(state_url, json={"state": "running"}, headers=headers).status_code, 400)
            listing = requests.get(base + "/api/mailboxes", headers=headers)
            self.assertEqual(listing.status_code, 200)
            for secret in ("secret-password", "client-id", "refresh-token"):
                self.assertNotIn(secret, response.text + listing.text + changed.text)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_manual_used_persists_and_is_skipped_by_single_and_batch(self):
        first = self.import_two()[0]
        self.tasks.set_mailbox_state(first["id"], "used")
        self.tasks.import_mailboxes("FIRST@example.com----replacement-password")
        self.tasks.store.close(); self.tasks.store = Store(self.path)
        self.assertEqual(self.states(), ["used", "unused"])
        self.assertEqual(self.tasks.store.mailbox(mailbox_id=first["id"])["password"], "first-password")
        for data in ({"mailbox_id": first["id"]}, {"email": "FIRST@example.com"}):
            with self.assertRaises(ValueError):
                self.tasks.submit(data)
        self.assertEqual(self.batch()["jobs"][0]["email"], "second@example.com")
        self.ads.create_profile.assert_not_called()

    def test_manual_reset_releases_completed_mailbox_without_changing_history(self):
        first = self.import_two()[0]
        old = self.tasks.submit({"mailbox_id": first["id"]})["id"]
        self.tasks.execute(old)
        history = self.tasks.store.get(old)
        changed = self.tasks.set_mailbox_state(first["id"], "unused")
        self.assertEqual(changed["counts"]["unused"], 2)
        self.assertEqual(changed["mailboxes"][0]["job_id"], old)
        self.assertEqual(self.tasks.store.get(old), history)
        self.tasks.store.close(); self.tasks.store = Store(self.path)
        self.assertEqual(self.states(), ["unused", "unused"])
        new = self.tasks.submit({"mailbox_id": first["id"]})["id"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.states(), ["reserved", "unused"])
        self.tasks.execute(new)
        self.assertEqual(self.states(), ["used", "unused"])

    def test_batch_uses_manually_reset_mailboxes_in_import_order(self):
        accounts = self.import_two()
        first_job = self.tasks.submit({"mailbox_id": accounts[0]["id"]})["id"]
        self.tasks.execute(first_job)
        self.tasks.set_mailbox_state(accounts[0]["id"], "unused")
        self.assertEqual([j["email"] for j in self.batch(2)["jobs"]], ["first@example.com", "second@example.com"])

    def test_manual_change_cannot_release_active_or_interrupted_reservations(self):
        first = self.import_two()[0]
        job_id = self.tasks.submit({"mailbox_id": first["id"]})["id"]
        for state in ("queued", "paused", "preparing", "running", "waiting", "stopping", "interrupted"):
            with self.subTest(state=state):
                self.tasks.store.update(job_id, state=state)
                for target in ("used", "unused"):
                    with self.assertRaisesRegex(ValueError, "占用"):
                        self.tasks.set_mailbox_state(first["id"], target)
        self.assertNotIn("usage_revision", self.tasks.store.mailbox(mailbox_id=first["id"]))

    def test_retry_after_reset_marks_used_again_and_cannot_steal_newer_usage(self):
        first = self.import_two()[0]
        old = self.tasks.submit({"mailbox_id": first["id"]})["id"]
        self.bot.return_value.register.side_effect = lambda: {"status": "failed"}
        self.tasks.execute(old)
        started = self.tasks.store.get(old)["payload"]["execution_started"]
        self.tasks.set_mailbox_state(first["id"], "unused")
        self.tasks.retry(old)
        self.assertEqual(self.states(), ["in_use", "unused"])
        self.tasks.execute(old)
        self.assertEqual(self.states(), ["used", "unused"])
        self.assertEqual(self.tasks.store.get(old)["payload"]["execution_started"], started)
        self.tasks.set_mailbox_state(first["id"], "unused")
        newer = self.tasks.submit({"mailbox_id": first["id"]})["id"]
        with self.assertRaises(ValueError):
            self.tasks.retry(old)
        self.assertFalse(self.tasks.queue_status()["paused"])
        self.tasks.execute(newer)
        self.assertEqual(self.states(), ["used", "unused"])
        with self.assertRaises(ValueError):
            self.tasks.retry(old)

    def test_invalid_manual_state_or_missing_mailbox_does_not_change_inventory(self):
        first = self.import_two()[0]
        for invalid in ("reserved", "in_use", "", None, {"state": "used"}):
            with self.assertRaises(ValueError):
                self.tasks.set_mailbox_state(first["id"], invalid)
        with self.assertRaises(ValueError):
            self.tasks.set_mailbox_state("missing-id", "used")
        self.assertEqual(self.states(), ["unused", "unused"])


if __name__ == "__main__":
    unittest.main()
