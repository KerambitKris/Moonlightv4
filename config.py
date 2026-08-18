import os


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан {name}")
    return value


def _parse_inbound_ids() -> list[int]:
    raw = os.getenv("INBOUND_IDS", "").strip()
    if raw:
        ids = []
        for item in raw.split(","):
            item = item.strip()
            if item:
                ids.append(int(item))
        if not ids:
            raise RuntimeError("INBOUND_IDS пуст")
        return ids

    # Обратная совместимость со старой переменной.
    return [int(os.getenv("INBOUND_ID", "1"))]


BOT_TOKEN = _required("BOT_TOKEN")
DATABASE_URL = _required("DATABASE_URL")

# 3X-UI
PANEL_URL = _required("PANEL_URL").rstrip("/")
PANEL_LOGIN = os.getenv("PANEL_LOGIN", "").strip()
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
PANEL_API_TOKEN = os.getenv("PANEL_API_TOKEN", "").strip()
INBOUND_IDS = _parse_inbound_ids()

if not PANEL_API_TOKEN and (not PANEL_LOGIN or not PANEL_PASSWORD):
    raise RuntimeError(
        "Задай либо PANEL_API_TOKEN, либо PANEL_LOGIN + PANEL_PASSWORD"
    )

# Домен, через который пользователю выдаётся subscription URL.
SUB_DOMAIN = os.getenv("SUB_DOMAIN", "https://moonlight-vpn.ru").strip().rstrip("/")

# DonationAlerts
DA_URL = _required("DA_URL").rstrip("/")
DA_TOKEN = os.getenv("DA_TOKEN", "").strip()

# Ссылки проекта. Можно переопределить в Railway Variables.
NEWS_URL = os.getenv("NEWS_URL", "https://t.me/moonlight_vpn_news").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/mtfunit").strip()

# Сколько устройств разрешено каждому тарифу.
DEFAULT_DEVICE_LIMIT = int(os.getenv("DEVICE_LIMIT", "5"))
