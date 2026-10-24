"""sms-activate 协议族接码客户端（GrizzlySMS / SMSBower / HeroSMS 等通用）。

协议是 GET handler_api.php：
    action=getNumber&service=<service>&country=<country>   -> ACCESS_NUMBER:<id>:<phone>
    action=getStatus&id=<id>                                -> STATUS_WAIT_CODE / STATUS_OK:<code>
    action=setStatus&id=<id>&status=<6|8>                   -> ACCESS_READY / ACCESS_CANCEL

用法由 services/phone_verification.py 统一调度；这里的客户端只负责协议细节。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests


class SmsActivateError(RuntimeError):
    pass


class SmsNoBalance(SmsActivateError):
    pass


class SmsNoNumbers(SmsActivateError):
    pass


DEFAULT_BASE = "https://api.grizzlysms.com/stubs/handler_api.php"


@dataclass
class Activation:
    activation_id: str
    phone: str


class SmsActivateClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        service: str = "",
        country: str = "",
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("SMS_API_BASE", DEFAULT_BASE)).strip()
        self.api_key = (api_key or os.getenv("SMS_API_KEY", "")).strip()
        self.service = (service or os.getenv("SMS_SERVICE", "dr")).strip()
        self.country = str(country or os.getenv("SMS_COUNTRY", "187")).strip()
        self.timeout = int(timeout or os.getenv("SMS_REQUEST_TIMEOUT", "30"))

    def _call(self, params: dict[str, str]) -> str:
        query = {"api_key": self.api_key, **params}
        response = requests.get(self.base_url, params=query, timeout=self.timeout)
        if response.status_code != 200:
            raise SmsActivateError(f"HTTP {response.status_code}: {response.text[:200]}")
        text = (response.text or "").strip()
        if text == "NO_BALANCE":
            raise SmsNoBalance("接码平台余额不足")
        if text == "NO_NUMBERS":
            raise SmsNoNumbers("接码平台暂无可用号码")
        if text == "BAD_KEY":
            raise SmsActivateError("接码平台 API key 无效")
        if text.startswith("The service is prohibited"):
            raise SmsActivateError(text)
        return text

    def acquire(self) -> Activation:
        text = self._call({"action": "getNumber", "service": self.service, "country": self.country})
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsActivateError(f"getNumber 非预期响应: {text[:200]}")
        _, activation_id, phone = text.split(":", 2)
        return Activation(activation_id=activation_id, phone=phone)

    def wait_code(self, activation_id: str, *, timeout: int = 180, interval: int = 5) -> str:
        deadline = time.time() + max(int(timeout), 30)
        while time.time() < deadline:
            text = self._call({"action": "getStatus", "id": activation_id})
            if text.startswith("STATUS_OK"):
                return text.split(":", 1)[1].strip() if ":" in text else ""
            if text in {"STATUS_CANCEL", "NO_ACTIVATION"}:
                return ""
            match = re.search(r"\b(\d{4,8})\b", text)
            if match and text.startswith("STATUS_OK"):
                return match.group(1)
            time.sleep(max(int(interval), 3))
        return ""

    def complete(self, activation_id: str) -> None:
        self._call({"action": "setStatus", "status": "6", "id": activation_id})

    def cancel(self, activation_id: str) -> None:
        try:
            self._call({"action": "setStatus", "status": "8", "id": activation_id})
        except Exception:
            pass
