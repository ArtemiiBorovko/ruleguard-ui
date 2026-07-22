import telebot
import os
import json
import requests
import threading
import pytz
from groq import Groq
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import pypdf
import docx2txt

from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends, HTTPException, status
import secrets

# Работа с PostgreSQL
from sqlalchemy import create_engine, text

# Веб-сервер и работа с файлами
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

# 1. ТОКЕНЫ И НАСТРОЙКА (Безопасное чтение из переменных окружения)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
RENDER_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "https://ruleguard-backend.onrender.com")

# Простая проверка на старте, чтобы сразу увидеть, если забыли указать переменную
if not all([TELEGRAM_TOKEN, GROQ_API_KEY, DATABASE_URL, TAVILY_API_KEY]):
    print("⚠️ ВНИМАНИЕ: Не все переменные окружения настроены на сервере!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)
engine = create_engine(DATABASE_URL)
app = FastAPI()

security = HTTPBasic()

def get_current_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "artemiiborovko")
    correct_password = secrets.compare_digest(credentials.password, "N5oXxMAhdw")
    
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. РАБОТА С БАЗОЙ ДАННЫХ
def init_db():
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                user_name TEXT, 
                business_description TEXT, 
                push_time TEXT DEFAULT '09:00',
                push_frequency TEXT DEFAULT 'daily',
                push_days TEXT DEFAULT 'everyday', -- <--- ДОБАВЬ ЭТУ СТРОЧКУ
                country TEXT,
                location TEXT,
                legal_form TEXT,
                tax_system TEXT,
                employee_count INT,
                has_ip_rights BOOLEAN,
                online_sales BOOLEAN,
                annual_turnover_bracket TEXT,
                main_risk_zones TEXT,
                timezone TEXT DEFAULT 'UTC',
                last_push_date DATE,
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
                role TEXT, 
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS tavily_cache (
                id SERIAL PRIMARY KEY,
                query_hash TEXT UNIQUE,
                search_result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))

    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN push_frequency TEXT DEFAULT 'daily';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN tax_system TEXT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN employee_count INT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN has_ip_rights BOOLEAN;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN online_sales BOOLEAN;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN annual_turnover_bracket TEXT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN main_risk_zones TEXT;"))
    except Exception:
        pass

def save_user_data_extended(user_id, username=None, business=None, country=None, location=None, legal_form=None, push_time=None, timezone=None, tax_system=None, employee_count=None, has_ip_rights=None, online_sales=None, annual_turnover_bracket=None, main_risk_zones=None, push_frequency=None, push_days=None):
    with engine.begin() as conn:
        # Безопасно добавляем недостающие колонки в базу, если их там физически еще нет
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS push_frequency TEXT DEFAULT 'daily';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS push_days TEXT DEFAULT 'everyday';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS tax_system TEXT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_count INT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_ip_rights BOOLEAN;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS online_sales BOOLEAN;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS annual_turnover_bracket TEXT;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS main_risk_zones TEXT;"))
        except Exception:
            pass

        # Делаем выборку существующих данных пользователя
        result = conn.execute(text("""
            SELECT user_name, business_description, country, location, legal_form, 
                   push_time, timezone, tax_system, employee_count, has_ip_rights, 
                   online_sales, annual_turnover_bracket, main_risk_zones, 
                   push_frequency, push_days 
            FROM users WHERE user_id = :user_id
        """), {"user_id": user_id})
        row = result.fetchone()
        
        # Корректно обрабатываем дни недели (если пришел список с галочками типа ['mon', 'wed'], превращаем в строку, иначе сохраняем как есть)
        if isinstance(push_days, list):
            formatted_push_days = ",".join(push_days)
        else:
            formatted_push_days = push_days

        # Обрабатываем зоны риска
        c_risks = json.dumps(main_risk_zones) if isinstance(main_risk_zones, list) else main_risk_zones

        if row:
            # Если пользователь уже есть в базе, подтягиваем старые данные, если новые не переданы
            c_name = username if username is not None else row[0]
            c_bus = business if business is not None else row[1]
            c_country = country if country is not None else row[2]
            c_loc = location if location is not None else row[3]
            c_form = legal_form if legal_form is not None else row[4]
            c_push = push_time if push_time is not None else row[5]
            c_tz = timezone if timezone is not None else (row[6] if row[6] else 'UTC')
            c_tax = tax_system if tax_system is not None else row[7]
            c_emp = employee_count if employee_count is not None else row[8]
            c_ip = has_ip_rights if has_ip_rights is not None else row[9]
            c_online = online_sales if online_sales is not None else row[10]
            c_turnover = annual_turnover_bracket if annual_turnover_bracket is not None else row[11]
            c_risk_val = c_risks if c_risks is not None else row[12]
            c_freq = push_frequency if push_frequency is not None else (row[13] if row[13] else 'daily')
            c_p_days = formatted_push_days if formatted_push_days is not None else (row[14] if row[14] else 'everyday')
            
            conn.execute(text('''
                UPDATE users 
                SET user_name = :name, business_description = :bus, country = :country, location = :loc, 
                    legal_form = :form, push_time = :push, timezone = :tz, tax_system = :tax, employee_count = :emp, 
                    has_ip_rights = :ip, online_sales = :online, annual_turnover_bracket = :turnover, main_risk_zones = :risks, 
                    push_frequency = :freq, push_days = :p_days,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = :user_id
            '''), {
                "name": c_name, "bus": c_bus, "country": c_country, "loc": c_loc, "form": c_form, 
                "push": c_push, "tz": c_tz, "tax": c_tax, "emp": c_emp, "ip": c_ip, "online": c_online, 
                "turnover": c_turnover, "risks": c_risk_val, "freq": c_freq, "p_days": c_p_days, "user_id": user_id
            })
        else:
            # Если пользователя нет, создаем новую запись с дефолтными значениями
            conn.execute(text('''
                INSERT INTO users (
                    user_id, user_name, business_description, country, location, legal_form, 
                    push_time, timezone, tax_system, employee_count, has_ip_rights, 
                    online_sales, annual_turnover_bracket, main_risk_zones, push_frequency, push_days
                ) 
                VALUES (
                    :user_id, :name, :bus, :country, :loc, :form, 
                    :push, :tz, :tax, :emp, :ip, 
                    :online, :turnover, :risks, :freq, :p_days
                )
            '''), {
                "user_id": user_id, 
                "name": username or "Предприниматель", 
                "bus": business or "Не указано", 
                "country": country or "Не указано", 
                "loc": location or "Не указано", 
                "form": legal_form or "Не указано", 
                "push": push_time or '09:00', 
                "tz": timezone or 'UTC',
                "tax": tax_system, 
                "emp": employee_count, 
                "ip": has_ip_rights, 
                "online": online_sales, 
                "turnover": annual_turnover_bracket, 
                "risks": c_risks,
                "freq": push_frequency or 'daily',
                "p_days": formatted_push_days or 'everyday'
            })

def save_user_data(user_id, username=None, business=None):
    save_user_data_extended(user_id, username=username, business=business)

def get_user_context(user_id):
    with engine.connect() as conn:
        result = conn.execute(text("SELECT user_name, business_description, country, location, legal_form, tax_system, employee_count, has_ip_rights, online_sales, annual_turnover_bracket, main_risk_zones FROM users WHERE user_id = :user_id"), {"user_id": user_id})
        row = result.fetchone()
    if row: 
        return f"Пользователь: {row[0] or 'Не указано'}. Страна: {row[2] or 'Не указано'}, Регион: {row[3] or 'Не указано'}, ОПФ: {row[4] or 'Не указано'}. Специфика бизнеса: {row[1] or 'Не указано'}. Система налогообложения: {row[5] or 'Не указано'}. Сотрудников: {row[6] or 'Не указано'}. Права на ИС: {row[7]}, Онлайн-продажи: {row[8]}, Оборот: {row[9] or 'Не указано'}. Зоны риска: {row[10] or 'Не указано'}."
    return "Новый пользователь без настроенного профиля."

def save_report_to_archive(user_id, input_text, report_text):
    try:
        with engine.begin() as conn:
            conn.execute(text('''
                INSERT INTO reports (user_id, input_text, report_text)
                VALUES (:user_id, :input_text, :report_text)
            '''), {"user_id": user_id, "input_text": input_text, "report_text": report_text})
    except Exception as e:
        print(f"Ошибка сохранения отчета: {e}")

def save_chat_message(user_id, role, text_msg):
    try:
        with engine.begin() as conn:
            conn.execute(text('''
                INSERT INTO chat_history (user_id, role, message_text)
                VALUES (:user_id, :role, :message_text)
            '''), {"user_id": user_id, "role": role, "message_text": text_msg})
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

# 3. УМНЫЙ РОУТЕР GROQ И ИНТЕЛЛЕКТУАЛЬНЫЙ ПАРСЕР КОМАНД СПРИНТА 3
def safe_groq_request(messages, temperature=0.3, max_tokens=None, is_dispatcher=False):
    if is_dispatcher:
        primary_model = "llama-3.1-8b-instant"
        fallback_model = "llama-3.1-8b-instant"
    else:
        primary_model = "llama-3.3-70b-versatile"
        fallback_model = "llama-3.1-8b-instant"
        
    kwargs = {"model": primary_model, "messages": messages, "temperature": temperature}
    if max_tokens: kwargs["max_tokens"] = max_tokens
        
    try:
        completion = groq_client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content
    except Exception as e:
        if "429" in str(e) or "rate_limit" in str(e):
            print(f"⚠️ Лимит {primary_model} исчерпан. Экстренный переход на {fallback_model}...")
            kwargs["model"] = fallback_model
            completion = groq_client.chat.completions.create(**kwargs)
            return completion.choices[0].message.content
        else:
            raise e

def check_if_search_needed(history, current_input):
    system_prompt = (
        "Ты — технический диспетчер системы RuleGuard. Твоя задача — определить, "
        "нужен ли глубокий поиск в актуальном интернете (Tavily API) для ответа на вопрос.\n"
        "Ответь строго ОДНИМ словом: 'SEARCH', если пользователь просит найти новые законы, "
        "актуальные штрафы, свежие новости по локации.\n"
        "Ответь строго ОДНИМ словом: 'DIALOG', если вопрос — это уточнение прошлого отчета, "
        "обычное рассуждение, приветствие или продолжение текущей беседы."
    )
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-2:]: 
            messages.append(msg)
        messages.append({"role": "user", "content": f"Вопрос пользователя: {current_input}"})
        
        decision = safe_groq_request(messages, temperature=0.0, max_tokens=5, is_dispatcher=True)
        return "SEARCH" in decision.strip().upper()
    except Exception:
        return True

# --- СПРИНТ 3: Интеллектуальный парсер ИИ-команд из текста/голоса ---
def parse_and_apply_ai_intent(user_id, text_input):
    system_prompt = (
        "Ты — анализатор намерений пользователя в приложении RuleGuard.\n"
        "Проанализируй текст и определи, хочет ли пользователь изменить настройки интерфейса или расписания уведомлений:\n"
        "1. Тема интерфейса: если просит включить светлую тему, белый интерфейс -> theme: 'light'. Если темную -> theme: 'dark'.\n"
        "2. Время пушей: если просит ставить уведомления/пуши на определенное время (например, на 13:00) -> push_time: 'HH:MM'.\n"
        "3. Дни пушей / Частота: если указывает дни недели (например, понедельник, среда, суббота) или 'каждый день', 'раз в месяц' -> push_frequency: укажи дни или расписание.\n\n"
        "Верни результат СТРОГО в формате JSON без лишнего текста и без markdown-оформления:\n"
        "{\n"
        "  \"action\": \"settings_updated\" или \"none\",\n"
        "  \"theme\": \"light\" или \"dark\" или null,\n"
        "  \"push_time\": \"HH:MM\" или null,\n"
        "  \"push_frequency\": \"...\" или null\n"
        "}"
    )
    try:
        response = safe_groq_request(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": text_input}],
            temperature=0.0, max_tokens=200, is_dispatcher=True
        )
        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        data = json.loads(cleaned.strip())
        
        action = data.get("action", "none")
        if action == "settings_updated":
            theme = data.get("theme")
            push_time = data.get("push_time")
            push_frequency = data.get("push_frequency")
            
            with engine.begin() as conn:
                if push_time:
                    conn.execute(text("UPDATE users SET push_time = :pt WHERE user_id = :uid"), {"pt": push_time, "uid": user_id})
                if push_frequency:
                    conn.execute(text("UPDATE users SET push_frequency = :pf WHERE user_id = :uid"), {"pf": push_frequency, "uid": user_id})

            return {
                "action": action,
                "updated_fields": {
                    "theme": theme,
                    "push_time": push_time,
                    "push_frequency": push_frequency
                }
            }
    except Exception as e:
        print(f"⚠️ Ошибка парсинга интента Спринта 3: {e}")
    
    return {"action": "none", "updated_fields": {}}

# 4. ПОИСК В ИНТЕРНЕТЕ
def search_internet(query):
    clean_query = query.lower()
    for trash in ["новый пользователь без настроенного профиля.", "вопрос:", "юридические риски штрафы законы актуальное", 
                  "изменения законы штрафы регуляция", "страна:", "локация:", "форма:", "детали:", "регион:", "опф:", "специфика бизнеса:"]:
        clean_query = clean_query.replace(trash, "")
    
    for char in [".", ",", ";", ":", "!", "?", "-", "_"]:
        clean_query = clean_query.replace(char, " ")
        
    clean_query = " ".join(clean_query.split()).strip()
    
    if len(clean_query) < 4:
        return "Недостаточно данных для интернет-поиска."

    try:
        with engine.connect() as conn:
            res = conn.execute(text('''
                SELECT search_result FROM tavily_cache 
                WHERE query_hash = :q AND created_at > CURRENT_TIMESTAMP - INTERVAL '30 minutes'
            '''), {"q": clean_query})
            row = res.fetchone()
            if row:
                return row[0]
    except Exception as e:
        print(f"Ошибка проверки кэша: {e}")

    try:
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": clean_query,
            "search_depth": "basic",
            "max_results": 3
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post("https://api.tavily.com/search", json=payload, headers=headers, timeout=15)
        
        if response.status_code == 200:
            results = response.json().get("results", [])
            if results:
                context = "\n".join([f"Источник: {r['url']}\nТекст: {r['content']}" for r in results])
                try:
                    with engine.begin() as conn:
                        conn.execute(text('''
                            INSERT INTO tavily_cache (query_hash, search_result) 
                            VALUES (:q, :res) ON CONFLICT (query_hash) 
                            DO UPDATE SET search_result = :res, created_at = CURRENT_TIMESTAMP
                        '''), {"q": clean_query, "res": context})
                except Exception as db_err:
                    print(f"Ошибка записи кэша: {db_err}")
                return context
    except Exception as e:
        print(f"❌ [Tavily] Ошибка API: {e}")
    return "Не удалось найти свежие нормативные данные в сети."

# 5. ЯДРО АНАЛИЗА БИЗНЕСА И ДИАЛОГОВ
def generate_report_logic(user_id, current_input_text):
    web_data = search_internet(current_input_text)

    system_instruction = (
        "Ты — профессиональный ИИ-юрист RuleGuard, защищающий бизнес от штрафов и проверок.\n"
        "Сделай глубокий анализ на основе предоставленных данных из сети на текущий момент.\n\n"
        "Твой ответ ДОЛЖЕН строго следовать следующей структуре:\n"
        "### 🔥 Главные юридические риски\n"
        "Выдели 2-3 критических риска. Опиши конкретные штрафы или санкции в цифрах, если они есть в контексте.\n\n"
        "### 🛡️ Инструкция по защите (Что проверить)\n"
        "Пошаговые легальные действия для предпринимателя, чтобы полностью себя обезопасить.\n\n"
        "### 📊 Уровень угрозы\n"
        "Напиши одну строчку: Низкий, Средний или Высокий, и кратко обоснуй почему.\n\n"
        "Отвечай уверенно, на русском языке, без лишней «воды»."
    )
    
    user_memory = get_user_context(user_id)
    full_prompt = (
        f"Контекст профиля: {user_memory}\n"
        f"АКТУАЛЬНЫЕ ДАННЫЕ СЕТИ ИЗ TAVILY API:\n{web_data}\n\n"
        f"Вводные данные для экспресс-анализа: {current_input_text}"
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": full_prompt}
    ]
    
    bot_response = safe_groq_request(messages, temperature=0.25)
    save_report_to_archive(user_id, current_input_text, bot_response)
    return bot_response

def get_legal_chat_reply(user_id, current_input_text):
    user_context = get_user_context(user_id)
    history_messages = get_recent_chat_history(user_id, limit=4)
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
        web_context = "Дополнительный веб-поиск не требовался."

    current_year = datetime.now().year
    system_instruction = (
        f"Ты — ИИ-юрист RuleGuard. Отвечай на вопросы пользователя в контексте его бизнеса.\n"
        f"Текущий год: {current_year}.\n"
        f"Данные бизнеса клиента: {user_context}\n"
        f"Свежие данные из сети (если запрашивались): {web_context}\n\n"
        "Отвечай коротко, по делу, понятным языком. Если пользователь просто здоровается или общается, поддерживай диалог. Пиши в увазительном тоне."
    )

    messages_payload = [{"role": "system", "content": system_instruction}]
    for msg in history_messages:
        messages_payload.append(msg)
    messages_payload.append({"role": "user", "content": current_input_text})

    bot_response = safe_groq_request(messages_payload, temperature=0.3)
    
    save_chat_message(user_id, "user", current_input_text)
    save_chat_message(user_id, "assistant", bot_response)
    return bot_response

def safe_reply_to(message, text_content):
    try:
        bot.reply_to(message, text_content, parse_mode='Markdown')
    except Exception:
        try:
            bot.reply_to(message, text_content)
        except Exception as e:
            print(f"Критическая ошибка отправки сообщения: {e}")

def run_legal_analysis(message, current_input_text):
    bot.send_chat_action(message.chat.id, 'typing')
    user_id = message.from_user.id
    try:
        bot_response = get_legal_chat_reply(user_id, current_input_text)
        save_chat_message(user_id, "user", current_input_text)
        save_chat_message(user_id, "assistant", bot_response)
        safe_reply_to(message, bot_response)
    except Exception as e:
        safe_reply_to(message, f"⚠️ Системная ошибка: {str(e)}")

# =====================================================================
# СЕРВЕРНЫЕ ЭНДПОИНТЫ ДЛЯ МИНИ-ПРИЛОЖЕНИЯ (WEBAPP)
# =====================================================================
@app.get("/")
def read_root():
    return {"status": "online"}

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        json_string = await request.body()
        update = telebot.types.Update.de_json(json_string.decode('utf-8'))
        bot.process_new_updates([update])
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка обработки вебхука: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/chat/history/{user_id}")
async def get_webapp_chat_history(user_id: int):
    try:
        history = get_recent_chat_history(user_id, limit=20)
        formatted = [{"role": m["role"], "message_text": m["content"]} for m in history]
        return {"status": "success", "history": formatted}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/chat/message")
async def handle_webapp_chat_message(request: Request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id'))
        text_msg = data.get('text', '').strip()
        if not text_msg: return {"status": "error", "message": "Empty text"}
        
        # Спринт 3: Проверяем команды изменения настроек через чат
        intent_result = parse_and_apply_ai_intent(user_id, text_msg)
        
        reply = get_legal_chat_reply(user_id, text_msg)
        
        # Дублируем ответ в Telegram, если чат вызван из WebApp
        flag = "🌐"
        try:
            bot.send_message(user_id, f"💬 <b>Сообщение из приложения:</b>\n{text_msg}\n\n🤖 <b>Ответ ИИ:</b>\n{reply}", parse_mode='HTML')
        except Exception:
            try:
                bot.send_message(user_id, f"💬 Сообщение из приложения:\n{text_msg}\n\n🤖 Ответ ИИ:\n{reply}")
            except:
                pass

        return {
            "status": "success", 
            "reply": reply, 
            "report": reply,
            "action": intent_result.get("action"),
            "updated_fields": intent_result.get("updated_fields")
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
        tax_system = data.get('tax_system', None)
        employee_count = data.get('employee_count', None)
        has_ip_rights = data.get('has_ip_rights', None)
        online_sales = data.get('online_sales', None)
        annual_turnover_bracket = data.get('annual_turnover_bracket', None)
        main_risk_zones = data.get('main_risk_zones', None)
        
        # Шаг 3 (исправленный): аккуратно достаем дни рассылки из пришедшего от фронтенда JSON
        raw_push_days = data.get('push_days', 'everyday')
        if isinstance(raw_push_days, list):
            push_days_str = ",".join(raw_push_days)  # если пришел список галочек, делаем строку через запятую
        else:
            push_days_str = str(raw_push_days)       # если пришла строка (например, 'everyday' или 'monthly'), оставляем как есть

        # Передаем все данные вместе с push_days в функцию сохранения
        save_user_data_extended(
            user_id, username=username, business=details, country=country, location=location, 
            legal_form=legal_form, push_time=push_time, timezone=user_tz, tax_system=tax_system, 
            employee_count=employee_count, has_ip_rights=has_ip_rights, online_sales=online_sales, 
            annual_turnover_bracket=annual_turnover_bracket, main_risk_zones=main_risk_zones,
            push_days=push_days_str
        )
        
        if not details and not location: return {"status": "success"}

        compiled_input = f"{country or ''} {location or ''} {legal_form or ''} {details or ''} Налог: {tax_system or ''}"
        report = generate_report_logic(user_id, compiled_input)
        
        flag = "🇺🇸" if country == "USA" else "🇷🇺" if country == "Russia" else "🌐"
        try:
            bot.send_message(user_id, f"{flag} <b>Новый анализ из приложения</b>\n\n{report}", parse_mode='HTML')
        except Exception:
            bot.send_message(user_id, f"{flag} Новый анализ из приложения\n\n{report}")
        return {"status": "success", "report": report, "reply": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/history/{user_id}")
async def get_user_history(user_id: int, tz: str = "UTC"):
    try:
        with engine.connect() as conn:
            user_res = conn.execute(text("SELECT push_time, timezone, push_frequency FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            user_row = user_res.fetchone()
            push_time = user_row[0] if user_row and user_row[0] else "09:00"
            user_tz_str = user_row[1] if user_row and user_row[1] else "UTC"
            push_frequency = user_row[2] if user_row and user_row[2] else "daily"
            
            try: tz_obj = pytz.timezone(user_tz_str)
            except: tz_obj = pytz.utc
            
            reports_res = conn.execute(text("SELECT input_text, report_text, created_at FROM reports WHERE user_id = :user_id ORDER BY created_at DESC"), {"user_id": user_id})
            history = []
            for row in reports_res.fetchall():
                utc_dt = row[2]
                if utc_dt.tzinfo is None: utc_dt = pytz.utc.localize(utc_dt)
                history.append({
                    "input_text": row[0],
                    "report_text": row[1],
                    "created_at": utc_dt.astimezone(tz_obj).strftime("%d.%m.%Y %H:%M")
                })
        return {"status": "success", "push_time": push_time, "push_frequency": push_frequency, "history": history}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/webapp/analyze-doc")
async def handle_webapp_doc(user_id: int, file: UploadFile = File(...)):
    try:
        file_name = file.filename.lower()
        if not (file_name.endswith('.pdf') or file_name.endswith('.docx')):
            return {"status": "error", "message": "Формат не поддерживается. Только PDF или DOCX."}
            
        content = await file.read()
        local_filename = f"webapp_doc_{user_id}_{file_name}"
        with open(local_filename, 'wb') as f:
            f.write(content)
            
        text_content = ""
        if file_name.endswith('.pdf'):
            with open(local_filename, 'rb') as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text: text_content += page_text + "\n"
        elif file_name.endswith('.docx'):
            text_content = docx2txt.process(local_filename)
            
        if os.path.exists(local_filename):
            os.remove(local_filename)
            
        text_content = text_content.strip()
        if len(text_content) < 50:
            return {"status": "error", "message": "Не удалось извлечь текст. Возможно, это скан-картинка."}
            
        if len(text_content) > 30000:
            text_content = text_content[:30000] + "\n\n...[Текст обрезан из-за ограничений размера]..."

        system_instruction = (
            "Ты — опытный ИИ-юрист корпоративного уровня RuleGuard. Проведи экспресс-аудит загруженного договора.\n"
            "Найди скрытые юридические ловушки, financial-риски, жесткие штрафы и кабальные условия.\n\n"
            "Сформируй ответ строго по этой структуре:\n"
            "### 🔎 Общий вердикт по документу\n\n### ⚠️ Кабальные условия и скрытые риски\n\n### 🛠️ Что потребовать изменить / Протокол разногласий"
        )
        user_context = get_user_context(user_id)
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Контекст компании клиента: {user_context}\n\nТЕКСТ ДОГОВОРА:\n{text_content}"}
        ]
        
        report = safe_groq_request(messages, temperature=0.2)
        
        save_chat_message(user_id, "user", f"[Документ: {file.filename}]")
        save_chat_message(user_id, "assistant", report)
        
        try: 
            bot.send_message(user_id, f"📋 <b>Результаты экспресс-аудита документа (из приложения):</b>\n\n{report}", parse_mode='HTML')
        except Exception:
            bot.send_message(user_id, f"📋 Результаты экспресс-аудита документа (из приложения):\n\n{report}")
        
        return {"status": "success", "report": report, "reply": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/webapp/analyze-voice")
async def handle_webapp_voice(user_id: int, file: UploadFile = File(...)):
    filename = f"webapp_voice_{user_id}.ogg"
    try:
        content = await file.read()
        with open(filename, 'wb') as f:
            f.write(content)
            
        with open(filename, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, audio_file.read()), model="whisper-large-v3", language="ru", response_format="text"
            )
            
        user_text = getattr(transcription, 'text', str(transcription)).strip()
        if not user_text:
            return {"status": "error", "message": "Не удалось распознать речь. Попробуйте еще раз."}
            
        # Спринт 3: Проверяем команды из аудиосообщения
        intent_result = parse_and_apply_ai_intent(user_id, user_text)
            
        reply = get_legal_chat_reply(user_id, user_text)
        
        try:
            bot.send_message(user_id, f"🗣️ <b>Голосовое из приложения:</b> {user_text}\n\n🤖 <b>Ответ:</b>\n{reply}", parse_mode='HTML')
        except:
            pass

        return {
            "status": "success", 
            "user_text": user_text, 
            "reply": reply, 
            "report": reply,
            "action": intent_result.get("action"),
            "updated_fields": intent_result.get("updated_fields")
        }
    except Exception as e:
        print(f"⚠️ Ошибка в analyze-voice: {e}")
        return {"status": "error", "message": f"Ошибка обработки голоса: {str(e)}"}
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass

@app.post("/api/reanalyze/{user_id}")
async def reanalyze(user_id: int):
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT country, location, legal_form, business_description
                FROM users
                WHERE user_id=:user_id
            """), {"user_id": user_id})
            row = result.fetchone()

        if not row:
            return {"status": "error", "message": "Пользователь не найден"}

        report = generate_report_logic(user_id, f"{row[0]} {row[1]} {row[2]} {row[3]}")
        return {"status": "success", "report": report, "reply": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# API для сбора статистики из базы
@app.get("/api/admin/stats")
def get_admin_stats(admin: str = Depends(get_current_admin)):
    try:
        with engine.connect() as conn:
            total_users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
            
            loc_res = conn.execute(text("""
                SELECT location, COUNT(*) as count 
                FROM users 
                WHERE location IS NOT NULL AND location != 'Не указано'
                GROUP BY location ORDER BY count DESC LIMIT 5
            """)).fetchall()
            locations = {"labels": [r[0] for r in loc_res], "data": [r[1] for r in loc_res]}
            
            push_res = conn.execute(text("""
                SELECT push_time, COUNT(*) as count 
                FROM users 
                GROUP BY push_time ORDER BY push_time
            """)).fetchall()
            pushes = {"labels": [r[0] for r in push_res], "data": [r[1] for r in push_res]}
            
            reports_count = conn.execute(text("SELECT COUNT(*) FROM reports")).scalar()
            chat_count = conn.execute(text("SELECT COUNT(*) FROM chat_history WHERE role = 'user'")).scalar()
            groq_requests = {"labels": ["Анализ бизнеса", "Диалоги в чате"], "data": [reports_count, chat_count]}

            users_query = text("""
                SELECT u.user_id, u.user_name, u.country, u.location, u.legal_form, u.business_description, u.push_time, u.timezone,
                       (SELECT COUNT(*) FROM reports r WHERE r.user_id = u.user_id) as reports_count,
                       (SELECT COUNT(*) FROM chat_history c WHERE c.user_id = u.user_id AND c.role = 'user') as chat_count
                FROM users u
            """)
            users_res = conn.execute(users_query).fetchall()
            users_details = []
            for row in users_res:
                users_details.append({
                    "user_id": row[0],
                    "user_name": row[1] or "Без имени",
                    "country": row[2] or "-",
                    "location": row[3] or "-",
                    "legal_form": row[4] or "-",
                    "business_description": row[5] or "-",
                    "push_time": row[6] or "-",
                    "timezone": row[7] or "-",
                    "reports_count": row[8],
                    "chat_count": row[9]
                })

        return {
            "status": "success",
            "total_users": total_users,
            "locations": locations,
            "pushes": pushes,
            "groq_requests": groq_requests,
            "users_details": users_details
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/admin", response_class=HTMLResponse)
def get_admin_dashboard(admin: str = Depends(get_current_admin)):
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>RuleGuard AI - Админ Панель</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #121212; color: #ffffff; margin: 0; padding: 20px; }
            .header { text-align: center; margin-bottom: 30px; display: flex; align-items: center; justify-content: center; gap: 15px; }
            .header h1 { margin: 0; color: #34C759; font-size: 2rem; }
            .logo-svg { width: 45px; height: 55px; }
            .logo-svg path { fill: #34C759; }
            .stat-box { background: #1e1e1e; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
            .stat-box h2 { font-size: 3rem; margin: 10px 0; color: #34C759; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 20px; }
            .chart-container { background: #1e1e1e; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
            .table-container { background: #1e1e1e; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); overflow-x: auto; }
            table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
            th, td { padding: 12px; border-bottom: 1px solid #2a2a2a; }
            th { color: #34C759; font-weight: 600; text-transform: uppercase; font-size: 12px; }
            tr:hover { background: #25252a; }
            .badge-report { background: #FF9800; color: #000; padding: 3px 8px; border-radius: 6px; font-weight: bold; }
            .badge-chat { background: #34C759; color: #000; padding: 3px 8px; border-radius: 6px; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="header">
            <svg class="logo-svg" viewBox="0 0 1185 1437" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path fill-rule="evenodd" clip-rule="evenodd" d="M591.223 0L74.8181 148.822C34.9697 159.499 0 199.712 0 245.253V776.295C0 1050.35 332.613 1319.88 591.223 1436.17C850.644 1310.93 1176.75 1069.4 1184.07 777.451V246.41C1184.07 204.935 1158.05 164.273 1109.25 148.822L591.223 0ZM398.956 839.257V1160C458.844 1210.78 519.1 1257.01 591.223 1289.79C667.667 1248.31 728.659 1210.91 804.29 1151.54L590.065 839.257H398.956ZM748.99 820.553L906 1051.5C973.557 972.557 1057.82 888.305 1055.36 777.117L1055.58 267.219L591.44 134.184L128.493 267.212V777.451C133.697 911.472 219.575 1014.37 269.995 1057.2V331.262L640.83 330.173C984.828 330.173 1001.09 732.724 748.99 820.553ZM398.704 710.766L398.486 458.664H657.094C840.072 458.664 834.597 710.766 657.312 710.766H398.704Z"/>
            </svg>
            <div>
                <h1>RuleGuard AI</h1>
                <p style="margin: 0; color: #8E8E93; font-size: 13px;">Панель мониторинга и аналитики аудитории</p>
            </div>
        </div>
        
        <div class="stat-box">
            <h3>Всего пользователей в системе</h3>
            <h2 id="totalUsers">...</h2>
        </div>

        <div class="grid">
            <div class="chart-container">
                <canvas id="locationsChart"></canvas>
            </div>
            <div class="chart-container">
                <canvas id="pushesChart"></canvas>
            </div>
            <div class="chart-container">
                <canvas id="groqChart"></canvas>
            </div>
        </div>

        <div class="table-container">
            <h3 style="margin-top: 0; color: #34C759;"><i class="fa-solid fa-users"></i> Детальные данные пользователей (Целевая аудитория)</h3>
            <table>
                <thead>
                    <tr>
                        <th>ID / Имя</th>
                        <th>Локация</th>
                        <th>Бизнес (Форма / Описание)</th>
                        <th>Пуш-время</th>
                        <th>Анализ бизнеса</th>
                        <th>Диалоги в чате</th>
                    </tr>
                </thead>
                <tbody id="usersTableBody">
                    <tr><td colspan="6" style="text-align: center; color: #8E8E93;">Загрузка данных...</td></tr>
                </tbody>
            </table>
        </div>

        <script>
            Chart.defaults.color = '#fff';
            
            async function loadData() {
                const response = await fetch('/api/admin/stats');
                const data = await response.json();
                
                document.getElementById('totalUsers').innerText = data.total_users;

                const tableBody = document.getElementById('usersTableBody');
                tableBody.innerHTML = '';
                if (data.users_details && data.users_details.length > 0) {
                    data.users_details.forEach(u => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td><b>${u.user_name}</b><br><span style="color: #8E8E93; font-size: 11px;">ID: ${u.user_id}</span></td>
                            <td>${u.country} / ${u.location}</td>
                            <td><b>${u.legal_form}</b><br><span style="color: #bbb; font-size: 12px;">${u.business_description}</span></td>
                            <td>${u.push_time} <span style="color:#8E8E93; font-size:11px;">(${u.timezone})</span></td>
                            <td><span class="badge-report">${u.reports_count}</span></td>
                            <td><span class="badge-chat">${u.chat_count}</span></td>
                        `;
                        tableBody.appendChild(tr);
                    });
                } else {
                    tableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: #8E8E93;">Нет зарегистрированных пользователей.</td></tr>`;
                }

                new Chart(document.getElementById('locationsChart'), {
                    type: 'bar',
                    data: {
                        labels: data.locations.labels,
                        datasets: [{ label: 'Пользователи по городам', data: data.locations.data, backgroundColor: '#34C759' }]
                    }
                });

                new Chart(document.getElementById('pushesChart'), {
                    type: 'line',
                    data: {
                        labels: data.pushes.labels,
                        datasets: [{ label: 'Время рассылки (UTC)', data: data.pushes.data, borderColor: '#30D158', tension: 0.3 }]
                    }
                });

                new Chart(document.getElementById('groqChart'), {
                    type: 'doughnut',
                    data: {
                        labels: data.groq_requests.labels,
                        datasets: [{ data: data.groq_requests.data, backgroundColor: ['#34C759', '#0A84FF'] }]
                    },
                    options: { plugins: { title: { display: true, text: 'Нагрузка на API (Использование)' } } }
                });
            }
            
            loadData();
        </script>
    </body>
    </html>
    """

# =====================================================================
# ПЛАНИРОВЩИК И АНТИ-СОН
# =====================================================================
def send_daily_push_notifications():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT user_id, user_name, business_description, location, push_time, timezone, country, legal_form, last_push_date FROM users"))
            all_users = result.fetchall()
        
        for user in all_users:
            user_id, username, business, location, push_time, user_tz, country, legal_form, last_push_date = user
            if not location or not business: continue
            if not user_tz: user_tz = 'UTC'
                
            tz = pytz.timezone(user_tz)
            now_tz = datetime.now(tz)
            today_date = now_tz.date()
            
            try:
                push_hour, push_minute = map(int, push_time.split(':'))
            except Exception:
                push_hour, push_minute = 9, 0
            
            if (now_tz.hour > push_hour or (now_tz.hour == push_hour and now_tz.minute >= push_minute)):
                if last_push_date != today_date:
                    search_query = f"{country or ''} {location or ''} {legal_form or ''} {business or ''}"
                    web_data = search_internet(search_query)
                    
                    messages = [
                        {"role": "system", "content": "Ты — ИИ-юрист RuleGuard. Напиши очень краткую сводку законов на сегодня (2-3 предложения)."},
                        {"role": "user", "content": f"Бизнес: {business}, Локация: {location}. Данные: {web_data}"}
                    ]
                    bot_response = safe_groq_request(messages, temperature=0.4)
                    
                    try:
                        bot.send_message(user_id, f"🛡️ <b>Ежедневный RuleGuard Радар</b>\n\n{bot_response}", parse_mode="HTML")
                    except Exception:
                        bot.send_message(user_id, f"🛡️ Ежедневный RuleGuard Радар\n\n{bot_response}")
                    
                    try:
                        with engine.begin() as update_conn:
                            update_conn.execute(
                                text("UPDATE users SET last_push_date = :today WHERE user_id = :uid"), 
                                {"today": today_date, "uid": user_id}
                            )
                    except Exception as db_err:
                        print(f"Ошибка обновления даты пуша для {user_id}: {db_err}")

    except Exception as e:
        print(f"Ошибка планировщика пушей: {e}")

def smart_ping_render():
    if 7 <= datetime.now().hour < 22:
        try: requests.get(RENDER_APP_URL, timeout=10)
        except: pass

# =====================================================================
# ОБРАБОТЧИКИ ДЛЯ ПРЯМОГО ПОТОКА ТЕЛЕГРАМ (ДИАЛОГ В ЧАТЕ)
# =====================================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    save_user_data(message.from_user.id, username=message.from_user.first_name)
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    web_app_info = telebot.types.WebAppInfo("https://artemiiborovko.github.io/ruleguard-ui/")
    markup.add(telebot.types.KeyboardButton(text="🚀 Открыть анкету RuleGuard", web_app=web_app_info))
    markup.add(telebot.types.KeyboardButton(text="🔄 Повторить последний анализ"))
    
    safe_reply_to(message, f"🛡️ **Привет, {message.from_user.first_name}!** Бот полностью активен. Вы можете общаться со мной здесь или открыть полноценное приложение.")

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    text_msg = message.text.strip()
    
    if text_msg == "🔄 Повторить последний анализ":
        bot.send_chat_action(message.chat.id, 'typing')
        with engine.connect() as conn:
            result = conn.execute(text("SELECT country, location, legal_form, business_description FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            row = result.fetchone()
            
        if not row or not row[3]:
            safe_reply_to(message, "📭 Заполните сначала анкету в приложении!")
            return
            
        safe_reply_to(message, "⏳ *Обновляю отчет через Tavily API...*")
        try:
            report = generate_report_logic(user_id, f"{row[0]} {row[1]} {row[2]} {row[3]}")
            safe_reply_to(message, f"🔄 **Свежий отчет:**\n\n{report}")
        except Exception as e:
            safe_reply_to(message, f"⚠️ Ошибка: {e}")
        return

    # 1. Сначала проверяем, не просит ли пользователь изменить настройки (время, дни, тему)
    intent_result = parse_and_apply_ai_intent(user_id, text_msg)
    if intent_result.get("action") == "settings_updated":
        fields = intent_result.get("updated_fields", {})
        updated_desc = []
        if fields.get("push_time"): updated_desc.append(f"время пушей: {fields['push_time']}")
        if fields.get("push_frequency"): updated_desc.append(f"расписание: {fields['push_frequency']}")
        if fields.get("theme"): updated_desc.append(f"тема: {fields['theme']}")
        
        response_text = "⚙️ Настройки успешно обновлены: " + ", ".join(updated_desc) if updated_desc else "⚙️ Настройки обновлены."
        safe_reply_to(message, response_text)
        return  # Прерываем выполнение, чтобы бот не уходил в юридический анализ обычного текста

    # 2. Если это обычный вопрос или обсуждение — отправляем юристу-аналитику
    run_legal_analysis(message, text_msg)

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    try:
        bot.send_chat_action(message.chat.id, 'record_audio')
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        filename = f"voice_{message.voice.file_id}.ogg"
        with open(filename, 'wb') as new_file: new_file.write(downloaded_file)
            
        with open(filename, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, audio_file.read()), model="whisper-large-v3", language="ru", response_format="text"
            )
        if os.path.exists(filename): os.remove(filename)
        
        user_text = getattr(transcription, 'text', str(transcription)).strip()
        if user_text:
            safe_reply_to(message, f"🗣️ *Текст вашего аудио:* {user_text}")
            run_legal_analysis(message, user_text)
    except Exception as e:
        safe_reply_to(message, f"⚠️ Ошибка обработки аудио: {str(e)}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    try:
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name.lower()
        
        if not (file_name.endswith('.pdf') or file_name.endswith('.docx')):
            safe_reply_to(message, "❌ Я принимаю только файлы в формате **PDF** или **DOCX**.")
            return
            
        bot.send_chat_action(message.chat.id, 'typing')
        safe_reply_to(message, "📥 *Скачиваю и изучаю документ...*")
        
        downloaded_file = bot.download_file(file_info.file_path)
        local_filename = f"doc_{message.document.file_id}_{file_name}"
        
        with open(local_filename, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        text_content = ""
        if file_name.endswith('.pdf'):
            with open(local_filename, 'rb') as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text: text_content += page_text + "\n"
        elif file_name.endswith('.docx'):
            text_content = docx2txt.process(local_filename)
            
        if os.path.exists(local_filename):
            os.remove(local_filename)
            
        text_content = text_content.strip()
        if len(text_content) < 50:
            safe_reply_to(message, "⚠️ Не удалось извлечь текст из документа.")
            return
            
        if len(text_content) > 30000:
            text_content = text_content[:30000] + "\n\n...[Текст обрезан]..."

        system_instruction = (
            "Ты — опытный ИИ-юрист корпоративного уровня RuleGuard. Проведи экспресс-аудит договора.\n"
            "Найди скрытые ловушки, финансовые риски, штрафы.\n\n"
            "Структура:\n### 🔎 Вердикт\n### ⚠️ Риски\n### 🛠️ Что изменить"
        )
        user_context = get_user_context(message.from_user.id)
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Контекст компании: {user_context}\n\nТЕКСТ:\n{text_content}"}
        ]
        
        report = safe_groq_request(messages, temperature=0.2)
        
        save_chat_message(message.from_user.id, "user", f"[Документ: {file_name}]")
        save_chat_message(message.from_user.id, "assistant", report)
        
        safe_reply_to(message, f"📋 **Результаты экспресс-аудита документа:**\n\n{report}")
    except Exception as e:
        safe_reply_to(message, f"⚠️ Ошибка при анализе документа: {str(e)}")

# 6. ЗАПУСК И НАСТРОЙКА ВЕБХУКА
init_db()
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(send_daily_push_notifications, 'interval', minutes=1)
scheduler.add_job(smart_ping_render, 'interval', minutes=10)
scheduler.start()

@app.on_event("startup")
def setup_webhook_on_startup():
    bot.remove_webhook()
    webhook_url = f"{RENDER_APP_URL}/api/telegram-webhook"
    bot.set_webhook(url=webhook_url)
    print(f"🚀 Роутер Вебхука успешно зарегистрирован на URL: {webhook_url}")
