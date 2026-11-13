import asyncio
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
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


def extract_prices_from_text(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    target_categories = [
        ("Танцпол", "танцпол"),
        ("Ранний Вход", "ранний вход"),
        ("VIP", "vip")
    ]
    result = []
    for display_name, prefix in target_categories:
        found_price = None
        for idx, line in enumerate(lines):
            if prefix in line.lower():
                chunk = " ".join(lines[idx:idx + 5])
                match = re.search(r"(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{4,6})\s*₸", chunk)
                if match:
                    num = int(re.sub(r"[\s\u00A0]+", "", match.group(1)))
                    if 3000 <= num <= 500000:
                        found_price = num
                        break
        if found_price is not None:
            final_price = round(found_price * (1 - DISCOUNT_PERCENT / 100))
            result.append({"name": display_name, "price": final_price})
        else:
            result.append({"name": display_name, "price": None})
    return result


async def fetch_topbilet_prices(retry_on_empty: bool = True):
    logging.info("Запускаю легкий парсинг Topbilet...")
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(TARGET_URL, timeout=30) as resp:
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            combined_text = soup.get_text("\n")

            extra_urls = set()
            for tag in soup.find_all(["iframe", "a", "script"]):
                src = tag.get("src") or tag.get("href") or ""
                if any(k in src.lower() for k in ["widget", "ticket", "seat", "hall", "checkout", "buy", "event"]):
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = "https://topbilet.kz" + src
                    if src.startswith("http") and src != TARGET_URL:
                        extra_urls.add(src)

            for script in soup.find_all("script"):
                if script.string:
                    for m in re.findall(r"https?://[^\s\"'<>]+(?:widget|ticket|hall|session|event)[^\s\"'<>]*", script.string):
                        extra_urls.add(m)

            for url in list(extra_urls)[:8]:
                try:
                    async with session.get(url, timeout=15) as sub_resp:
                        if sub_resp.status == 200:
                            sub_text = await sub_resp.text()
                            sub_soup = BeautifulSoup(sub_text, "html.parser")
                            combined_text += "\n" + sub_soup.get_text("\n")
                except Exception:
                    pass

            result = extract_prices_from_text(combined_text)
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

    asyncio.create_task(fetch_topbilet_prices())

    scheduler = AsyncIOScheduler(timezone=ALMATY_TZ)
    scheduler.add_job(fetch_topbilet_prices, "cron", hour=10, minute=0)
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
