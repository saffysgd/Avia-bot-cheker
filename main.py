import asyncio
import logging
import json
from datetime import datetime, timedelta, timezone
from calendar import monthrange
from typing import Optional, Tuple, List, Set

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==================== НАСТРОЙКИ ====================

TELEGRAM_BOT_TOKEN = "8823207989:AAEA4dw8sDApbf3T438GnyHRSH9f7-B_BCE"
AVIASALES_API_TOKEN = "19311b34c815711c2a8b70f2f3dbffa0"
ORIGIN_CITY = "OVB"
TARGET_CHAT_ID = -1003873649064
CHECK_INTERVAL_MINUTES = 30
SEEN_OFFERS_FILE = "seen_offers.json"

# === ОГРАНИЧЕНИЕ ПО ДАТАМ ===
SEARCH_MONTHS_AHEAD = 1  # Текущий + 1 месяц (до 31 августа 2026)

# === ФИЛЬТР ПО СТРАНАМ ===
ALLOWED_COUNTRIES = {
    "AZ", "AM", "BY", "GE", "KZ", "KG", "MD", "RU", "TJ", "TM", "UZ", "UA",
    "RS", "BA", "ME", "MK", "AL", "TR",
    "TH", "VN", "KH", "LA", "MY", "SG", "ID", "PH", "KR", "JP", "CN", "MN",
    "AE", "QA", "OM", "BH", "KW", "JO", "LB", "IR", "PK", "BD", "LK", "MV",
    "NP", "BT", "MM", "BN", "TL",
    "EG", "MA", "TN", "ZA", "CU", "DO", "VE", "BR", "AR", "CL", "PE", "EC",
    "BO", "UY", "PY", "SR", "GY", "FK",
}

# ==================== ЛОГИ ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== БОТ ====================

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ==================== ХРАНИЛИЩЕ ====================

def load_seen_offers() -> set:
    try:
        with open(SEEN_OFFERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_offers(seen: set):
    with open(SEEN_OFFERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

seen_offers = load_seen_offers()

# ==================== ОГРАНИЧЕНИЕ ДАТ ====================

def get_max_search_date() -> datetime:
    """Возвращает максимальную дату поиска в UTC."""
    today = datetime.now(timezone.utc)
    target_year = today.year
    target_month = today.month + SEARCH_MONTHS_AHEAD
    
    while target_month > 12:
        target_month -= 12
        target_year += 1
    
    last_day = monthrange(target_year, target_month)[1]
    return datetime(target_year, target_month, last_day, 23, 59, 59, tzinfo=timezone.utc)

def parse_api_date(date_str: str) -> Optional[datetime]:
    """Парсит дату из API (форматы: 2027-01-07T13:20:00Z или 2027-01-07T13:20:00+00:00)."""
    if not date_str:
        return None
    
    try:
        # Заменяем Z на +00:00 для совместимости с fromisoformat
        normalized = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception as e:
        logger.debug(f"Не удалось распарсить дату '{date_str}': {e}")
        return None

def is_date_in_range(date_str: str) -> bool:
    """Проверяет, что дата в пределах текущего месяца + SEARCH_MONTHS_AHEAD."""
    dt = parse_api_date(date_str)
    if not dt:
        return False
    
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    max_date = get_max_search_date()
    
    result = today <= dt <= max_date
    logger.debug(f"Дата {date_str} -> {dt} | today={today} | max={max_date} | in_range={result}")
    return result

# ==================== API: АЭРОПОРТЫ ====================

IATA_TO_COUNTRY_CACHE = {}
AIRPORTS_LOADED = False

async def load_airports_data(session: Optional[aiohttp.ClientSession] = None):
    global IATA_TO_COUNTRY_CACHE, AIRPORTS_LOADED
    
    if AIRPORTS_LOADED and IATA_TO_COUNTRY_CACHE:
        return
    
    url = "https://api.travelpayouts.com/data/ru/airports.json"
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                airports = await resp.json()
                for airport in airports:
                    code = airport.get("code")
                    country = airport.get("country_code")
                    if code and country:
                        IATA_TO_COUNTRY_CACHE[code] = country
                AIRPORTS_LOADED = True
                logger.info(f"Загружено {len(IATA_TO_COUNTRY_CACHE)} аэропортов")
    except Exception as e:
        logger.warning(f"Не удалось загрузить данные аэропортов: {e}")
    finally:
        if close_session:
            await session.close()

async def get_country_by_iata(iata_code: str) -> Optional[str]:
    if not iata_code or len(iata_code) != 3:
        return None
    
    iata_upper = iata_code.upper()
    
    if iata_upper in IATA_TO_COUNTRY_CACHE:
        return IATA_TO_COUNTRY_CACHE[iata_upper]
    
    fallback = {
        "BKK": "TH", "HKT": "TH", "CNX": "TH", "KBV": "TH", "USM": "TH",
        "SGN": "VN", "HAN": "VN", "DAD": "VN", "NHA": "VN",
        "DPS": "ID", "CGK": "ID",
        "KUL": "MY", "PEN": "MY", "LGK": "MY",
        "SIN": "SG",
        "MNL": "PH", "CEB": "PH",
        "ICN": "KR", "PUS": "KR",
        "NRT": "JP", "HND": "JP", "KIX": "JP", "FUK": "JP",
        "DXB": "AE", "AUH": "AE", "SHJ": "AE",
        "DOH": "QA",
        "MCT": "OM",
        "GYD": "AZ",
        "EVN": "AM",
        "TBS": "GE",
        "ALA": "KZ", "NQZ": "KZ", "GUW": "KZ",
        "FRU": "KG",
        "TAS": "UZ",
        "DYU": "TJ",
        "ASB": "TM",
        "KIV": "MD",
        "BEG": "RS", "INI": "RS",
        "TGD": "ME",
        "TIA": "AL",
        "SKP": "MK",
        "SJJ": "BA",
        "IST": "TR", "SAW": "TR", "AYT": "TR", "ADB": "TR",
        "CAI": "EG", "HRG": "EG", "SSH": "EG",
        "CMN": "MA", "RAK": "MA",
        "TUN": "TN",
        "JNB": "ZA", "CPT": "ZA",
        "HAV": "CU",
        "PUJ": "DO", "SDQ": "DO",
        "MLE": "MV",
        "CMB": "LK",
        "KTM": "NP",
        "PBH": "BT",
        "RGN": "MM",
        "BWN": "BN",
        "DIL": "TL",
        "SKD": "UZ",  # Самарканд
    }
    
    country = fallback.get(iata_upper)
    if country:
        IATA_TO_COUNTRY_CACHE[iata_upper] = country
    return country

# ==================== API AVIASALES ====================

async def fetch_special_offers() -> list:
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
        if not AIRPORTS_LOADED:
            await load_airports_data(session)
        
        try:
            async with session.get(url, params=params, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"API вернул статус {resp.status}")
                    return []
                
                data = await resp.json()
                
                if not data.get("success"):
                    logger.error(f"API ошибка: {data.get('error')}")
                    return []
                
                offers = data.get("data", [])
                
                # === ФИЛЬТР ПО ДАТАМ И СТРАНАМ ===
                filtered_offers = []
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                max_date = get_max_search_date()
                
                allowed_count = 0
                blocked_count = 0
                date_filtered = 0
                
                for offer in offers:
                    dest_iata = offer.get("destination", "")
                    dep_raw = offer.get("departure_at", "")
                    
                    # === ФИЛЬТР ПО ДАТЕ ===
                    dep_dt = parse_api_date(dep_raw)
                    if dep_dt:
                        if dep_dt < today or dep_dt > max_date:
                            date_filtered += 1
                            logger.info(f"❌ ОТФИЛЬТРОВАНО ПО ДАТЕ: {dest_iata} | {dep_raw} | {dep_dt.strftime('%d.%m.%Y')}")
                            continue
                        else:
                            logger.info(f"✅ ДАТА ОК: {dest_iata} | {dep_raw} | {dep_dt.strftime('%d.%m.%Y')}")
                    else:
                        logger.warning(f"⚠️ Не удалось распарсить дату: {dep_raw}")
                        continue  # Пропускаем если дату не распарсили
                    
                    # === ФИЛЬТР ПО СТРАНЕ ===
                    country = await get_country_by_iata(dest_iata)
                    
                    if country and country.upper() in ALLOWED_COUNTRIES:
                        filtered_offers.append(offer)
                        allowed_count += 1
                    else:
                        blocked_count += 1
                        logger.info(f"❌ ЗАБЛОКИРОВАНО ПО СТРАНЕ: {dest_iata} | страна {country}")
                
                logger.info(
                    f"=== ИТОГО === API: {len(offers)} | "
                    f"Отфильтровано по дате: {date_filtered} | "
                    f"Разрешено: {allowed_count} | "
                    f"Заблокировано по стране: {blocked_count} | "
                    f"Диапазон: {today.strftime('%d.%m.%Y')} - {max_date.strftime('%d.%m.%Y')}"
                )
                
                return filtered_offers
                
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе к API")
            return []
        except Exception as e:
            logger.error(f"Ошибка при запросе к API: {e}")
            return []

# ==================== ФОРМАТИРОВАНИЕ ====================

def get_country_flag(country_code: str) -> str:
    flags = {
        "TH": "🇹🇭", "VN": "🇻🇳", "ID": "🇮🇩", "MY": "🇲🇾", "SG": "🇸🇬",
        "PH": "🇵🇭", "KR": "🇰🇷", "JP": "🇯🇵", "CN": "🇨🇳", "MN": "🇲🇳",
        "AE": "🇦🇪", "QA": "🇶🇦", "OM": "🇴🇲", "BH": "🇧🇭", "KW": "🇰🇼",
        "JO": "🇯🇴", "LB": "🇱🇧", "IR": "🇮🇷", "PK": "🇵🇰", "BD": "🇧🇩",
        "LK": "🇱🇰", "MV": "🇲🇻", "NP": "🇳🇵", "BT": "🇧🇹", "MM": "🇲🇲",
        "BN": "🇧🇳", "TL": "🇹🇱", "KZ": "🇰🇿", "KG": "🇰🇬", "UZ": "🇺🇿",
        "TJ": "🇹🇯", "TM": "🇹🇲", "AZ": "🇦🇿", "AM": "🇦🇲", "GE": "🇬🇪",
        "MD": "🇲🇩", "RS": "🇷🇸", "BA": "🇧🇦", "ME": "🇲🇪", "MK": "🇲🇰",
        "AL": "🇦🇱", "TR": "🇹🇷", "EG": "🇪🇬", "MA": "🇲🇦", "TN": "🇹🇳",
        "ZA": "🇿🇦", "CU": "🇨🇺", "DO": "🇩🇴", "VE": "🇻🇪", "BR": "🇧🇷",
        "AR": "🇦🇷", "CL": "🇨🇱", "PE": "🇵🇪", "EC": "🇪🇨", "BO": "🇧🇴",
        "UY": "🇺🇾", "PY": "🇵🇾", "SR": "🇸🇷", "GY": "🇬🇾", "FK": "🇫🇰",
        "RU": "🇷🇺", "BY": "🇧🇾", "UA": "🇺🇦",
    }
    return flags.get(country_code.upper(), "🌍")

def format_offer_message(offer: dict) -> Tuple[str, str]:
    origin_name = offer.get("origin_name", offer.get("origin", "Неизвестно"))
    destination_name = offer.get("destination_name", offer.get("destination", "Неизвестно"))
    dest_iata = offer.get("destination", "")
    price = offer.get("price", 0)
    airline = offer.get("airline_title", offer.get("airline", "Неизвестно"))
    flight_number = offer.get("flight_number", "")
    
    departure_raw = offer.get("departure_at", "")
    try:
        dep_dt = parse_api_date(departure_raw)
        if dep_dt:
            departure_str = dep_dt.strftime("%d %B %Y, %H:%M")
        else:
            departure_str = departure_raw
    except:
        departure_str = departure_raw
    
    return_raw = offer.get("return_at", "")
    return_str = ""
    if return_raw:
        try:
            ret_dt = parse_api_date(return_raw)
            if ret_dt:
                return_str = f"\n🔙 <b>Обратно:</b> {ret_dt.strftime('%d %B %Y, %H:%M')}"
            else:
                return_str = f"\n🔙 <b>Обратно:</b> {return_raw}"
        except:
            return_str = f"\n🔙 <b>Обратно:</b> {return_raw}"
    
    duration = offer.get("duration", 0)
    duration_str = f"{duration // 60}ч {duration % 60}м" if duration else "Неизвестно"
    
    link_suffix = offer.get("link", "")
    search_link = f"https://www.aviasales.ru/search{link_suffix}" if link_suffix else "https://www.aviasales.ru"
    
    offer_id = offer.get("search_id", "") or offer.get("signature", "")
    
    country = IATA_TO_COUNTRY_CACHE.get(dest_iata.upper(), "")
    country_flag = get_country_flag(country)
    
    message = (
        f"✈️ <b>Горящее предложение!</b> {country_flag}\n\n"
        f"🛫 <b>{origin_name}</b> → <b>{destination_name}</b>\n"
        f"💰 <b>Цена:</b> {price:,} ₽\n"
        f"🏢 <b>Авиакомпания:</b> {airline} {flight_number}\n"
        f"📅 <b>Вылет:</b> {departure_str}{return_str}\n"
        f"⏱ <b>В пути:</b> {duration_str}\n\n"
        f"🔗 <a href='{search_link}'>Посмотреть на Aviasales</a>"
    )
    
    return message, offer_id

# ==================== ОТПРАВКА ====================

async def send_new_offers():
    global seen_offers
    
    logger.info("Проверяю новые спецпредложения...")
    offers = await fetch_special_offers()
    
    if not offers:
        logger.info("Новых предложений не найдено")
        return 0
    
    new_count = 0
    
    for offer in offers:
        message, offer_id = format_offer_message(offer)
        
        if offer_id in seen_offers:
            continue
        
        try:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            seen_offers.add(offer_id)
            new_count += 1
            logger.info(f"Отправлено: {offer_id}")
            await asyncio.sleep(1)
            
        except TelegramForbiddenError as e:
            logger.error(f"❌ Бот заблокирован в чате {TARGET_CHAT_ID}!")
            return new_count
            
        except TelegramBadRequest as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            if "chat not found" in str(e).lower():
                logger.error(f"❌ Чат {TARGET_CHAT_ID} не найден!")
            return new_count
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки: {e}")
    
    if new_count > 0:
        save_seen_offers(seen_offers)
        logger.info(f"Отправлено {new_count} новых предложений")
    else:
        logger.info("Новых уникальных предложений не найдено")
    
    return new_count

# ==================== КОМАНДЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    max_date = get_max_search_date()
    await message.answer(
        f"🤖 <b>Бот спецпредложений Aviasales</b>\n\n"
        f"📍 Город вылета: Новосибирск (OVB)\n"
        f"📅 Ищем билеты до: <b>{max_date.strftime('%d %B %Y')}</b>\n"
        f"🌍 Безвизовые направления для россиян\n\n"
        f"<b>Команды:</b>\n"
        f"/check — проверить предложения\n"
        f"/test — тест отправки в группу\n"
        f"/status — статус\n"
        f"/help — справка",
        parse_mode="HTML"
    )

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔍 Проверяю предложения...")
    count = await send_new_offers()
    if count == 0:
        await message.answer("📭 Новых предложений нет.")
    else:
        await message.answer(f"✅ Отправлено {count} предложений!")

@dp.message(Command("test"))
async def cmd_test(message: types.Message):
    await message.answer(f"🧪 Отправляю тест в чат {TARGET_CHAT_ID}...")
    
    try:
        test_msg = await bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="🧪 <b>Тестовое сообщение</b>\n\nЕсли ты видишь это — бот работает!",
            parse_mode="HTML"
        )
        await message.answer(f"✅ Тест отправлен! Message ID: {test_msg.message_id}")
        
    except TelegramForbiddenError:
        await message.answer(
            "❌ <b>Ошибка:</b> Бот заблокирован в группе!\n\n"
            "Решение:\n"
            "1. Добавь бота в группу\n"
            "2. Сделай бота <b>администратором</b>",
            parse_mode="HTML"
        )
        
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            await message.answer(
                f"❌ <b>Чат не найден!</b>\n\n"
                f"Текущий TARGET_CHAT_ID: <code>{TARGET_CHAT_ID}</code>\n\n"
                f"Как получить правильный ID:\n"
                f"1. Добавь бота в группу\n"
                f"2. Отправь любое сообщение\n"
                f"3. Перейди: <code>https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates</code>",
                parse_mode="HTML"
            )
        else:
            await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    max_date = get_max_search_date()
    today = datetime.now(timezone.utc)
    await message.answer(
        f"📊 <b>Статус</b>\n\n"
        f"🛫 Город: Новосибирск (OVB)\n"
        f"📅 Сегодня: {today.strftime('%d %B %Y')}\n"
        f"📅 До: {max_date.strftime('%d %B %Y')}\n"
        f"🌍 Стран: {len(set(ALLOWED_COUNTRIES))}\n"
        f"🎯 Чат ID: <code>{TARGET_CHAT_ID}</code>\n"
        f"⏱ Интервал: {CHECK_INTERVAL_MINUTES} мин\n"
        f"📨 Отправлено: {len(seen_offers)}",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Бот показывает только безвизовые направления:\n"
        "• 🇪🇺 СНГ, Балканы, Турция, Грузия, Армения\n"
        "• 🌏 Таиланд, Вьетнам, ОАЭ, Корея, Япония и др.\n\n"
        "❌ Исключены: ЕС (шенген), США, Канада\n\n"
        "<b>Команды:</b>\n"
        "/check — проверить сейчас\n"
        "/test — тест отправки\n"
        "/status — статус",
        parse_mode="HTML"
    )

# ==================== ЗАПУСК ====================

async def main():
    await load_airports_data()
    
    # Диагностика чата
    logger.info("=" * 60)
    logger.info("ДИАГНОСТИКА ЧАТА")
    logger.info(f"TARGET_CHAT_ID: {TARGET_CHAT_ID}")
    
    try:
        chat = await bot.get_chat(TARGET_CHAT_ID)
        logger.info(f"✅ Чат найден: {chat.title or chat.full_name}")
        logger.info(f"   Тип: {chat.type}")
        
        test = await bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="🤖 Бот запущен и готов к работе!",
            parse_mode="HTML"
        )
        logger.info(f"✅ Тест отправлен! Message ID: {test.message_id}")
        
    except TelegramForbiddenError:
        logger.error("❌ Бот НЕ имеет доступа к чату!")
        logger.error("   Решение: добавь бота в группу и сделай администратором")
        
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            logger.error("❌ Чат не найден!")
            logger.error(f"   Проверь TARGET_CHAT_ID: {TARGET_CHAT_ID}")
        else:
            logger.error(f"❌ Ошибка: {e}")
            
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка: {e}")
    
    logger.info("=" * 60)
    
    max_date = get_max_search_date()
    logger.info(f"Макс. дата: {max_date.strftime('%d.%m.%Y')}")
    logger.info(f"Стран: {len(set(ALLOWED_COUNTRIES))}")
    
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
    
    await send_new_offers()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())