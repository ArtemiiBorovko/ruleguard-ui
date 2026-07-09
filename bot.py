import telebot
import sqlite3
import os
import json
import requests
import threading
from groq import Groq
from duckduckgo_search import DDGS
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# Библиотеки для веб-сервера
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# 1. ТОКЕНЫ И НАСТРОЙКА
TELEGRAM_TOKEN = "8811867508:AAFxcE58OJbSbt9lmZHRFcpayMYfOE0AXLI"
GROQ_API_KEY = "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"

# Ссылка на твое будущее приложение на Render (мы впишем её сюда после создания на Render)
RENDER_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "https://ruleguard-backend.onrender.com")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

# Инициализируем веб-сервер
app = FastAPI()

# Разрешаем твоему сайту на GitHub Pages общаться с сервером без блокировок
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. РАБОТА С БАЗОЙ ДАННЫХ
def init_db():
    conn = sqlite3.connect('ruleguard.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            user_name TEXT, 
            business_description TEXT, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    additional_columns = {
        "push_time": "TEXT DEFAULT '09:00'",
        "country": "TEXT",
        "location": "TEXT",
        "legal_form": "TEXT",
        "last_report": "TEXT"
    }
    
    for col_name, col_type in additional_columns.items():
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass
            
    conn.commit()
    conn.close()

def save_user_data_extended(user_id, username=None, business=None, country=None, location=None, legal_form=None, push_time=None):
    conn = sqlite3.connect('ruleguard.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_name, business_description, country, location, legal_form, push_time FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row:
        c_name = username if username else row[0]
        c_bus = business if business else row[1]
        c_country = country if country else row[2]
        c_loc = location if location else row[3]
        c_form = legal_form if legal_form else row[4]
        c_push = push_time if push_time else row[5]
        
        cursor.execute('''
            UPDATE users 
            SET user_name = ?, business_description = ?, country = ?, location = ?, legal_form = ?, push_time = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (c_name, c_bus, c_country, c_loc, c_form, c_push, user_id))
    else:
        cursor.execute('''
            INSERT INTO users (user_id, user_name, business_description, country, location, legal_form, push_time) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, business, country, location, legal_form, push_time or '09:00'))
        
    conn.commit()
    conn.close()

def save_user_data(user_id, username=None, business=None):
    save_user_data_extended(user_id, username=username, business=business)

def get_user_context(user_id):
    conn = sqlite3.connect('ruleguard.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_name, business_description, country, location, legal_form FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row: 
        return f"Пользователь: {row[0] or 'Не указано'}. Страна: {row[2] or 'Не указано'}, Регион: {row[3] or 'Не указано'}, ОПФ: {row[4] or 'Не указано'}. Специфика: {row[1] or 'Не указано'}."
    return "Новый пользователь."

# 3. ПОИСК В ИНТЕРНЕТЕ
def search_internet(query):
    try:
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=3)]
            if results:
                context = "\n".join([f"Источник: {r['href']}\nТекст: {r['body']}" for r in results])
                return context
    except Exception as e:
        print(f"Ошибка поиска: {e}")
    return "Не удалось найти свежие данные в сети."

# 4. ЯДРО АНАЛИЗА (ГЕНЕРАЦИЯ ОТЧЕТА)
def generate_report_logic(user_id, current_input_text):
    """Вынесенная чистая логика ИИ-анализа для использования и в боте, и на веб-сайте"""
    user_memory = get_user_context(user_id)
    search_query = f"юридические риски штрафы законы 2026 {current_input_text}"
    web_data = search_internet(search_query)

    system_instruction = (
        "Ты — профессиональный ИИ-юрист RuleGuard. Твоя цель — защитить бизнес пользователя.\n"
        "Тебе предоставлены свежие результаты поиска из интернета. На основе этих данных "
        "выдели 2-3 главных риска и предложи легальные пути их обхода. Отвечай на русском языке, структурировано."
    )
    
    full_prompt = (
        f"Данные из базы о юзере: {user_memory}\n"
        f"АКТУАЛЬНЫЕ DАННЫЕ ИЗ ИНТЕРНЕТА:\n{web_data}\n\n"
        f"Запрос пользователя: {current_input_text}"
    )
    
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": full_prompt}
        ],
        temperature=0.3
    )
    bot_response = completion.choices[0].message.content
    
    # Сохраняем готовый отчет в БД для Архива
    try:
        conn = sqlite3.connect('ruleguard.db')
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_report = ? WHERE user_id = ?", (bot_response, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка сохранения отчета в БД: {e}")
        
    return bot_response

def run_legal_analysis(message, current_input_text):
    """Анализ для обычных сообщений в чате бота"""
    bot.send_chat_action(message.chat.id, 'typing')
    user_id = message.from_user.id
    telegram_name = message.from_user.first_name or "Пользователь"
    
    if "как меня зовут" in current_input_text.lower() or "помнишь" in current_input_text.lower():
        user_memory = get_user_context(user_id)
        bot.reply_to(message, f"Я отлично помню тебя, {telegram_name}! Твой профиль: {user_memory}")
        return

    try:
        bot_response = generate_report_logic(user_id, current_input_text)
        if len(current_input_text) > 15:
            save_user_data(user_id, username=telegram_name, business=current_input_text)
        bot.reply_to(message, bot_response)
    except Exception as e:
        bot.reply_to(message, f"⚠️ Ошибка ИИ Groq: {str(e)}")

# =====================================================================
# НОВОЕ: СЕРВЕРНЫЕ ЭНДПОИНТЫ ДЛЯ МИНИ-ПРИЛОЖЕНИЯ (БЕЗ ЗАКРЫТИЯ ОКНА)
# =====================================================================
@app.get("/")
def read_root():
    return {"status": "online", "project": "RuleGuard AI Backend"}

@app.post("/api/analyze")
async def handle_web_analysis(request: Request):
    """Сайт будет вызывать этот адрес. Окно приложения останется открытым!"""
    try:
        data = await request.json()
        user_id = int(data.get('user_id'))
        username = data.get('username', 'Предприниматель')
        country = data.get('country', 'Не указано')
        location = data.get('location', 'Не указано')
        legal_form = data.get('legal_form', 'Не указано')
        details = data.get('business_details', 'Не указано')
        push_time = data.get('push_time', '09:00')
        
        compiled_input = f"Страна: {country}, Локация: {location}. Форма: {legal_form}. Детали: {details}"
        
        # 1. Сохраняем расширенные данные в БД
        save_user_data_extended(user_id, username, details, country, location, legal_form, push_time)
        
        # 2. Запускаем тяжелый анализ ИИ
        report = generate_report_logic(user_id, compiled_input)
        
        # 3. Отправляем копию в чат Телеграма для удобства
        flag = "🇺🇸" if country == "USA" else "🇷🇺" if country == "Russia" else "🌐"
        safe_report = report.replace("<", "&lt;").replace(">", "&gt;")
        bot.send_message(user_id, f"{flag} <b>Новый анализ из приложения</b>\n\n{safe_report}", parse_mode='HTML')
        
        # 4. Возвращаем отчет прямо на экран Mini App!
        return {"status": "success", "report": report}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =====================================================================
# ПЛАНИРОВЩИК: ПУШИ И УМНЫЙ ХАК ПРОТИВ УХОДА RENDER В СОН
# =====================================================================
def send_daily_push_notifications():
    current_time_str = datetime.now().strftime("%H:%M")
    try:
        conn = sqlite3.connect('ruleguard.db')
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, user_name, business_description, country, location, legal_form FROM users WHERE push_time = ?", (current_time_str,))
        users_to_alert = cursor.fetchall()
        conn.close()
        
        if not users_to_alert:
            return
            
        print(f"⏰ Найдено пользователей для пуша: {len(users_to_alert)}")
        for user in users_to_alert:
            user_id, username, business, country, location, legal_form = user
            if not location or not business: continue
                
            search_query = f"юридические изменения законы риски 2026 {location} {business}"
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
                    {"role": "user", "content": completion}
                ],
                temperature=0.4
            )
            push_text = completion.choices[0].message.content
            bot.send_message(user_id, f"🛡️ *Ежедневный RuleGuard Радар*\n\n{push_text}", parse_mode="Markdown")
            
    except Exception as e:
        print(f"Ошибка планировщика пушей: {e}")

def smart_ping_render():
    """ХАК: Пингуем себя с 07:00 до 22:00, чтобы сэкономить часы бесплатного тарифа"""
    current_hour = datetime.now().hour
    
    if 7 <= current_hour < 22:
        try:
            print(f"⏰ [Пинг] Время {datetime.now().strftime('%H:%M')}. Держим Render бодрствующим...")
            response = requests.get(RENDER_APP_URL, timeout=10)
            print(f"ℹ️ [Пинг] Ответ сервера: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Ошибка автопина: {e}")
    else:
        print(f"🌙 [Пинг] Время {datetime.now().strftime('%H:%M')}. Ночной режим: позволяем Render уснуть.")

# 5. ОБРАБОТЧИКИ ТЕЛЕГРАМ БОТА
@bot.message_handler(commands=['start'])
def send_welcome(message):
    save_user_data(message.from_user.id, username=message.from_user.first_name)
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    web_app_info = telebot.types.WebAppInfo("https://artemiiborovko.github.io/ruleguard-ui/")
    web_app_button = telebot.types.KeyboardButton(text="🚀 Открыть анкету RuleGuard", web_app=web_app_info)
    markup.add(web_app_button)
    
    welcome_text = (
        f"🛡️ **Привет, {message.from_user.first_name}! Бот RuleGuard перешел на серверную архитектуру.**\n\n"
        "Открой приложение кнопкой ниже — теперь оно работает без закрытия окон!"
    )
    bot.reply_to(message, welcome_text, reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(content_types=['text'])
def handle_text(message):
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
        bot.reply_to(message, f"⚠️ Ошибка: {str(e)}")

# Оставляем старый метод для подстраховки, если кто-то нажмет кнопку в старой версии сайта
@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    bot.reply_to(message, "⚠️ Пожалуйста, обновите приложение (Ctrl+F5). Переходим на серверный анализ!")

# 6. ЗАПУСК ВСЕЙ СИСТЕМЫ
init_db()

# Запускаем фоновый планировщик задач
scheduler = BackgroundScheduler(daemon=True)
# Проверка пушей каждую минуту
scheduler.add_job(send_daily_push_notifications, 'interval', minutes=1)
# Умный пинг Render каждые 10 минут
scheduler.add_job(smart_ping_render, 'interval', minutes=10)
scheduler.start()

print("🚀 Робот готов. Фоновые задачи (Пуши и Дневной Автопинг) запущены.")

# Для работы на Render нам нужен объект FastAPI, запускаемый через uvicorn
# Поэтому infinity_polling убираем в отдельный фоновый поток
threading.Thread(target=bot.infinity_polling, daemon=True).start()
