"""Loopback-only API for the local ShopFlow web interface."""
import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests

from profile_source import COUNTRIES, fetch_profile
from web_tasks import Tasks, public_job


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, tasks=None):
        super().__init__(address, Handler)
        self.tasks = tasks or Tasks()
        self.token = secrets.token_urlsafe(32)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def check_access(self):
        port = self.server.server_port
        if self.headers.get("Host", "") not in ("localhost:" + str(port), "127.0.0.1:" + str(port)):
            raise PermissionError("不允许的访问地址")
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://localhost:5173", "http://127.0.0.1:5173",
                                     "http://localhost:" + str(port), "http://127.0.0.1:" + str(port)):
            raise PermissionError("不允许跨站访问本机控制台")
        if self.path.split("?", 1)[0] not in ("/api/bootstrap", "/api/health"):
            if not secrets.compare_digest(self.headers.get("X-ShopFlow-Token", ""), self.server.token):
                raise PermissionError("页面连接已过期，请刷新网页")

    def read_body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("请求格式应为 JSON")
        length = int(self.headers.get("Content-Length", "0"))
        limit = 2 * 1024 * 1024 if self.path == "/api/mailboxes/import" else 65536
        if not 0 < length <= limit:
            raise ValueError("请求大小无效")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求应为对象")
        return data

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        try:
            self.check_access()
            tasks = self.server.tasks
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            data = self.read_body() if post else {}
            if not post and path == "/api/health":
                return self.send_json({"ok": True, "app": "shopflow", "version": 1})
            if not post and path == "/api/bootstrap":
                return self.send_json({"token": self.server.token, "settings": tasks.settings(),
                                       "countries": [{"code": c, **v} for c, v in COUNTRIES.items()]})
            if not post and path == "/api/jobs":
                return self.send_json({"jobs": [public_job(j) for j in tasks.store.jobs(limit=None)],
                                       "queue": tasks.queue_status()})
            if post and path == "/api/queue/continue":
                return self.send_json({"queue": tasks.continue_queue(data.get("blocked_job_id", ""))})
            if not post and path == "/api/mailboxes":
                return self.send_json(tasks.mailbox_inventory())
            if post and path == "/api/mailboxes/import":
                return self.send_json(tasks.import_mailboxes(data.get("text", "")))
            if post and path == "/api/profile":
                return self.send_json({"profile": fetch_profile(str(data.get("country", "es")))})
            if post and path == "/api/connection":
                return self.send_json(tasks.connection())
            if post and path == "/api/settings":
                return self.send_json({"settings": tasks.save_settings(data)})
            if post and path == "/api/jobs":
                return self.send_json({"job": tasks.submit(data)}, status=201)
            if post and path == "/api/batches":
                return self.send_json(tasks.submit_batch(data), status=201)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "mailboxes"] and parts[3] == "state" and post:
                return self.send_json(tasks.set_mailbox_state(parts[2], data.get("state")))
            if len(parts) == 4 and parts[:2] == ["api", "batches"] and post:
                return self.send_json(tasks.batch_action(parts[2], parts[3]))
            if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                job_id = parts[2]
                if len(parts) == 3 and not post:
                    return self.send_json({"job": public_job(tasks.store.get(job_id)),
                                           "logs": tasks.store.logs(job_id, max(0, int(query.get("after", ["0"])[0])))})
                if len(parts) == 4 and post:
                    action = parts[3]
                    if action == "retry":
                        return self.send_json({"job": tasks.retry(job_id)})
                    if action == "replace-proxy":
                        return self.send_json({"job": tasks.change_proxy(job_id)})
                    if action == "bind-proxy":
                        return self.send_json({"job": tasks.change_proxy(job_id, str(data.get("proxy", "")))})
                    if action == "cancel":
                        tasks.cancel(job_id)
                    elif action == "resume":
                        tasks.resume(job_id, data.get("answer", ""))
                    elif action == "bind":
                        tasks.bind_profile(job_id, data.get("profile_id", ""))
                    elif action == "credentials":
                        return self.send_json({"password": tasks.store.get(job_id)["payload"].get("shopify_password", "")})
                    else:
                        raise KeyError("接口不存在")
                    return self.send_json({"ok": True})
            raise KeyError("接口不存在")
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except KeyError as exc:
            self.send_json({"error": str(exc).strip("'")}, 404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except requests.RequestException:
            self.send_json({"error": "连接失败，请检查 AdsPower 是否已打开，或资料网站是否可访问。"}, 502)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 409)
        except Exception:
            self.send_json({"error": "本机服务处理失败，请查看启动窗口；已保存的任务会保留。"}, 500)


def make_server(port=8765, tasks=None):
    return AppServer(("127.0.0.1", port), tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.port)
    print("ShopFlow API: http://127.0.0.1:{}/api/health".format(args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
