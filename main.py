import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==================== НАСТРОЙКИ ====================

TELEGRAM_BOT_TOKEN = "8823207989:AAEA4dw8sDApbf3T438GnyHRSH9f7-B_BCE"  # Замени на свой токен от @BotFather
AVIASALES_API_TOKEN = "19311b34c815711c2a8b70f2f3dbffa0"  # Замени на токен из Travelpayouts

# IATA-код Новосибирска (Толмачёво)
ORIGIN_CITY = "OVB"

# ID чата/канала, куда отправлять спецпредложения
# Узнать ID: @userinfobot или @getidsbot
TARGET_CHAT_ID = -1003873649064  # Замени на ID своего чата/канала

# Интервал проверки (в минутах)
CHECK_INTERVAL_MINUTES = 30

# Минимальная скидка в % для отправки (опционально)
MIN_DISCOUNT_PERCENT = 20

# Файл для хранения уже отправленных предложений
SEEN_OFFERS_FILE = "seen_offers.json"

# ==================== ЛОГИ ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== БОТ ====================

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ==================== ХРАНИЛИЩЕ ОТПРАВЛЕННЫХ ====================

def load_seen_offers() -> set:
    """Загружает ID уже отправленных предложений."""
    try:
        with open(SEEN_OFFERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_offers(seen: set):
    """Сохраняет ID отправленных предложений."""
    with open(SEEN_OFFERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

seen_offers = load_seen_offers()

# ==================== API AVIASALES ====================

async def fetch_special_offers() -> list:
    """
    Получает спецпредложения через API Aviasales/Travelpayouts.
    Документация: https://support.travelpayouts.com/hc/en-us/articles/203956163
    """
    url = "https://api.travelpayouts.com/aviasales/v3/get_special_offers"
    
    params = {
        "origin": ORIGIN_CITY,
        "locale": "ru",
        "currency": "rub",
        "market": "ru",
        "token": AVIASALES_API_TOKEN,
    }
    
    headers = {
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"API вернул статус {resp.status}")
                    return []
                
                data = await resp.json()
                
                if not data.get("success"):
                    logger.error(f"API ошибка: {data.get('error')}")
                    return []
                
                return data.get("data", [])
                
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе к API")
            return []
        except Exception as e:
            logger.error(f"Ошибка при запросе к API: {e}")
            return []

# ==================== ФОРМАТИРОВАНИЕ СООБЩЕНИЯ ====================

def format_offer_message(offer: dict) -> str:
    """Форматирует предложение в красивое сообщение для Telegram."""
    
    origin_name = offer.get("origin_name", offer.get("origin", "Неизвестно"))
    destination_name = offer.get("destination_name", offer.get("destination", "Неизвестно"))
    price = offer.get("price", 0)
    airline = offer.get("airline_title", offer.get("airline", "Неизвестно"))
    flight_number = offer.get("flight_number", "")
    
    # Дата вылета
    departure_raw = offer.get("departure_at", "")
    try:
        departure_dt = datetime.fromisoformat(departure_raw.replace("Z", "+00:00"))
        departure_str = departure_dt.strftime("%d %B %Y, %H:%M")
    except:
        departure_str = departure_raw
    
    # Дата возврата
    return_raw = offer.get("return_at", "")
    return_str = ""
    if return_raw:
        try:
            return_dt = datetime.fromisoformat(return_raw.replace("Z", "+00:00"))
            return_str = f"\n🔙 <b>Обратно:</b> {return_dt.strftime('%d %B %Y, %H:%M')}"
        except:
            return_str = f"\n🔙 <b>Обратно:</b> {return_raw}"
    
    # Длительность перелёта
    duration = offer.get("duration", 0)
    duration_str = f"{duration // 60}ч {duration % 60}м" if duration else "Неизвестно"
    
    # Ссылка на билет
    link_suffix = offer.get("link", "")
    search_link = f"https://www.aviasales.ru/search{link_suffix}" if link_suffix else "https://www.aviasales.ru"
    
    # Уникальный ID предложения
    offer_id = offer.get("search_id", "") or offer.get("signature", "")
    
    message = (
        f"✈️ <b>Горящее предложение из Новосибирска!</b>\n\n"
        f"🛫 <b>{origin_name}</b> → <b>{destination_name}</b>\n"
        f"💰 <b>Цена:</b> {price:,} ₽\n"
        f"🏢 <b>Авиакомпания:</b> {airline} {flight_number}\n"
        f"📅 <b>Вылет:</b> {departure_str}{return_str}\n"
        f"⏱ <b>В пути:</b> {duration_str}\n\n"
        f"🔗 <a href='{search_link}'>Посмотреть на Aviasales</a>"
    )
    
    return message, offer_id

# ==================== ОТПРАВКА В ЧАТ ====================

async def send_new_offers():
    """Получает предложения и отправляет новые в чат."""
    global seen_offers
    
    logger.info("Проверяю новые спецпредложения...")
    offers = await fetch_special_offers()
    
    if not offers:
        logger.info("Новых предложений не найдено")
        return
    
    new_count = 0
    
    for offer in offers:
        message, offer_id = format_offer_message(offer)
        
        # Пропускаем уже отправленные
        if offer_id in seen_offers:
            continue
        
        # Отправляем в чат
        try:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            seen_offers.add(offer_id)
            new_count += 1
            logger.info(f"Отправлено предложение: {offer_id}")
            
            # Небольшая задержка между сообщениями
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
    
    if new_count > 0:
        save_seen_offers(seen_offers)
        logger.info(f"Отправлено {new_count} новых предложений")
    else:
        logger.info("Новых уникальных предложений не найдено")

# ==================== КОМАНДЫ БОТА ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 <b>Бот спецпредложений Aviasales</b>\n\n"
        "Я автоматически проверяю горящие билеты из Новосибирска (OVB) "
        "и отправляю их в чат.\n\n"
        "<b>Команды:</b>\n"
        "/check — проверить предложения прямо сейчас\n"
        "/status — статус бота\n"
        "/help — справка",
        parse_mode="HTML"
    )

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    """Ручная проверка предложений."""
    await message.answer("🔍 Проверяю предложения...")
    await send_new_offers()
    await message.answer("✅ Проверка завершена!")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Статус бота."""
    await message.answer(
        f"📊 <b>Статус бота</b>\n\n"
        f"🛫 Город вылета: Новосибирск (OVB)\n"
        f"⏱ Интервал проверки: каждые {CHECK_INTERVAL_MINUTES} мин\n"
        f"📨 Отправлено предложений: {len(seen_offers)}\n"
        f"✅ Бот работает",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Бот использует официальный API Travelpayouts для получения "
        "спецпредложений авиакомпаний.\n\n"
        "Данные обновляются автоматически каждые 30 минут.\n"
        "Чтобы не спамить, одинаковые предложения не отправляются повторно.",
        parse_mode="HTML"
    )

# ==================== ЗАПУСК ====================

async def main():
    # Планировщик для автоматической проверки
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_new_offers,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="check_offers",
        replace_existing=True
    )
    scheduler.start()
    
    logger.info("Бот запущен!")
    logger.info(f"Проверка каждые {CHECK_INTERVAL_MINUTES} минут")
    
    # Первичная проверка при запуске
    await send_new_offers()
    
    # Запуск polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
