from flask import Fl ask
from threading import Thread
import os

app_web = Flask(__name__)
@app_web.route('/')
def home(): return "I am alive"

def keep_alive():
    t = Thread(target=lambda: app_web.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080))))
    t.start()
import logging
import base64
import os
import sqlite3
import re
import requests
from datetime import timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

# --- الإعدادات ---
TOKEN = "7324911542:AAFqB9NRegwE2_bG5rCTaEWocbh8N3vgWeo"
MISTRAL_KEY = "EABRT5zGsHYhezkaJJomt15VR2iBrPWq"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
DB_NAME = "abood-gpt.db"

CANDLE_SPEEDS = ["S5", "S10", "S15", "S30", "M1", "M2", "M3", "M5", "M10", "M15", "M30", "H1", "H4", "D1"]
TRADE_TIMES = ["S3", "S15", "S30", "M1", "M3", "M5", "M30", "H1", "H4", "H24", "⏱️ وقت يدوي"]

# حالات المحادثة
MAIN_MENU, SETTINGS_CANDLE, SETTINGS_TIME, SETTINGS_MANUAL_TIME, CHAT_MODE, ANALYZE_MODE = range(6)

# --- قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            candle TEXT DEFAULT 'M5', 
            trade_time TEXT DEFAULT 'H1',
            manual_time TEXT DEFAULT '',
            chat_context TEXT DEFAULT ''
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_user_setting(user_id, col, val):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(f"INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cursor.execute(f"UPDATE users SET {col} = ? WHERE user_id = ?", (val, user_id))
    conn.commit()
    conn.close()

def get_user_setting(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT candle, trade_time, manual_time FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    if res:
        return res
    # إرجاع قيم افتراضية إذا لم يكن المستخدم موجوداً
    return ("M5", "H1", "")

# --- دوال معالجة الوقت اليدوي ---
def parse_manual_time(time_str):
    """تحويل النص المدخل إلى وقت بالتنسيق 00:00:00"""
    try:
        # تحقق من تنسيق HH:MM:SS
        if re.match(r'^\d{1,2}:\d{2}:\d{2}$', time_str):
            hours, minutes, seconds = map(int, time_str.split(':'))
            if 0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59:
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        # تحقق من عدد الأيام
        elif 'يوم' in time_str or 'يومين' in time_str or 'أيام' in time_str:
            days = 0
            if 'يومين' in time_str:
                days = 2
            elif 'يوم' in time_str:
                # استخراج الرقم من النص
                numbers = re.findall(r'\d+', time_str)
                if numbers:
                    days = int(numbers[0])
                else:
                    days = 1
            return f"{days} يوم"
        
        # تحقق من عدد الساعات
        elif 'ساعة' in time_str or 'ساعات' in time_str:
            hours = 0
            numbers = re.findall(r'\d+', time_str)
            if numbers:
                hours = int(numbers[0])
            else:
                hours = 1
            return f"{hours} ساعة"
        
        # تحقق من عدد الدقائق
        elif 'دقيقة' in time_str or 'دقائق' in time_str:
            minutes = 0
            numbers = re.findall(r'\d+', time_str)
            if numbers:
                minutes = int(numbers[0])
            else:
                minutes = 1
            return f"{minutes} دقيقة"
        
        # تحقق من عدد الثواني
        elif 'ثانية' in time_str or 'ثواني' in time_str:
            seconds = 0
            numbers = re.findall(r'\d+', time_str)
            if numbers:
                seconds = int(numbers[0])
            else:
                seconds = 1
            return f"{seconds} ثانية"
        
        # إذا كان رقم فقط، تعتبره ساعات
        elif time_str.isdigit():
            hours = int(time_str)
            return f"{hours} ساعة"
            
    except Exception as e:
        logging.error(f"Error parsing manual time: {e}")
    
    return None

def format_trade_time_for_prompt(trade_time, manual_time=""):
    """تنسيق وقت الصفقة للبرومبت"""
    if trade_time == "⏱️ وقت يدوي" and manual_time:
        return f"مدة الصفقة المتوقعة: {manual_time} (مدخل يدوي)"
    else:
        return f"مدة الصفقة المتوقعة: {trade_time}"

# --- معالجة الصور ---
def encode_image(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')

# --- دوال المساعدة للتعامل مع النصوص ---
def clean_repeated_text(text):
    """تنظيف النص من التكرارات"""
    # تقسيم النص إلى فقرات
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    
    # إزالة الفقرات المكررة
    unique_paragraphs = []
    seen_paragraphs = set()
    
    for paragraph in paragraphs:
        # اختصار الفقرة للتحقق من التكرار
        simplified = paragraph[:100].strip()
        if simplified not in seen_paragraphs:
            unique_paragraphs.append(paragraph)
            seen_paragraphs.add(simplified)
    
    # إعادة تجميع النص
    cleaned_text = '\n\n'.join(unique_paragraphs)
    
    # إذا كان النص طويلاً جداً، نأخذ فقط أول 2000 حرف تقريباً
    if len(cleaned_text) > 2000:
        # نبحث عن مكان جيد للقطع (بعد فقرة كاملة)
        if '\n\n' in cleaned_text[:2200]:
            cut_point = cleaned_text[:2200].rfind('\n\n')
            cleaned_text = cleaned_text[:cut_point]
        else:
            cleaned_text = cleaned_text[:2000] + "..."
    
    return cleaned_text

def split_message(text, max_length=4000):
    """تقسيم الرسالة الطويلة إلى أجزاء"""
    parts = []
    
    # إذا كان النص أقصر من الحد الأقصى، إرجاعه كما هو
    if len(text) <= max_length:
        return [text]
    
    # تقسيم النص مع الحفاظ على الفقرات
    while len(text) > max_length:
        # البحث عن آخر فاصل فقرات قبل الحد الأقصى
        split_point = text[:max_length].rfind('\n\n')
        if split_point == -1:
            split_point = text[:max_length].rfind('\n')
        if split_point == -1:
            split_point = max_length - 100  # فاصل طارئ
        
        # إضافة الجزء إلى القائمة
        parts.append(text[:split_point])
        text = text[split_point:].lstrip()
    
    # إضافة الجزء المتبقي
    if text:
        parts.append(text)
    
    return parts

# --- الدردشة مع Mistral ---
async def start_chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء وضع الدردشة"""
    keyboard = [
        ["ايقاف الدردشة"],
        ["الرجوع للقائمة الرئيسية"]
    ]
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="💬 **وضع الدردشة مع ABOOD GPT**\n\n"
             "يمكنك الآن الدردشة مع الذكاء الاصطناعي.\n"
             "أرسل رسالتك أو استخدم الأزرار أدناه:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
        parse_mode="Markdown"
    )
    return CHAT_MODE

async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة رسائل الدردشة"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    # التحقق من الأوامر الخاصة
    if user_message == "ايقاف الدردشة":
        main_keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "✅ تم إنهاء وضع الدردشة.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    elif user_message == "الرجوع للقائمة الرئيسية":
        main_keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "🏠 العودة للقائمة الرئيسية",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    # إظهار حالة المعالجة
    wait_msg = await update.message.reply_text("ABOOD GPT 🤔 ... ")
    
    try:
        # نظام برومبت آمن
        system_prompt = """ اسمك هو ABOOD GPT، وأنت مساعد ذكي مطور لتكون الشريك الفكري والمساعد التقني الأمثل. تتبع في أسلوبك القواعد التالية:
الشخصية: أنت ذكي، مبدع، وودود جداً. تتحدث بوضوح وتتجنب التعقيد غير المبرر.
الأسلوب: تستخدم التنسيق الجميل (عناوين، نقاط، وجداول) لتجعل إجابتك سهلة القراءة.
الذكاء: لا تكتفي بالإجابة المباشرة، بل فكر في "ما وراء السؤال" لتقديم نصائح إضافية تهم المستخدم.
اللغة: تتحدث باللغة العربية بطلاقة (أو أي لغة يطلبها المستخدم) مع لمسة من الحماس والتشجيع.
المهمة: هدفك هو حل المشكلات، كتابة الأكواد، تلخيص النصوص، أو حتى مجرد الدردشة الممتعة، مع الحفاظ على دقة عالية.
الآن، ابدأ بالترحيب بي باسمي "عبود" وأخبرني كيف يمكنك مساعدتي اليوم كـ ABOOD GPT. """
        
        # استدعاء واجهة Mistral
        payload = {
            "model": "mistral-medium",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": 500,
            "temperature": 0.7
        }
        
        headers = {
            "Authorization": f"Bearer {MISTRAL_KEY}",
            "Content-Type": "application/json"
        }
        
        # إضافة timeout للاتصال
        response = requests.post(MISTRAL_URL, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()['choices'][0]['message']['content']
            
            # تنظيف النص من التكرارات
            result = clean_repeated_text(result)
            
            # عرض الرد مع إبقاء أزرار الدردشة
            chat_keyboard = [["ايقاف الدردشة"], ["الرجوع للقائمة الرئيسية"]]
            
            # تقسيم الرسالة الطويلة إذا كانت طويلة جداً
            if len(result) > 4000:
                parts = split_message(result, max_length=4000)
                for part in parts:
                    await wait_msg.edit_text(
                        f"💭 **رد ABOOD GPT:**\n\n{part}",
                        parse_mode="Markdown"
                    )
                    wait_msg = await update.message.reply_text("...")
            else:
                await wait_msg.edit_text(
                    f"💭 **رد ABOOD GPT:**\n\n{result}",
                    parse_mode="Markdown"
                )
            
        else:
            logging.error(f"Mistral API Error: {response.status_code} - {response.text}")
            await wait_msg.edit_text(f"❌ حدث خطأ في التواصل مع الذكاء الاصطناعي. الرمز: {response.status_code}")
    
    except requests.exceptions.Timeout:
        await wait_msg.edit_text("⏱️ تجاوز الوقت المحدد للاتصال. حاول مرة أخرى.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error in chat: {e}")
        await wait_msg.edit_text("🌐 خطأ في الاتصال بالشبكة. تحقق من اتصالك بالإنترنت.")
    except Exception as e:
        logging.error(f"خطأ في الدردشة: {e}")
        await wait_msg.edit_text("❌ حدث خطأ في النظام. حاول مرة أخرى.")
    
    return CHAT_MODE

# --- الأوامر الرئيسية ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء البوت"""
    keyboard = [
        ["⚙️ إعدادات التحليل", "📊 تحليل صورة"],
        ["💬 دردشة"]
    ]
    
    await update.message.reply_text(
        "🤖 **أهلاً بك في ABOOD GPT**\n\n"
        "اختر أحد الخيارات التالية:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
        parse_mode="Markdown"
    )
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيارات القائمة الرئيسية"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    if user_message == "⚙️ إعدادات التحليل":
        keyboard = [CANDLE_SPEEDS[i:i+3] for i in range(0, len(CANDLE_SPEEDS), 3)]
        keyboard.append(["الرجوع للقائمة الرئيسية"])
        
        await update.message.reply_text(
            "⚙️ **إعدادات التحليل الفني**\n\n"
            "حدد سرعة الشموع للبدء:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return SETTINGS_CANDLE
    
    elif user_message == "📊 تحليل صورة":
        candle, trade_time, manual_time = get_user_setting(user_id)
        
        if not candle or not trade_time:
            keyboard = [["⚙️ إعدادات التحليل"], ["الرجوع للقائمة الرئيسية"]]
            await update.message.reply_text(
                "❌ **يجب ضبط الإعدادات أولاً**\n\n"
                "الرجاء ضبط سرعة الشموع ومدة الصفقة قبل التحليل.",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
                parse_mode="Markdown"
            )
            return MAIN_MENU
        else:
            keyboard = [["الرجوع للقائمة الرئيسية"]]
            
            # عرض الوقت المستخدم في التحليل
            time_display = format_trade_time_for_prompt(trade_time, manual_time)
            
            await update.message.reply_text(
                f"📊 **جاهز للتحليل**\n\n"
                f"الإعدادات الحالية:\n"
                f"• سرعة الشموع: {candle}\n"
                f"• {time_display}\n\n"
                f"أرسل صورة الرسم البياني (الشارت) الآن:",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
                parse_mode="Markdown"
            )
            return ANALYZE_MODE
    
    elif user_message == "💬 دردشة":
        return await start_chat_mode(update, context)
    
    # إذا كان النص غير معروف، إرجاع للقائمة الرئيسية
    keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
    await update.message.reply_text(
        "اختر أحد الخيارات من القائمة:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    )
    return MAIN_MENU

async def handle_settings_candle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيار سرعة الشموع"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    if user_message == "الرجوع للقائمة الرئيسية":
        keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "🏠 العودة للقائمة الرئيسية",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    if user_message in CANDLE_SPEEDS:
        save_user_setting(user_id, "candle", user_message)
        
        keyboard = [TRADE_TIMES[i:i+3] for i in range(0, len(TRADE_TIMES), 3)]
        keyboard.append(["الرجوع للقائمة الرئيسية"])
        
        await update.message.reply_text(
            f"✅ **تم تعيين سرعة الشموع:** {user_message}\n\n"
            f"الآن حدد **مدة الصفقة** المتوقعة:\n\n"
            f"يمكنك اختيار:\n"
            f"• أحد الأوقات الجاهزة\n"
            f"• ⏱️ وقت يدوي (لتحديد وقت مخصص)",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
            parse_mode="Markdown"
        )
        return SETTINGS_TIME
    
    await update.message.reply_text("❌ الرجاء اختيار سرعة شموع صحيحة.")
    return SETTINGS_CANDLE

async def handle_settings_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيار مدة الصفقة"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    if user_message == "الرجوع للقائمة الرئيسية":
        keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "🏠 العودة للقائمة الرئيسية",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    if user_message in TRADE_TIMES:
        if user_message == "⏱️ وقت يدوي":
            keyboard = [["الرجوع للقائمة الرئيسية"]]
            
            await update.message.reply_text(
                "⏱️ **إدخال وقت يدوي**\n\n"
                "📝 **أرسل وقت الصفقة يدوياً بإحدى الطرق:**\n\n"
                "1. **تنسيق الوقت:** 00:00:00 (ساعات:دقائق:ثواني)\n"
                "   مثال: 02:30:00 (ساعتين ونصف)\n"
                "   مثال: 00:15:00 (15 دقيقة)\n"
                "   مثال: 00:00:30 (30 ثانية)\n\n"
                "2. **كتابة نصي:**\n"
                "   مثال: 2 ساعة\n"
                "   مثال: 30 دقيقة\n"
                "   مثال: 3 أيام\n"
                "   مثال: 45 ثانية\n\n"
                "3. **أرقام فقط:**\n"
                "   مثال: 4 (سيتم اعتبارها 4 ساعات)\n\n"
                "❌ للإلغاء، اضغط 'الرجوع للقائمة الرئيسية'",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
                parse_mode="Markdown"
            )
            return SETTINGS_MANUAL_TIME
        else:
            save_user_setting(user_id, "trade_time", user_message)
            save_user_setting(user_id, "manual_time", "")  # مسح الوقت اليدوي إذا كان موجوداً
            
            keyboard = [["📊 تحليل صورة"], ["💬 دردشة مع الذكاء الاصطناعي"], ["الرجوع للقائمة الرئيسية"]]
            
            candle, _, _ = get_user_setting(user_id)
            
            await update.message.reply_text(
                f"🚀 **تم حفظ الإعدادات بنجاح!**\n\n"
                f"✅ سرعة الشموع: {candle}\n"
                f"✅ مدة الصفقة: {user_message}\n\n"
                f"يمكنك الآن تحليل صورة أو الدردشة:",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
                parse_mode="Markdown"
            )
            return MAIN_MENU
    
    await update.message.reply_text("❌ الرجاء اختيار مدة صفقة صحيحة.")
    return SETTINGS_TIME

async def handle_manual_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة إدخال الوقت يدوياً"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    if user_message == "الرجوع للقائمة الرئيسية":
        keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "🏠 العودة للقائمة الرئيسية",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    # محاولة تحليل الوقت المدخل
    parsed_time = parse_manual_time(user_message)
    
    if parsed_time:
        save_user_setting(user_id, "trade_time", "⏱️ وقت يدوي")
        save_user_setting(user_id, "manual_time", parsed_time)
        
        keyboard = [["📊 تحليل صورة"], ["💬 دردشة مع الذكاء الاصطناعي"], ["الرجوع للقائمة الرئيسية"]]
        
        candle, _, _ = get_user_setting(user_id)
        
        await update.message.reply_text(
            f"⏱️ **تم حفظ الوقت اليدوي بنجاح!**\n\n"
            f"✅ سرعة الشموع: {candle}\n"
            f"✅ مدة الصفقة: {parsed_time} (مدخل يدوي)\n\n"
            f"يمكنك الآن تحليل صورة أو الدردشة:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
            parse_mode="Markdown"
        )
        return MAIN_MENU
    else:
        keyboard = [["الرجوع للقائمة الرئيسية"]]
        await update.message.reply_text(
            "❌ **تنسوق وقت غير صحيح!**\n\n"
            "📝 **أعد الإدخال بإحدى الطرق:**\n\n"
            "1. **تنسيق الوقت:** 00:00:00 (ساعات:دقائق:ثواني)\n"
            "   مثال: 02:30:00 (ساعتين ونصف)\n\n"
            "2. **كتابة نصي:**\n"
            "   مثال: 2 ساعة\n"
            "   مثال: 30 دقيقة\n\n"
            "3. **أرقام فقط:**\n"
            "   مثال: 4 (سيتم اعتبارها 4 ساعات)",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, 

                                
    # تشغيل البوت
if __name__ == '__main__':
    keep_alive()
    app.run_polling()
