"""一键启动：内网穿透(trycloudflare 免注册) + Web看板，公网地址自动写入 config.web.public_url。

用法: py312\\python.exe tools\\tunnel_web.py
- 首次运行自动下载 cloudflared.exe 到 tools/（直连失败走镜像）
- 手机/微信经 https://xxx.trycloudflare.com 访问，与电脑页面完全一致
- 推送卡"看板详情"链接自动使用当前隧道地址
"""
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
CD_EXE = os.path.join(TOOLS, "cloudflared.exe")
PY = os.path.join(ROOT, "py312", "python.exe") if os.path.exists(
    os.path.join(ROOT, "py312", "python.exe")) else sys.executable
CD_URLS = [
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "https://mirror.ghproxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
]


def ensure_cloudflared() -> bool:
    if os.path.exists(CD_EXE) and os.path.getsize(CD_EXE) > 10_000_000:
        return True
    os.makedirs(TOOLS, exist_ok=True)
    for url in CD_URLS:
        try:
            print(f"[下载] cloudflared ... {url.split('/')[2]}")
            urllib.request.urlretrieve(url, CD_EXE)
            if os.path.getsize(CD_EXE) > 10_000_000:
                print("[下载] 完成")
                return True
        except Exception as e:
            print(f"[下载] 失败: {e}")
    return False


def update_config_public_url(url: str) -> None:
    """把隧道地址写入 config.yaml 的 web.public_url（保留行内注释）。"""
    import yaml
    path = os.path.join(ROOT, "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new_text, n = re.subn(r'(public_url:\s*")[^"]*(")', lambda m: m.group(1) + url + m.group(2), text)
    if n == 0:  # 无该配置则补一段
        new_text = text.rstrip() + f'\n\nweb:\n  public_url: "{url}"\n'
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    # 校验yaml可解析
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)
    print(f"[配置] web.public_url = {url}")


def start_tunnel():
    proc = subprocess.Popen(
        [CD_EXE, "tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    url = None
    deadline = time.time() + 60
    buf = []
    while time.time() < deadline and proc.poll() is None:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.3)
            continue
        buf.append(line)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
    if not url:
        tail = "\n".join(buf[-8:])
        print(f"[隧道] 未获取到地址，输出尾部:\n{tail}")
        proc.kill()
        return None, None
    print(f"[隧道] 公网地址: {url}")
    return proc, url


def push_url_notice(url: str) -> None:
    """服务启动后把最新看板地址推送到微信（隧道地址每次重启都变，手机端随时可查）。"""
    try:
        import socket
        import yaml
        sys.path.insert(0, ROOT)
        from infrastructure.push import Pusher

        lan = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan = f"http://{s.getsockname()[0]}:8000"
            s.close()
        except Exception:
            pass
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        pusher = Pusher(cfg)
        content = (f"**公网访问**（任意网络）：\n{url}\n"
                   + (f"\n**局域网访问**（同一WiFi）：\n{lan}\n" if lan else "")
                   + "\n<font color=\"comment\">手机浏览器打开后，用“添加到主屏幕”"
                     "可生成App图标，地址变化时会重新推送</font>")
        ok = pusher.send("服务已启动·看板地址", content, level="INFO", is_alert=True)
        print(f"[推送] 地址通知{'成功' if ok else '失败'}"
              + (f"：{pusher.last_errors}" if pusher.last_errors else ""))
    except Exception as e:
        print(f"[推送] 地址通知异常: {e}")


def main():
    if not ensure_cloudflared():
        print("[错误] cloudflared 下载失败，可手动下载放到 tools/cloudflared.exe 后重试")
        sys.exit(1)
    tunnel, url = start_tunnel()
    try:
        if url:
            update_config_public_url(url)  # 推送卡深链用最新隧道地址
            push_url_notice(url)           # 微信推送最新看板地址（重启后地址会变）
        web = subprocess.Popen([PY, os.path.join(ROOT, "main.py"), "web"],
                               cwd=ROOT)  # 前台输出服务日志
        try:
            web.wait()
        finally:
            if tunnel:
                tunnel.kill()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
