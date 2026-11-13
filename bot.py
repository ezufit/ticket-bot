import asyncio
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
TARGET_URL = "https://topbilet.kz/ru/event/platina-v-almaty-2"
DISCOUNT_PERCENT = 40
ALMATY_TZ = ZoneInfo("Asia/Almaty")

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


def ensure_chromium():
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True
        )
    except Exception as e:
        logging.error(f"Не удалось скачать Chromium: {e}")


async def collect_all_text(context) -> str:
    full_text = ""
    for p in context.pages:
        try:
            full_text += "\n" + await p.inner_text("body")
        except Exception:
            pass
        for frame in p.frames:
            try:
                full_text += "\n" + await frame.inner_text("body")
            except Exception:
                pass
    return full_text


async def fetch_topbilet_prices(retry_on_empty: bool = True):
    logging.info("Запускаю парсинг Topbilet...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",
                    "--no-zygote"
                ]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()

            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            buy_btn = page.get_by_text("Купить билет").locator("visible=true").first
            await buy_btn.wait_for(state="visible", timeout=20000)
            await buy_btn.scroll_into_view_if_needed()
            await buy_btn.click(force=True)

            full_text = ""
            for attempt in range(45):
                await page.wait_for_timeout(1000)
                full_text = await collect_all_text(context)
                if "танцпол" in full_text.lower() and "₸" in full_text:
                    await page.wait_for_timeout(1500)
                    full_text = await collect_all_text(context)
                    logging.info(f"Страница с ценами загрузилась за {attempt + 1} сек!")
                    break

            await browser.close()

            lines = [line.strip() for line in full_text.splitlines() if line.strip()]

            target_categories = [
                ("Танцпол", "танцпол"),
                ("Ранний Вход", "ранний вход"),
                ("VIP", "vip")
            ]

            result = []
            found_any = False

            for display_name, prefix in target_categories:
                found_price = None

                for idx, line in enumerate(lines):
                    if line.lower().startswith(prefix):
                        chunk = " ".join(lines[idx:idx + 4])
                        match = re.search(r"(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{4,6})\s*₸", chunk)
                        if match:
                            num = int(re.sub(r"[\s\u00A0]+", "", match.group(1)))
                            if 3000 <= num <= 500000:
                                found_price = num
                                break

                if found_price is not None:
                    found_any = True
                    final_price = round(found_price * (1 - DISCOUNT_PERCENT / 100))
                    result.append({
                        "name": display_name,
                        "price": final_price
                    })
                else:
                    result.append({
                        "name": display_name,
                        "price": None
                    })

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
    ensure_chromium()
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
