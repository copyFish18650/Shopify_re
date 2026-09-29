"""One command starts both local services; closing this process stops its children."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def fetch(url):
    try:
        # Health checks target localhost, regardless of the system proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return ""


def main():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("请先安装 Node.js 22 或更新版本")
    if not (ROOT / "web" / "node_modules").exists():
        raise RuntimeError("网页依赖尚未安装，请在 web 文件夹运行 npm ci")
    processes = []
    log_files = []
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def start(command, folder, name):
        log_dir = ROOT / "local_data"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / (name + ".log")
        log_file = log_path.open("a", encoding="utf-8")
        log_files.append(log_file)
        process = subprocess.Popen(command, cwd=folder, creationflags=creationflags,
                                   stdout=log_file, stderr=subprocess.STDOUT)
        processes.append(process)
        process.shopflow_log = log_path

    def check_processes():
        for process in processes:
            if process.poll() is not None:
                details = process.shopflow_log.read_text(encoding="utf-8", errors="replace")[-1800:]
                raise RuntimeError("服务启动失败：\n" + details)

    try:
        health = fetch("http://127.0.0.1:8765/api/health")
        if not health:
            start([sys.executable, "-X", "utf8", "web_backend.py"], ROOT, "backend")
        elif json.loads(health).get("app") != "shopflow":
            raise RuntimeError("8765 端口被其他程序占用")
        page = fetch("http://localhost:5173/")
        if not page:
            start([node, str(ROOT / "web/node_modules/vinext/dist/cli.js"), "dev", "--host", "127.0.0.1", "--port", "5173"],
                  ROOT / "web", "frontend")
        elif "ShopFlow" not in page:
            raise RuntimeError("5173 端口已有服务，但没有返回工作台页面；请关闭旧的启动窗口再重试")
        print("正在启动店铺工作台...", flush=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            check_processes()
            if fetch("http://127.0.0.1:8765/api/health") and "ShopFlow" in fetch("http://localhost:5173/"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("网页启动超时，请检查启动窗口")
        print("已启动：http://localhost:5173/\n保持此窗口打开。按 Ctrl+C 关闭本次启动的服务。", flush=True)
        if "--no-browser" not in sys.argv:
            webbrowser.open("http://localhost:5173/")
        while processes:
            check_processes()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
                else:
                    process.terminate()
        for log_file in log_files:
            log_file.close()


if __name__ == "__main__":
    main()
