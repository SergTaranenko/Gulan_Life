# -*- coding: utf-8 -*-
"""
Бот «Делатель орудий» (Мезолит) v5.0
- Зимне-весенние промпты (февраль-апрель)
- Ритуальное изделие каждое 10-е (+18ч)
- Янтарь с Балтики при 52 орудиях
- Бунт при >24ч без изделий
"""

import os
import json
import random
import logging
import asyncio
import ssl
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

import pytz
import aiohttp
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler
)

# ============== КОНФИГУРАЦИЯ ==============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GIGACHAT_AUTH = os.environ.get("GIGACHAT_AUTH")  # Ключ из Сбера
DATA_DIR = Path("/app/data")
TIMEZONE = pytz.timezone("Europe/Moscow")

BOT_START = datetime(2026, 1, 17, 16, 0, tzinfo=TIMEZONE)
BOT_END = datetime(2026, 4, 11, 23, 59, tzinfo=TIMEZONE)

WAKEUP_HOUR = 5
WAKEUP_MINUTE = 30
REPORT_HOUR = 8  # Понедельник 8:00
REPORT_MINUTE = 0

HUNGER_WARNING_HOURS = 12
HUNGER_RIOT_HOURS = 24

DOPAMINE_START_HOUR = 6
DOPAMINE_END_HOUR = 22

# GigaChat URLs
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1"

# Пауза между генерациями (сек)
IMAGE_DELAY = 30

# Типы орудий и материалы
TOOL_TYPES = {
    "arrowhead": "Наконечник стрелы",
    "knife": "Кремневый нож", 
    "scraper": "Скребок",
    "axe": "Топор-адза",
    "spear_tip": "Наконечник копья",
    "harpoon": "Гарпун",
    "drill": "Долото/сверло"
}

MATERIALS = {
    "flint": "Кремень",
    "obsidian": "Обсидиан",  # 10% шанс
    "jasper": "Яшма",
    "quartzite": "Кварцит"
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============== GIGACHAT API ==============
class GigaChatAPI:
    def __init__(self):
        self.token_cache = {"token": None, "expires": None}
    
    async def get_token(self):
        if self.token_cache["token"] and self.token_cache["expires"]:
            if datetime.now().timestamp() < self.token_cache["expires"] - 60:
                return self.token_cache["token"]
        
        if not GIGACHAT_AUTH:
            return None
        
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GIGACHAT_OAUTH_URL,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                        "RqUID": str(uuid.uuid4()),
                        "Authorization": f"Basic {GIGACHAT_AUTH}"
                    },
                    data="scope=GIGACHAT_API_PERS",
                    ssl=ssl_context
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.token_cache["token"] = data["access_token"]
                        self.token_cache["expires"] = data["expires_at"] / 1000
                        return data["access_token"]
        except Exception as e:
            logger.error(f"GigaChat auth error: {e}")
        return None
    
    async def generate_image(self, prompt):
        """Генерация через GigaChat-Max"""
        token = await self.get_token()
        if not token:
            return None
        
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        try:
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{GIGACAT_API_URL}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}"
                    },
                    json={
                        "model": "GigaChat-Max",
                        "messages": [{"role": "user", "content": prompt}],
                        "function_call": "auto"
                    },
                    ssl=ssl_context
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    if "<img src=\"" in content:
                        start = content.find("<img src=\"") + 10
                        end = content.find("\"", start)
                        file_id = content[start:end]
                        
                        async with session.get(
                            f"{GIGACHAT_API_URL}/files/{file_id}/content",
                            headers={"Authorization": f"Bearer {token}"},
                            ssl=ssl_context
                        ) as img_resp:
                            if img_resp.status == 200:
                                return await img_resp.read()
        except Exception as e:
            logger.error(f"Image generation error: {e}")
        return None

gigachat = GigaChatAPI()

# ============== РАБОТА С ДАННЫМИ ==============
def load_data():
    file_path = DATA_DIR / "stoyanka_data.json"
    default = {
        "user_id": None,
        "current_date": None,
        "morning_done": False,
        "waiting_for_plans": False,
        "plans_confirmed": None,
        "last_feed_time": None,
        "hunger_notified": False,
        "last_dopamine_hour": None,
        "goodnight_sent": False,
        "arsenal": {
            "total_created": 0,
            "current_week_tools": [],
            "week_start": datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        },
        "amber_achieved": False
    }
    try:
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key in default:
                    if key not in data:
                        data[key] = default[key]
                return data
        return default
    except Exception as e:
        logger.error(f"Load error: {e}")
        return default

def save_data(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_path = DATA_DIR / "stoyanka_data.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def now_msk():
    return datetime.now(TIMEZONE)

def today_str():
    return now_msk().strftime("%Y-%m-%d")

def get_hunger_hours(data):
    last = data.get("last_feed_time")
    if not last:
        return 0
    last_dt = datetime.fromisoformat(last)
    return (now_msk() - last_dt).total_seconds() / 3600

def get_hunger_mode(data):
    hours = get_hunger_hours(data)
    if hours < HUNGER_WARNING_HOURS:
        return "good"
    elif hours < HUNGER_RIOT_HOURS:
        return "bad"
    return "riot"

# ============== ПРОМПТЫ (ЗИМНЕ-ВЕСЕННИЕ) ==============
def get_sunrise_prompt():
    return ("Мезолитическая мастерская на берегу реки Дубна, зимнее утро, "
            "рассвет, снег тает, берёзовые рощи, костер тлеет, кремнёвые "
            "заготовки на шкуре бизона, атмосфера начала дня, реалистичный "
            "археологический стиль, тёплые тона")

def get_tool_prompt(tool_type, material, is_ritual=False):
    base = (f"{material} {tool_type}, мезолит русской равнины, "
            f"зима-ранняя весна, только что изготовлено, лежит на берёзовой "
            f"коре или шкуре, костер на заднем плане, снег, реалистичная "
            f"археологическая реконструкция")
    if is_ritual:
        base += (", с геометрическим орнаментом ёлочкой, насечками, "
                "тщательно отполировано, статусный объект мастера, "
                "украшения из резцов")
    return base

def get_amber_prompt():
    return ("Мезолитический делатель орудий держит в руке кусок балтийского "
            "янтаря, зимний лес, река Дубна, мастерская на заднем плане, "
            "торжественная атмосфера, обмен состоялся, реалистичный стиль")

def get_night_prompt():
    return ("Мезолитическая мастерская ночью, костер тлеет, готовые орудия "
            "лежат на шкуре бизона, северное сияние или звёздное небо, "
            "зима, снег, атмосфера покоя и уюта, берёзовые рощи на фоне")

# ============== ОБРАБОТЧИКИ ==============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_data()
    data["user_id"] = user_id
    data["current_date"] = today_str()
    if not data["last_feed_time"]:
        data["last_feed_time"] = now_msk().isoformat()
    save_data(data)
    
    await update.message.reply_text(
        "⚒️ ДЕЛАТЕЛЬ ОРУДИЙ — МЕЗОЛИТ РУССКОЙ РАВНИНЫ\n\n"
        "Твоя задача: ковать орудия для охотников.\n\n"
        "Команды:\n"
        "/done или 'сделал' — Орудие готово (+12ч, +18ч каждое 10-е)\n"
        "/tried или 'попробовал' — Работаю над формой (+4ч)\n"
        "/penalty — Неудача в мастерской (-1ч)\n"
        "/status — Проверить запасы\n\n"
        "Цель: создать 52 орудия до 11 апреля и получить Янтарь с Балтики.\n"
        "Утром спрошу про твои дела."
    )

async def handle_plans_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, data: dict):
    user_id = update.effective_user.id
    text_lower = text.lower()
    
    if any(word in text_lower for word in ["есть", "да", "готов", "yes"]):
        data["plans_confirmed"] = True
        data["morning_done"] = True
        data["waiting_for_plans"] = False
        save_data(data)
        
        await update.message.reply_text("✅ Отлично, Мастер! План есть — племя будет сыто.")
        
        # Генерация рассвета
        img_data = await gigachat.generate_image(get_sunrise_prompt())
        if img_data:
            await context.bot.send_photo(chat_id=user_id, photo=BytesIO(img_data),
                                       caption="🌅 Рассвет в мастерской. День обещает быть плодотворным.")
        else:
            await update.message.reply_text("🌅 Рассвет в мастерской...")
            
    elif any(word in text_lower for word in ["нет", "нету", "не", "no"]):
        data["plans_confirmed"] = False
        data["morning_done"] = True
        data["waiting_for_plans"] = False
        save_data(data)
        
        # Генерация 4 кодов (старой логики)
        g1, g2 = f"G{random.randint(1,20)}", f"G{random.randint(21,40)}"
        p1, m1 = f"P{random.randint(1,20)}", f"M{random.randint(1,20)}"
        tasks = random.sample([g1, g2, p1, m1], 4)
        
        msg = ("⚒️ Тогда вот твои цели на сегодня:\n" + 
               "\n".join(f"• `{t}`" for t in tasks) +
               "\n\nУкажи охотникам путь.")
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Ответь просто: 'есть' или 'нет'")

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (BOT_START <= now_msk() < BOT_END):
        await update.message.reply_text("Бот неактивен.")
        return
    
    data = load_data()
    
    # Проверяем, не ждём ли ответ о планах
    if data.get("waiting_for_plans"):
        await update.message.reply_text("Сначала ответь: есть ли у тебя 4 дела? (есть/нет)")
        return
    
    # Определяем, ритуальное ли это изделие (каждое 10-е)
    current_total = data["arsenal"]["total_created"]
    next_num = current_total + 1
    is_ritual = (next_num % 10 == 0)
    
    # Выбор типа и материала
    tool_type_key = random.choice(list(TOOL_TYPES.keys()))
    material_key = random.choice(list(MATERIALS.keys()))
    
    # 10% шанс на обсидиан (редкий)
    if random.random() < 0.1:
        material_key = "obsidian"
    
    tool_name = TOOL_TYPES[tool_type_key]
    material_name = MATERIALS[material_key]
    
    # Генерация картинки
    prompt = get_tool_prompt(tool_name, material_name, is_ritual)
    img_data = await gigachat.generate_image(prompt)
    
    # Обновление времени (12 или 18 часов)
    bonus_hours = 18 if is_ritual else 12
    current_hunger = get_hunger_hours(data)
    new_hunger = current_hunger - bonus_hours
    new_feed_time = now_msk() - timedelta(hours=new_hunger)
    
    data["last_feed_time"] = new_feed_time.isoformat()
    data["hunger_notified"] = False
    
    # Обновление арсенала
    data["arsenal"]["total_created"] = next_num
    data["arsenal"]["current_week_tools"].append({
        "date": today_str(),
        "type": tool_name,
        "material": material_name,
        "ritual": is_ritual
    })
    
    # Проверка на Янтарь (52 орудия)
    if next_num == 52 and not data.get("amber_achieved"):
        data["amber_achieved"] = True
        amber_img = await gigachat.generate_image(get_amber_prompt())
        if amber_img:
            await context.bot.send_photo(
                chat_id=data["user_id"], 
                photo=BytesIO(amber_img),
                caption="🎉 Великое достижение! Ты создал 52 орудия. "
                        "Племя обменяло их на Янтарь с Балтики. "
                        "Твой статус — Легендарный Мастер."
            )
    
    save_data(data)
    
    # Отправка результата
    if is_ritual:
        text = (f"⚡ РИТУАЛЬНОЕ ИЗДЕЛИЕ! ({next_num}-е)\n"
                f"⚒️ Создано: {material_name} {tool_name}\n"
                f"✨ Украшено орнаментом ёлочкой и насечками\n"
                f"⏳ +{bonus_hours} часов сытости")
    else:
        text = (f"⚒️ Создано: {material_name} {tool_name}\n"
                f"⏳ +{bonus_hours} часов сытости")
    
    await update.message.reply_text(text)
    
    if img_data:
        await context.bot.send_photo(chat_id=data["user_id"], photo=BytesIO(img_data))
    else:
        await update.message.reply_text("(Изображение временно недоступно)")
    
    # Информация о прогрессе
    await update.message.reply_text(f"📊 Всего создано: {next_num}/52")

async def cmd_tried(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (BOT_START <= now_msk() < BOT_END):
        await update.message.reply_text("Бот неактивен.")
        return
    
    data = load_data()
    current_hunger = get_hunger_hours(data)
    new_hunger = current_hunger - 4
    new_feed_time = now_msk() - timedelta(hours=new_hunger)
    
    data["last_feed_time"] = new_feed_time.isoformat()
    save_data(data)
    
    phrases = [
        "Тропа не ясна, но ты ищешь. +4 часа.",
        "Не удалось изготовить, но опыт остаётся. +4ч",
        "Кремень раскололся неудачно, но ты не сдаёшься. +4ч"
    ]
    await update.message.reply_text(random.choice(phrases))

async def cmd_penalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Штраф -1 час (без крыс, аутентично)"""
    if not (BOT_START <= now_msk() < BOT_END):
        await update.message.reply_text("Бот неактивен.")
        return
    
    data = load_data()
    current_hunger = get_hunger_hours(data)
    new_hunger = current_hunger + 1
    new_feed_time = now_msk() - timedelta(hours=new_hunger)
    
    data["last_feed_time"] = new_feed_time.isoformat()
    save_data(data)
    
    penalties = [
        "🔥 Угли в мастерской погасли. Огонь придётся разводить заново. -1ч",
        "💔 Трещина в роге лося! Заготовка раскололась при обработке. -1ч",
        "🌧️ Ливень промочил берестяную ёмкость — кость намокла. -1ч",
        "🪵 Древесина дала сучок — стамеска соскользнула. -1ч",
        "❄️ Мороз сделал кость ломкой — отломился край пластины. -1ч",
        "🌬️ Ветер сдул берёзовый дёготь из ёмкости. -1ч"
    ]
    await update.message.reply_text(random.choice(penalties))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    hours = get_hunger_hours(data)
    mode = get_hunger_mode(data)
    total = data["arsenal"]["total_created"]
    
    if mode == "good":
        status = f"✅ Мастерская работает\n⏳ До кризиса: {HUNGER_WARNING_HOURS - hours:.1f} ч."
        emoji = "😊"
    elif mode == "bad":
        status = f"⚠️ Орудия тупятся\n⏳ До бунта: {HUNGER_RIOT_HOURS - hours:.1f} ч."
        emoji = "😟"
    else:
        overtime = hours - HUNGER_RIOT_HOURS
        status = f"🔥 БУНТ! Охотники без оружия {overtime:.1f} ч.!"
        emoji = "😡"
    
    msg = (f"📊 СТАТУС ДЕЛАТЕЛЯ {emoji}\n\n"
           f"⚒️ Орудий создано: {total}/52\n"
           f"⏱️ Без дела: {hours:.1f} ч.\n"
           f"{status}\n\n")
    
    if total >= 52:
        msg += "🟡 Янтарь с Балтики получен!"
    else:
        msg += f"🎯 Осталось до Янтаря: {52 - total}"
    
    await update.message.reply_text(msg)

# ============== ТАЙМЕРЫ ==============
async def main_timer(context: ContextTypes.DEFAULT_TYPE):
    if not (BOT_START <= now_msk() < BOT_END):
        return
    
    data = load_data()
    user_id = data.get("user_id")
    if not user_id:
        return
    
    now = now_msk()
    current_hour, current_minute = now.hour, now.minute
    current_weekday = now.weekday()  # 0=Monday
    
    # Сброс дня
    if data.get("current_date") != today_str():
        data["current_date"] = today_str()
        data["morning_done"] = False
        data["waiting_for_plans"] = False
        data["hunger_notified"] = False
        data["last_dopamine_hour"] = None
        data["goodnight_sent"] = False
        save_data(data)
    
    # Утренний диалог (5:30)
    if current_hour == WAKEUP_HOUR and current_minute == WAKEUP_MINUTE:
        if not data.get("morning_done"):
            await context.bot.send_message(
                chat_id=user_id,
                text="⚒️ Вставай, Делатель. У тебя есть 4 дела на сегодня? (есть/нет)"
            )
            data["waiting_for_plans"] = True
            save_data(data)
    
    # Проверка голода (бунт каждые 30 мин при >24ч)
    mode = get_hunger_mode(data)
    
    if mode == "riot" and current_minute in [0, 30]:
        riots = [
            "🔥 БУНТ! Охотники без оружия уже 24 часа!",
            "🔥 Племя теряет терпение! Где новые орудия?!",
            "🔥 Кризис! Мастерская пустует слишком долго!"
        ]
        await context.bot.send_message(chat_id=user_id, text=random.choice(riots))
    
    elif mode == "bad" and not data.get("hunger_notified"):
        data["hunger_notified"] = True
        save_data(data)
        await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ Орудия тупятся. Охотники нервничают. Действуй!"
        )
    
    # Дофамин в :55
    if current_minute == 55 and DOPAMINE_START_HOUR <= current_hour <= DOPAMINE_END_HOUR:
        if data.get("last_dopamine_hour") != current_hour:
            data["last_dopamine_hour"] = current_hour
            save_data(data)
            await context.bot.send_message(
                chat_id=user_id,
                text=random.choice([
                    "☀️ Момент покоя в мастерской...",
                    "🌿 Запах кремня и берёзового дёгтя...",
                    "🔥 Огонь кузницы горит ровно..."
                ])
            )
    
    # Вечер (23:00) — только если режим good
    if current_hour == 23 and current_minute == 0 and not data.get("goodnight_sent"):
        if mode == "good":
            data["goodnight_sent"] = True
            save_data(data)
            img = await gigachat.generate_image(get_night_prompt())
            if img:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=BytesIO(img),
                    caption="🌙 Спокойной ночи, Делатель. Арсенал пополнен."
                )
            else:
                await context.bot.send_message(chat_id=user_id, text="🌙 Спокойной ночи, Делатель.")
    
    # Понедельник 8:00 — отчёт за неделю
    if current_weekday == 0 and current_hour == REPORT_HOUR and current_minute == REPORT_MINUTE:
        week_tools = data["arsenal"]["current_week_tools"]
        count = len(week_tools)
        
        if count == 0:
            await context.bot.send_message(
                chat_id=user_id,
                text="📉 Неделя прошла зря. Арсенал пуст. Племя недовольно."
            )
        else:
            # Отправка списка
            tools_list = "\n".join([f"• {t['material']} {t['type']}" + 
                                   (" (ритуальное)" if t.get('ritual') else "")
                                   for t in week_tools[-10:]])  # последние 10
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📊 ОТЧЁТ НЕДЕЛИ\nСоздано орудий: {count}\n\n{tools_list}"
            )
            
            # Если 7+ — коллекция
            if count >= 7:
                collage = await gigachat.generate_image(
                    "Мезолитическая мастерская, 7 кремнёвых орудий лежат на шкуре бизона "
                    "в ряд: ножи, наконечники стрел, топор. Зима, снег, костер. "
                    "Коллекция мастера, реалистичный стиль."
                )
                if collage:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=BytesIO(collage),
                        caption="🏆 Полный арсенал недели! Великолепная работа."
                    )
        
        # Сброс недели
        data["arsenal"]["current_week_tools"] = []
        data["arsenal"]["week_start"] = today_str()
        save_data(data)

# ============== ОБРАБОТКА ТЕКСТА ==============
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()
    data = load_data()
    
    # Если ждём планы
    if data.get("waiting_for_plans"):
        await handle_plans_response(update, context, text, data)
        return
    
    # Команды текстом
    if any(word in text for word in ["сделал", "готово", "сделала"]):
        await cmd_done(update, context)
    elif any(word in text for word in ["попробовал", "старался", "пыт"]):
        await cmd_tried(update, context)
    elif "неудач" in text or "плохо" in text:
        await cmd_penalty(update, context)

# ============== MAIN ==============
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN!")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Хендлеры
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("tried", cmd_tried))
    app.add_handler(CommandHandler("penalty", cmd_penalty))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Таймер каждую минуту
    app.job_queue.run_repeating(main_timer, interval=60, first=10)
    
    logger.info("Делатель орудий v5.0 запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
