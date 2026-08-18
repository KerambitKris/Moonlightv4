import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp

from config import (
    INBOUND_IDS,
    PANEL_API_TOKEN,
    PANEL_LOGIN,
    PANEL_PASSWORD,
    PANEL_URL,
    SUB_DOMAIN,
)


_session = None
_lock = asyncio.Lock()


def _email(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def _subscription(sub_id: str) -> str:
    return f"{SUB_DOMAIN}/sub/{sub_id}"


def _traffic_bytes(traffic_gb):
    if traffic_gb is None:
        return 0
    return int(traffic_gb) * 1024 * 1024 * 1024


def _expiry_from_ms(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


async def _open_session(force: bool = False):
    global _session

    if _session and not _session.closed and not force:
        return _session

    if _session and not _session.closed:
        await _session.close()

    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25),
        cookie_jar=aiohttp.CookieJar(),
        headers={"Accept": "application/json"},
    )

    if PANEL_API_TOKEN:
        _session.headers.update({
            "Authorization": f"Bearer {PANEL_API_TOKEN}",
            "Content-Type": "application/json",
        })
        return _session

    async with _session.post(
        f"{PANEL_URL}/login",
        json={"username": PANEL_LOGIN, "password": PANEL_PASSWORD},
    ) as response:
        text = await response.text()
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {}

        if response.status != 200 or data.get("success") is False:
            raise RuntimeError(
                f"Ошибка входа в 3X-UI: HTTP {response.status} {text[:500]}"
            )

    return _session


async def _request(method: str, path: str, payload=None, retry_auth: bool = True):
    session = await _open_session()
    kwargs = {"allow_redirects": True}
    if payload is not None:
        kwargs["json"] = payload

    async with session.request(method, f"{PANEL_URL}{path}", **kwargs) as response:
        text = await response.text()
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {}

        if response.status in (401, 403) and retry_auth and not PANEL_API_TOKEN:
            await _open_session(force=True)
            return await _request(method, path, payload, retry_auth=False)

        logging.info(
            "3X-UI %s %s -> HTTP %s | %s",
            method,
            path,
            response.status,
            text[:800],
        )
        return response.status, data, text


async def _get_inbound(inbound_id: int):
    status, data, text = await _request(
        "GET", f"/panel/api/inbounds/get/{inbound_id}"
    )
    if status != 200 or data.get("success") is False:
        raise RuntimeError(
            f"Не удалось получить inbound {inbound_id}: HTTP {status} {text[:500]}"
        )
    return data.get("obj") or data.get("data") or {}


async def _get_client_modern(email: str):
    status, data, text = await _request(
        "GET", f"/panel/api/clients/get/{quote(email, safe='')}"
    )

    if status == 404 or data.get("success") is False:
        return None
    if status != 200:
        raise RuntimeError(
            f"Ошибка чтения клиента 3X-UI: HTTP {status} {text[:500]}"
        )

    obj = data.get("obj") or data.get("data") or {}
    if not isinstance(obj, dict):
        return None

    client = obj.get("client") if isinstance(obj.get("client"), dict) else obj
    if not isinstance(client, dict) or not client.get("email"):
        return None

    return {
        "client": dict(client),
        "inbound_ids": list(obj.get("inboundIds") or []),
    }


async def _get_client_legacy(email: str):
    for inbound_id in INBOUND_IDS:
        inbound = await _get_inbound(inbound_id)
        settings = inbound.get("settings") or {}
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except json.JSONDecodeError:
                settings = {}

        for item in settings.get("clients", []):
            if str(item.get("email", "")) == email:
                return {
                    "client": dict(item),
                    "inbound_ids": [inbound_id],
                }
    return None


async def get_client(telegram_id: int):
    email = _email(telegram_id)
    modern = await _get_client_modern(email)
    if modern:
        return modern
    return await _get_client_legacy(email)


async def _attach_missing_modern(email: str, attached_ids):
    missing = [item for item in INBOUND_IDS if item not in set(attached_ids or [])]
    if not missing:
        return

    status, data, text = await _request(
        "POST",
        f"/panel/api/clients/{quote(email, safe='')}/attach",
        {"inboundIds": missing},
    )
    if status in (200, 201) and data.get("success") is not False:
        return

    if status != 404:
        raise RuntimeError(
            f"Не удалось прикрепить VPN к inbound {missing}: "
            f"HTTP {status} {text[:500]}"
        )

    # Старый API: добавляем того же клиента на недостающие inbound'ы.
    client_info = await _get_client_modern(email)
    if not client_info:
        raise RuntimeError("3X-UI не вернула клиента для legacy attach")
    client = client_info["client"]
    await _legacy_add_to_inbounds(client, missing)


async def _legacy_add_to_inbounds(client: dict, inbound_ids):
    for inbound_id in inbound_ids:
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]}, ensure_ascii=False),
        }
        status, data, text = await _request(
            "POST", "/panel/api/inbounds/addClient", payload
        )
        if status != 200 or data.get("success") is False:
            raise RuntimeError(
                f"Ошибка добавления клиента в inbound {inbound_id}: "
                f"HTTP {status} {text[:500]}"
            )


async def create_client(
    telegram_id: int,
    *,
    days: int,
    traffic_gb,
    devices: int,
):
    async with _lock:
        email = _email(telegram_id)
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(days=days)
        sub_id = uuid.uuid4().hex[:16]
        client_id = str(uuid.uuid4())

        client = {
            "id": client_id,
            "email": email,
            "subId": sub_id,
            "enable": True,
            "expiryTime": int(expiry.timestamp() * 1000),
            "totalGB": _traffic_bytes(traffic_gb),
            "limitIp": int(devices),
            "tgId": int(telegram_id),
            "comment": "Moonlight VPN",
        }

        status, data, text = await _request(
            "POST",
            "/panel/api/clients/add",
            {"client": client, "inboundIds": INBOUND_IDS},
        )

        used_legacy = status == 404
        if used_legacy:
            await _legacy_add_to_inbounds(client, INBOUND_IDS)
        elif status not in (200, 201) or data.get("success") is False:
            raise RuntimeError(
                f"Ошибка создания VPN: HTTP {status} {text[:1000]}"
            )

        saved = await get_client(telegram_id)
        if saved:
            saved_client = saved["client"]
            client_id = str(saved_client.get("id") or client_id)
            sub_id = str(saved_client.get("subId") or sub_id)
            # На современной панели сразу гарантируем все настроенные страны.
            # В legacy-режиме клиент уже добавлен циклом во все INBOUND_IDS.
            if not used_legacy and saved.get("inbound_ids"):
                await _attach_missing_modern(email, saved["inbound_ids"])

        return {
            "client_id": client_id,
            "sub_id": sub_id,
            "subscription": _subscription(sub_id),
            "expire": expiry,
        }


async def _reset_traffic(email: str):
    status, data, text = await _request(
        "POST", f"/panel/api/clients/resetTraffic/{quote(email, safe='')}"
    )
    if status in (200, 201) and data.get("success") is not False:
        return

    if status != 404:
        logging.warning(
            "Не удалось сбросить трафик modern API: HTTP %s %s",
            status,
            text[:500],
        )
        return

    # Совместимость со старым API.
    for inbound_id in INBOUND_IDS:
        status, data, text = await _request(
            "POST",
            f"/panel/api/inbounds/{inbound_id}/resetClientTraffic/{quote(email, safe='')}",
        )
        if status not in (200, 201) or data.get("success") is False:
            logging.warning(
                "Не удалось сбросить трафик inbound=%s: HTTP %s %s",
                inbound_id,
                status,
                text[:300],
            )


async def _legacy_update_client(client: dict):
    client_id = str(client.get("id") or "")
    if not client_id:
        raise RuntimeError("У legacy-клиента нет id")

    for inbound_id in INBOUND_IDS:
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]}, ensure_ascii=False),
        }
        status, data, text = await _request(
            "POST",
            f"/panel/api/inbounds/updateClient/{quote(client_id, safe='')}",
            payload,
        )

        # Если пользователя на этом inbound ещё нет — добавим.
        if status == 404 or data.get("success") is False:
            add_status, add_data, add_text = await _request(
                "POST", "/panel/api/inbounds/addClient", payload
            )
            if add_status != 200 or add_data.get("success") is False:
                raise RuntimeError(
                    f"Ошибка обновления inbound {inbound_id}: "
                    f"HTTP {add_status} {add_text[:500]}"
                )


async def extend_client(
    telegram_id: int,
    *,
    days: int,
    traffic_gb,
    devices: int,
    fallback_expiry=None,
):
    # Сначала проверяем наличие клиента без lock. Если его уже нет в панели,
    # создаём нового обычным create_client(), который сам возьмёт lock.
    info = await get_client(telegram_id)
    if not info:
        return await create_client(
            telegram_id,
            days=days,
            traffic_gb=traffic_gb,
            devices=devices,
        )

    async with _lock:
        # Перечитываем после входа в критическую секцию на случай параллельного запроса.
        info = await get_client(telegram_id)
        if not info:
            raise RuntimeError("Клиент исчез из 3X-UI во время продления")

        email = _email(telegram_id)
        client = dict(info["client"])
        now = datetime.now(timezone.utc)
        panel_expiry = _expiry_from_ms(client.get("expiryTime"))
        current_expiry = panel_expiry or fallback_expiry
        base = current_expiry if current_expiry and current_expiry > now else now
        expiry = base + timedelta(days=days)

        client.update({
            "email": email,
            "enable": True,
            "expiryTime": int(expiry.timestamp() * 1000),
            "totalGB": _traffic_bytes(traffic_gb),
            "limitIp": int(devices),
            "tgId": int(telegram_id),
            "comment": client.get("comment") or "Moonlight VPN",
        })

        status, data, text = await _request(
            "POST",
            f"/panel/api/clients/update/{quote(email, safe='')}",
            client,
        )

        modern_ok = status in (200, 201) and data.get("success") is not False
        if modern_ok:
            await _attach_missing_modern(email, info.get("inbound_ids") or [])
        elif status == 404:
            await _legacy_update_client(client)
        else:
            raise RuntimeError(
                f"Ошибка продления VPN: HTTP {status} {text[:1000]}"
            )

        # Каждый оплаченный тариф начинает свой полный пакет трафика заново.
        await _reset_traffic(email)

        sub_id = str(client.get("subId") or "").strip()
        client_id = str(client.get("id") or "").strip()
        if not sub_id:
            refreshed = await get_client(telegram_id)
            if refreshed:
                sub_id = str(refreshed["client"].get("subId") or "").strip()
                client_id = str(refreshed["client"].get("id") or client_id).strip()

        if not sub_id:
            raise RuntimeError("3X-UI не вернула subId клиента")

        return {
            "client_id": client_id,
            "sub_id": sub_id,
            "subscription": _subscription(sub_id),
            "expire": expiry,
        }


async def close_panel() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None
