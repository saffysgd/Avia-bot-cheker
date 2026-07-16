import asyncio
import logging
import json
from datetime import datetime, timedelta
from calendar import monthrange
from typing import Optional, Tuple, List, Set

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==================== НАСТРОЙКИ ====================

TELEGRAM_BOT_TOKEN = "8823207989:AAEA4dw8sDApbf3T438GnyHRSH9f7-B_BCE"
AVIASALES_API_TOKEN = "19311b34c815711c2a8b70f2f3dbffa0"
ORIGIN_CITY = "OVB"
TARGET_CHAT_ID = -1003873649064
CHECK_INTERVAL_MINUTES = 30
MIN_DISCOUNT_PERCENT = 20
SEEN_OFFERS_FILE = "seen_offers.json"

# === ОГРАНИЧЕНИЕ ПО ДАТАМ ===
SEARCH_MONTHS_AHEAD = 1  # Текущий + 1 месяц

# === ФИЛЬТР ПО СТРАНАМ ===
# Только безвизовые для россиян (по состоянию на 2026 год)
ALLOWED_COUNTRIES = {
    # === ЕВРОПА (безвизовые) ===
    "AZ",  # Азербайджан
    "AM",  # Армения
    "BY",  # Беларусь
    "GE",  # Грузия
    "KZ",  # Казахстан
    "KG",  # Кыргызстан
    "MD",  # Молдова
    "TJ",  # Таджикистан
    "TM",  # Туркменистан
    "UZ",  # Узбекистан
    "UA",  # Украина (по внутреннему паспорту РФ)
    "RS",  # Сербия
    "BA",  # Босния и Герцеговина
    "ME",  # Черногория
    "MK",  # Северная Македония
    "AL",  # Албания
    "TR",  # Турция (электронная виза, но условно включаем)
    
    # === АЗИЯ (безвизовые) ===
    "TH",  # Таиланд
    "VN",  # Вьетнам
    "KH",  # Камбоджа
    "LA",  # Лаос
    "MY",  # Малайзия
    "SG",  # Сингапур
    "ID",  # Индонезия
    "PH",  # Филиппины
    "KR",  # Южная Корея
    "JP",  # Япония (электронная виза, но включаем)
    "CN",  # Китай (групповая безвиза, отдельные города)
    "MN",  # Монголия
    "AE",  # ОАЭ
    "QA",  # Катар
    "OM",  # Оман
    "BH",  # Бахрейн
    "KW",  # Кувейт
    "JO",  # Иордания
    "LB",  # Ливан
    "IR",  # Иран
    "PK",  # Пакистан
    "BD",  # Бангладеш
    "LK",  # Шри-Ланка
    "MV",  # Мальдивы
    "NP",  # Непал
    "BT",  # Бутан
    "MM",  # Мьянма
    "BN",  # Бруней
    "TL",  # Восточный Тимор
    
    # === ДРУГИЕ ===
    "EG",  # Египет
    "MA",  # Марокко
    "TN",  # Тунис
    "ZA",  # ЮАР
    "CU",  # Куба
    "DO",  # Доминикана
    "VE",  # Венесуэла
    "BR",  # Бразилия
    "AR",  # Аргентина
    "CL",  # Чили
    "PE",  # Перу
    "EC",  # Эквадор
    "BO",  # Боливия
    "CO",  # Колумбия
    "UY",  # Уругвай
    "PY",  # Парагвай
    "SR",  # Суринам
    "GY",  # Гайана
    "FK",  # Фолкленды
}

# Страны, которые точно НЕ показываем (визовые ЕС, США, Канада и т.д.)
BLOCKED_COUNTRIES = {
    "US", "CA", "GB", "FR", "DE", "IT", "ES", "PT", "NL", "BE", "AT", "CH",
    "SE", "NO", "DK", "FI", "IS", "IE", "PL", "CZ", "SK", "HU", "RO", "BG",
    "HR", "SI", "LT", "LV", "EE", "LU", "MT", "CY", "GR", "LI", "MC", "SM",
    "AD", "VA", "AU", "NZ", "IL", "IN",
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
    today = datetime.now()
    target_year = today.year
    target_month = today.month + SEARCH_MONTHS_AHEAD
    
    while target_month > 12:
        target_month -= 12
        target_year += 1
    
    last_day = monthrange(target_year, target_month)[1]
    return datetime(target_year, target_month, last_day, 23, 59, 59)

# ==================== API: ПЕРЕВОД IATA → СТРАНА ====================

# Кэш IATA кодов → страна
IATA_TO_COUNTRY_CACHE = {}

async def get_country_by_iata(iata_code: str) -> Optional[str]:
    """Определяет страну по IATA коду аэропорта."""
    if not iata_code or len(iata_code) != 3:
        return None
    
    if iata_code in IATA_TO_COUNTRY_CACHE:
        return IATA_TO_COUNTRY_CACHE[iata_code]
    
    # Используем API Travelpayouts для получения данных об аэропорте
    url = "https://api.travelpayouts.com/data/en/airports.json"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    airports = await resp.json()
                    for airport in airports:
                        if airport.get("code") == iata_code.upper():
                            country_code = airport.get("country_code")
                            IATA_TO_COUNTRY_CACHE[iata_code] = country_code
                            return country_code
    except Exception as e:
        logger.warning(f"Не удалось определить страну для {iata_code}: {e}")
    
    # Fallback: популярные аэропорты
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
        "MLE": "MV",
        "ALA": "KZ", "NQZ": "KZ",
        "GYD": "AZ",
        "EVN": "AM",
        "TBS": "GE",
    }
    
    country = fallback.get(iata_code.upper())
    IATA_TO_COUNTRY_CACHE[iata_code] = country
    return country

def is_allowed_destination(iata_code: str) -> bool:
    """Проверяет, разрешена ли страна назначения."""
    if not iata_code:
        return False
    
    # Проверяем по кэшу
    country = IATA_TO_COUNTRY_CACHE.get(iata_code.upper())
    if country:
        return country.upper() in ALLOWED_COUNTRIES
    
    # Если страна не определена — пропускаем (лучше перебдеть)
    return False

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
                
                # === ЗАГРУЖАЕМ ДАННЫЕ ОБ АЭРОПОРТАХ ===
                await load_airports_data(session)
                
                # === ФИЛЬТР ПО ДАТАМ И СТРАНАМ ===
                filtered_offers = []
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                max_date = get_max_search_date()
                
                allowed_count = 0
                blocked_count = 0
                date_filtered = 0
                
                for offer in offers:
                    dest_iata = offer.get("destination", "")
                    dep_raw = offer.get("departure_at", "")
                    
                    # Фильтр по дате
                    if dep_raw:
                        try:
                            dep_dt = datetime.fromisoformat(dep_raw.replace("Z", "+00:00"))
                            if dep_dt < today or dep_dt > max_date:
                                date_filtered += 1
                                continue
                        except:
                            pass
                    
                    # Фильтр по стране
                    country = await get_country_by_iata(dest_iata)
                    
                    if country and country.upper() in ALLOWED_COUNTRIES:
                        filtered_offers.append(offer)
                        allowed_count += 1
                    else:
                        blocked_count += 1
                        logger.debug(f"Заблокировано: {dest_iata} → страна {country}")
                
                logger.info(
                    f"API: {len(offers)} предложений | "
                    f"По дате отфильтровано: {date_filtered} | "
                    f"Разрешено: {allowed_count} | "
                    f"Заблокировано: {blocked_count}"
                )
                
                return filtered_offers
                
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе к API")
            return []
        except Exception as e:
            logger.error(f"Ошибка при запросе к API: {e}")
            return []

async def load_airports_data(session: aiohttp.ClientSession):
    """Загружает данные об аэропортах для кэширования стран."""
    global IATA_TO_COUNTRY_CACHE
    
    if IATA_TO_COUNTRY_CACHE:
        return  # Уже загружено
    
    url = "https://api.travelpayouts.com/data/ru/airports.json"
    
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                airports = await resp.json()
                for airport in airports:
                    code = airport.get("code")
                    country = airport.get("country_code")
                    if code and country:
                        IATA_TO_COUNTRY_CACHE[code] = country
                logger.info(f"Загружено {len(IATA_TO_COUNTRY_CACHE)} аэропортов")
    except Exception as e:
        logger.warning(f"Не удалось загрузить данные аэропортов: {e}")

# ==================== ФОРМАТИРОВАНИЕ ====================

def format_offer_message(offer: dict) -> Tuple[str, str]:
    origin_name = offer.get("origin_name", offer.get("origin", "Неизвестно"))
    destination_name = offer.get("destination_name", offer.get("destination", "Неизвестно"))
    dest_iata = offer.get("destination", "")
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
    
    # Флаг страны (если известен)
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

def get_country_flag(country_code: str) -> str:
    """Возвращает флаг страны по коду."""
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

# ==================== ОТПРАВКА ====================

async def send_new_offers():
    global seen_offers
    
    logger.info("Проверяю новые спецпредложения...")
    offers = await fetch_special_offers()
    
    if not offers:
        logger.info("Новых предложений не найдено")
        return
    
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
            logger.info(f"Отправлено предложение: {offer_id}")
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
    
    if new_count > 0:
        save_seen_offers(seen_offers)
        logger.info(f"Отправлено {new_count} новых предложений")
    else:
        logger.info("Новых уникальных предложений не найдено")

# ==================== КОМАНДЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    max_date = get_max_search_date()
    countries_count = len(ALLOWED_COUNTRIES)
    await message.answer(
        f"🤖 <b>Бот спецпредложений Aviasales</b>\n\n"
        f"📍 Город вылета: Новосибирск (OVB)\n"
        f"📅 Ищем билеты до: <b>{max_date.strftime('%d %B %Y')}</b>\n"
        f"🌍 Стран: <b>{countries_count}</b> безвизовых направлений\n\n"
        f"Показываем только:\n"
        f"• 🇪🇺 Европа (СНГ, Балканы, Турция)\n"
        f"• 🌏 Азия (ЮВА, Ближний Восток, Средняя Азия)\n\n"
        f"<b>Команды:</b>\n"
        f"/check — проверить предложения\n"
        f"/countries — список стран\n"
        f"/status — статус\n"
        f"/help — справка",
        parse_mode="HTML"
    )

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔍 Проверяю предложения...")
    await send_new_offers()
    await message.answer("✅ Проверка завершена!")

@dp.message(Command("countries"))
async def cmd_countries(message: types.Message):
    """Показывает список разрешённых стран."""
    # Группируем по регионам
    europe = []
    asia = []
    other = []
    
    country_names = {
        "AZ": "🇦🇿 Азербайджан", "AM": "🇦🇲 Армения", "BY": "🇧🇾 Беларусь",
        "GE": "🇬🇪 Грузия", "KZ": "🇰🇿 Казахстан", "KG": "🇰🇬 Кыргызстан",
        "MD": "🇲🇩 Молдова", "RU": "🇷🇺 Россия", "TJ": "🇹🇯 Таджикистан",
        "TM": "🇹🇲 Туркменистан", "UZ": "🇺🇿 Узбекистан", "UA": "🇺🇦 Украина",
        "RS": "🇷🇸 Сербия", "BA": "🇧🇦 Босния и Герцеговина", "ME": "🇲🇪 Черногория",
        "MK": "🇲🇰 Северная Македония", "AL": "🇦🇱 Албания", "TR": "🇹🇷 Турция",
        "TH": "🇹🇭 Таиланд", "VN": "🇻🇳 Вьетнам", "KH": "🇰🇭 Камбоджа",
        "LA": "🇱🇦 Лаос", "MY": "🇲🇾 Малайзия", "SG": "🇸🇬 Сингапур",
        "ID": "🇮🇩 Индонезия", "PH": "🇵🇭 Филиппины", "KR": "🇰🇷 Южная Корея",
        "JP": "🇯🇵 Япония", "CN": "🇨🇳 Китай", "MN": "🇲🇳 Монголия",
        "AE": "🇦🇪 ОАЭ", "QA": "🇶🇦 Катар", "OM": "🇴🇲 Оман",
        "BH": "🇧🇭 Бахрейн", "KW": "🇰🇼 Кувейт", "JO": "🇯🇴 Иордания",
        "LB": "🇱🇧 Ливан", "IR": "🇮🇷 Иран", "PK": "🇵🇰 Пакистан",
        "BD": "🇧🇩 Бангладеш", "LK": "🇱🇰 Шри-Ланка", "MV": "🇲🇻 Мальдивы",
        "NP": "🇳🇵 Непал", "BT": "🇧🇹 Бутан", "MM": "🇲🇲 Мьянма",
        "BN": "🇧🇳 Бруней", "TL": "🇹🇱 Восточный Тимор",
        "EG": "🇪🇬 Египет", "MA": "🇲🇦 Марокко", "TN": "🇹🇳 Тунис",
        "ZA": "🇿🇦 ЮАР", "CU": "🇨🇺 Куба", "DO": "🇩🇴 Доминикана",
        "VE": "🇻🇪 Венесуэла", "BR": "🇧🇷 Бразилия", "AR": "🇦🇷 Аргентина",
        "CL": "🇨🇱 Чили", "PE": "🇵🇪 Перу", "EC": "🇪🇨 Эквадор",
        "BO": "🇧🇴 Боливия", "UY": "🇺🇾 Уругвай", "PY": "🇵🇾 Парагвай",
    }
    
    for code in sorted(set(ALLOWED_COUNTRIES)):
        name = country_names.get(code, f"🏳️ {code}")
        if code in ["AZ", "AM", "BY", "GE", "KZ", "KG", "MD", "RU", "TJ", "TM", "UZ", "UA",
                    "RS", "BA", "ME", "MK", "AL", "TR"]:
            europe.append(name)
        elif code in ["EG", "MA", "TN", "ZA", "CU", "DO", "VE", "BR", "AR", "CL", "PE",
                      "EC", "BO", "UY", "PY", "SR", "GY", "FK"]:
            other.append(name)
        else:
            asia.append(name)
    
    text = "🌍 <b>Безвизовые направления для россиян</b>\n\n"
    text += f"🇪🇺 <b>Европа и СНГ ({len(europe)}):</b>\n" + "\n".join(europe) + "\n\n"
    text += f"🌏 <b>Азия ({len(asia)}):</b>\n" + "\n".join(asia) + "\n\n"
    if other:
        text += f"🌎 <b>Другие ({len(other)}):</b>\n" + "\n".join(other)
    
    # Разбиваем на части если слишком длинно
    if len(text) > 4000:
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                parts.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            parts.append(current)
        
        for part in parts:
            await message.answer(part, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    max_date = get_max_search_date()
    await message.answer(
        f"📊 <b>Статус бота</b>\n\n"
        f"🛫 Город вылета: Новосибирск (OVB)\n"
        f"📅 Диапазон поиска: до {max_date.strftime('%d %B %Y')}\n"
        f"🌍 Безвизовых стран: {len(ALLOWED_COUNTRIES)}\n"
        f"⏱ Интервал проверки: каждые {CHECK_INTERVAL_MINUTES} мин\n"
        f"📨 Отправлено предложений: {len(seen_offers)}\n"
        f"✅ Бот работает",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Бот показывает только безвизовые направления для россиян:\n"
        "• 🇪🇺 Европа: СНГ, Балканы, Турция, Грузия, Армения\n"
        "• 🌏 Азия: Таиланд, Вьетнам, ОАЭ, Корея, Япония и др.\n\n"
        "Исключены: 🇪🇺 ЕС (шенген), 🇺🇸 США, 🇨🇦 Канада и др.\n\n"
        "<b>Команды:</b>\n"
        "/check — проверить сейчас\n"
        "/countries — список стран\n"
        "/status — статус\n"
        "/start — главное меню",
        parse_mode="HTML"
    )

# ==================== ЗАПУСК ====================

async def main():
    max_date = get_max_search_date()
    logger.info(f"Максимальная дата поиска: {max_date.strftime('%d.%m.%Y')}")
    logger.info(f"Разрешено стран: {len(ALLOWED_COUNTRIES)}")
    
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
    
    await send_new_offers()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())