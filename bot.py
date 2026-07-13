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

# Работа с PostgreSQL
from sqlalchemy import create_engine, text

# Веб-сервер и работа с файлами
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

# 1. ТОКЕНЫ И НАСТРОЙКА
TELEGRAM_TOKEN = "8811867508:AAFxcE58OJbSbt9lmZHRFcpayMYfOE0AXLI"
PART1 = "gsk_xzHKSXzDAGqaXlkDN"
PART2 = "aruWGdyb3FYHLzc9L0QEclH8aW2ZGrMi3Ye"
GROQ_API_KEY = PART1 + PART2
DATABASE_URL = "postgresql://admin:qmoBE1mBhoi4ANcFHBs8du2Jw3hSql3g@dpg-d97s2pnavr4c73di73hg-a/ruleguard"
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

# 2. РАБОТА С БАЗОЙ ДАННЫХ
def init_db():
    with engine.begin() as conn:
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

def save_user_data_extended(user_id, username=None, business=None, country=None, location=None, legal_form=None, push_time=None, timezone=None):
    with engine.begin() as conn:
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

# 3. УМНЫЙ РОУТЕР GROQ (Оптимизация токенов и каскадный фоллбэк)
def safe_groq_request(messages, temperature=0.3, max_tokens=None, is_dispatcher=False):
    # Диспетчеру не нужна тяжелая модель 70b, используем быструю и экономную
    if is_dispatcher:
        primary_model = "llama-3.1-8b-instant"
        fallback_model = "llama-3.1-8b-instant"
    else:
        primary_model = "llama-3.3-70b-versatile"
        fallback_model = "llama-3.1-8b-instant" # Резерв на случай 429 ошибки
        
    kwargs = {"model": primary_model, "messages": messages, "temperature": temperature}
    if max_tokens: kwargs["max_tokens"] = max_tokens
        
    try:
        completion = groq_client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content
    except Exception as e:
        # Если словили 429, прозрачно для пользователя переключаемся на легкую модель
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
        
        # Используем диспетчера для экономии
        decision = safe_groq_request(messages, temperature=0.0, max_tokens=5, is_dispatcher=True)
        return "SEARCH" in decision.strip().upper()
    except Exception:
        return True

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
    history_messages = get_recent_chat_history(user_id, limit=4) # Уменьшили историю с 6 до 4 для экономии
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
        reply = get_legal_chat_reply(user_id, text_msg)
        return {"status": "success", "reply": reply, "report": reply}
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
        
        save_user_data_extended(user_id, username, details, country, location, legal_form, push_time, user_tz)
        if not details and not location: return {"status": "success"}

        compiled_input = f"{country or ''} {location or ''} {legal_form or ''} {details or ''}"
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
            user_res = conn.execute(text("SELECT push_time, timezone FROM users WHERE user_id = :user_id"), {"user_id": user_id})
            user_row = user_res.fetchone()
            push_time = user_row[0] if user_row and user_row[0] else "09:00"
            user_tz_str = user_row[1] if user_row and user_row[1] else "UTC"
            
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
        return {"status": "success", "push_time": push_time, "history": history}
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
        
        try: 
            bot.send_message(user_id, f"📋 **Результаты экспресс-аудита документа (из приложения):**\n\n{report}", parse_mode='Markdown')
        except Exception:
            bot.send_message(user_id, f"📋 Результаты экспресс-аудита документа (из приложения):\n\n{report}")
        
        return {"status": "success", "report": report, "reply": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/webapp/analyze-voice")
async def handle_webapp_voice(user_id: int, file: UploadFile = File(...)):
    try:
        content = await file.read()
        filename = f"webapp_voice_{user_id}.ogg"
        with open(filename, 'wb') as f:
            f.write(content)
            
        with open(filename, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, audio_file.read()), model="whisper-large-v3", language="ru", response_format="text"
            )
        if os.path.exists(filename):
            os.remove(filename)
            
        user_text = getattr(transcription, 'text', str(transcription)).strip()
        if not user_text:
            return {"status": "error", "message": "Не удалось распознать речь."}
            
        reply = get_legal_chat_reply(user_id, user_text)
        return {"status": "success", "user_text": user_text, "reply": reply, "report": reply}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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

# =====================================================================
# ПЛАНИРОВЩИК И АНТИ-СОН
# =====================================================================
def send_daily_push_notifications():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT user_id, user_name, business_description, location, push_time, timezone, country, legal_form FROM users"))
            all_users = result.fetchall()
        
        for user in all_users:
            user_id, username, business, location, push_time, user_tz, country, legal_form = user
            if not location or not business: continue
            if not user_tz: user_tz = 'UTC'
                
            tz = pytz.timezone(user_tz)
            if datetime.now(tz).strftime("%H:%M") == push_time:
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
    if message.text == "🔄 Повторить последний анализ":
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
    else:
        run_legal_analysis(message, message.text)

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
