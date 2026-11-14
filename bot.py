import asyncio
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
TARGET_URL = "https://topbilet.kz/ru/event/platina-v-almaty-2"
DISCOUNT_PERCENT = 40
ALMATY_TZ = ZoneInfo("Asia/Almaty")

price_data = {"tickets": [], "updated_at": "Еще не обновлялось"}
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
parse_lock = asyncio.Lock()

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[[
        KeyboardButton(text="🎟 Актуальные цены"),
        KeyboardButton(text="🔄 Обновить цены")
    ]],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие ниже"
)


def scrape_sync() -> str:
    options = Options()
    if os.path.exists("/usr/bin/google-chrome"):
        options.binary_location = "/usr/bin/google-chrome"
    for arg in (
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-extensions", "--disable-background-networking",
        "--window-size=1280,900", "--js-flags=--max-old-space-size=128"
    ):
        options.add_argument(arg)
    options.page_load_strategy = "eager"
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.fonts": 2,
    })

    service = Service("/usr/local/bin/chromedriver") if os.path.exists("/usr/local/bin/chromedriver") else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)

    try:
        driver.get(TARGET_URL)
        driver.implicitly_wait(5)
        for btn in driver.find_elements(By.XPATH, "//*[contains(text(), 'Купить билет')]"):
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                break

        full_text = ""
        for attempt in range(40):
            time.sleep(1)
            texts = []
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                try:
                    texts.append(driver.find_element(By.TAG_NAME, "body").text)
                except Exception:
                    pass
                for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                    try:
                        driver.switch_to.frame(frame)
                        texts.append(driver.find_element(By.TAG_NAME, "body").text)
                    except Exception:
                        pass
                    finally:
                        driver.switch_to.default_content()

            full_text = "\n".join(texts)
            if "танцпол" in full_text.lower() and "₸" in full_text:
                logging.info(f"Страница загрузилась за {attempt + 1} сек!")
                break
        return full_text
    finally:
        driver.quit()


def parse_lines(full_text: str) -> tuple[list[dict], bool]:
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    categories = [("Танцпол", "танцпол"), ("Ранний Вход", "ранний вход"), ("VIP", "vip")]
    result, found_any = [], False

    for name, prefix in categories:
        found_price = None
        for idx, line in enumerate(lines):
            if line.lower().startswith(prefix):
                match = re.search(r"(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{4,6})\s*₸", " ".join(lines[idx:idx + 4]))
                if match:
                    num = int(re.sub(r"[\s\u00A0]+", "", match.group(1)))
                    if 3000 <= num <= 500000:
                        found_price = num
                        break
        if found_price is not None:
            found_any = True
            result.append({"name": name, "price": round(found_price * (1 - DISCOUNT_PERCENT / 100))})
        else:
            result.append({"name": name, "price": None})
    return result, found_any


async def fetch_topbilet_prices(retry: bool = True):
    if parse_lock.locked():
        return
    async with parse_lock:
        logging.info("Запускаю парсинг Topbilet...")
        loop = asyncio.get_running_loop()
        for attempt in range(2 if retry else 1):
            try:
                text = await loop.run_in_executor(None, scrape_sync)
                result, found_any = parse_lines(text)
                if found_any or not price_data["tickets"]:
                    price_data["tickets"] = result
                    price_data["updated_at"] = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M")
                    logging.info(f"Цены обновлены: {result}")
                if found_any:
                    return
                if attempt == 0 and retry:
                    await asyncio.sleep(15)
            except Exception as e:
                logging.error(f"Ошибка парсинга: {e}")
                if attempt == 0 and retry:
                    await asyncio.sleep(15)


async def send_prices(message: types.Message):
    if not price_data["tickets"]:
        await message.answer("⏳ Цены еще загружаются. Подождите 10 секунд.", reply_markup=main_keyboard)
        return

    lines = [
        "🎤 **Платина в Алматы (Motor Club)**",
        "📅 13.12.2026, 20:00\n",
        "🎟 **Актуальная стоимость билетов:**\n"
    ]
    for item in price_data["tickets"]:
        price_str = f"`{item['price']:,}".replace(",", " ") + " ₸`" if item["price"] else "Нет в наличии"
        lines.append(f"• **{item['name']}:** {price_str}")
    lines.append(f"\n🕒 _Обновлено: {price_data['updated_at']}_")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard)


@dp.message(Command("start", "price"))
@dp.message(F.text == "🎟 Актуальные цены")
async def cmd_price(message: types.Message):
    await send_prices(message)


@dp.message(Command("force_update"))
@dp.message(F.text == "🔄 Обновить цены")
async def cmd_force_update(message: types.Message):
    if parse_lock.locked():
        await message.answer("⏳ Цены уже обновляются, подождите...", reply_markup=main_keyboard)
        return
    await message.answer("🔄 Обновляю актуальные цены...", reply_markup=main_keyboard)
    await fetch_topbilet_prices(retry=False)
    await send_prices(message)


async def main():
    await bot.set_my_commands([
        BotCommand(command="price", description="🎟 Актуальные цены"),
        BotCommand(command="force_update", description="🔄 Обновить цены")
    ])
    asyncio.create_task(fetch_topbilet_prices())
    scheduler = AsyncIOScheduler(timezone=ALMATY_TZ)
    scheduler.add_job(fetch_topbilet_prices, "cron", hour=10, minute=0)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
