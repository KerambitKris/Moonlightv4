import asyncio
import html
import logging
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils import executor

from config import BOT_TOKEN, DA_TOKEN, DA_URL, NEWS_URL, SUPPORT_URL
from database import (
    add_balance,
    close_db,
    credit_donation_once,
    get_payment_codes,
    get_user,
    init_db,
    register_user,
    remember_old_donation,
    save_vpn,
    subtract_balance,
)
from tariffs import TARIFFS, all_tariffs_text, tariff_button_text, tariff_card
from xui import close_panel, create_client, extend_client


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

PROMO_CODES = {
    "FREE30": {"amount": 30, "uses": 5},
    "VIP100": {"amount": 100, "uses": 2},
}
waiting_promo = set()
donations_initialized = False


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------
def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🚀 Подключиться"))
    kb.row(
        KeyboardButton("💎 Тарифы"),
        KeyboardButton("👤 Мой VPN"),
    )
    kb.row(
        KeyboardButton("💰 Баланс"),
        KeyboardButton("🎁 Промокод"),
    )
    kb.row(KeyboardButton("🆘 Помощь"))
    return kb


def tariffs_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(
            tariff_button_text(code), callback_data=f"tariff:{code}"
        )
        for code in TARIFFS
    ]
    kb.add(*buttons)
    kb.row(InlineKeyboardButton("💰 Пополнить баланс", callback_data="balance"))
    return kb


def tariff_confirm_keyboard(code: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("✅ Оплатить с баланса", callback_data=f"buy:{code}"),
        InlineKeyboardButton("← Все тарифы", callback_data="tariffs"),
    )
    return kb


def balance_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 Пополнить через DonationAlerts", url=DA_URL))
    kb.add(InlineKeyboardButton("🔥 Выбрать тариф", callback_data="tariffs"))
    return kb


def connect_keyboard(subscription: str):
    kb = InlineKeyboardMarkup(row_width=1)
    if subscription:
        kb.add(InlineKeyboardButton("🔗 Открыть подписку", url=subscription))
    kb.add(
        InlineKeyboardButton("📱 Как подключить", callback_data="howto"),
        InlineKeyboardButton("💎 Продлить", callback_data="tariffs"),
    )
    return kb


def help_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    if SUPPORT_URL:
        kb.add(InlineKeyboardButton("💬 Техническая поддержка", url=SUPPORT_URL))
    if NEWS_URL:
        kb.add(InlineKeyboardButton("📣 Новости Moonlight", url=NEWS_URL))
    kb.add(InlineKeyboardButton("📱 Инструкция по подключению", callback_data="howto"))
    return kb


def _format_money(value) -> str:
    return f"{float(value or 0):.2f} ₽"


def _format_date(value) -> str:
    if not value:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _is_active(user) -> bool:
    expiry = user["vpn_expires"] if user else None
    if not expiry:
        return False
    return expiry > datetime.now(timezone.utc)


def _traffic_text(user) -> str:
    if not user:
        return "—"
    value = user["vpn_traffic_gb"]
    if value is None and user["vpn_tariff_days"] in (180, 365):
        return "♾ Безлимит"
    if value is None:
        return "—"
    return f"📶 {value} ГБ"


def home_text(user) -> str:
    if _is_active(user):
        status = (
            "🟢 <b>VPN активен</b>\n"
            f"⏳ До {_format_date(user['vpn_expires'])}"
        )
    else:
        status = "⚪️ <b>VPN не активирован</b>"

    return (
        "🌙 <b>Moonlight VPN</b>\n\n"
        "Быстрый доступ к интернету на всех ваших устройствах.\n\n"
        f"{status}\n"
        f"💳 Баланс: <b>{_format_money(user['balance'])}</b>\n\n"
        "🌍 Одна подписка — все доступные страны\n"
        "📱 До 5 устройств\n\n"
        "Выберите действие 👇"
    )


def profile_text(user) -> str:
    if not user or not user["vpn_key"]:
        return (
            "👤 <b>Мой VPN</b>\n\n"
            "⚪️ Подписки пока нет.\n"
            "Выберите тариф — после оплаты бот выдаст одну ссылку для Happ, "
            "v2rayTun и других совместимых клиентов."
        )

    active = _is_active(user)
    status = "🟢 Активен" if active else "🔴 Истёк"
    traffic = _traffic_text(user)
    devices = int(user["vpn_device_limit"] or 5)

    return (
        "👤 <b>Мой VPN</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"⏳ До: <b>{_format_date(user['vpn_expires'])}</b>\n"
        f"{traffic}\n"
        f"📱 До {devices} устройств\n\n"
        "🌍 Все доступные страны находятся внутри одной подписки."
    )


def subscription_text(user) -> str:
    key = html.escape(str(user["vpn_key"] or ""))
    return (
        "🌙 <b>Moonlight VPN</b>\n\n"
        "🟢 <b>Подписка готова</b>\n"
        f"⏳ До: <b>{_format_date(user['vpn_expires'])}</b>\n"
        f"{_traffic_text(user)}\n"
        f"📱 До {int(user['vpn_device_limit'] or 5)} устройств\n\n"
        "🔗 <b>Ваша ссылка:</b>\n"
        f"<code>{key}</code>\n\n"
        "Добавьте эту ссылку как <b>подписку</b> в Happ, v2rayTun или другой "
        "совместимый клиент. После обновления подписки появится список стран."
    )


def howto_text() -> str:
    return (
        "📱 <b>Как подключиться</b>\n\n"
        "1️⃣ Установите Happ, v2rayTun или другой клиент с поддержкой VLESS/подписок.\n\n"
        "2️⃣ Нажмите «🚀 Подключиться» в боте и скопируйте вашу ссылку.\n\n"
        "3️⃣ В приложении выберите добавление <b>подписки по URL</b> и вставьте ссылку.\n\n"
        "4️⃣ Обновите подписку — появится список доступных стран.\n\n"
        "5️⃣ Выберите страну и подключитесь.\n\n"
        "⚠️ Ссылку подписки не передавайте другим людям: это ваш личный доступ."
    )


async def ensure_user(message: types.Message):
    return await register_user(
        message.from_user.id,
        message.from_user.username or "",
    )


# ---------------------------------------------------------
# DonationAlerts
# ---------------------------------------------------------
async def donation_loop():
    global donations_initialized

    await asyncio.sleep(5)

    if not DA_TOKEN:
        logging.warning("DonationAlerts: DA_TOKEN не задан, автозачисление отключено")
        return

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        auth_failed = False

        while True:
            try:
                headers = {
                    "Authorization": f"Bearer {DA_TOKEN}",
                    "Accept": "application/json",
                }
                async with session.get(
                    "https://www.donationalerts.com/api/v1/alerts/donations",
                    params={"page": 1},
                    headers=headers,
                ) as response:
                    body = await response.text()

                    if response.status == 401:
                        if not auth_failed:
                            logging.error(
                                "DonationAlerts HTTP 401: проверь DA_TOKEN и scope oauth-donation-index"
                            )
                            auth_failed = True
                        await asyncio.sleep(60)
                        continue

                    auth_failed = False
                    if response.status != 200:
                        logging.error(
                            "DonationAlerts HTTP %s: %s",
                            response.status,
                            body[:500],
                        )
                    else:
                        try:
                            payload = await response.json(content_type=None)
                        except Exception:
                            payload = {}

                        donations = payload.get("data", [])
                        if not donations_initialized:
                            for donation in donations:
                                try:
                                    await remember_old_donation(
                                        int(donation["id"]),
                                        float(donation.get("amount", 0)),
                                        str(donation.get("currency", "")),
                                    )
                                except Exception:
                                    logging.exception("Ошибка инициализации доната")
                            donations_initialized = True
                            logging.info("DonationAlerts: история инициализирована")
                        else:
                            for donation in reversed(donations):
                                await process_donation(donation)

            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Ошибка проверки DonationAlerts")

            await asyncio.sleep(20)


async def process_donation(donation):
    try:
        donation_id = int(donation["id"])
        amount = float(donation["amount"])
        currency = str(donation.get("currency", "")).upper()
        comment = str(donation.get("message") or "").upper()

        if currency != "RUB" or amount <= 0:
            return

        target_uid = None
        for row in await get_payment_codes():
            code = str(row["payment_code"] or "").upper()
            if code and code in comment:
                target_uid = int(row["telegram_id"])
                break

        if not target_uid:
            logging.warning(
                "Донат %s на %.2f RUB без payment_code: %r",
                donation_id,
                amount,
                comment,
            )
            return

        new_balance = await credit_donation_once(
            donation_id,
            target_uid,
            amount,
            currency,
        )
        if new_balance is None:
            return

        await bot.send_message(
            target_uid,
            "✅ <b>Пополнение получено</b>\n\n"
            f"💰 +{amount:.2f} ₽\n"
            f"💳 Баланс: <b>{_format_money(new_balance)}</b>",
            reply_markup=main_menu(),
        )

    except Exception:
        logging.exception("Ошибка обработки DonationAlerts")


# ---------------------------------------------------------
# Main handlers
# ---------------------------------------------------------
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user = await ensure_user(message)
    await message.answer(home_text(user), reply_markup=main_menu())


@dp.message_handler(lambda m: m.text in {"💎 Тарифы", "💳 Купить/Продлить"})
async def show_tariffs(message: types.Message):
    await ensure_user(message)
    await message.answer(all_tariffs_text(), reply_markup=tariffs_keyboard())


@dp.callback_query_handler(lambda c: c.data == "tariffs")
async def cb_tariffs(call: types.CallbackQuery):
    await call.answer()
    await call.message.edit_text(all_tariffs_text(), reply_markup=tariffs_keyboard())


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("tariff:"))
async def cb_tariff(call: types.CallbackQuery):
    await call.answer()
    code = call.data.split(":", 1)[1]
    if code not in TARIFFS:
        return

    user = await register_user(
        call.from_user.id,
        call.from_user.username or "",
    )
    tariff = TARIFFS[code]
    text = (
        "🌙 <b>Moonlight VPN</b>\n\n"
        f"{tariff_card(code)}\n\n"
        f"💳 Ваш баланс: <b>{_format_money(user['balance'])}</b>\n\n"
        "После оплаты текущая подписка будет создана или продлена."
    )
    await call.message.edit_text(text, reply_markup=tariff_confirm_keyboard(code))


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
async def cb_buy(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    if code not in TARIFFS:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await call.answer("Проверяю оплату…")
    uid = call.from_user.id
    user = await register_user(uid, call.from_user.username or "")
    tariff = TARIFFS[code]

    new_balance = await subtract_balance(uid, tariff["price"])
    if new_balance is None:
        user = await get_user(uid)
        text = (
            "❌ <b>Недостаточно средств</b>\n\n"
            f"Тариф: {tariff['days']} дней — {tariff['price']} ₽\n"
            f"Ваш баланс: <b>{_format_money(user['balance'])}</b>\n\n"
            f"🧾 Код для зачисления: <code>{html.escape(user['payment_code'])}</code>\n"
            "Укажите этот код в сообщении к донату."
        )
        await call.message.edit_text(text, reply_markup=balance_keyboard())
        return

    try:
        if user["vpn_client_id"] or user["vpn_key"]:
            vpn = await extend_client(
                uid,
                days=tariff["days"],
                traffic_gb=tariff["traffic_gb"],
                devices=tariff["devices"],
                fallback_expiry=user["vpn_expires"],
            )
        else:
            vpn = await create_client(
                uid,
                days=tariff["days"],
                traffic_gb=tariff["traffic_gb"],
                devices=tariff["devices"],
            )

        await save_vpn(
            uid,
            subscription=vpn["subscription"],
            client_id=vpn["client_id"],
            sub_id=vpn["sub_id"],
            expires=vpn["expire"],
            traffic_gb=tariff["traffic_gb"],
            device_limit=tariff["devices"],
            tariff_days=tariff["days"],
        )

        updated_user = await get_user(uid)
        traffic_line = (
            "♾ Безлимит"
            if tariff["traffic_gb"] is None
            else f"📶 {tariff['traffic_gb']} ГБ"
        )
        text = (
            "✅ <b>Moonlight VPN активирован</b>\n\n"
            f"📅 {tariff['days']} дней\n"
            f"{traffic_line}\n"
            f"📱 {tariff['devices']} устройств\n"
            f"⏳ До: <b>{_format_date(vpn['expire'])}</b>\n"
            f"💳 Остаток: <b>{_format_money(new_balance)}</b>\n\n"
            "🌍 В одной подписке будут все доступные страны.\n\n"
            "🔗 <b>Ссылка подписки:</b>\n"
            f"<code>{html.escape(vpn['subscription'])}</code>"
        )
        await call.message.edit_text(
            text,
            reply_markup=connect_keyboard(updated_user["vpn_key"]),
        )

    except Exception:
        await add_balance(uid, tariff["price"])
        logging.exception("Не удалось выдать VPN пользователю %s", uid)
        await call.message.edit_text(
            "⚠️ <b>Не удалось выдать VPN</b>\n\n"
            "Оплата возвращена на баланс. Попробуйте ещё раз или напишите в поддержку.",
            reply_markup=help_keyboard(),
        )


@dp.message_handler(lambda m: m.text == "🚀 Подключиться")
async def connect(message: types.Message):
    user = await ensure_user(message)
    if not user["vpn_key"] or not _is_active(user):
        await message.answer(
            "⚪️ <b>Активного VPN сейчас нет</b>\n\n"
            "Выберите тариф, чтобы получить подписку.",
            reply_markup=tariffs_keyboard(),
        )
        return

    await message.answer(
        subscription_text(user),
        reply_markup=connect_keyboard(user["vpn_key"]),
    )


@dp.message_handler(lambda m: m.text in {"👤 Мой VPN", "📱 Мои устройства"})
async def profile(message: types.Message):
    user = await ensure_user(message)
    kb = connect_keyboard(user["vpn_key"]) if user["vpn_key"] else tariffs_keyboard()
    await message.answer(profile_text(user), reply_markup=kb)


@dp.message_handler(lambda m: m.text == "💰 Баланс")
async def balance(message: types.Message):
    user = await ensure_user(message)
    await message.answer(
        "💰 <b>Баланс Moonlight</b>\n\n"
        f"Доступно: <b>{_format_money(user['balance'])}</b>\n\n"
        "Для автоматического зачисления укажите этот код в сообщении к донату:\n"
        f"<code>{html.escape(user['payment_code'])}</code>\n\n"
        "После зачисления бот пришлёт уведомление.",
        reply_markup=balance_keyboard(),
    )


@dp.callback_query_handler(lambda c: c.data == "balance")
async def cb_balance(call: types.CallbackQuery):
    await call.answer()
    user = await register_user(call.from_user.id, call.from_user.username or "")
    await call.message.edit_text(
        "💰 <b>Баланс Moonlight</b>\n\n"
        f"Доступно: <b>{_format_money(user['balance'])}</b>\n\n"
        "Код для сообщения к донату:\n"
        f"<code>{html.escape(user['payment_code'])}</code>",
        reply_markup=balance_keyboard(),
    )


@dp.message_handler(lambda m: m.text in {"🎁 Промокод", "🎟 Ввести промокод"})
async def promo(message: types.Message):
    await ensure_user(message)
    waiting_promo.add(message.from_user.id)
    await message.answer(
        "🎁 <b>Промокод</b>\n\nОтправьте код одним сообщением."
    )


@dp.message_handler(lambda m: m.text == "🆘 Помощь")
async def help_handler(message: types.Message):
    await ensure_user(message)
    await message.answer(
        "🆘 <b>Помощь Moonlight</b>\n\n"
        "Если VPN не подключается, сначала обновите подписку в приложении и попробуйте другую страну.\n\n"
        "Если проблема осталась — напишите в поддержку.",
        reply_markup=help_keyboard(),
    )


@dp.callback_query_handler(lambda c: c.data == "howto")
async def cb_howto(call: types.CallbackQuery):
    await call.answer()
    await call.message.edit_text(howto_text(), reply_markup=help_keyboard())


@dp.message_handler()
async def text_router(message: types.Message):
    uid = message.from_user.id

    if uid in waiting_promo:
        waiting_promo.remove(uid)
        code = (message.text or "").strip().upper()
        promo_item = PROMO_CODES.get(code)

        if not promo_item or promo_item["uses"] <= 0:
            await message.answer(
                "❌ Промокод неверный или уже закончился.",
                reply_markup=main_menu(),
            )
            return

        new_balance = await add_balance(uid, promo_item["amount"])
        promo_item["uses"] -= 1
        if promo_item["uses"] <= 0:
            PROMO_CODES.pop(code, None)

        await message.answer(
            "✅ <b>Промокод активирован</b>\n\n"
            f"💰 +{promo_item['amount']} ₽\n"
            f"💳 Баланс: <b>{_format_money(new_balance)}</b>",
            reply_markup=main_menu(),
        )
        return

    await message.answer(
        "Выберите действие в меню 👇",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------
async def on_startup(_):
    await init_db()
    logging.info("Database connected")
    asyncio.create_task(donation_loop())
    logging.info("Moonlight bot started")


async def on_shutdown(_):
    await close_panel()
    await close_db()
    await bot.session.close()


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )
