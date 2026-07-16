import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === КОНФИГУРАЦИЯ ===
TP_API_TOKEN = "19311b34c815711c2a8b70f2f3dbffa0"
TP_MARKER = "322810"
BOT_TOKEN = "8823207989:AAEA4dw8sDApbf3T438GnyHRSH9f7-B_BCE"
CHANNEL_ID = "-1003873649064"
CHECK_INTERVAL_HOURS = 3
SENT_IDS_FILE = Path("sent_ids.json")
MAX_SENT_IDS = 500

CITY_INFO = {
    # === ЕВРОПА (Без визы) ===
    "IST": ("Стамбул", "TR", "EU"),
    "SAW": ("Стамбул (Сабиха)", "TR", "EU"),
    "AYT": ("Анталья", "TR", "EU"),
    "BJV": ("Бодрум", "TR", "EU"),
    "DLM": ("Даламан", "TR", "EU"),
    "ESB": ("Анкара", "TR", "EU"),
    "MNS": ("Минск", "BY", "EU"),
    "BEG": ("Белград", "RS", "EU"),
    "TIV": ("Тиват", "ME", "EU"),
    "KIV": ("Кишинёв", "MD", "EU"),

    # === АЗИЯ (Без визы) ===
    "DXB": ("Дубай", "AE", "ASIA"),
    "AUH": ("Абу-Даби", "AE", "ASIA"),
    "SHJ": ("Шарджа", "AE", "ASIA"),
    "DWC": ("Дубай (Аль Мактум)", "AE", "ASIA"),
    "BKK": ("Бангкок", "TH", "ASIA"),
    "DMK": ("Бангкок (Дон Мыанг)", "TH", "ASIA"),
    "HKT": ("Пхукет", "TH", "ASIA"),
    "CNX": ("Чиангмай", "TH", "ASIA"),
    "USM": ("Самуи", "TH", "ASIA"),
    "SGN": ("Хошимин", "VN", "ASIA"),
    "HAN": ("Ханой", "VN", "ASIA"),
    "DAD": ("Дананг", "VN", "ASIA"),
    "KUL": ("Куала-Лумпур", "MY", "ASIA"),
    "LGK": ("Лангкави", "MY", "ASIA"),
    "PEN": ("Пенанг", "MY", "ASIA"),
    "SIN": ("Сингапур", "SG", "ASIA"),
    "DPS": ("Бали (Денпасар)", "ID", "ASIA"),
    "CGK": ("Джакарта", "ID", "ASIA"),
    "MNL": ("Манила", "PH", "ASIA"),
    "CEB": ("Себу", "PH", "ASIA"),
    "PEK": ("Пекин", "CN", "ASIA"),
    "PKX": ("Пекин (Дасин)", "CN", "ASIA"),
    "SHA": ("Шанхай (Хунцяо)", "CN", "ASIA"),
    "PVG": ("Шанхай (Пудун)", "CN", "ASIA"),
    "MLE": ("Мале (Мальдивы)", "MV", "ASIA"),
    "GYD": ("Баку", "AZ", "ASIA"),
    "EVN": ("Ереван", "AM", "ASIA"),
    "TBS": ("Тбилиси", "GE", "ASIA"),
    "KUT": ("Кутаиси", "GE", "ASIA"),
    "BUS": ("Батуми", "GE", "ASIA"),
    "ALA": ("Алматы", "KZ", "ASIA"),
    "NQZ": ("Астана", "KZ", "ASIA"),
    "CIT": ("Шымкент", "KZ", "ASIA"),
    "TAS": ("Ташкент", "UZ", "ASIA"),
    "SKD": ("Самарканд", "UZ", "ASIA"),
    "BHK": ("Бухара", "UZ", "ASIA"),
    "FRU": ("Бишкек", "KG", "ASIA"),
    "OSS": ("Ош", "KG", "ASIA"),

    # === РОССИЯ (Внутренние рейсы) ===
    "BAX": ("Барнаул", "RU", "RU"),
    "KEJ": ("Кемерово", "RU", "RU"),
    "TOF": ("Томск", "RU", "RU"),
    "NOZ": ("Новокузнецк", "RU", "RU"),
    "MOW": ("Москва", "RU", "RU"),
    "LED": ("Санкт-Петербург", "RU", "RU"),
    "AER": ("Сочи", "RU", "RU"),
    "MRV": ("Минеральные Воды", "RU", "RU"),
    "KRR": ("Краснодар", "RU", "RU"),
    "SVX": ("Екатеринбург", "RU", "RU"),
    "KZN": ("Казань", "RU", "RU"),
    "VVO": ("Владивосток", "RU", "RU"),
    "KHV": ("Хабаровск", "RU", "RU"),
    "IKT": ("Иркутск", "RU", "RU"),
    "PKC": ("Петропавловск-Камчатский", "RU", "RU"),
    "UUS": ("Южно-Сахалинск", "RU", "RU"),
    "CEK": ("Челябинск", "RU", "RU"),
    "TJM": ("Тюмень", "RU", "RU"),
    "SUR": ("Сургут", "RU", "RU"),
    "PEE": ("Пермь", "RU", "RU"),
    "SGC": ("Сургут", "RU", "RU"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)


# === РАБОТА С СОСТОЯНИЕМ ===
def load_sent_ids() -> set:
    if SENT_IDS_FILE.exists():
        try:
            return set(json.loads(SENT_IDS_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_sent_ids(ids: set):
    trimmed = list(ids)[-MAX_SENT_IDS:]
    SENT_IDS_FILE.write_text(json.dumps(trimmed), encoding="utf-8")


# === ЗАПРОС К API (DATA API + FALLBACK) ===
async def _fetch_combined_fallback() -> list[dict]:
    """Запасной комбинированный источник, если special-offers не работает"""
    all_tickets = []
    seen_keys = set()

    async with httpx.AsyncClient(timeout=30) as client:
        # Источник 1: /latest
        try:
            resp = await client.get(
                "https://api.travelpayouts.com/v2/prices/latest",
                params={
                    "token": TP_API_TOKEN,
                    "marker": TP_MARKER,
                    "currency": "RUB",
                    "limit": 100,
                    "show_to_affiliates": "true",
                }
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                ovb_latest = [t for t in data if t.get("origin") == "OVB"]
                logger.info(f"[FALLBACK /latest] {len(ovb_latest)} из OVB")
                for t in ovb_latest:
                    key = f"{t.get('destination')}_{t.get('depart_date', '')[:10]}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_tickets.append(t)
        except Exception as e:
            logger.error(f"[FALLBACK /latest] Ошибка: {e}")

        # Источник 2: /prices_for_dates
        now = datetime.now()
        months = [
            now.strftime("%Y-%m"),
            (now.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
        ]
        for month in months:
            try:
                resp = await client.get(
                    "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                    params={
                        "token": TP_API_TOKEN,
                        "origin": "OVB",
                        "currency": "RUB",
                        "limit": 50,
                        "sorting": "price",
                        "one_way": "true",
                        "departure_at": month,
                        "direct": "false",
                    }
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    logger.info(f"[FALLBACK prices_for_dates {month}] {len(data)}")
                    for t in data:
                        key = f"{t.get('destination')}_{t.get('depart_date', '')[:10]}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_tickets.append(t)
            except Exception as e:
                logger.error(f"[FALLBACK prices_for_dates {month}] Ошибка: {e}")

    logger.info(f"[FALLBACK] Всего уникальных: {len(all_tickets)}")
    filtered = [t for t in all_tickets if t.get("destination", "") in CITY_INFO]
    logger.info(f"[FALLBACK] После фильтра: {len(filtered)}")
    return filtered


async def fetch_hot_offers() -> list[dict]:
    """
    Основной источник: Special Offers Data API.
    Токен передаётся через заголовок X-Access-Token согласно документации.
    """
    url = "https://api.travelpayouts.com/v2/prices/special-offers"
    params = {
        "currency": "RUB",
        "limit": 100,
        "origin": "OVB",
    }
    headers = {
        "X-Access-Token": TP_API_TOKEN,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=headers)

        if resp.status_code == 404 or resp.status_code == 403:
            logger.warning(
                f"[special-offers] Вернул {resp.status_code}, "
                f"переключаемся на комбинированный поиск"
            )
            return await _fetch_combined_fallback()

        resp.raise_for_status()
        data = resp.json()

    tickets = data.get("data", [])
    logger.info(f"[special-offers] Получено {len(tickets)} спецпредложений")

    filtered = [t for t in tickets if t.get("destination", "") in CITY_INFO]
    logger.info(f"[special-offers] После фильтра (Безвизовые): {len(filtered)}")

    # Если спецпредложений по фильтру нет — дополняем fallback-источниками
    if not filtered:
        logger.info("[special-offers] Нет совпадений по фильтру, добавляем fallback")
        fallback = await _fetch_combined_fallback()
        # Объединяем без дубликатов
        existing_keys = {
            f"{t.get('destination')}_{t.get('depart_date', '')[:10]}"
            for t in filtered
        }
        for t in fallback:
            key = f"{t.get('destination')}_{t.get('depart_date', '')[:10]}"
            if key not in existing_keys:
                filtered.append(t)
        logger.info(f"[combined] Итого после объединения: {len(filtered)}")

    return filtered


# === ФОРМАТИРОВАНИЕ ===
def format_offer(offer: dict) -> str | None:
    origin_code = offer.get("origin", "")
    dest_code = offer.get("destination", "")
    price = offer.get("price", 0)
    depart_date_raw = offer.get("depart_date", "")

    if not depart_date_raw:
        return None

    dest_info = CITY_INFO.get(dest_code)
    if not dest_info:
        return None

    dest_name_ru = dest_info[0]
    region = dest_info[2]

    if region == "EU":
        region_badge = "🇪🇺 Европа"
    elif region == "ASIA":
        region_badge = "🌏 Азия"
    else:
        region_badge = "🇷🇺 Россия"

    depart_date = depart_date_raw[:10]

    transfers = offer.get("transfers", 0)
    if transfers == 0:
        transfer_text = "✈️ Прямой"
    elif transfers == 1:
        transfer_text = "🔄 1 пересадка"
    else:
        transfer_text = f"🔄 {transfers} пересадки"

    link = (
        f"https://www.aviasales.ru/search/"
        f"{origin_code}-{dest_code}/{depart_date}/1?marker={TP_MARKER}"
    )

    return (
        f"🔥 <b>Новосибирск → {dest_name_ru}</b>\n"
        f"{region_badge} • 🛂 Без визы\n"
        f"💰 {price:,} ₽\n"
        f"📅 {depart_date}\n"
        f"{transfer_text}\n"
        f'<a href="{link}">Найти билет</a>'
    )


# === ОТПРАВКА ОДНИМ СООБЩЕНИЕМ ===
async def check_and_post():
    logger.info("Проверка новых горящих предложений...")
    sent_ids = load_sent_ids()

    try:
        offers = await fetch_hot_offers()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP ошибка API: {e.response.status_code} — {e.response.text}")
        return
    except Exception as e:
        logger.error(f"Ошибка запроса к API: {e}")
        return

    new_offers_texts = []
    new_ids = set()

    for offer in offers:
        offer_id = (
            f"{offer.get('origin')}_"
            f"{offer.get('destination')}_"
            f"{offer.get('price')}_"
            f"{offer.get('depart_date', '')[:10]}_"
            f"{offer.get('transfers', 0)}"
        )

        if offer_id in sent_ids:
            continue

        text = format_offer(offer)
        if text is None:
            continue

        new_offers_texts.append(text)
        new_ids.add(offer_id)

    if not new_offers_texts:
        logger.info("Новых горящих предложений по фильтру нет")
        return

    MAX_MSG_LEN = 4000
    separator = "\n\n━━━━━━━━━━━━━━━\n\n"
    chunks = []
    current_chunk = ""

    for item_text in new_offers_texts:
        if len(current_chunk) + len(separator) + len(item_text) > MAX_MSG_LEN:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = item_text
        else:
            if current_chunk:
                current_chunk += separator + item_text
            else:
                current_chunk = item_text

    if current_chunk:
        chunks.append(current_chunk)

    for i, chunk in enumerate(chunks):
        header = ""
        if len(chunks) > 1:
            header = (
                f"<b>🔥 Горящие билеты из Новосибирска "
                f"(часть {i + 1}/{len(chunks)})</b>\n\n"
            )
        else:
            header = "<b>🔥 Горящие билеты из Новосибирска</b>\n\n"

        try:
            await bot.send_message(
                CHANNEL_ID,
                header + chunk,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка отправки чанка {i + 1}: {e}")

    sent_ids.update(new_ids)
    save_sent_ids(sent_ids)
    msg_word = "сообщении" if len(chunks) == 1 else "сообщениях"
    logger.info(
        f"Отправлено {len(new_offers_texts)} предложений в {len(chunks)} {msg_word}"
    )


# === ТОЧКА ВХОДА ===
async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_post,
        trigger="interval",
        hours=CHECK_INTERVAL_HOURS,
        next_run_time=None
    )

    await check_and_post()

    scheduler.start()
    logger.info(
        f"Бот запущен. Интервал: {CHECK_INTERVAL_HOURS} ч. "
        f"Режим: Data API (X-Access-Token) + Fallback"
    )

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
