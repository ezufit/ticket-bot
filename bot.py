import asyncio
import logging
import os
import re
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
parse_lock = asyncio.Lock()

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🎟 Актуальные цены"),
            KeyboardButton(text="🔄 Обновить цены")
        ]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие ниже"
)


def scrape_sync() -> str:
    options = Options()
    if os.path.exists("/usr/bin/google-chrome"):
        options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--js-flags=--max-old-space-size=128")
    options.page_load_strategy = "eager"

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.fonts": 2,
    }
    options.add_experimental_option("prefs", prefs)

    service = Service("/usr/local/bin/chromedriver") if os.path.exists("/usr/local/bin/chromedriver") else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)

    try:
        driver.get(TARGET_URL)
        driver.implicitly_wait(5)

        buttons = driver.find_elements(By.XPATH, "//*[contains(text(), 'Купить билет')]")
        for btn in buttons:
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                break

        full_text = ""
        for attempt in range(40):
            import time
            time.sleep(1)

            texts = []
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                try:
                    texts.append(driver.find_element(By.TAG_NAME, "body").text)
                except Exception:
                    pass
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for frame in iframes:
                    try:
                        driver.switch_to.frame(frame)
                        texts.append(driver.find_element(By.TAG_NAME, "body").text)
                        driver.switch_to.default_content()
                    except Exception:
                        driver.switch_to.default_content()

            full_text = "\n".join(texts)
            if "танцпол" in full_text.lower() and "₸" in full_text:
                logging.info(f"Страница с ценами загрузилась за {attempt + 1} сек!")
                break

        return full_text
    finally:
        driver.quit()


async def fetch_topbilet_prices(retry_on_empty: bool = True):
    if parse_lock.locked():
        logging.info("Парсинг уже выполняется, пропускаю дублирующий запуск.")
        return

    async with parse_lock:
        logging.info("Запускаю парсинг Topbilet через встроенный Chrome...")
        try:
            loop = asyncio.get_running_loop()
            full_text = await loop.run_in_executor(None, scrape_sync)

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
                    result.append({"name": display_name, "price": final_price})
                else:
                    result.append({"name": display_name, "price": None})

            if found_any or not price_data["tickets"]:
                price_data["tickets"] = result
                price_data["updated_at"] = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M")
                logging.info(f"Цены обновлены: {result}")
            else:
                logging.warning("Парсер вернул пустой список, оставляю предыдущие цены.")

            if not found_any and retry_on_empty:
                logging.info("Повторная попытка парсинга через 15 секунд...")
                await asyncio.sleep(15)
                full_text_retry = await loop.run_in_executor(None, scrape_sync)
                # Повторная проверка
                lines_r = [line.strip() for line in full_text_retry.splitlines() if line.strip()]
                res_r = []
                found_r = False
                for display_name, prefix in target_categories:
                    fp = None
                    for idx, line in enumerate(lines_r):
                        if line.lower().startswith(prefix):
                            chunk = " ".join(lines_r[idx:idx + 4])
                            match = re.search(r"(\d{1,3}(?:[\s\u00A0]\d{3})+|\d{4,6})\s*₸", chunk)
                            if match:
                                num = int(re.sub(r"[\s\u00A0]+", "", match.group(1)))
                                if 3000 <= num <= 500000:
                                    fp = num
                                    break
                    if fp is not None:
                        found_r = True
                        res_r.append({"name": display_name, "price": round(fp * (1 - DISCOUNT_PERCENT / 100))})
                    else:
                        res_r.append({"name": display_name, "price": None})
                if found_r:
                    price_data["tickets"] = res_r
                    price_data["updated_at"] = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M")
                    logging.info(f"Цены обновлены со второй попытки: {res_r}")

        except Exception as e:
            logging.error(f"Ошибка при парсинге Topbilet: {e}")


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
@dp.message(F.text == "🔄 Обновить цены")
async def cmd_force_update(message: types.Message):
    if parse_lock.locked():
        await message.answer("⏳ Цены уже обновляются, подождите пару секунд...", reply_markup=main_keyboard)
        return
    await message.answer("🔄 Обновляю актуальные цены...", reply_markup=main_keyboard)
    await fetch_topbilet_prices(retry_on_empty=False)
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
