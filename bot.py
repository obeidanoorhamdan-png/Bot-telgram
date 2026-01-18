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
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

# --- الإعدادات ---
TOKEN = "7324911542:AAFqB9NRegwE2_bG5rCTaEWocbh8N3vgWeo"
MISTRAL_KEY = "EABRT5zGsHYhezkaJJomt15VR2iBrPWq"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
DB_NAME = "abood-gpt.db"

CANDLE_SPEEDS = ["S5", "S10", "S15", "S30", "M1", "M2", "M3", "M5", "M10", "M15", "M30", "H1", "H4", "D1"]
TRADE_TIMES = ["S3", "S15", "S30", "M1", "M3", "M5", "M30", "H1", "H4", "H24"]

# حالات المحادثة
MAIN_MENU, SETTINGS_CANDLE, SETTINGS_TIME, CHAT_MODE, ANALYZE_MODE = range(5)

# --- قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            candle TEXT DEFAULT 'M5', 
            trade_time TEXT DEFAULT 'H1',
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
    cursor.execute("SELECT candle, trade_time FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    if res:
        return res
    # إرجاع قيم افتراضية إذا لم يكن المستخدم موجوداً
    return ("M5", "H1")

# --- معالجة الصور ---
def encode_image(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')

# --- الدردشة مع Mistral ---
async def start_chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء وضع الدردشة"""
    keyboard = [
        ["ايقاف الدردشة"],
        ["الرجوع للقائمة الرئيسية"]
    ]
    
    # استخدام reply_text بدلاً من message.reply_text للتأكد من أن الرسالة تصل
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
        system_prompt = """ أنت الآن تعمل كمساعد ذكي خبير وشامل (Thought Partner). مهمتك هي الإجابة على أي استفسار أطرحه عليك بدقة وموضوعية. اتبع القواعد التالية في إجاباتك:
التحليل العميق: قبل الإجابة، قم بتحليل القصد الحقيقي من سؤالي لتقديم الفائدة القصوى.
الهيكلية: استخدم العناوين، النقاط، والجداول إذا لزم الأمر لتنظيم المعلومات وجعلها سهلة القراءة.
التوازن: اجمع بين الدقة العلمية والأسلوب الودود والمبسط.
الشفافية: إذا كان السؤال يحتمل أكثر من إجابة أو وجهة نظر، فاذكر الخيارات المتاحة.
الإيجاز غير المخل: لا تطل في الشرح إذا كانت الإجابة المباشرة كافية، ولا تقتضب إذا كان الموضوع يحتاج تفصيلاً.
اسمك هو : ABOOD GPT 🤖  """
        
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
            
            # عرض الرد مع إبقاء أزرار الدردشة
            chat_keyboard = [["ايقاف الدردشة"], ["الرجوع للقائمة الرئيسية"]]
            
            # تقسيم الرسالة الطويلة إذا كانت طويلة جداً
            if len(result) > 4000:
                parts = [result[i:i+4000] for i in range(0, len(result), 4000)]
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
        candle, trade_time = get_user_setting(user_id)
        
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
            await update.message.reply_text(
                f"📊 **جاهز للتحليل**\n\n"
                f"الإعدادات الحالية:\n"
                f"• سرعة الشموع: {candle}\n"
                f"• مدة الصفقة: {trade_time}\n\n"
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
            f"الآن حدد **مدة الصفقة** المتوقعة:",
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
        save_user_setting(user_id, "trade_time", user_message)
        
        keyboard = [["📊 تحليل صورة"], ["💬 دردشة مع الذكاء الاصطناعي"], ["الرجوع للقائمة الرئيسية"]]
        
        candle, _ = get_user_setting(user_id)
        
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

# --- معالجة الصور للتحليل ---
async def handle_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الصور للتحليل الفني"""
    user_id = update.effective_user.id
    candle, trade_time = get_user_setting(user_id)
    
    if not candle or not trade_time:
        keyboard = [["⚙️ إعدادات التحليل"], ["الرجوع للقائمة الرئيسية"]]
        await update.message.reply_text(
            "❌ **يجب ضبط الإعدادات أولاً**\n\n"
            "الرجاء استخدام أزرار القائمة لضبط الإعدادات قبل تحليل الصور.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
            parse_mode="Markdown"
        )
        return MAIN_MENU

    wait_msg = await update.message.reply_text("🔍 **جاري فحص الشارت 📊 ...**")
    photo = await update.message.photo[-1].get_file()
    path = f"img_{user_id}.jpg"
    await photo.download_to_drive(path)

    try:
        base64_img = encode_image(path)
        
        # برومبت آمن للتحليل الفني
        prompt = f"""
        أنت محلل فني خبير في أسواق المال. الصورة المرفقة هي رسم بياني (شارت) للتداول.
        
        **الإعدادات المطلوبة:**
        - سرعة الشموع: {candle}
        - مدة الصفقة المتوقعة: {trade_time}
        
        **مطلوب منك:**
        1. تحليل شامل للصورة
        2. تحديد الأنماط الفنية الظاهرة
        3. تقييم قوة الاتجاه
        4. تقديم توقع واضح
        5. تحليل ذكي للصورة 
        6. توقعات ناحجة جدآ 
        7. قدم إجابات دقيقة وموضوعية تعتمد على الحقائق والبيانات المتاحة 
        8. لا تقدم نسب مخاطرة وهمية ولا توقعات مضمونة.
        9. كن واقعياً وموضوعياً في جميع إجاباتك.
        10. توقعات ناحجة و رسمية بدون اي إجابات سريعة أو وهمية
        11. انتا ذكي جدآ وتوقعات مضمونة و صحيحة 100٪
        12. اجعل كل شئ بالغة العربية.
        13. اختصار الإجابة بدقة و وضوح و صحة بيانات
        
        **التنسيق المطلوب للإجابة:**
        📊 **التحليل الفني:**
        - النمط السائد: (تصاعدي/تنازلي/جانبي)
        - الشموع البارزة: ( وصف توقع اتجاه صعود أو نزول )
        - مستويات الدعم/المقاومة: (إن وجدت)
        - توقع مستويات الدعم/المقاومة القادم: (إن وجدت)
        🎯 **التوقع:**
        - الإتجاه: (🟢 صعود ⬆️ / 🔴 نزول ⬇️ / 🟡 ثابت ➡️ )
        - توقع الإتجاه: ( صعود عالي ⬆️⬆️ / صعود منخفض ⬆️⬇️ / صعود متوسط ⬆️➡️ / نزول مرتفع ⬇️⬆️ / نزول منخفض ⬇️⬇️/ نزول متوسط ⬇️➡️ )
        - توقع: ( بيع 🔴 / شراء 🟢 / الإحتفاظ 🟡 )
        - مستوى الثقة: XX٪
        - نقطة الدخول المقترحة: 
        - توقع نقطة الوصول:
        - هدف الربح: 
        - توقع هدف الربح:
        - وقف الخسارة:
        - توقع هدف الخسارة:
        
        ⚠️ **التحذيرات والمخاطر:**
        - المخاطر المحتملة:
        """
        
        payload = {
            "model": "pixtral-12b-2409",
            "messages": [
                {
                    "role": "user", 
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}
                    ]
                }
            ],
            "max_tokens": 800,
            "temperature": 0.3
        }
        
        headers = {
            "Authorization": f"Bearer {MISTRAL_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(MISTRAL_URL, headers=headers, json=payload, timeout=45)
        
        if response.status_code == 200:
            result = response.json()['choices'][0]['message']['content']
            
            keyboard = [["📊 تحليل صورة أخرى"], ["💬 دردشة"], ["الرجوع للقائمة الرئيسية"]]
            
            # تقسيم النتيجة إذا كانت طويلة
            if len(result) > 4000:
                parts = [result[i:i+4000] for i in range(0, len(result), 4000)]
                await wait_msg.edit_text(
                    f"✅ **تم التحليل بنجاح!**\n"
                    f"📈 **نتائج تحليل الشارت:**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{parts[0]}",
                    parse_mode="Markdown"
                )
                
                # إرسال الأجزاء المتبقية
                for part in parts[1:]:
                    await update.message.reply_text(part, parse_mode="Markdown")
            else:
                await wait_msg.edit_text(
                    f"✅ **تم التحليل بنجاح!**\n"
                    f"📈 **نتائج تحليل الشارت:**\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"{result}\n\n"
                    f"📊 **الإعدادات المستخدمة:**\n"
                    f"• سرعة الشموع: {candle}\n"
                    f"• مدة الصفقة: {trade_time}",
                    parse_mode="Markdown"
                )
            
            # إرسال الأزرار
            await update.message.reply_text(
                "📊 **اختر الإجراء التالي:**",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
            )
        else:
            logging.error(f"Mistral Vision API Error: {response.status_code} - {response.text}")
            keyboard = [["الرجوع للقائمة الرئيسية"]]
            await wait_msg.edit_text(f"❌ **خطأ في تحليل الصورة:** {response.status_code}")
            
    except requests.exceptions.Timeout:
        await wait_msg.edit_text("⏱️ تجاوز الوقت المحدد لتحليل الصورة. حاول مرة أخرى.")
    except Exception as e:
        logging.error(f"خطأ في تحليل الصورة: {e}")
        keyboard = [["الرجوع للقائمة الرئيسية"]]
        await wait_msg.edit_text("❌ **حدث خطأ في تحليل الصورة.**\nيرجى التأكد من وضوح الصورة والمحاولة مرة أخرى.")
    finally:
        if os.path.exists(path):
            os.remove(path)
    
    return MAIN_MENU

async def handle_analyze_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة وضع التحليل"""
    user_message = update.message.text
    user_id = update.effective_user.id
    
    if user_message == "الرجوع للقائمة الرئيسية":
        keyboard = [["⚙️ إعدادات التحليل", "📊 تحليل صورة"], ["💬 دردشة"]]
        await update.message.reply_text(
            "🏠 العودة للقائمة الرئيسية",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        )
        return MAIN_MENU
    
    # إذا أرسل المستخدم نصاً بدلاً من صورة
    await update.message.reply_text(
        "📤 **الرجاء إرسال صورة الشارت فقط**\nأو اضغط 'الرجوع للقائمة الرئيسية'",
        reply_markup=ReplyKeyboardMarkup([["الرجوع للقائمة الرئيسية"]], resize_keyboard=True, one_time_keyboard=False)
    )
    return ANALYZE_MODE

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر المساعدة"""
    help_text = """
    🤖 **أوامر البوت:**
    
    /start - بدء البوت والعودة للقائمة الرئيسية
    /help - عرض رسالة المساعدة
    
    ⚙️ **كيفية الاستخدام:**
    1. استخدم أزرار القائمة للتنقل
    2. أرسل صورة الشارت للتحليل
    3. اختر "دردشة" للاستفسارات النصية
    
    📊 **مميزات البوت:**
    • تحليل فني للرسوم البيانية
    • دردشة ذكية مع الذكاء الاصطناعي
    • حفظ إعداداتك الشخصية
    • واجهة سهلة بالأزرار
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def handle_photo_in_analyze_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الصور في وضع التحليل"""
    return await handle_photo_analysis(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء المحادثة"""
    await update.message.reply_text(
        "تم الإلغاء. اكتب /start للبدء من جديد.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

if __name__ == "__main__":
    # إعداد التسجيل
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        filename='bot.log'  # حفظ السجلات في ملف
    )
    
    # تصحيح الخطأ الأولي
    init_db()
    
    # إنشاء التطبيق
    app = Application.builder().token(TOKEN).build()
    
    # معالج المحادثة الرئيسي
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu)
            ],
            SETTINGS_CANDLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_candle)
            ],
            SETTINGS_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_time)
            ],
            CHAT_MODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_message)
            ],
            ANALYZE_MODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_analyze_mode),
                MessageHandler(filters.PHOTO, handle_photo_in_analyze_mode)
            ],
        },
        fallbacks=[CommandHandler('start', start), CommandHandler('cancel', cancel)],
        allow_reentry=True  # السماح بإعادة الدخول للولايات
    )
    
    # إضافة المعالجات
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    
    # إضافة معالج لجميع الرسائل النصية غير المعالجة
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))
    
    print("🤖 --- البوت يعمل الآن ---")
    print("📊 - نظام التحليل الفني مفعل")
    print("💬 - نظام الدردشة مفعل")
    print("🔧 - تم تصحيح مشاكل الأزرار والاتصال")
    print("✅ - تم تشغيل البوت بنجاح")
        
                                
    # تشغيل البوت
if __name__ == '__main__':
    keep_alive()
    app.run_polling()
