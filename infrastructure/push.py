"""推送适配层：console / Server酱 / PushPlus / 企业微信机器人。统一 push(title, content, level)。

图片能力（设计稿策略：图片为增强、文字为保底）：
- 企业微信机器人原生支持图片消息（base64+md5，≤2MB），send_image 直发
- 其余通道无图片接口，send_image 返回 False，调用方降级为文字卡
"""
import base64
import hashlib
import os

import requests


class ConsoleChannel:
    name = "console"

    def send(self, title: str, content: str, level: str = "INFO") -> bool:
        print(f"\n{'=' * 46}\n【{level}】{title}\n{'-' * 46}\n{content}\n{'=' * 46}")
        return True

    def send_image(self, title: str, path: str) -> bool:
        if path and os.path.exists(path):
            print(f"[图表] {title}: {path}")
            return True
        return False


class ServerChanChannel:
    name = "serverchan"

    def __init__(self, conf: dict):
        self.key = conf.get("sendkey", "")

    def send(self, title, content, level="INFO"):
        if not self.key or self.key.startswith("SCT_xxx"):
            print(f"[serverchan未配置] {title}")
            return False
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{self.key}.send",
                              data={"title": f"[{level}] {title}", "desp": content}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False


class PushPlusChannel:
    name = "pushplus"

    def __init__(self, conf: dict):
        self.token = conf.get("token", "")

    def send(self, title, content, level="INFO"):
        if not self.token or self.token == "xxx":
            print(f"[pushplus未配置] {title}")
            return False
        try:
            r = requests.post("http://www.pushplus.plus/send",
                              json={"token": self.token, "title": f"[{level}] {title}",
                                    "content": content.replace("\n", "<br/>"),
                                    "template": "markdown"}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False


class WecomBotChannel:
    name = "wecom_bot"
    _MAX_IMG = 2 * 1024 * 1024   # 企微图片消息上限
    _MAX_MD = 4096               # 企微markdown字节上限
    # 级别 → 企微支持的颜色（info绿 / warning橙 / comment灰）
    _LV_COLOR = {"S": "warning", "A": "warning", "B": "comment",
                 "LOF": "info", "TURN": "warning", "TEST": "comment", "INFO": "info"}

    def __init__(self, conf: dict):
        self.webhook = conf.get("webhook", "")
        self.last_error = ""

    def _md(self, title: str, content: str, level: str) -> str:
        color = self._LV_COLOR.get(str(level).upper(), "info")
        md = f'**<font color="{color}">[{level}]</font> {title}**\n{content}'
        # 超长截断（utf-8字节，企微上限4096）
        raw = md.encode("utf-8")
        if len(raw) > self._MAX_MD:
            md = raw[:self._MAX_MD - 60].decode("utf-8", "ignore") + \
                "\n…（内容过长已截断）"
        return md

    def send(self, title, content, level="INFO"):
        self.last_error = ""
        if not self.webhook or "key=xxx" in self.webhook:
            self.last_error = "webhook未配置"
            print(f"[wecom_bot未配置] {title}")
            return False
        try:
            r = requests.post(self.webhook,
                              json={"msgtype": "markdown",
                                    "markdown": {"content": self._md(title, content, level)}},
                              timeout=10)
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                return False
            data = r.json()          # 企微失败也返回200，必须查errcode
            if data.get("errcode") != 0:
                self.last_error = f"errcode {data.get('errcode')}: {data.get('errmsg')}"
                print(f"[wecom_bot失败] {self.last_error}")
                return False
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def send_image(self, title: str, path: str) -> bool:
        """企微机器人图片消息：base64 + md5。"""
        if not self.webhook or "key=xxx" in self.webhook:
            return False
        try:
            if not path or not os.path.exists(path) or os.path.getsize(path) > self._MAX_IMG:
                return False
            with open(path, "rb") as f:
                raw = f.read()
            payload = {"msgtype": "image", "image": {
                "base64": base64.b64encode(raw).decode(),
                "md5": hashlib.md5(raw).hexdigest()}}
            r = requests.post(self.webhook, json=payload, timeout=10)
            return r.status_code == 200 and r.json().get("errcode") == 0
        except Exception:
            return False


class Pusher:
    def __init__(self, cfg: dict):
        push_cfg = cfg.get("push", {})
        self.daily_limit = int(push_cfg.get("daily_push_limit", 12))
        self.sent_today = 0
        self.channels = []
        self.last_errors = []      # 最近一次send各通道失败原因（供运维排查）
        factories = {"console": ConsoleChannel, "serverchan": ServerChanChannel,
                     "pushplus": PushPlusChannel, "wecom_bot": WecomBotChannel}
        for name in push_cfg.get("channels", ["console"]):
            if name == "console":
                self.channels.append(ConsoleChannel())
            elif name in factories:
                self.channels.append(factories[name](push_cfg.get(name, {})))

    def send(self, title: str, content: str, level: str = "INFO",
             is_alert: bool = False) -> bool:
        """is_alert=True 为系统告警，不受每日限额限制。
        返回True=至少一通道成功；各通道失败明细在 self.last_errors。"""
        self.last_errors = []
        if not is_alert:
            if self.sent_today >= self.daily_limit:
                print(f"[推送超限] 今日已达{self.daily_limit}条上限，聚合: {title}")
                self.last_errors = [f"今日已达{self.daily_limit}条上限"]
                return False
            self.sent_today += 1
        ok = False
        for ch in self.channels:
            try:
                if ch.send(title, content, level):
                    ok = True
                else:
                    self.last_errors.append(
                        f"{ch.name}: {getattr(ch, 'last_error', '') or '发送失败'}")
            except Exception as e:
                self.last_errors.append(f"{ch.name}: {type(e).__name__}: {e}")
        return ok

    def push_image(self, title: str, path: str) -> bool:
        """图片推送：有图片能力的通道直发，无则降级跳过（文字卡已先行发出）。"""
        ok = False
        for ch in self.channels:
            try:
                if hasattr(ch, "send_image"):
                    ok = ch.send_image(title, path) or ok
            except Exception:
                continue
        return ok
