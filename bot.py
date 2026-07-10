import telebot
import os
import json
import requests
import threading
import pytz
from groq import Groq
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# Работа с PostgreSQL
from sqlalchemy import create_engine, text

# Веб-сервер
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# 1. ТОКЕНЫ И НАСТРОЙКА
TELEGRAM_TOKEN = "8811867508:AAFxcE58OJbSbt9lmZHRFcpayMYfOE0AXLI"
GROQ_API_KEY = "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"
DATABASE_URL = "postgresql://admin:qmoBE1mBhoi4ANcFHBs8du2Jw3hSql3g@dpg-d97s2pnavr4c73di73hg-a/ruleguard"

# Твой бесплатный ключ Tavily
TAVILY_API_KEY = "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"

RENDER_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "https://ruleguard-backend.onrender.com")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

engine = create_engine(DATABASE_URL)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. РАБОТА С БАЗОЙ ДАННЫХ (POSTGRESQL)
def init_db():
    """Создание таблиц пользователей, отчетов, истории чата и кэша Tavily в PostgreSQL"""
    with engine.connect() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                user_name TEXT, 
                business_description TEXT, 
                push_time TEXT DEFAULT '09:00',
                country TEXT,
                location TEXT,
                legal_form TEXT,
                timezone TEXT DEFAULT 'UTC',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                input_text TEXT,
                report_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))

        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                role TEXT, -- 'user' или 'assistant'
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))

        # Надежная таблица кэширования для предотвращения параллельных дублей на Render
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS tavily_cache (
                id SERIAL PRIMARY KEY,
                query_hash TEXT UNIQUE,
                search_result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        conn.commit()

def save_user_data_extended(user_id, username=None, business=None, country=None, location=None, legal_form=None, push_time=None, timezone=None):
    with engine.connect() as conn:
        result = conn.execute(text("SELECT user_name, business_description, country, location, legal_form, push_time, timezone FROM users WHERE user_id = :user_id"), {"user_id": user_id})
        row = result.fetchone()
        
        if row:
            c_name = username if username is not None else row[0]
            c_bus = business if business is not None else row[1]
            c_country = country if country is not None else row[2]
            c_loc = location if location is not None else row[3]
            c_form = legal_form if legal_form is not None else row[4]
            c_push = push_time if push_time is not None else row[5]
            c_tz = timezone if timezone is not None else (row[6] if row[6] else 'UTC')
            
            conn.execute(text('''
                UPDATE users 
                SET user_name = :name, business_description = :bus, country = :country, location = :loc, 
                    legal_form = :form, push_time = :push, timezone = :tz, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = :user_id
            '''), {"name": c_name, "bus": c_bus, "country": c_country, "loc": c_loc, "form": c_form, "push": c_push, "tz": c_tz, "user_id": user_id})
        else:
            conn.execute(text('''
                INSERT INTO users (user_id, user_name, business_description, country, location, legal_form, push_time, timezone) 
                VALUES (:user_id, :name, :bus, :country, :loc, :form, :push, :tz)
            '''), {
                "user_id": user_id, "name": username or "Предприниматель", "bus": business or "Не указано", "country": country or "Не указано", 
                "loc": location or "Не указано", "form": legal_form or "Не указано", "push": push_time or '09:00', "tz": timezone or 'UTC'
            })
        conn.commit()

def save_user_data(user_id, username=None, business=None):
    save_user_data_extended(user_id, username=username, business=business)

def get_user_context(user_id):
    with engine.connect() as conn:
        result = conn.execute(text("SELECT user_name, business_description, country, location, legal_form FROM users WHERE user_id = :user_id"), {"user_id": user_id})
        row = result.fetchone()
    if row: 
        return f"Пользователь: {row[0] or 'Не указано'}. Страна: {row[2] or 'Не указано'}, Регион: {row[3] or 'Не указано'}, ОПФ: {row[4] or 'Не указано'}. Специфика бизнеса: {row[1] or 'Не указано'}."
    return "Новый пользователь без настроенного профиля."

def save_report_to_archive(user_id, input_text, report_text):
    try:
        with engine.connect() as conn:
            conn.execute(text('''
                INSERT INTO reports (user_id, input_text, report_text)
                VALUES (:user_id, :input_text, :report_text)
            '''), {"user_id": user_id, "input_text": input_text, "report_text": report_text})
            conn.commit()
    except Exception as e:
        print(f"Ошибка сохранения отчета в Архив: {e}")

def save_chat_message(user_id, role, text_msg):
    try:
        with engine.connect() as conn:
            conn.execute(text('''
                INSERT INTO chat_history (user_id, role, message_text)
                VALUES (:user_id, :role, :message_text)
            '''), {"user_id": user_id, "role": role, "message_text": text_msg})
            conn.commit()
    except Exception as e:
        print(f"Ошибка сохранения сообщения: {e}")

def get_recent_chat_history(user_id, limit=6):
    try:
        with engine.connect() as conn:
            result = conn.execute(text('''
                SELECT role, message_text FROM chat_history 
                WHERE user_id = :user_id 
                ORDER BY created_at DESC LIMIT :limit
            '''), {"user_id": user_id, "limit": limit})
            rows = result.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception as e:
        print(f"Ошибка получения истории чата: {e}")
        return []

# Интеллектуальный ИИ-диспетчер лимитов поиска
def check_if_search_needed(history, current_input):
    """Определяет, требуется ли вызов веб-поиска для ответа на текущую реплику"""
    system_prompt = (
        "Ты — технический диспетчер системы RuleGuard. Твоя задача — определить, "
        "нужен ли глубокий поиск в актуальном интернете (Tavily API) для ответа на вопрос.\n"
        "Ответь строго ОДНИМ словом: 'SEARCH', если пользователь просит найти новые законы, "
        "актуальные штрафы, свежие новости по локации.\n"
        "Ответь строго ОДНИМ словом: 'DIALOG', если вопрос — это уточнение прошлого отчета, "
        "обычное рассуждение, приветствие или продолжение текущей беседы (например: 'что мне делать?', 'а если так?', "
        "'поясни третий пункт', 'какой сейчас год', 'как быть')."
    )
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-2:]: 
            messages.append(msg)
        messages.append({"role": "user", "content": f"Вопрос пользователя: {current_input}"})
        
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.0,
            max_tokens=5
        )
        decision = completion.choices[0].message.content.strip().upper()
        print(f"🤖 [ИИ-Диспетчер] Вердикт для запроса '{current_input}': {decision}")
        return "SEARCH" in decision
    except Exception as e:
        print(f"Ошибка диспетчера интентов: {e}")
        return True

# 3. ПОИСК В ИНТЕРНЕТЕ С ЛОГИРОВАНИЕМ И ЗАЩИТОЙ
def search_internet(query):
    clean_query = query.replace("Новый пользователь без настроенного профиля.", "")
    clean_query = clean_query.replace("вопрос:", "").strip()
    
    if len(clean_query) < 5:
        return "Недостаточно данных для поиска."

    # Проверка параллельных дубликатов в базе данных
    try:
        with engine.connect() as conn:
            res = conn.execute(text('''
                SELECT search_result FROM tavily_cache 
                WHERE query_hash = :q AND created_at > CURRENT_TIMESTAMP - INTERVAL '15 seconds'
            '''), {"q": clean_query})
            row = res.fetchone()
            if row:
                print(f"🛡️ [Блокировка БД] Перехвачен параллельный дубликат! Возвращаем кэш.")
                return row[0]
    except Exception as e:
        print(f"Ошибка кэша БД: {e}")

    try:
        print(f"🔍 [Tavily] Выполняю одиночный веб-поиск: '{clean_query}'")
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": clean_query,
            "search_depth": "advanced",
            "max_results": 3
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post("https://api.tavily.com/search", json=payload, headers=headers, timeout=15)
        
        if response.status_code == 200:
            results = response.json().get("results", [])
            if results:
                context = "\n".join([f"Источник: {r['url']}\nТекст: {r['content']}" for r in results])
                
                # Запись свежего результата поиска в кэш БД
                try:
                    with engine.connect() as conn:
                        conn.execute(text('''
                            INSERT INTO tavily_cache (query_hash, search_result) 
                            VALUES (:q, :res) ON CONFLICT (query_hash) 
                            DO UPDATE SET search_result = :res, created_at = CURRENT_TIMESTAMP
                        '''), {"q": clean_query, "res": context})
                        conn.commit()
                except Exception as db_err:
                    print(f"Ошибка сохранения кэша: {db_err}")
                    
                return context
    except Exception as e:
        print(f"❌ [Tavily] Ошибка выполнения API-запроса: {e}")
    return "Не удалось найти свежие нормативные данные в сети."

# 4. ЯДРО АНАЛИЗА (Генерация отчетов из анкеты)
def generate_report_logic(user_id, current_input_text):
    user_memory = get_user_context(user_id)
    search_query = f"юридические риски штрафы законы актуальное {current_input_text}"
    web_data = search_internet(search_query)

    system_instruction = (
        "Ты — профессиональный ИИ-юрист RuleGuard, защищающий бизнес от штрафов и проверок.\n"
        "Сделай глубокий анализ на основе предоставленных данных из сети на текущий момент.\n\n"
        "Твой ответ ДОЛЖЕН строго следовать следующей структуре (используй Markdown для заголовков):\n"
        "### 🔥 Главные юридические риски\n"
        "Выдели 2-3 критических риска. Опиши конкретные штрафы или санкции в цифрах, если они есть в контексте.\n\n"
        "### 🛡️ Инструкция по защите (Что проверить)\n"
        "Пошаговые легальные действия для предпринимателя, чтобы полностью себя обезопасить.\n\n"
        "### 📊 Уровень угрозы\n"
        "Напиши одну строчку: Низкий, Средний или Высокий, и кратко обоснуй почему.\n\n"
        "Отвечай уверенно, на русском языке, без лишней «воды» и общих фраз."
    )
    
    full_prompt = (
        f"Контекст профиля: {user_memory}\n"
        f"АКТУАЛЬНЫЕ ДАННЫЕ СЕТИ ИЗ TAVILY API:\n{web_data}\n\n"
        f"Вводные данные для экспресс-анализа: {current_input_text}"
    )
    
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": full_prompt}
        ],
        temperature=0.25
    )
    bot_response = completion.choices[0].message.content
    
    save_report_to_archive(user_id, current_input_text, bot_response)
    return bot_response

# 5. УМНЫЙ ЧАТ С КОНТЕНТОМ И ПАМЯТЬЮ
def run_legal_analysis(message, current_input_text):
    bot.send_chat_action(message.chat.id, 'typing')
    user_id = message.from_user.id
    
    user_context = get_user_context(user_id)
    history_messages = get_recent_chat_history(user_id, limit=6)

    # Запускаем классификатор интента: нужен ли веб-поиск?
    need_search = check_if_search_needed(history_messages, current_input_text)
    
    web_context = ""
    if need_search:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT country, location FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            row = res.fetchone()
            loc_context = f"{row[0]} {row[1]}" if row else ""

        search_query = f"{loc_context} {current_input_text}".strip()
        web_context = search_internet(search_query)
    else:
        print(f"💡 [Экономия] Запрос определен как DIALOG. Поиск Tavily пропущен (0 кредитов).")
        web_context = "Дополнительный веб-поиск не требовался. Отвечай, опираясь на имеющийся бизнес-профиль и контекст диалога."

    current_year = datetime.now().year

    system_instruction = (
        f"Ты — ИИ-юрист RuleGuard. Отвечай на вопросы пользователя в контексте его бизнеса.\n"
        f"Текущий год: {current_year}.\n"
        f"Данные бизнеса клиента: {user_context}\n"
        f"Свежие данные из сети (если запрашивались): {web_context}\n\n"
        "Отвечай коротко, по делу, понятным языком. Если пользователь просит уточнить пункт "
        "или задает связанный вопрос — используй историю сообщений. Пиши в уважительном тоне. "
        "Никогда не используй отговорок про отсутствие доступа к реальному времени — у тебя есть все необходимые вводные."
    )

    messages_payload = [{"role": "system", "content": system_instruction}]
    for msg in history_messages:
        messages_payload.append(msg)
    messages_payload.append({"role": "user", "content": current_input_text})

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile", 
            messages=messages_payload,
            temperature=0.3
        )
        bot_response = completion.choices[0].message.content
        
        save_chat_message(user_id, "user", current_input_text)
        save_chat_message(user_id, "assistant", bot_response)
        
        bot.reply_to(message, bot_response, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"⚠️ Ошибка ИИ Groq: {str(e)}")

# =====================================================================
# СЕРВЕРНЫЕ ЭНДПОИНТЫ ДЛЯ МИНИ-ПРИЛОЖЕНИЯ
# =====================================================================
@app.get("/")
def read_root():
    return {"status": "online", "project": "RuleGuard AI PostgreSQL + Tavily Search"}

@app.post("/api/analyze")
async def handle_web_analysis(request: Request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id'))
        username = data.get('username', 'Предприниматель')
        country = data.get('country', None)
        location = data.get('location', None)
        legal_form = data.get('legal_form', None)
        details = data.get('business_details', None)
        push_time = data.get('push_time', None)
        user_tz = data.get('timezone', None)
        
        save_user_data_extended(user_id, username, details, country, location, legal_form, push_time, user_tz)
        
        if not details and not location:
            return {"status": "success", "message": "Settings updated"}

        compiled_input = f"Страна: {country or 'Не указано'}, Локация: {location or 'Не указано'}. Форма: {legal_form or 'Не указано'}. Детали: {details or 'Не указано'}"
        report = generate_report_logic(user_id, compiled_input)
        
        flag = "🇺🇸" if country == "USA" else "🇷🇺" if country == "Russia" else "🌐"
        safe_report = report.replace("<", "&lt;").replace(">", "&gt;")
        bot.send_message(user_id, f"{flag} <b>Новый анализ из приложения</b>\n\n{safe_report}", parse_mode='HTML')
        
        return {"status": "success", "report": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}
        
@app.get("/api/history/{user_id}")
async def get_user_history(user_id: int, tz: str = "UTC"):
    try:
        with engine.connect() as conn:
            user_res = conn.execute(text("SELECT push_time, timezone FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            user_row = user_res.fetchone()
            
            push_time = "09:00"
            user_tz_str = "UTC"
            
            if user_row:
                push_time = user_row[0] if user_row[0] else "09:00"
                user_tz_str = user_row[1] if user_row[1] else "UTC"
            
            try:
                user_tz = pytz.timezone(user_tz_str)
            except Exception:
                user_tz = pytz.utc
            
            reports_res = conn.execute(text(
                "SELECT input_text, report_text, created_at FROM reports WHERE user_id = :user_id ORDER BY created_at DESC"
            ), {"user_id": user_id})
            
            history = []
            for row in reports_res.fetchall():
                utc_dt = row[2]
                if utc_dt.tzinfo is None:
                    utc_dt = pytz.utc.localize(utc_dt)
                
                local_dt = utc_dt.astimezone(user_tz)
                history.append({
                    "input_text": row[0],
                    "report_text": row[1],
                    "created_at": local_dt.strftime("%d.%m.%Y %H:%M")
                })
                
        return {"status": "success", "push_time": push_time, "history": history}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/reanalyze/{user_id}")
async def handle_fast_reanalyze(user_id: int):
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT country, location, legal_form, business_description FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            row = result.fetchone()
            
        if not row or not row[3]:
            return {"status": "error", "message": "Профиль бизнеса не найден. Сначала заполните анкету!"}
            
        compiled_input = f"Страна: {row[0]}, Локация: {row[1]}. Form: {row[2]}. Details: {row[3]}"
        report = generate_report_logic(user_id, compiled_input)
        
        flag = "🇺🇸" if row[0] == "USA" else "🇷🇺" if row[0] == "Russia" else "🌐"
        safe_report = report.replace("<", "&lt;").replace(">", "&gt;")
        bot.send_message(user_id, f"{flag} <b>🔄 Свежий экспресс-анализ</b>\n\n{safe_report}", parse_mode='HTML')
        
        return {"status": "success", "report": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =====================================================================
# ПЛАНИРОВЩИК ПУШЕЙ И АНТИ-СОН
# =====================================================================
def send_daily_push_notifications():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT user_id, user_name, business_description, location, push_time, timezone FROM users"))
            all_users = result.fetchall()
        
        for user in all_users:
            user_id, username, business, location, push_time, user_tz = user
            if not location or not business: continue
            if not user_tz: user_tz = 'UTC'
                
            tz = pytz.timezone(user_tz)
            user_current_time = datetime.now(tz).strftime("%H:%M")
            
            if user_current_time == push_time:
                search_query = f"изменения законы штрафы регуляция {location} {business}"
                web_data = search_internet(search_query)
                
                system_instruction = (
                    "Ты — ИИ-юрист RuleGuard. Твоя задача — прислать ежедневную сводку спокойствия.\n"
                    "Пиши очень кратко (максимум 2-3 предложения). Скажи, есть ли критические изменения по законам на сегодня.\n"
                    "Если всё спокойно, поддержи предпринимателя. В конце обязательно напиши фразу: 'Ваш бизнес под защитой. RuleGuard AI на связи!'"
                )
                
                completion = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile", 
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": f"Данные бизнеса: {business}, Локация: {location}. Контекст сети: {web_data}"}
                    ],
                    temperature=0.4
                )
                push_text = completion.choices[0].message.content
                bot.send_message(user_id, f"🛡️ <b>Ежедневный RuleGuard Радар</b>\n\n{push_text}", parse_mode="HTML")
            
    except Exception as e:
        print(f"Ошибка планировщика пушей: {e}")

def smart_ping_render():
    current_hour = datetime.now().hour
    if 7 <= current_hour < 22:
        try:
            print(f"⏰ [Пинг] Держим Render бодрствующим...")
            response = requests.get(RENDER_APP_URL, timeout=10)
            print(f"ℹ️ [Пинг] Ответ сервера: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Ошибка автопина: {e}")

# =====================================================================
# ОБРАБОТЧИКИ ТЕЛЕГРАМ БОТА
# =====================================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    save_user_data(message.from_user.id, username=message.from_user.first_name)
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    web_app_info = telebot.types.WebAppInfo("https://artemiiborovko.github.io/ruleguard-ui/")
    
    btn_open_app = telebot.types.KeyboardButton(text="🚀 Открыть анкету RuleGuard", web_app=web_app_info)
    btn_re_analyze = telebot.types.KeyboardButton(text="🔄 Повторить последний анализ")
    
    markup.add(btn_open_app)
    markup.add(btn_re_analyze)
    
    welcome_text = (
        f"🛡️ **Привет, {message.from_user.first_name}! Бот RuleGuard запущен на базе PostgreSQL + Tavily.**\n\n"
        "• Чтобы настроить профиль, нажми **Открыть анкету RuleGuard**.\n"
        "• Чтобы обновить юридический отчет по профилю, нажми **Повторить последний анализ**.\n"
        "• Либо просто напиши мне любой вопрос прямо сюда, в чат!"
    )
    bot.reply_to(message, welcome_text, reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    if message.text == "🔄 Повторить последний анализ":
        bot.send_chat_action(message.chat.id, 'typing')
        with engine.connect() as conn:
            result = conn.execute(text("SELECT country, location, legal_form, business_description FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            row = result.fetchone()
            
        if not row or not row[3]:
            bot.reply_to(message, "📭 У вас еще нет сохраненного профиля бизнеса. Пожалуйста, откройте анкету и заполните её!")
            return
            
        compiled_input = f"Страна: {row[0]}, Локация: {row[1]}. Форма: {row[2]}. Детали: {row[3]}"
        bot.reply_to(message, "⏳ *Запрашиваю новые законы через Tavily API и перегенерирую отчет...*", parse_mode='Markdown')
        
        try:
            report = generate_report_logic(user_id, compiled_input)
            bot.send_message(user_id, f"🔄 **Свежий повторный анализ профиля:**\n\n{report}", parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, f"⚠️ Ошибка генерации: {e}")
    else:
        run_legal_analysis(message, message.text)

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    try:
        bot.send_chat_action(message.chat.id, 'record_audio')
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        filename = f"voice_{message.voice.file_id}.ogg"
        with open(filename, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        with open(filename, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, audio_file.read()),
                model="whisper-large-v3",
                language="ru",
                response_format="text"
            )
        if os.path.exists(filename): os.remove(filename)
            
        user_text = str(transcription).strip()
        if not user_text:
            bot.reply_to(message, "Не смог распознать звук.")
            return
            
        bot.reply_to(message, f"🗣️ *Текст:* {user_text}", parse_mode='Markdown')
        run_legal_analysis(message, user_text)
    except Exception as e:
        bot.reply_to(message, f"⚠️ Ошибка голосового ввода: {str(e)}")

# 6. ЗАПУСК ВСЕЙ СИСТЕМЫ 
init_db()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(send_daily_push_notifications, 'interval', minutes=1)
scheduler.add_job(smart_ping_render, 'interval', minutes=10)
scheduler.start()

print("🚀 Робот готов. Подключена база PostgreSQL + Tavily Search API.")

@app.on_event("startup")
def start_bot_polling():
    print("🤖 Запуск Telegram бот пуллинга в безопасном режиме...")
    threading.Thread(target=bot.infinity_polling, kwargs={"skip_pending": True}, daemon=True).start()
