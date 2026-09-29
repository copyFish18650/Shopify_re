import json
import os
import unittest
from unittest.mock import patch

import requests

from adspower_client import AdsPowerClient
from cliproxy_client import CliproxyClient
from profile_source import fetch_profile
from run_web import fetch


class DirectConnectionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "HTTP_PROXY": "http://broken-proxy.invalid:7897",
            "HTTPS_PROXY": "https://broken-proxy.invalid:7897",
            "ALL_PROXY": "http://broken-proxy.invalid:7897",
            "NO_PROXY": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def response(body, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = body.encode("utf-8")
        response._content_consumed = True
        return response

    def profile_page(self):
        values = {"givenname": "Test", "surname": "Person", "streetaddress": "Test street 1",
                  "city": "Madrid", "zipcode": "28001", "country": "ES", "telephonenumber": "910000000"}
        items = [{key: i + 1 for i, key in enumerate(values)}] + list(values.values())
        return '<script id="__NUXT_DATA__">' + json.dumps(items) + '</script>'

    def assert_direct(self, send):
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertFalse(kwargs["proxies"])
        self.assertIs(kwargs["verify"], True)

    def test_profile_request_ignores_broken_proxy_and_keeps_tls_verification(self):
        with patch("requests.sessions.Session.send", return_value=self.response(self.profile_page())) as send:
            result = fetch_profile("es")
        self.assertEqual(result["country"], "es")
        self.assertEqual(result["name"], "Test Person")
        self.assert_direct(send)

    def test_windows_registry_proxy_is_not_consulted(self):
        with patch("requests.sessions.get_environ_proxies", side_effect=AssertionError("system proxy consulted")):
            with patch("requests.sessions.Session.send", return_value=self.response(self.profile_page())):
                self.assertEqual(fetch_profile("es")["country"], "es")

    def test_profile_failure_is_readable_without_raw_proxy_details(self):
        for error in (requests.Timeout("private proxy detail"), requests.ConnectionError("private proxy detail")):
            with self.subTest(error=type(error).__name__):
                with patch("requests.sessions.Session.send", side_effect=error) as send:
                    with self.assertRaises(RuntimeError) as raised:
                        fetch_profile("es")
                self.assertNotIn("private proxy detail", str(raised.exception))
                send.assert_called_once()

    def test_clip_proxy_control_request_stays_direct_and_extracts_only_one(self):
        client = CliproxyClient("test-key")
        self.addCleanup(client.session.close)
        with patch("requests.sessions.Session.send", return_value=self.response("proxy.example:443:user:pass")) as send:
            result = client.extract("es", 443)
        self.assert_direct(send)
        self.assertIn("num=1", send.call_args.args[0].url)
        self.assertEqual(result["config"]["proxy_host"], "proxy.example")

    def test_adspower_local_api_does_not_use_system_proxy(self):
        client = AdsPowerClient("http://127.0.0.1:50325")
        self.addCleanup(client.session.close)
        with patch("requests.sessions.Session.send", return_value=self.response('{"code": 0}')) as send:
            client.check_ready()
        self.assert_direct(send)

    def test_launcher_health_check_works_with_broken_proxy(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"app":"shopflow"}')

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(fetch("http://127.0.0.1:{}/api/health".format(server.server_port)), '{"app":"shopflow"}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
