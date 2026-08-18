from collections import OrderedDict


# traffic_gb=None означает безлимит.
TARIFFS = OrderedDict({
    "5": {
        "days": 5,
        "price": 19,
        "traffic_gb": 500,
        "devices": 5,
    },
    "14": {
        "days": 14,
        "price": 49,
        "traffic_gb": 500,
        "devices": 5,
    },
    "30": {
        "days": 30,
        "price": 99,
        "traffic_gb": 500,
        "devices": 5,
    },
    "60": {
        "days": 60,
        "price": 189,
        "traffic_gb": 500,
        "devices": 5,
    },
    "90": {
        "days": 90,
        "price": 249,
        "traffic_gb": 500,
        "devices": 5,
    },
    "180": {
        "days": 180,
        "price": 439,
        "traffic_gb": None,
        "devices": 5,
    },
    "365": {
        "days": 365,
        "price": 799,
        "traffic_gb": None,
        "devices": 5,
    },
})


def traffic_label(tariff: dict) -> str:
    traffic = tariff.get("traffic_gb")
    return "♾ Безлимит" if traffic is None else f"📶 {traffic} ГБ"


def tariff_button_text(code: str) -> str:
    tariff = TARIFFS[code]
    return f"{tariff['days']} дней · {tariff['price']} ₽"


def tariff_card(code: str) -> str:
    tariff = TARIFFS[code]
    traffic = traffic_label(tariff)
    return (
        f"<b>{tariff['days']} дней</b>\n"
        f"💰 {tariff['price']}₽\n"
        f"{traffic}\n"
        f"📱 {tariff['devices']} устройств"
    )


def all_tariffs_text() -> str:
    cards = [tariff_card(code) for code in TARIFFS]
    return "🌙 <b>Moonlight VPN</b>\n\n🔥 <b>Тарифы:</b>\n\n" + "\n\n".join(cards)
