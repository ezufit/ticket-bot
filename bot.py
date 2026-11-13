import asyncio
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
TARGET_URL = "https://topbilet.kz/ru/event/platina-v-almaty-2"
DISCOUNT_PERCENT = 40
ALMATY_TZ = ZoneInfo("Asia/Almaty")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": TARGET_URL,
}

price_data = {
    "tickets": [],
    "updated_at": "Еще не обновлялось"
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎟 Актуальные цены")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Нажмите кнопку ниже, чтобы узнать цены"
)


def parse_prices_from_any(raw_text: str) -> list[dict]:
    """Ищет цены как в обычном тексте, так и внутри JSON-ответов API виджетов."""
    target_categories = [
        ("Танцпол", "танцпол"),
        ("Ранний Вход", "ранний вход"),
        ("VIP", "vip")
    ]
    found_map = {}

    # 1. Пробуем распарсить как JSON (наиболее частый формат API билетных виджетов)
    def walk_json(obj):
        if isinstance(obj, dict):
            name_val = ""
            price_val = None
            for k, v in obj.items():
                kl = k.lower()
                if kl in ("name", "title", "category", "sector", "tariff", "label") and isinstance(v, str):
                    name_val = v.lower()
                elif kl in ("price", "cost", "amount", "value", "minprice", "min_price"):
                    if isinstance(v, (int, float)) and 3000 <= v <= 500000:
                        price_val = int(v)
                    elif isinstance(v, str) and v.isdigit() and 3000 <= int(v) <= 500000:
                        price_val = int(v)
            if name_val and price_val:
                for display_name, prefix in target_categories:
                    if prefix in name_val and display_name not in found_map:
                        found_map[display_name] = price_val
            for v in obj.values():
                walk_json(v)
        elif isinstance(obj, list):
            for item in obj:
                walk_json(item)

    try:
        parsed = json.loads(raw_text)
        walk_json(parsed)
    except Exception:
        pass

    # 2. Если в JSON не нашлось, ищем регулярками по тексту/HTML/JS-переменным
    if len(found_map) < 3:
        clean_text = BeautifulSoup(raw_text, "html.parser").get_text("\n")
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
        for display_name, prefix in target_categories:
            if display_name in found_map:
                continue
            for idx, line in enumerate(lines):
                if prefix in line.lower():
                    chunk = " ".join(lines[idx:idx + 5])
                    match = re.search(r"(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{4,6})\s*(?:₸|KZT|тг)?", chunk, re.IGNORECASE)
                    if match:
                        num = int(re.sub(r"[\s\u00A0]+", "", match.group(1)))
                        if 3000 <= num <= 500000:
                            found_map[display_name] = num
                            break

    result = []
    for display_name, _ in target_categories:
        raw_p = found_map.get(display_name)
        if raw_p is not None:
            result.append({
                "name": display_name,
                "price": round(raw_p * (1 - DISCOUNT_PERCENT / 100))
            })
        else:
            result.append({"name": display_name, "price": None})
    return result


async def fetch_topbilet_prices(retry_on_empty: bool = True):
    logging.info("Запускаю парсинг Topbilet через API/HTML...")
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(TARGET_URL, timeout=30) as resp:
                html = await resp.text()

            combined_chunks = [html]
            soup = BeautifulSoup(html, "html.parser")

            # Собираем все ссылки на виджеты, API и JS-бандлы события
            candidate_urls = set()
            for tag in soup.find_all(["iframe", "a", "script", "div"]):
                for attr in ("src", "href", "data-url", "data-src", "data-widget", "data-event-id", "data-id"):
                    val = tag.get(attr)
                    if not val or not isinstance(val, str):
                        continue
                    if val.startswith("//"):
                        candidate_urls.add("https:" + val)
                    elif val.startswith("/") and any(k in val.lower() for k in ["api", "event", "ticket", "widget", "session", "hall", "seat"]):
                        candidate_urls.add("https://topbilet.kz" + val)
                    elif val.startswith("http") and any(k in val.lower() for k in ["topbilet", "ticket", "widget", "api", "event", "session", "seat", "hall"]):
                        candidate_urls.add(val)

            # Ищем скрытые API-пути и ID события внутри inline-скриптов (Next.js / Nuxt / Vue / Laravel)
            for script in soup.find_all("script"):
                if not script.string:
                    continue
                for m in re.findall(r"https?://[^\s\"'<>\\]+", script.string):
                    if any(k in m.lower() for k in ["api", "widget", "ticket", "event", "session", "seat", "hall", "price"]):
                        candidate_urls.add(m)
                for m in re.findall(r"[\"'](/api/[^\"'<>\\]+|/ru/[^\"'<>\\]+|/widget/[^\"'<>\\]+)[\"']", script.string):
                    candidate_urls.add("https://topbilet.kz" + m)

            # Извлекаем числовые ID события из HTML/URL (например event_id / session_id)
            event_ids = set(re.findall(r"(?:event_id|session_id|eventId|sessionId|widget_id)[\"']?\s*[:=]\s*[\"']?(\d{3,10})", html))
            for eid in list(event_ids)[:5]:
                candidate_urls.update([
                    f"https://topbilet.kz/api/events/{eid}",
                    f"https://topbilet.kz/api/event/{eid}",
                    f"https://topbilet.kz/api/sessions/{eid}",
                    f"https://topbilet.kz/api/tickets/{eid}",
                    f"https://topbilet.kz/widget/event/{eid}",
                ])

            logging.info(f"Найдено кандидатов URL/API: {len(candidate_urls)}")

            for url in list(candidate_urls)[:20]:
                if url == TARGET_URL or url.endswith((".png", ".jpg", ".css", ".woff2", ".svg")):
                    continue
                try:
                    async with session.get(url, timeout=12) as sub_resp:
                        if sub_resp.status == 200:
                            txt = await sub_resp.text()
                            if any(w in txt.lower() for w in ["танцпол", "ранний", "vip", "price", "ticket"]):
                                logging.info(f"Нашел полезные данные в: {url}")
                                combined_chunks.append(txt)
                except Exception:
                    pass

            result = parse_prices_from_any("\n".join(combined_chunks))
            found_any = any(item["price"] is not None for item in result)

            if found_any or not price_data["tickets"]:
                price_data["tickets"] = result
                price_data["updated_at"] = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M")
                logging.info(f"Цены обновлены: {result}")
            else:
                logging.warning("Парсер вернул пустой список, оставляю предыдущие цены.")

            if not found_any and retry_on_empty:
                logging.info("Повторная попытка парсинга через 15 секунд...")
                await asyncio.sleep(15)
                await fetch_topbilet_prices(retry_on_empty=False)

    except Exception as e:
        logging.error(f"Ошибка при парсинге Topbilet: {e}")
        if retry_on_empty:
            await asyncio.sleep(15)
            await fetch_topbilet_prices(retry_on_empty=False)


def format_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def send_prices(message: types.Message):
    if not price_data["tickets"]:
        await message.answer(
            "⏳ Цены еще загружаются. Подождите 10 секунд и нажмите кнопку еще раз.",
            reply_markup=main_keyboard
        )
        return

    lines = [
        "🎤 **Платина в Алматы (Motor Club)**",
        "📅 13.12.2026, 20:00\n",
        "🎟 **Актуальная стоимость билетов:**\n"
    ]

    for item in price_data["tickets"]:
        if item["price"] is not None:
            lines.append(f"• **{item['name']}:** `{format_num(item['price'])} ₸`")
        else:
            lines.append(f"• **{item['name']}:** Нет в наличии")

    lines.append(f"\n🕒 _Обновлено: {price_data['updated_at']}_")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard)


@dp.message(Command("start", "price"))
@dp.message(F.text == "🎟 Актуальные цены")
async def cmd_price(message: types.Message):
    await send_prices(message)


@dp.message(Command("force_update"))
async def cmd_force_update(message: types.Message):
    await message.answer("🔄 Обновляю актуальные цены...", reply_markup=main_keyboard)
    await fetch_topbilet_prices(retry_on_empty=False)
    await send_prices(message)


async def main():
    await bot.set_my_commands([
        BotCommand(command="price", description="🎟 Актуальные цены")
    ])

    asyncio.
