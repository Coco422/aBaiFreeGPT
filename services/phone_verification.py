"""手机验证（5sim）——Codex OAuth add_phone 步骤的可选实现。

OpenAI 的 add_phone 流程（实测）：
    POST https://auth.openai.com/api/accounts/add-phone/send
         {"phone_number": "+48...", "channel": "sms"}
      -> 200 {"page": {"type": "phone_otp_verification"}, "continue_url": ".../phone-verification"}
    POST https://auth.openai.com/api/accounts/add-phone/verify
         {"code": "123456"}

本模块只负责「买号 → 发码 → 等码 → 回填」，由调用方持有 auth.openai.com 的会话
cookies（注册协议里同一会话）。默认关闭，需显式设置：

    PHONE_VERIFY_ENABLED=1
    FIVESIM_TOKEN=...
    FIVESIM_COUNTRY=poland|indonesia|england...
    FIVESIM_OPERATOR=virtual66（留空则按价格从低到高自动挑有库存的）
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

FIVESIM_BASE = "https://5sim.net/v1"
AUTH_BASE = "https://auth.openai.com"
PHONE_HEADERS = {
    "content-type": "application/json",
    "origin": AUTH_BASE,
    "referer": f"{AUTH_BASE}/add-phone",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"
    ),
}
VERIFY_ENDPOINTS = (
    "/api/accounts/add-phone/verify",
    "/api/accounts/add-phone/confirm",
    "/api/accounts/add-phone/validate",
)


def enabled() -> bool:
    return os.getenv("PHONE_VERIFY_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _token() -> str:
    return os.getenv("FIVESIM_TOKEN", "").strip()


def _proxies(proxy: str | None) -> dict[str, str] | None:
    return {"http": proxy, "https": proxy} if proxy else None


def buy_number(*, country: str = "", operator: str = "", product: str = "openai",
               max_price: float | None = None) -> dict[str, Any]:
    """Buy one 5sim activation number and return the order payload."""
    country = (country or os.getenv("FIVESIM_COUNTRY", "poland")).strip().lower()
    operator = (operator or os.getenv("FIVESIM_OPERATOR", "")).strip()
    max_price = float(max_price if max_price is not None else os.getenv("FIVESIM_MAX_PRICE", "0") or 0)
    if not operator:
        prices = requests.get(f"{FIVESIM_BASE}/guest/prices", params={"product": product}, timeout=20).json()
        offers = [
            (float(info.get("cost") or 0), name)
            for name, info in (prices.get(product, {}).get(country) or {}).items()
            if int(info.get("count") or 0) > 0
        ]
        offers.sort()
        if max_price:
            offers = [item for item in offers if item[0] <= max_price]
        if not offers:
            raise RuntimeError(f"5sim 没有可用号码: {country}/{product} (max=${max_price or '-'})")
        operator = offers[0][1]
    response = requests.get(
        f"{FIVESIM_BASE}/user/buy/activation/{country}/{operator}/{product}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    payload = response.json() if response.content else {}
    if response.status_code != 200 or not str(payload.get("phone") or "").startswith("+"):
        raise RuntimeError(f"5sim 购号失败: HTTP {response.status_code} {str(payload)[:200]}")
    payload["operator"] = payload.get("operator") or operator
    return payload


def cancel(order_id: Any) -> None:
    try:
        requests.get(f"{FIVESIM_BASE}/user/cancel/{order_id}",
                     headers={"Authorization": f"Bearer {_token()}"}, timeout=20)
    except Exception:
        pass


def wait_code(order_id: Any, *, timeout: int = 180) -> str:
    deadline = time.time() + max(int(timeout), 30)
    while time.time() < deadline:
        try:
            payload = requests.get(
                f"{FIVESIM_BASE}/user/check/{order_id}",
                headers={"Authorization": f"Bearer {_token()}"},
                timeout=20,
            ).json()
        except Exception:
            time.sleep(4)
            continue
        for sms in payload.get("sms") or []:
            code = str(sms.get("code") or "").strip()
            if code:
                return code
            match = re.search(r"\b(\d{6})\b", str(sms.get("text") or ""))
            if match:
                return match.group(1)
        if str(payload.get("status") or "").upper() in {"FINISHED"}:
            return ""
        time.sleep(4)
    return ""


def send_code(session: requests.Session, phone: str, *, proxy: str | None = None) -> dict[str, Any]:
    response = session.post(
        f"{AUTH_BASE}/api/accounts/add-phone/send",
        json={"phone_number": phone, "channel": "sms"},
        headers=PHONE_HEADERS,
        proxies=_proxies(proxy),
        timeout=30,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    return {"status": response.status_code, "payload": payload, "text": response.text[:300]}


def submit_code(session: requests.Session, code: str, *, proxy: str | None = None) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for endpoint in VERIFY_ENDPOINTS:
        response = session.post(
            AUTH_BASE + endpoint,
            json={"code": code},
            headers=PHONE_HEADERS,
            proxies=_proxies(proxy),
            timeout=30,
        )
        last = {"endpoint": endpoint, "status": response.status_code, "text": response.text[:300]}
        if response.status_code == 200:
            return last
        if response.status_code not in (404, 405):
            break
    return last


def complete(session: requests.Session, *, proxy: str | None = None,
             country: str = "", operator: str = "", timeout: int = 180,
             log=print) -> dict[str, Any]:
    """Full add_phone flow for an existing auth.openai.com session."""
    order = buy_number(country=country, operator=operator)
    phone = str(order.get("phone") or "")
    log(f"[phone] 5sim 号码 {phone}（{order.get('operator')} / ${order.get('price')}）")
    sent = send_code(session, phone, proxy=proxy)
    log(f"[phone] send -> HTTP {sent['status']} {str(sent['payload'])[:160]}")
    if sent["status"] != 200:
        if "phone_number_in_use" in sent["text"] or "suspicious" in sent["text"]:
            cancel(order.get("id"))
        return {"ok": False, "stage": "send", **sent, "phone": phone}
    code = wait_code(order.get("id"), timeout=timeout)
    log(f"[phone] 短信验证码: {code or '(超时)'}")
    if not code:
        cancel(order.get("id"))
        return {"ok": False, "stage": "sms_timeout", "phone": phone}
    verified = submit_code(session, code, proxy=proxy)
    log(f"[phone] verify {verified.get('endpoint')} -> HTTP {verified.get('status')} {str(verified.get('text'))[:160]}")
    return {"ok": verified.get("status") == 200, "stage": "verify", "phone": phone, **verified}
