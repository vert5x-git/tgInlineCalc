import asyncio
import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, UTC
from decimal import Decimal, getcontext, InvalidOperation
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

# --- Конфигурация ---
# ❗️ Вставьте сюда токен вашего бота, полученный от @BotFather
BOT_TOKEN = "4"
# ❗️ Вставьте сюда ваш Telegram User ID, чтобы иметь доступ к команде /stats
ADMIN_ID = 0  # Замените 0 на ваш ID

# Настройка точности для финансовых вычислений
getcontext().prec = 18

# Настройка логирования для отслеживания работы бота
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Файл для хранения статистики ---
STATS_FILE = Path("bot_stats.json")

# --- API Endpoints & URLs ---
CBR_API_URL = "https://www.cbr.ru/scripts/XML_daily.asp"  # API Центробанка РФ
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol={}"

# --- Словарь для сокращений ---
TICKER_MAP = {
    # Для CBR, эти ключи будут сопоставлены с CharCode в XML
    "ur": "USD",
    "cr": "CNY",
    "er": "EUR",
    # eu будет вычислен
    # Для Binance
    "bnc": "BTCUSDT",
    "eth": "ETHUSDT",
}


# --- Управление статистикой ---

def load_stats() -> dict:
    if not STATS_FILE.exists():
        return {"total_requests": 0, "daily_requests": {}, "users": {}}
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"total_requests": 0, "daily_requests": {}, "users": {}}


def save_stats(data: dict):
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except IOError as e:
        logging.error(f"Не удалось сохранить статистику: {e}")


async def update_stats(user_id: int):
    """Обновляет статистику, используя современный метод получения времени в UTC."""
    stats = load_stats()
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    stats['total_requests'] = stats.get('total_requests', 0) + 1
    daily_counts = stats.get('daily_requests', {})
    daily_counts[today] = daily_counts.get(today, 0) + 1
    stats['daily_requests'] = daily_counts
    stats['users'][str(user_id)] = datetime.now(UTC).isoformat()
    save_stats(stats)


# --- Логика получения данных ---

async def fetch_cbr_prices(session: aiohttp.ClientSession) -> dict[str, Decimal]:
    """Асинхронно получает и парсит курсы валют с API Центробанка РФ."""
    prices = {}
    try:
        async with session.get(CBR_API_URL) as response:
            response.raise_for_status()
            text = await response.text()
            root = ET.fromstring(text)

            valute_map = {"USD": "ur", "CNY": "cr", "EUR": "er"}

            for valute in root.findall('Valute'):
                char_code = valute.find('CharCode').text
                if char_code in valute_map:
                    value_str = valute.find('Value').text.replace(',', '.')
                    nominal = valute.find('Nominal').text
                    price = Decimal(value_str) / Decimal(nominal)
                    prices[valute_map[char_code]] = price
        return prices
    except Exception as e:
        logging.error(f"Ошибка при получении данных с CBR API: {e}")
        return {}


async def get_binance_price(session: aiohttp.ClientSession, ticker: str) -> Decimal | None:
    """Получает цену с Binance."""
    try:
        async with session.get(BINANCE_API_URL.format(ticker)) as response:
            response.raise_for_status()
            data = await response.json()
            return Decimal(data['price'])
    except Exception as e:
        logging.error(f"Ошибка при получении данных с Binance для {ticker}: {e}")
        return None


async def fetch_all_prices() -> dict[str, Decimal]:
    """
    Асинхронно запрашивает все необходимые курсы с CBR и Binance.
    """
    all_prices = {}
    async with aiohttp.ClientSession() as session:
        # Создаем задачи для всех источников
        cbr_task = fetch_cbr_prices(session)
        bnc_task = get_binance_price(session, TICKER_MAP['bnc'])
        eth_task = get_binance_price(session, TICKER_MAP['eth'])

        results = await asyncio.gather(cbr_task, bnc_task, eth_task)

        # Обрабатываем результаты
        cbr_prices, bnc_price, eth_price = results

        if cbr_prices:
            all_prices.update(cbr_prices)
        if bnc_price:
            all_prices['bnc'] = bnc_price
        if eth_price:
            all_prices['eth'] = eth_price

    # Вычисляем производные тикеры
    if 'er' in all_prices and 'ur' in all_prices:
        all_prices['eu'] = all_prices['er'] / all_prices['ur']
    if 'bnc' in all_prices and 'ur' in all_prices:
        all_prices['ob'] = all_prices['bnc'] * all_prices['ur']
    if 'eth' in all_prices and 'ur' in all_prices:
        all_prices['oe'] = all_prices['eth'] * all_prices['ur']

    return all_prices


# --- Логика вычислений (без изменений) ---

async def evaluate_expression(expression: str) -> str:
    original_expression = expression
    try:
        prices = await fetch_all_prices()
        if not prices:
            return "Ошибка: не удалось загрузить курсы."

        for key in sorted(prices.keys(), key=len, reverse=True):
            expression = re.sub(r'\b' + key + r'\b', str(prices[key]), expression)

        expression = re.sub(r'(\(?\d[\d\.\s]*\)?)\s*\+\s*(\d+\.?\d*)\s*%', r'(\1 * (1 + \2 / 100))', expression)
        expression = re.sub(r'(\(?\d[\d\.\s]*\)?)\s*-\s*(\d+\.?\d*)\s*%', r'(\1 * (1 - \2 / 100))', expression)

        safe_pattern = re.compile(r'^[ \d\.\+\-\*\/\(\)]+$')
        if not safe_pattern.match(expression):
            return "Ошибка: недопустимые символы"

        result = eval(expression, {"__builtins__": {}}, {})

        if any(crypto in original_expression for crypto in ['bnc', 'eth', 'ob', 'oe']):
            result = round(Decimal(result), 8)
        else:
            result = round(Decimal(result), 2)

        return f'{result:,}'.replace(',', ' ').replace('.', ',')

    except (SyntaxError, NameError):
        return "Ошибка в синтаксисе"
    except ZeroDivisionError:
        return "Ошибка: деление на ноль"
    except (InvalidOperation, TypeError):
        return "Ошибка в вычислениях"
    except Exception as e:
        logging.error(f"Ошибка при вычислении '{original_expression}': {e}")
        return "Неизвестная ошибка"


# --- Обработчики команд ---

async def send_start_and_help(message: Message):
    """Отправляет приветствие и справку на русском языке."""
    bot_user = await message.bot.get_me()
    bot_name = bot_user.username
    help_text = f"""
<b>🧮 Калькулятор в инлайн-режиме</b>

Начните печатать в любом чате имя бота (<code>@{bot_name}</code>), а затем математическое выражение. Результат появится во всплывающем окне.

<b>Примеры:</b>
<code>@{bot_name} (237+78)/(66623-1000)</code>
<code>@{bot_name} ur+1.5%</code>
<code>@{bot_name} ob+4%</code>

<b>⚖️ Сокращения:</b>
<code>ur</code> - ЦБ РФ USD/RUB
<code>cr</code> - ЦБ РФ CNY/RUB
<code>er</code> - ЦБ РФ EUR/RUB
<code>eu</code> - EUR/USD (кросс-курс)
<code>bnc</code> - Binance BTC/USDT
<code>eth</code> - Binance ETH/USDT

<b>Производные:</b>
<code>ob</code> - bnc * ur (Bitcoin в рублях)
<code>oe</code> - eth * ur (Ethereum в рублях)

Для получения этой справки на английском, используйте /help_en
    """
    await message.answer(text=help_text, parse_mode=ParseMode.HTML)


async def send_help_en(message: Message):
    """Отправляет справку на английском языке."""
    bot_user = await message.bot.get_me()
    bot_name = bot_user.username
    help_text = f"""
<b>🧮 Inline Mode Calculator</b>

Start typing the bot's username (<code>@{bot_name}</code>) in any chat, followed by a mathematical expression. The result will appear in a pop-up window.

<b>Examples:</b>
<code>@{bot_name} (237+78)/(66623-1000)</code>
<code>@{bot_name} ur+1.5%</code>
<code>@{bot_name} ob+4%</code>

<b>⚖️ Shortcuts:</b>
<code>ur</code> - CBR USD/RUB
<code>cr</code> - CBR CNY/RUB
<code>er</code> - CBR EUR/RUB
<code>eu</code> - EUR/USD (cross-rate)
<code>bnc</code> - Binance BTC/USDT
<code>eth</code> - Binance ETH/USDT

<b>Derivatives:</b>
<code>ob</code> - bnc * ur (Bitcoin in RUB)
<code>oe</code> - eth * ur (Ethereum in RUB)

To get this help in Russian, use /help
    """
    await message.answer(text=help_text, parse_mode=ParseMode.HTML)


async def send_stats(message: Message):
    if message.from_user.id != ADMIN_ID and ADMIN_ID != 0:
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    stats = load_stats()
    now = datetime.now(UTC)

    dau = 0
    mau = 0
    if 'users' in stats:
        for last_seen_iso in stats['users'].values():
            last_seen = datetime.fromisoformat(last_seen_iso).astimezone(UTC)
            if now - last_seen <= timedelta(days=1):
                dau += 1
            if now - last_seen <= timedelta(days=30):
                mau += 1

    total_req = stats.get('total_requests', 0)
    today_req = stats.get('daily_requests', {}).get(now.strftime('%Y-%m-%d'), 0)

    stats_text = f"""
<b>📊 Статистика бота</b>

<b>Активные пользователи (DAU):</b> {dau}
<b>Активные пользователи (MAU):</b> {mau}

<b>Запросов сегодня:</b> {today_req}
<b>Всего запросов:</b> {total_req}
    """
    await message.answer(stats_text, parse_mode=ParseMode.HTML)


async def handle_other_messages(message: Message):
    """Обрабатывает любые текстовые сообщения, не являющиеся командами."""
    bot_user = await message.bot.get_me()
    bot_name = bot_user.username
    await message.answer(
        f"Я работаю в инлайн-режиме.\n\n"
        f"Просто начните печатать <code>@{bot_name}</code> и ваше выражение в любом чате.\n"
        f"Для получения подробной инструкции, отправьте команду /help.",
        parse_mode=ParseMode.HTML
    )


# --- Обработчик инлайн-запросов ---

async def inline_handler(query: InlineQuery, bot: Bot):
    user_query = query.query.lower().strip()
    if not user_query:
        return

    await update_stats(query.from_user.id)

    result_text = await evaluate_expression(user_query)

    # Для HTML нужно экранировать только <, > и &
    escaped_query = html.escape(query.query)
    escaped_result = html.escape(result_text)

    message_content = f"<code>{escaped_query}</code> = <b>{escaped_result}</b>"

    result_article = InlineQueryResultArticle(
        id='1',
        title=f"Результат: {result_text}",
        input_message_content=InputTextMessageContent(
            message_text=message_content,
            parse_mode=ParseMode.HTML
        ),
        description="Нажмите, чтобы отправить результат расчета."
    )

    await query.answer([result_article], cache_time=30, is_personal=True)


# --- Главная функция для запуска бота ---

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.inline_query.register(inline_handler)
    # Команды /start и /help теперь вызывают одну и ту же функцию
    dp.message.register(send_start_and_help, CommandStart())
    dp.message.register(send_start_and_help, Command("help"))
    dp.message.register(send_help_en, Command("help_en"))
    dp.message.register(send_stats, Command("stats"))

    dp.message.register(handle_other_messages, F.text)

    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Бот запущен и готов к работе...")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")