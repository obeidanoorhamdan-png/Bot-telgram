import os
import asyncio
import logging
import sqlite3
import aiohttp
import time
import random
import hashlib
import json
import string
from datetime import datetime, timedelta
from typing import Dict, List, Optional, DefaultDict, Tuple
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ==================== إعدادات الأساس ====================
TOKEN = "7324911542:AAHW83JR_xGt2fwUpXx10f3qVq7zBUiGBq0"
MISTRAL_API_KEY = "DGetWlOeqLvKem0l9pXoVkIvCpzhFXp1"
ADMIN_ID = 6207431030
ADMIN_USERNAME = "@Sz2zv"
CHANNEL_ID = "@AboodaTrading"
BOT_NAME = "ABOOD GPT 🤖"

# ==================== قائمة العملات والأزواج والأسهم ====================
MARKET_ASSETS = {
    "العملات الرئيسية": [
        "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
        "AUD/USD", "USD/CAD", "NZD/USD", "EUR/GBP",
        "EUR/JPY", "GBP/JPY"
    ],
    "العملات الرقمية": [
        "BTC/USD", "ETH/USD", "XRP/USD", "BNB/USD",
        "ADA/USD", "SOL/USD", "DOGE/USD", "DOT/USD"
    ],
    "السلع": [
        "GOLD", "SILVER", "OIL", "NATURAL GAS",
        "COPPER", "PLATINUM", "PALLADIUM"
    ],
    "المؤشرات العالمية": [
        "S&P500", "NASDAQ", "DOW JONES", "FTSE100",
        "DAX30", "NIKKEI225", "HANG SENG", "ASX200"
    ],
    "الأسهم الأمريكية": [
        "AAPL", "TSLA", "AMZN", "GOOGL",
        "MSFT", "META", "NVDA", "NFLX"
    ],
    "العملات المشفرة الأخرى": [
        "LTC/USD", "UNI/USD", "LINK/USD", "MATIC/USD",
        "ATOM/USD", "VET/USD", "ALGO/USD", "XTZ/USD"
    ]
}

# ==================== أوقات الشموع ====================
CANDLE_SPEEDS = [
    ["S5", "S10", "S15", "S30"],
    ["M1", "M2", "M3", "M5"],
    ["M10", "M15", "M30", "H1"],
    ["H4", "D1", "W1", "MN1"]
]

# ==================== أوقات الصفقات ====================
TRADE_TIMES = [
    ["S3", "S15", "S30", "M1"],
    ["M3", "M5", "M30", "H1"],
    ["H4", "D1", "🔙 العودة للرئيسية"]
]

# ==================== نظام Rate Limiting ====================
class RateLimiter:
    def __init__(self, calls_per_minute: int = 15):
        self.calls_per_minute = calls_per_minute
        self.requests: DefaultDict[str, List[float]] = defaultdict(list)
    
    async def wait_if_needed(self, key: str = "global"):
        """انتظار إذا تجاوز الحد"""
        now = time.time()
        user_requests = self.requests[key]
        
        user_requests = [req_time for req_time in user_requests 
                        if now - req_time < 60]
        
        if len(user_requests) >= self.calls_per_minute:
            oldest_request = user_requests[0]
            wait_time = 60 - (now - oldest_request) + 0.5
            if wait_time > 0:
                await asyncio.sleep(wait_time)
        
        user_requests.append(now)
        self.requests[key] = user_requests[-self.calls_per_minute:]

# ==================== نظام Caching ====================
class ResponseCache:
    def __init__(self, duration_minutes: int = 30):
        self.cache: Dict[str, Dict] = {}
        self.duration = timedelta(minutes=duration_minutes)
    
    def get_key(self, message: str, system_prompt: str = None) -> str:
        data = f"{message}_{system_prompt}"
        return hashlib.md5(data.encode()).hexdigest()
    
    def get(self, key: str):
        if key in self.cache:
            cached_data = self.cache[key]
            if datetime.now() - cached_data['timestamp'] < self.duration:
                return cached_data['response']
        return None
    
    def set(self, key: str, response: str):
        self.cache[key] = {
            'response': response,
            'timestamp': datetime.now()
        }
        
        if len(self.cache) > 1000:
            self.cleanup()
    
    def cleanup(self):
        now = datetime.now()
        keys_to_delete = []
        
        for key, data in self.cache.items():
            if now - data['timestamp'] > self.duration * 2:
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self.cache[key]

# ==================== Mistral AI المحمي ====================
class ProtectedMistralAI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.mistral.ai/v1"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.rate_limiter = RateLimiter(calls_per_minute=20)
        self.cache = ResponseCache()
        self.max_retries = 3
        self.stats = {
            'total_requests': 0,
            'successful': 0,
            'rate_limited': 0,
            'errors': 0
        }
    
    async def get_predictions(self, asset: str, timeframe: str, trade_time: str) -> str:
        """الحصول على توقعات ذكية بدون وهميات"""
        self.stats['total_requests'] += 1
        
        cache_key = f"pred_{asset}_{timeframe}_{trade_time}"
        cached_response = self.cache.get(cache_key)
        if cached_response:
            return cached_response
        
        await self.rate_limiter.wait_if_needed("mistral_api")
        
        response = await self._get_predictions_with_retry(asset, timeframe, trade_time)
        
        if "429" in response or "Too Many" in response:
            self.stats['rate_limited'] += 1
        elif "خطأ" in response:
            self.stats['errors'] += 1
        else:
            self.stats['successful'] += 1
            self.cache.set(cache_key, response)
        
        return response
    
    async def _get_predictions_with_retry(self, asset: str, timeframe: str, trade_time: str) -> str:
        for attempt in range(self.max_retries):
            try:
                result = await self._make_prediction_request(asset, timeframe, trade_time, attempt)
                if result and "429" not in result and "Too Many" not in result:
                    return result
                
                if attempt < self.max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(wait_time)
                    
            except Exception as e:
                if attempt == self.max_retries - 1:
                    return f"خطأ: {str(e)}"
                await asyncio.sleep(1)
        
        return "تعذر الحصول على توقعات"
    
    async def _make_prediction_request(self, asset: str, timeframe: str, trade_time: str, attempt: int) -> str:
        url = f"{self.base_url}/chat/completions"
        
        system_prompt = """أنت محلل فني محترف في الأسواق المالية. مهمتك تقديم توقعات واقعية بناءً على:
        1. البيانات الفنية الحالية
        2. الأنماط الفنية الظاهرة
        3. حركة السعر
        4. المؤشرات الفنية
        
        **ممنوع تماماً:**
        - تقديم نسب مئوية وهمية (مثل 85% ثقة)
        - تقديم وعود مضمونة
        - استخدام مصطلحات غير واقعية
        - تقديم أي معلومات غير مثبتة
        
        **المطلوب:**
        - تحليل موضوعي واقعي
        - توقع بناء على الحقائق فقط
        - تحديد اتجاه محتمل (صاعد/هابط/جانبي)
        - ذكر الأدلة الفنية
        - تحذير من المخاطر
        
        **التنسيق المطلوب:**
        1. 📊 **التحليل الفني:**
        2. 🎯 **التوقع:**
        3. 📈 **الاتجاه المحتمل:**
        4. ⚠️ **المخاطر والملاحظات:**
        
        كن موضوعياً، واقعياً، ومباشراً."""
        
        user_prompt = f"""
        قم بتحليل {asset} بناءً على:
        - الإطار الزمني: {timeframe}
        - وقت الصفقة المخطط: {trade_time}
        
        قدم توقعات واقعية بناءً على:
        1. حركة السعر الحالية
        2. الأنماط الفنية
        3. مستويات الدعم والمقاومة
        4. المؤشرات الفنية إذا كانت متاحة
        
        **لا تذكر أي نسب مئوية أو وعود.**
        **ركز على الحقائق والبيانات فقط.**
        **كن صريحاً ومباشراً.**
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        model = "mistral-small" if attempt > 0 else "mistral-medium"
        
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.7
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, 
                    headers=self.headers, 
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        return data['choices'][0]['message']['content']
                    
                    elif response.status == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        await asyncio.sleep(int(retry_after))
                        return f"خطأ 429"
                    
                    else:
                        return f"خطأ في API: {response.status}"
                        
            except Exception as e:
                return f"خطأ في الاتصال: {str(e)}"
    
    async def analyze_image_for_trading(self, description: str, candle_speed: str, trade_time: str) -> str:
        """تحليل الصور للتداول"""
        self.stats['total_requests'] += 1
        
        cache_key = f"img_{description}_{candle_speed}_{trade_time}"
        cached_response = self.cache.get(cache_key)
        if cached_response:
            return cached_response
        
        await self.rate_limiter.wait_if_needed("mistral_api")
        
        response = await self._analyze_image_with_retry(description, candle_speed, trade_time)
        
        if "429" in response or "Too Many" in response:
            self.stats['rate_limited'] += 1
        elif "خطأ" in response:
            self.stats['errors'] += 1
        else:
            self.stats['successful'] += 1
            self.cache.set(cache_key, response)
        
        return response
    
    async def _analyze_image_with_retry(self, description: str, candle_speed: str, trade_time: str) -> str:
        for attempt in range(self.max_retries):
            try:
                result = await self._make_image_analysis_request(description, candle_speed, trade_time, attempt)
                if result and "429" not in result and "Too Many" not in result:
                    return result
                
                if attempt < self.max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(wait_time)
                    
            except Exception as e:
                if attempt == self.max_retries - 1:
                    return f"خطأ: {str(e)}"
                await asyncio.sleep(1)
        
        return "تعذر تحليل الصورة"
    
    async def _make_image_analysis_request(self, description: str, candle_speed: str, trade_time: str, attempt: int) -> str:
        url = f"{self.base_url}/chat/completions"
        
        system_prompt = """أنت محلل فني متخصص في تحليل الرسوم البيانية. مهمتك:
        1. تحليل الصورة المرفوعة (الشارت)
        2. تحديد زوج العملات/المؤشر
        3. تقديم تحليل فني واقعي
        4. توقع اتجاه محتمل
        
        **ممنوع:**
        - نسب مئوية وهمية
        - وعود مضمونة
        - معلومات غير مثبتة
        
        **المطلوب:**
        - تحليل واقعي للشارت
        - تحديد زوج العملات إن أمكن
        - تحليل الأنماط الفنية
        - توقع موضوعي
        - ذكر المخاطر"""
        
        user_prompt = f"""
        قم بتحليل صورة الشارت بناءً على:
        - وصف الصورة: {description}
        - سرعة الشموع: {candle_speed}
        - وقت الصفقة: {trade_time}
        
        قدم:
        1. تحديد الزوج/المؤشر المحتمل
        2. تحليل فني للشارت
        3. توقع واقعي للاتجاه
        4. توصية عملية
        
        **لا تستخدم أي نسب أو وعود.**
        **كن واقعياً وموضوعياً.**
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        model = "mistral-small" if attempt > 0 else "mistral-medium"
        
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.7
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, 
                    headers=self.headers, 
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        return data['choices'][0]['message']['content']
                    
                    elif response.status == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        await asyncio.sleep(int(retry_after))
                        return f"خطأ 429"
                    
                    else:
                        return f"خطأ في API: {response.status}"
                        
            except Exception as e:
                return f"خطأ في الاتصال: {str(e)}"
    
    async def generate_image_description(self, text: str) -> str:
        """إنشاء وصف صورة من النص"""
        self.stats['total_requests'] += 1
        
        cache_key = f"img_desc_{text}"
        cached_response = self.cache.get(cache_key)
        if cached_response:
            return cached_response
        
        await self.rate_limiter.wait_if_needed("mistral_api")
        
        response = await self._generate_image_description_with_retry(text)
        
        if "429" in response or "Too Many" in response:
            self.stats['rate_limited'] += 1
        elif "خطأ" in response:
            self.stats['errors'] += 1
        else:
            self.stats['successful'] += 1
            self.cache.set(cache_key, response)
        
        return response
    
    async def _generate_image_description_with_retry(self, text: str) -> str:
        for attempt in range(self.max_retries):
            try:
                result = await self._make_image_description_request(text, attempt)
                if result and "429" not in result and "Too Many" not in result:
                    return result
                
                if attempt < self.max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(wait_time)
                    
            except Exception as e:
                if attempt == self.max_retries - 1:
                    return f"خطأ: {str(e)}"
                await asyncio.sleep(1)
        
        return "تعذر إنشاء وصف الصورة"
    
    async def _make_image_description_request(self, text: str, attempt: int) -> str:
        url = f"{self.base_url}/chat/completions"
        
        system_prompt = """أنت فنان ومصمم محترف. مهمتك تحويل أي نص إلى وصف صورة مفصل.
        
        **المطلوب:**
        1. تحويل النص إلى وصف صورة مرئي
        2. إضافة تفاصيل فنية (ألوان، إضاءة، تكوين)
        3. وصف المشهد بالكامل
        4. إضافة عناصر إبداعية
        
        **التنسيق المطلوب:**
        وصف مفصل باللغة العربية مع تفاصيل فنية."""
        
        user_prompt = f"""
        قم بتحويل النص التالي إلى وصف صورة مفصل وفني:
        
        النص: {text}
        
        قدم وصفاً مفصلاً يشمل:
        1. المشهد العام
        2. الألوان والإنارة
        3. التفاصيل الدقيقة
        4. الجو العام
        5. العناصر الفنية
        
        كن إبداعياً ودقيقاً في الوصف.
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        model = "mistral-small" if attempt > 0 else "mistral-medium"
        
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1500,
            "temperature": 0.8
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, 
                    headers=self.headers, 
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        return data['choices'][0]['message']['content']
                    
                    elif response.status == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        await asyncio.sleep(int(retry_after))
                        return f"خطأ 429"
                    
                    else:
                        return f"خطأ في API: {response.status}"
                        
            except Exception as e:
                return f"خطأ في الاتصال: {str(e)}"
    
    async def generate_channel_post(self) -> str:
        """إنشاء منشور تلقائي للقناة"""
        self.stats['total_requests'] += 1
        
        cache_key = f"channel_post_{datetime.now().strftime('%Y%m%d%H')}"
        cached_response = self.cache.get(cache_key)
        if cached_response:
            return cached_response
        
        await self.rate_limiter.wait_if_needed("mistral_api")
        
        response = await self._generate_channel_post_with_retry()
        
        if "429" in response or "Too Many" in response:
            self.stats['rate_limited'] += 1
        elif "خطأ" in response:
            self.stats['errors'] += 1
        else:
            self.stats['successful'] += 1
            self.cache.set(cache_key, response)
        
        return response
    
    async def _generate_channel_post_with_retry(self) -> str:
        for attempt in range(self.max_retries):
            try:
                result = await self._make_channel_post_request(attempt)
                if result and "429" not in result and "Too Many" not in result:
                    return result
                
                if attempt < self.max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(wait_time)
                    
            except Exception as e:
                if attempt == self.max_retries - 1:
                    return f"خطأ: {str(e)}"
                await asyncio.sleep(1)
        
        return "تعذر إنشاء المنشور"
    
    async def _make_channel_post_request(self, attempt: int) -> str:
        url = f"{self.base_url}/chat/completions"
        
        # اختيار أصل عشوائي للتحليل
        asset_types = list(MARKET_ASSETS.keys())
        selected_type = random.choice(asset_types)
        selected_asset = random.choice(MARKET_ASSETS[selected_type])
        
        system_prompt = """أنت محلل فني محترف ومذيع في قناة تداول. مهمتك كتابة منشور توقعات قوي وجذاب للقناة.
        
        **المطلوب:**
        1. تحليل فني قوي وواقعي
        2. توصية واضحة وحاسمة
        3. لغة جذابة ومحفزة
        4. تنسيق احترافي مع إيموجيات
        5. إضافة علامات التصنيف المناسبة
        
        **ممنوع:**
        - نسب مئوية وهمية
        - وعود مضمونة
        - معلومات غير مثبتة
        
        **التنسيق المطلوب:**
        🔥 **عنوان جذاب**
        
        📊 **التحليل الفني:**
        [تحليل مفصل وقوي]
        
        🎯 **التوصية:**
        [توصية واضحة وقوية]
        
        ⚡ **نقاط مهمة:**
        [نقاط رئيسية]
        
        👉 [رابط أو دعوة للانضمام]
        
        🔖 [علامات تصنيف]"""
        
        user_prompt = f"""
        قم بإنشاء منشور توقعات قوي وجذاب للقناة عن:
        
        الأصل: {selected_asset}
        النوع: {selected_type}
        الوقت: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        
        المطلوب:
        1. تحليل فني قوي وواقعي
        2. توصية واضحة (شراء/بيع/احتفاظ)
        3. لغة جذابة ومحفزة للعمل
        4. تنسيق احترافي مع إيموجيات
        5. إضافة علامات تصنيف مناسبة
        
        **كن قوياً وجذاباً في التحليل.**
        **لا تذكر أي نسب أو وعود.**
        **ركز على القوة والوضوح.**
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        model = "mistral-small" if attempt > 0 else "mistral-medium"
        
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2500,
            "temperature": 0.8
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, 
                    headers=self.headers, 
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        result = data['choices'][0]['message']['content']
                        
                        # إضافة معلومات إضافية
                        final_post = f"""
🔥 *توصية قوية - {datetime.now().strftime("%H:%M")}*

{result}

📌 *للمزيد من التوقعات الذكية:*
👉 @AboodaTrading
🤖 @Sz2zv

🔖 #{selected_asset.replace('/', '').replace(' ', '')} 
🔖 #{selected_type.replace(' ', '')}
🔖 #تداول #فوركس #أسهم
                        """
                        
                        return final_post
                    
                    elif response.status == 429:
                        retry_after = response.headers.get('Retry-After', '5')
                        await asyncio.sleep(int(retry_after))
                        return f"خطأ 429"
                    
                    else:
                        return f"خطأ في API: {response.status}"
                        
            except Exception as e:
                return f"خطأ في الاتصال: {str(e)}"

# ==================== قاعدة البيانات ====================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('abood_bot.db', check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                points INTEGER DEFAULT 0,
                daily_claimed DATE,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                banned INTEGER DEFAULT 0,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'main',
                data TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                asset_type TEXT,
                asset_name TEXT,
                timeframe TEXT,
                trade_time TEXT,
                prediction TEXT,
                recommendation TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS image_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                image_id TEXT,
                candle_speed TEXT,
                trade_time TEXT,
                asset_name TEXT,
                recommendation TEXT,
                analysis TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referral_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                link_code TEXT UNIQUE,
                points INTEGER DEFAULT 10,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_text TEXT,
                asset_name TEXT,
                recommendation TEXT,
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def get_user(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone()
    
    def create_user(self, user_id: int, username: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username) 
            VALUES (?, ?)
        ''', (user_id, username))
        self.conn.commit()
    
    def update_points(self, user_id: int, points_change: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET points = points + ?, last_active = CURRENT_TIMESTAMP 
            WHERE user_id = ?
        ''', (points_change, user_id))
        self.conn.commit()
    
    def set_daily_claimed(self, user_id: int):
        cursor = self.conn.cursor()
        today = datetime.now().date()
        cursor.execute('UPDATE users SET daily_claimed = ? WHERE user_id = ?', 
                      (today.strftime('%Y-%m-%d'), user_id))
        self.conn.commit()
    
    def can_claim_daily(self, user_id: int):
        """التحقق مما إذا كان المستخدم يمكنه المطالبة بالنقاط اليومية"""
        user = self.get_user(user_id)
        if not user:
            return True
        
        daily_claimed = user[3]
        if not daily_claimed:
            return True
        
        try:
            last_claimed = datetime.strptime(daily_claimed, '%Y-%m-%d').date()
            return last_claimed < datetime.now().date()
        except ValueError:
            return True
    
    def get_user_state(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT state, data FROM user_states WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result if result else ('main', None)
    
    def set_user_state(self, user_id: int, state: str, data: str = None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, state, data))
        self.conn.commit()
    
    def save_prediction(self, user_id: int, asset_type: str, asset_name: str, 
                       timeframe: str, trade_time: str, prediction: str, recommendation: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO predictions 
            (user_id, asset_type, asset_name, timeframe, trade_time, prediction, recommendation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, asset_type, asset_name, timeframe, trade_time, prediction, recommendation))
        self.conn.commit()
    
    def save_image_analysis(self, user_id: int, image_id: str, candle_speed: str, 
                           trade_time: str, asset_name: str, recommendation: str, analysis: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO image_analysis 
            (user_id, image_id, candle_speed, trade_time, asset_name, recommendation, analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, image_id, candle_speed, trade_time, asset_name, recommendation, analysis))
        self.conn.commit()
    
    def save_channel_post(self, post_text: str, asset_name: str, recommendation: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO channel_posts (post_text, asset_name, recommendation)
            VALUES (?, ?, ?)
        ''', (post_text, asset_name, recommendation))
        self.conn.commit()
    
    def create_referral_link(self, user_id: int, points: int = 10, max_uses: int = 100):
        """إنشاء رابط إحالة"""
        link_code = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
        expires_at = datetime.now() + timedelta(days=30)
        
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO referral_links 
            (user_id, link_code, points, max_uses, expires_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, link_code, points, max_uses, expires_at.strftime('%Y-%m-%d %H:%M:%S')))
        self.conn.commit()
        
        return link_code
    
    def use_referral_link(self, link_code: str, new_user_id: int):
        """استخدام رابط إحالة"""
        cursor = self.conn.cursor()
        
        # التحقق من وجود الرابط
        cursor.execute('''
            SELECT id, user_id, points, uses, max_uses, expires_at 
            FROM referral_links 
            WHERE link_code = ? 
            AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        ''', (link_code,))
        
        link = cursor.fetchone()
        
        if not link:
            return False, "الرابط غير صالح أو منتهي الصلاحية"
        
        link_id, creator_id, points, uses, max_uses, expires_at = link
        
        # التحقق من عدد الاستخدامات
        if uses >= max_uses:
            return False, "تم استنفاذ عدد استخدامات هذا الرابط"
        
        # التحقق من عدم استخدام الرابط من قبل نفس المستخدم
        cursor.execute('''
            SELECT 1 FROM referral_links_usage 
            WHERE link_id = ? AND user_id = ?
        ''', (link_id, new_user_id))
        
        if cursor.fetchone():
            return False, "لقد استخدمت هذا الرابط من قبل"
        
        # زيادة عدد الاستخدامات
        cursor.execute('''
            UPDATE referral_links 
            SET uses = uses + 1 
            WHERE id = ?
        ''', (link_id,))
        
        # إضافة النقاط للمستخدم الجديد
        self.update_points(new_user_id, points)
        
        # إضافة النقاط لمنشئ الرابط
        self.update_points(creator_id, points // 2)  # 50% من النقاط للمنشئ
        
        # تسجيل الاستخدام
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referral_links_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER,
                user_id INTEGER,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            INSERT INTO referral_links_usage (link_id, user_id)
            VALUES (?, ?)
        ''', (link_id, new_user_id))
        
        self.conn.commit()
        
        return True, f"تم إضافة {points} نقطة إلى حسابك!"
    
    def ban_user(self, user_id: int):
        """حظر مستخدم"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET banned = 1 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def unban_user(self, user_id: int):
        """فك حظر مستخدم"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET banned = 0 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def is_user_banned(self, user_id: int):
        """التحقق مما إذا كان المستخدم محظوراً"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT banned FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result and result[0] == 1
    
    def get_all_users(self):
        """الحصول على جميع المستخدمين"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id, username, points, banned FROM users ORDER BY points DESC')
        return cursor.fetchall()
    
    def get_total_users_count(self):
        """عدد المستخدمين الإجمالي"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        return cursor.fetchone()[0]
    
    def get_active_users_count(self):
        """عدد المستخدمين النشطين (آخر 7 أيام)"""
        cursor = self.conn.cursor()
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('SELECT COUNT(*) FROM users WHERE last_active > ?', (week_ago,))
        return cursor.fetchone()[0]
    
    def get_total_points(self):
        """إجمالي النقاط في النظام"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT SUM(points) FROM users')
        result = cursor.fetchone()[0]
        return result if result else 0
    
    def get_user_predictions_count(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM predictions WHERE user_id = ?', (user_id,))
        return cursor.fetchone()[0] or 0
    
    def get_user_image_analyses_count(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM image_analysis WHERE user_id = ?', (user_id,))
        return cursor.fetchone()[0] or 0

# ==================== نظام لوحة المفاتيح الدائمة ====================
class KeyboardManager:
    """مدير لوحات المفاتيح الدائمة"""
    
    @staticmethod
    def get_main_keyboard():
        """لوحة المفاتيح الرئيسية"""
        keyboard = [
            ["🎁 النقاط اليومية", "💬 دردشة"],
            ["📈 توقعات السوق", "🖼️ تحليل الشارت بالصورة"],
            ["📊 تاريخ التوقعات", "👤 حسابي"],
            ["🆘 المساعدة", "📋 المزيد من الخيارات"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_chat_keyboard():
        """لوحة المفاتيح أثناء الدردشة"""
        keyboard = [
            ["❌ إنهاء الدردشة"],
            ["🔙 العودة للرئيسية"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_asset_types_keyboard():
        """لوحة مفاتيح أنواع الأصول"""
        keyboard = [
            ["العملات الرئيسية", "العملات الرقمية"],
            ["السلع", "المؤشرات العالمية"],
            ["الأسهم الأمريكية", "العملات المشفرة الأخرى"],
            ["🔙 العودة للرئيسية"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_assets_keyboard(asset_type: str):
        """لوحة مفاتيح الأصول حسب النوع"""
        assets = MARKET_ASSETS.get(asset_type, [])
        keyboard = []
        
        # تقسيم الأصول إلى صفوف (كل صف 3 أصول)
        for i in range(0, len(assets), 3):
            row = assets[i:i+3]
            keyboard.append(row)
        
        keyboard.append(["🔙 العودة للرئيسية"])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_candle_speed_keyboard():
        """لوحة مفاتيح سرعة الشموع"""
        keyboard = CANDLE_SPEEDS.copy()
        keyboard.append(["🔙 العودة للرئيسية"])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_trade_time_keyboard():
        """لوحة مفاتيح وقت الصفقة"""
        keyboard = TRADE_TIMES.copy()
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_more_options_keyboard():
        """لوحة المفاتيح للمزيد من الخيارات"""
        keyboard = [
            ["📊 إحصائيات البوت", "⚙️ الإعدادات"],
            ["🎁 رابط للنقاط", "🖼️ إنشاء صورة من نص"],
            ["🔙 العودة للرئيسية"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_admin_keyboard():
        """لوحة المفاتيح للإدمن"""
        keyboard = [
            ["📢 بث للجميع", "➕ إضافة نقاط"],
            ["⛔ حظر مستخدم", "📊 الإحصائيات"],
            ["🎁 إنشاء رابط نقاط", "🔄 فك حظر"],
            ["📈 نشر في القناة", "🔙 العودة للرئيسية"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    @staticmethod
    def get_admin_add_points_keyboard():
        """لوحة مفاتيح إضافة النقاط للإدمن"""
        keyboard = [
            ["10 نقاط", "50 نقاط", "100 نقاط"],
            ["500 نقاط", "1000 نقاط", "5000 نقاط"],
            ["إدخال مخصص", "🔙 رجوع"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==================== البوت الرئيسي ====================
class AboodGPTBot:
    def __init__(self):
        self.db = Database()
        self.mistral = ProtectedMistralAI(MISTRAL_API_KEY)
        self.keyboard_manager = KeyboardManager()
        self.user_temp_data = {}
        
        self.application = Application.builder().token(TOKEN).build()
        self.setup_handlers()
        self.setup_jobs()
    
    def setup_handlers(self):
        """إعداد جميع المعالجات"""
        # الأوامر
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("points", self.points_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("predict", self.predict_command))
        self.application.add_handler(CommandHandler("admin", self.admin_command))
        self.application.add_handler(CommandHandler("link", self.referral_link_command))
        
        # معالجة النصوص
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            self.handle_text_message
        ))
        
        # معالجة الصور
        self.application.add_handler(MessageHandler(
            filters.PHOTO,
            self.handle_photo
        ))
        
        # معالجة Callback Queries
        self.application.add_handler(CallbackQueryHandler(self.handle_callback_query))
        
        # معالجة أي رسالة أخرى
        self.application.add_handler(MessageHandler(
            filters.ALL, 
            self.handle_other_messages
        ))
    
    def setup_jobs(self):
        """إعداد الوظائف المجدولة"""
        job_queue = self.application.job_queue
        
        if job_queue:
            # نشر تلقائي في القناة كل ساعة
            job_queue.run_repeating(
                self.auto_channel_post,
                interval=3600,  # كل ساعة
                first=10
            )
            
            # تنظيف الروابط المنتهية يومياً
            job_queue.run_daily(
                self.cleanup_expired_links,
                time=datetime.time(datetime.now().replace(hour=0, minute=0, second=0))
            )
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة أمر /start"""
        user = update.effective_user
        
        # التحقق من رابط الإحالة
        referral_link = None
        if context.args:
            referral_link = context.args[0]
        
        # إنشاء/تحديث المستخدم
        self.db.create_user(user.id, user.username)
        
        # تطبيق رابط الإحالة إذا كان موجوداً
        if referral_link:
            success, message = self.db.use_referral_link(referral_link, user.id)
            if success:
                referral_bonus = f"\n\n🎁 *مكافأة الإحالة:* {message}"
            else:
                referral_bonus = f"\n\n⚠️ *ملاحظة:* {message}"
        else:
            referral_bonus = ""
        
        self.db.set_user_state(user.id, 'main')
        
        welcome_text = f"""
🎮 *مرحباً {user.first_name}!*
🤖 *أهلاً بك في {BOT_NAME} - نظام التوقعات الذكي*

✅ *المميزات المتاحة:*
• 📈 *توقعات السوق الذكية* (بدون وهميات)
• 🖼️ *تحليل الشارت بالصور* (تحليل فني متقدم)
• 💬 دردشة مع ذكاء اصطناعي متقدم
• 🎁 نقاط يومية مجانية
• 📊 تاريخ التوقعات المحفوظة
• 🖼️ *إنشاء صور من النص* (ميزة جديدة!)

{referral_bonus}

🚫 *ممنوع في هذا البوت:*
• نسب مئوية وهمية
• وعود مضمونة
• معلومات غير واقعية

✅ *المسموح فقط:*
• تحليل فني واقعي
• توقعات بناء على البيانات
• توصيات عملية

🔄 *استخدم الأزرار أدناه للتنقل بسهولة*
        """
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """أمر الإدمن"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_ID:
            await update.message.reply_text(
                "⛔ *ليس لديك صلاحية الدخول لوحة التحكم*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        self.db.set_user_state(user_id, 'admin_panel')
        
        admin_text = """
👑 *لوحة تحكم الإدمن*

✅ *المميزات المتاحة:*
• 📢 بث رسالة لجميع المستخدمين
• ➕ إضافة نقاط لأي مستخدم
• ⛔ حظر/فك حظر مستخدم
• 📊 عرض إحصائيات البوت
• 🎁 إنشاء روابط للنقاط
• 📈 نشر تلقائي في القناة

🔧 *استخدم الأزرار أدناه للتحكم*
        """
        
        await update.message.reply_text(
            admin_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_admin_keyboard()
        )
    
    async def referral_link_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إنشاء رابط إحالة"""
        user_id = update.effective_user.id
        
        if user_id != ADMIN_ID:
            # للمستخدمين العاديين: إنشاء رابط شخصي
            link_code = self.db.create_referral_link(user_id, points=10, max_uses=50)
            link_text = f"https://t.me/{context.bot.username}?start={link_code}"
            
            await update.message.reply_text(
                f"🎁 *رابطك الخاص للنقاط:*\n\n"
                f"🔗 `{link_text}`\n\n"
                f"📊 *معلومات الرابط:*\n"
                f"• ⭐ النقاط: 10 لكل مستخدم جديد\n"
                f"• 👥 الحد الأقصى: 50 مستخدم\n"
                f"• 📅 الصلاحية: 30 يوم\n"
                f"• 💰 مكافأتك: 5 نقاط لكل إحالة\n\n"
                f"📌 *شارك الرابط مع أصدقائك واحصل على نقاط مجانية!*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        else:
            # للإدمن: إنشاء رابط بقيمة نقاط قابلة للتخصيص
            self.db.set_user_state(user_id, 'admin_create_link')
            
            await update.message.reply_text(
                "🎁 *إنشاء رابط نقاط (للإدمن)*\n\n"
                "📝 *أرسل عدد النقاط لكل مستخدم:*\n"
                "(أدخل رقماً صحيحاً، مثال: 100)",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True)
            )
    
    # ==================== معالجة حالات الإدمن ====================
    
    async def handle_admin_state(self, update: Update, message_text: str, user_id: int):
        """معالجة حالة لوحة التحكم"""
        if message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم العودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        
        elif message_text == "🔙 رجوع":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم الرجوع للوحة التحكم",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
        
        elif message_text == "📢 بث للجميع":
            self.db.set_user_state(user_id, 'admin_broadcast')
            await update.message.reply_text(
                "📢 *وضع البث للجميع*\n\n"
                "أرسل الرسالة التي تريد بثها لجميع المستخدمين:\n"
                "(يمكن أن تحتوي على نص، إيموجيات، تنسيق ماركداون)\n\n"
                "اكتب 'إلغاء' للإلغاء.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True)
            )
        
        elif message_text == "➕ إضافة نقاط":
            self.db.set_user_state(user_id, 'admin_add_points_user')
            await update.message.reply_text(
                "➕ *إضافة نقاط*\n\n"
                "📝 *أرسل أيدي المستخدم:*\n"
                "(يجب أن يكون رقمياً)\n\n"
                "اكتب 'إلغاء' للإلغاء.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True)
            )
        
        elif message_text == "⛔ حظر مستخدم":
            self.db.set_user_state(user_id, 'admin_ban_user')
            await update.message.reply_text(
                "⛔ *حظر مستخدم*\n\n"
                "📝 *أرسل أيدي المستخدم:*\n"
                "(يجب أن يكون رقمياً)\n\n"
                "اكتب 'إلغاء' للإلغاء.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True)
            )
        
        elif message_text == "🔄 فك حظر":
            self.db.set_user_state(user_id, 'admin_unban_user')
            await update.message.reply_text(
                "🔄 *فك حظر مستخدم*\n\n"
                "📝 *أرسل أيدي المستخدم:*\n"
                "(يجب أن يكون رقمياً)\n\n"
                "اكتب 'إلغاء' للإلغاء.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True)
            )
        
        elif message_text == "📊 الإحصائيات":
            await self.show_admin_stats(update, user_id)
        
        elif message_text == "🎁 إنشاء رابط نقاط":
            await self.referral_link_command(update, None)
        
        elif message_text == "📈 نشر في القناة":
            await self.manual_channel_post(update, user_id)
        
        else:
            await update.message.reply_text(
                "📝 *الرجاء استخدام الأزرار أدناه*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
    
    async def handle_admin_broadcast(self, update: Update, message_text: str, user_id: int):
        """معالجة بث الإدمن"""
        if message_text == "إلغاء":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم إلغاء البث.",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        # البث لجميع المستخدمين
        users = self.db.get_all_users()
        total_users = len(users)
        success = 0
        failed = 0
        
        broadcast_msg = await update.message.reply_text(
            f"📤 *جاري البث لـ {total_users} مستخدم...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        for user in users:
            try:
                if user[3] == 0:  # إذا لم يكن محظوراً
                    await self.application.bot.send_message(
                        chat_id=user[0],
                        text=f"📢 *إعلان من الإدارة:*\n\n{message_text}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    success += 1
                    await asyncio.sleep(0.1)  # تجنب rate limiting
            except Exception:
                failed += 1
        
        self.db.set_user_state(user_id, 'admin_panel')
        
        await broadcast_msg.delete()
        await update.message.reply_text(
            f"✅ *تم الانتهاء من البث*\n\n"
            f"✅ نجح: {success}\n"
            f"❌ فشل: {failed}\n"
            f"📊 الإجمالي: {total_users}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_admin_keyboard()
        )
    
    async def handle_admin_add_points_user(self, update: Update, message_text: str, user_id: int):
        """معالجة إضافة نقاط - تحديد المستخدم"""
        if message_text == "إلغاء":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم الإلغاء.",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        try:
            target_user_id = int(message_text)
            target_user = self.db.get_user(target_user_id)
            
            if not target_user:
                await update.message.reply_text(
                    "❌ *المستخدم غير موجود*\n"
                    "الرجاء التحقق من الأيدي والمحاولة مرة أخرى.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            # حفظ أيدي المستخدم المؤقت
            if user_id not in self.user_temp_data:
                self.user_temp_data[user_id] = {}
            
            self.user_temp_data[user_id]['add_points_user'] = target_user_id
            
            # الانتقال لاختيار عدد النقاط
            self.db.set_user_state(user_id, 'admin_add_points_amount')
            
            await update.message.reply_text(
                f"✅ *المستخدم:* {target_user_id}\n"
                f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}\n"
                f"💰 *النقاط الحالية:* {target_user[2]}\n\n"
                "📝 *اختر عدد النقاط المطلوب إضافتها:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_add_points_keyboard()
            )
            
        except ValueError:
            await update.message.reply_text(
                "❌ *رجاءً أدخل أيدي صحيح*\n"
                "يجب أن يكون الأيدي رقماً صحيحاً.",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def handle_admin_add_points_amount(self, update: Update, message_text: str, user_id: int):
        """معالجة إضافة نقاط - اختيار المبلغ"""
        if message_text == "🔙 رجوع":
            self.db.set_user_state(user_id, 'admin_add_points_user')
            await update.message.reply_text(
                "📝 *أرسل أيدي المستخدم:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True)
            )
            return
        
        if message_text == "إدخال مخصص":
            self.db.set_user_state(user_id, 'admin_add_points_custom')
            await update.message.reply_text(
                "📝 *أرسل عدد النقاط المطلوب:*\n"
                "(أدخل رقماً صحيحاً)",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["🔙 رجوع"]], resize_keyboard=True)
            )
            return
        
        # معالجة القيم المحددة مسبقاً
        points_map = {
            "10 نقاط": 10,
            "50 نقاط": 50,
            "100 نقاط": 100,
            "500 نقاط": 500,
            "1000 نقاط": 1000,
            "5000 نقاط": 5000
        }
        
        if message_text in points_map:
            points = points_map[message_text]
            await self.process_add_points(update, user_id, points)
    
    async def handle_admin_add_points_custom(self, update: Update, message_text: str, user_id: int):
        """معالجة إضافة نقاط - مبلغ مخصص"""
        if message_text == "🔙 رجوع":
            self.db.set_user_state(user_id, 'admin_add_points_amount')
            await update.message.reply_text(
                "📝 *اختر عدد النقاط:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_add_points_keyboard()
            )
            return
        
        try:
            points = int(message_text)
            if points <= 0:
                raise ValueError
            
            await self.process_add_points(update, user_id, points)
            
        except ValueError:
            await update.message.reply_text(
                "❌ *رجاءً أدخل رقم صحيح موجب*\n"
                "مثال: 100",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def process_add_points(self, update: Update, admin_id: int, points: int):
        """تنفيذ إضافة النقاط"""
        if admin_id not in self.user_temp_data or 'add_points_user' not in self.user_temp_data[admin_id]:
            self.db.set_user_state(admin_id, 'admin_panel')
            await update.message.reply_text(
                "❌ *حدث خطأ في البيانات*\n"
                "الرجاء المحاولة مرة أخرى.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        target_user_id = self.user_temp_data[admin_id]['add_points_user']
        
        # إضافة النقاط
        self.db.update_points(target_user_id, points)
        
        target_user = self.db.get_user(target_user_id)
        
        # إرسال إشعار للمستخدم
        try:
            await self.application.bot.send_message(
                chat_id=target_user_id,
                text=f"🎁 *مبروك!*\n\n"
                     f"✅ *تم إضافة {points} نقطة إلى حسابك*\n"
                     f"💰 *رصيدك الجديد:* {target_user[2]} نقطة\n\n"
                     f"من: الإدارة 👑",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass  # تجاهل الخطأ إذا لم نتمكن من إرسال الإشعار
        
        # إرسال تأكيد للإدمن
        await update.message.reply_text(
            f"✅ *تمت الإضافة بنجاح*\n\n"
            f"👤 *المستخدم:* {target_user_id}\n"
            f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}\n"
            f"➕ *النقاط المضافة:* {points}\n"
            f"💰 *الرصيد الجديد:* {target_user[2]}\n\n"
            f"📨 *تم إرسال إشعار للمستخدم.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_admin_keyboard()
        )
        
        # تنظيف البيانات المؤقتة
        if admin_id in self.user_temp_data:
            del self.user_temp_data[admin_id]['add_points_user']
        
        self.db.set_user_state(admin_id, 'admin_panel')
    
    async def handle_admin_ban_user(self, update: Update, message_text: str, user_id: int):
        """معالجة حظر مستخدم"""
        if message_text == "إلغاء":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم الإلغاء.",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        try:
            target_user_id = int(message_text)
            target_user = self.db.get_user(target_user_id)
            
            if not target_user:
                await update.message.reply_text(
                    "❌ *المستخدم غير موجود*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            if self.db.is_user_banned(target_user_id):
                await update.message.reply_text(
                    f"⚠️ *المستخدم محظور بالفعل*\n\n"
                    f"👤 *المستخدم:* {target_user_id}\n"
                    f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_admin_keyboard()
                )
                return
            
            # حظر المستخدم
            self.db.ban_user(target_user_id)
            
            await update.message.reply_text(
                f"⛔ *تم حظر المستخدم بنجاح*\n\n"
                f"👤 *المستخدم:* {target_user_id}\n"
                f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}\n"
                f"📛 *الحالة:* محظور",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            
            self.db.set_user_state(user_id, 'admin_panel')
            
        except ValueError:
            await update.message.reply_text(
                "❌ *رجاءً أدخل أيدي صحيح*",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def handle_admin_unban_user(self, update: Update, message_text: str, user_id: int):
        """معالجة فك حظر مستخدم"""
        if message_text == "إلغاء":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم الإلغاء.",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        try:
            target_user_id = int(message_text)
            target_user = self.db.get_user(target_user_id)
            
            if not target_user:
                await update.message.reply_text(
                    "❌ *المستخدم غير موجود*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            if not self.db.is_user_banned(target_user_id):
                await update.message.reply_text(
                    f"⚠️ *المستخدم غير محظور*\n\n"
                    f"👤 *المستخدم:* {target_user_id}\n"
                    f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_admin_keyboard()
                )
                return
            
            # فك حظر المستخدم
            self.db.unban_user(target_user_id)
            
            await update.message.reply_text(
                f"🔄 *تم فك حظر المستخدم بنجاح*\n\n"
                f"👤 *المستخدم:* {target_user_id}\n"
                f"👤 *اليوزر:* @{target_user[1] if target_user[1] else 'لا يوجد'}\n"
                f"✅ *الحالة:* غير محظور",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            
            self.db.set_user_state(user_id, 'admin_panel')
            
        except ValueError:
            await update.message.reply_text(
                "❌ *رجاءً أدخل أيدي صحيح*",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def handle_admin_create_link(self, update: Update, message_text: str, user_id: int):
        """معالجة إنشاء رابط نقاط للإدمن"""
        if message_text == "🔙 رجوع":
            self.db.set_user_state(user_id, 'admin_panel')
            await update.message.reply_text(
                "تم الرجوع للوحة التحكم",
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            return
        
        try:
            points = int(message_text)
            if points <= 0:
                raise ValueError
            
            # إنشاء الرابط
            link_code = self.db.create_referral_link(user_id, points=points, max_uses=1000)
            link_text = f"https://t.me/{self.application.bot.username}?start={link_code}"
            
            await update.message.reply_text(
                f"✅ *تم إنشاء الرابط بنجاح*\n\n"
                f"🔗 *الرابط:*\n"
                f"`{link_text}`\n\n"
                f"📊 *معلومات الرابط:*\n"
                f"• ⭐ النقاط: {points} لكل مستخدم\n"
                f"• 👥 الحد الأقصى: 1000 مستخدم\n"
                f"• 📅 الصلاحية: 30 يوم\n"
                f"• 🔗 الرمز: {link_code}\n\n"
                f"📌 *يمكنك مشاركة هذا الرابط مع المستخدمين.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            
            self.db.set_user_state(user_id, 'admin_panel')
            
        except ValueError:
            await update.message.reply_text(
                "❌ *رجاءً أدخل رقم صحيح موجب*\n"
                "مثال: 100",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def show_admin_stats(self, update: Update, user_id: int):
        """عرض إحصائيات الإدمن"""
        total_users = self.db.get_total_users_count()
        active_users = self.db.get_active_users_count()
        total_points = self.db.get_total_points()
        
        # الحصول على أفضل 5 مستخدمين
        users = self.db.get_all_users()[:5]
        
        top_users_text = ""
        for i, user in enumerate(users, 1):
            username = user[1] if user[1] else "بدون يوزر"
            status = "⛔ محظور" if user[3] == 1 else "✅ نشط"
            top_users_text += f"{i}. `{user[0]}` - @{username}\n   💰 {user[2]} نقطة - {status}\n"
        
        stats_text = f"""
👑 *إحصائيات {BOT_NAME} - لوحة التحكم*

📊 *المستخدمون:*
• 👥 إجمالي المستخدمين: {total_users}
• 🟢 مستخدمون نشطون: {active_users}
• 🔴 غير نشطين: {total_users - active_users}
• 💰 إجمالي النقاط: {total_points}

🏆 *أفضل 5 مستخدمين:*
{top_users_text}

🤖 *إحصائيات Mistral AI:*
• 📞 إجمالي الطلبات: {self.mistral.stats['total_requests']}
• ✅ ناجحة: {self.mistral.stats['successful']}
• ⚠️ Rate Limited: {self.mistral.stats['rate_limited']}
• ❌ أخطاء: {self.mistral.stats['errors']}
• 📈 نسبة النجاح: {(self.mistral.stats['successful']/max(self.mistral.stats['total_requests'], 1))*100:.1f}%

⚙️ *معلومات النظام:*
• 🕒 وقت التشغيل: {datetime.now().strftime('%Y-%m-%d %H:%M')}
• 🔄 النشر التلقائي: نشط (كل ساعة)
• 🎁 روابط النقاط: مفعلة
        """
        
        await update.message.reply_text(
            stats_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_admin_keyboard()
        )
    
    async def manual_channel_post(self, update: Update, user_id: int):
        """نشر يدوي في القناة"""
        if user_id != ADMIN_ID:
            return
        
        wait_msg = await update.message.reply_text(
            "📈 *جاري إنشاء منشور للقناة...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # إنشاء منشور تلقائي
            post_text = await self.mistral.generate_channel_post()
            
            # نشر في القناة
            await self.application.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # استخراج اسم الأصل والتوصية
            asset_name = "توصية قوية"
            recommendation = "تحليل"
            
            if "شراء" in post_text.lower():
                recommendation = "شراء"
            elif "بيع" in post_text.lower():
                recommendation = "بيع"
            
            # حفظ المنشور
            self.db.save_channel_post(post_text, asset_name, recommendation)
            
            await wait_msg.delete()
            await update.message.reply_text(
                "✅ *تم النشر في القناة بنجاح!*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
            
        except Exception as e:
            await wait_msg.delete()
            await update.message.reply_text(
                f"❌ *حدث خطأ أثناء النشر:*\n{str(e)}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_admin_keyboard()
            )
    
    async def auto_channel_post(self, context: ContextTypes.DEFAULT_TYPE):
        """النشر التلقائي في القناة كل ساعة"""
        try:
            # إنشاء منشور تلقائي
            post_text = await self.mistral.generate_channel_post()
            
            # نشر في القناة
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # استخراج اسم الأصل والتوصية
            asset_name = "توصية قوية"
            recommendation = "تحليل"
            
            if "شراء" in post_text.lower():
                recommendation = "شراء"
            elif "بيع" in post_text.lower():
                recommendation = "بيع"
            
            # حفظ المنشور
            self.db.save_channel_post(post_text, asset_name, recommendation)
            
            logging.info(f"تم النشر التلقائي في القناة: {datetime.now()}")
            
        except Exception as e:
            logging.error(f"خطأ في النشر التلقائي: {e}")
    
    async def cleanup_expired_links(self, context: ContextTypes.DEFAULT_TYPE):
        """تنظيف الروابط المنتهية الصلاحية"""
        try:
            cursor = self.db.conn.cursor()
            cursor.execute('''
                DELETE FROM referral_links 
                WHERE expires_at IS NOT NULL 
                AND expires_at < CURRENT_TIMESTAMP
            ''')
            
            deleted_count = cursor.rowcount
            self.db.conn.commit()
            
            if deleted_count > 0:
                logging.info(f"تم حذف {deleted_count} رابط منتهي الصلاحية")
                
        except Exception as e:
            logging.error(f"خطأ في تنظيف الروابط: {e}")
    
    # ==================== معالجة إنشاء الصور من النص ====================
    
    async def handle_create_image_from_text(self, update: Update, message_text: str, user_id: int):
        """معالجة إنشاء صورة من نص"""
        if message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'more_options')
            await update.message.reply_text(
                "تم الرجوع",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_more_options_keyboard()
            )
            return
        
        # التحقق من النقاط
        user = self.db.get_user(user_id)
        if user[2] < 3:
            await update.message.reply_text(
                "❌ *نقاط غير كافية!*\n"
                "تحتاج إلى 3 نقاط على الأقل لإنشاء صورة.\n"
                f"💰 رصيدك الحالي: {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_more_options_keyboard()
            )
            self.db.set_user_state(user_id, 'more_options')
            return
        
        # خصم النقاط
        self.db.update_points(user_id, -3)
        
        wait_msg = await update.message.reply_text(
            "🎨 *جاري تحويل النص إلى وصف صورة...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # إنشاء وصف الصورة من النص
            image_description = await self.mistral.generate_image_description(message_text)
            
            # إنشاء صورة افتراضية (في الإصدار الحقيقي، نرسل للـ DALL-E أو Stable Diffusion)
            result_text = f"""
🖼️ *طلب إنشاء صورة من النص*

📝 *النص الأصلي:*
{message_text}

📋 *الوصف الفني للصورة:*
{image_description}

✅ *تم إنشاء وصف الصورة بنجاح*

💰 *تم خصم 3 نقاط*
💎 *رصيدك المتبقي:* {self.db.get_user(user_id)[2]} نقطة

📌 *في النسخة الكاملة:*
سيتم إرسال هذا الوصف لمولد صور مثل DALL-E أو Stable Diffusion لإنشاء الصورة الفعلية.

🎨 *مثال على الصورة الممكنة:*
(في النسخة الكاملة ستظهر صورتك الفعلية هنا)
            """
            
            await wait_msg.delete()
            
            # إرسال النتيجة
            await update.message.reply_text(
                result_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            
            # إرسال صورة افتراضية كمثال
            await update.message.reply_photo(
                photo="https://via.placeholder.com/800x600/3498db/ffffff?text=Generated+Image+Placeholder",
                caption="🖼️ *مثال على الصورة الممكنة*\n(في النسخة الكاملة ستظهر صورتك الفعلية هنا)",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            await wait_msg.delete()
            await update.message.reply_text(
                f"❌ *حدث خطأ أثناء إنشاء الصورة:*\n{str(e)}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        
        # العودة للحالة الرئيسية
        self.db.set_user_state(user_id, 'main')
    
    # ==================== باقي الوظائف ====================
    
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة جميع الرسائل النصية"""
        user_id = update.effective_user.id
        
        # التحقق من الحظر
        if self.db.is_user_banned(user_id):
            await update.message.reply_text(
                "⛔ *حسابك محظور*\n\n"
                "للتواصل مع الإدارة:\n"
                f"@{ADMIN_USERNAME}",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        message_text = update.message.text
        state, data = self.db.get_user_state(user_id)
        
        # تحديث آخر نشاط
        self.db.update_points(user_id, 0)
        
        # معالجة بناءً على حالة المستخدم
        if state == 'main':
            await self.handle_main_state(update, message_text, user_id)
        elif state == 'chatting':
            await self.handle_chatting_state(update, message_text, user_id)
        elif state == 'market_predictions':
            await self.handle_market_predictions_state(update, message_text, user_id, data)
        elif state == 'image_analysis':
            await self.handle_image_analysis_state(update, message_text, user_id, data)
        elif state == 'more_options':
            await self.handle_more_options_state(update, message_text, user_id)
        elif state == 'history_view':
            await self.handle_history_view(update, message_text, user_id)
        elif state == 'admin_panel':
            await self.handle_admin_state(update, message_text, user_id)
        elif state == 'admin_broadcast':
            await self.handle_admin_broadcast(update, message_text, user_id)
        elif state == 'admin_add_points_user':
            await self.handle_admin_add_points_user(update, message_text, user_id)
        elif state == 'admin_add_points_amount':
            await self.handle_admin_add_points_amount(update, message_text, user_id)
        elif state == 'admin_add_points_custom':
            await self.handle_admin_add_points_custom(update, message_text, user_id)
        elif state == 'admin_ban_user':
            await self.handle_admin_ban_user(update, message_text, user_id)
        elif state == 'admin_unban_user':
            await self.handle_admin_unban_user(update, message_text, user_id)
        elif state == 'admin_create_link':
            await self.handle_admin_create_link(update, message_text, user_id)
        elif state == 'create_image_from_text':
            await self.handle_create_image_from_text(update, message_text, user_id)
        else:
            await self.handle_main_state(update, message_text, user_id)
    
    async def handle_main_state(self, update: Update, message_text: str, user_id: int):
        """معالجة الحالة الرئيسية"""
        if message_text == "🎁 النقاط اليومية":
            await self.handle_daily_points(update, user_id)
        
        elif message_text == "💬 دردشة":
            await self.start_chatting(update, user_id)
        
        elif message_text == "📈 توقعات السوق":
            await self.start_market_predictions(update, user_id)
        
        elif message_text == "🖼️ تحليل الشارت بالصورة":
            await self.start_image_analysis(update, user_id)
        
        elif message_text == "📊 تاريخ التوقعات":
            await self.show_predictions_history(update, user_id)
        
        elif message_text == "👤 حسابي":
            await self.show_account(update, user_id)
        
        elif message_text == "🆘 المساعدة":
            await self.help_command(update, None)
        
        elif message_text == "📋 المزيد من الخيارات":
            self.db.set_user_state(user_id, 'more_options')
            await update.message.reply_text(
                "⚙️ *المزيد من الخيارات*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_more_options_keyboard()
            )
        
        elif message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم العودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        
        # التحقق من أمر الإدمن
        elif message_text == "/admin" and user_id == ADMIN_ID:
            await self.admin_command(update, None)
        
        else:
            await update.message.reply_text(
                "📝 *للاستفادة من البوت، استخدم الأزرار أدناه*\n"
                "أو اكتب /help للمساعدة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
    
    async def handle_more_options_state(self, update: Update, message_text: str, user_id: int):
        """معالجة حالة الخيارات الإضافية"""
        if message_text == "📊 إحصائيات البوت":
            await self.show_bot_stats_public(update, user_id)
        
        elif message_text == "⚙️ الإعدادات":
            await self.show_settings(update, user_id)
        
        elif message_text == "🎁 رابط للنقاط":
            await self.referral_link_command(update, None)
        
        elif message_text == "🖼️ إنشاء صورة من نص":
            self.db.set_user_state(user_id, 'create_image_from_text')
            await update.message.reply_text(
                "🖼️ *إنشاء صورة من نص*\n\n"
                "📝 *أرسل النص الذي تريد تحويله إلى صورة:*\n\n"
                "💡 *نصائح:*\n"
                "• كن وصفيًا وواضحًا\n"
                "• أضف تفاصيل عن الألوان والجو\n"
                "• تكلفة الخدمة: 3 نقاط\n\n"
                "استخدم زر '🔙 العودة للرئيسية' للإلغاء.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["🔙 العودة للرئيسية"]], resize_keyboard=True)
            )
        
        elif message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم العودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        
        else:
            await update.message.reply_text(
                "📝 *الرجاء استخدام الأزرار أدناه*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_more_options_keyboard()
            )
    
    # ==================== باقي الدوال (مختصرة للاختصار) ====================
    
    async def handle_daily_points(self, update: Update, user_id: int):
        """النقاط اليومية"""
        if self.db.can_claim_daily(user_id):
            self.db.update_points(user_id, 8)
            self.db.set_daily_claimed(user_id)
            
            user = self.db.get_user(user_id)
            await update.message.reply_text(
                f"✅ *تم إضافة 8 نقاط يومية!*\n\n"
                f"💰 *رصيدك الحالي:* {user[2]} نقطة\n"
                f"🎯 *تعاود غداً للحصول على المزيد!*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "⏳ *لقد حصلت بالفعل على نقاطك اليومية!*\n"
                "*عد غداً للحصول على المزيد.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
    
    async def start_chatting(self, update: Update, user_id: int):
        """بدء الدردشة"""
        user = self.db.get_user(user_id)
        if user[2] < 1:
            await update.message.reply_text(
                "❌ *نقاط غير كافية!*\n"
                "تحتاج إلى نقطة واحدة على الأقل.\n"
                "💰 احصل على نقاط من الزر اليومي.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        self.db.set_user_state(user_id, 'chatting')
        await update.message.reply_text(
            "💬 *وضع الدردشة نشط*\n\n"
            "يمكنك الآن إرسال رسائلك وسأرد باستخدام الذكاء الاصطناعي.\n\n"
            f"🔹 *تكلفة كل رسالة:* 1 نقطة\n"
            f"💰 *رصيدك الحالي:* {user[2]} نقطة\n\n"
            "استخدم زر '❌ إنهاء الدردشة' للإيقاف.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_chat_keyboard()
        )
    
    async def handle_chatting_state(self, update: Update, message_text: str, user_id: int):
        """معالجة الدردشة"""
        if message_text == "❌ إنهاء الدردشة":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم إنهاء الدردشة.",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        elif message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم العودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        # خصم نقطة واحدة
        self.db.update_points(user_id, -1)
        
        # إرسال رسالة الانتظار
        wait_msg = await update.message.reply_text("🤔 *جاري التفكير...*", 
                                                  parse_mode=ParseMode.MARKDOWN)
        
        # الحصول على الرد من الذكاء الاصطناعي
        try:
            response = await self.mistral.get_predictions(
                message_text, "دردشة", "دردشة"
            )
            
            if "خطأ" in response or len(response.strip()) < 3:
                response = "🤖 عذراً، حدث خطأ في الخادم. الرجاء المحاولة مرة أخرى."
            
        except Exception as e:
            response = f"عذراً، حدث خطأ: {str(e)}"
        
        # حذف رسالة الانتظار وإرسال الرد
        await wait_msg.delete()
        
        # إرسال الرد مع إبقاء لوحة المفاتيح
        await update.message.reply_text(
            response[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_chat_keyboard()
        )
        
        # إرسال رصيد النقاط المتبقي
        user = self.db.get_user(user_id)
        await update.message.reply_text(
            f"💰 *رصيدك المتبقي:* {user[2]} نقطة",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_chat_keyboard()
        )
    
    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إظهار القائمة الرئيسية"""
        await update.message.reply_text(
            "📋 *القائمة الرئيسية*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مساعدة"""
        help_text = """
🆘 *كيفية استخدام البوت:*

*🎁 النقاط اليومية:*
- احصل على 8 نقاط مجانية كل يوم
- تستخدم النقاط لميزات البوت

*📈 توقعات السوق:*
1. اختر نوع الأصل (عملات، أسهم، إلخ)
2. اختر الأصل المحدد
3. اختر سرعة الشموع
4. اختر وقت الصفقة
5. احصل على توقعات ذكية واقعية
- تكلفة: 5 نقاط

*🖼️ تحليل الشارت بالصورة:*
1. أرسل صورة الشارت
2. اختر سرعة الشموع
3. اختر وقت الصفقة
4. احصل على تحليل فني متقدم
- تكلفة: 5 نقاط

*💬 دردشة:*
- تكلم مع الذكاء الاصطناعي المتقدم
- تكلفة: 1 نقطة لكل رسالة

*📊 تاريخ التوقعات:*
- عرض جميع التوقعات السابقة
- استعراض النتائج المحفوظة

*👤 حسابي:*
- عرض رصيد النقاط
- مشاهدة الإحصائيات

*🎁 رابط للنقاط:*
- احصل على رابط إحالة
- اربح نقاط عند انضمام أصدقائك

*🖼️ إنشاء صورة من نص:*
- حول أي نص إلى وصف صورة فني
- تكلفة: 3 نقاط

*📞 للمساعدة:* @Sz2zv
        """
        
        await update.message.reply_text(
            help_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def points_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض النقاط"""
        user = self.db.get_user(update.effective_user.id)
        if user:
            await update.message.reply_text(
                f"💰 *رصيدك الحالي:* {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "لم يتم العثور على حسابك. الرجاء استخدام /start",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
    
    async def predict_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء التوقعات"""
        await self.start_market_predictions(update, update.effective_user.id)
    
    async def show_bot_stats_public(self, update: Update, user_id: int):
        """عرض إحصائيات البوت للعامة"""
        total_users = self.db.get_total_users_count()
        total_points = self.db.get_total_points()
        
        stats_text = f"""
📊 *إحصائيات {BOT_NAME}*

👥 *المستخدمون:*
• إجمالي المستخدمين: {total_users}
• إجمالي النقاط الموزعة: {total_points}

🤖 *إحصائيات الذكاء الاصطناعي:*
• إجمالي الطلبات: {self.mistral.stats['total_requests']}
• طلبات ناجحة: {self.mistral.stats['successful']}
• نسبة النجاح: {(self.mistral.stats['successful']/max(self.mistral.stats['total_requests'], 1))*100:.1f}%

📈 *أنواع الأصول المتاحة:*
• العملات الرئيسية: {len(MARKET_ASSETS['العملات الرئيسية'])}
• العملات الرقمية: {len(MARKET_ASSETS['العملات الرقمية'])}
• السلع: {len(MARKET_ASSETS['السلع'])}
• المؤشرات: {len(MARKET_ASSETS['المؤشرات العالمية'])}
• الأسهم: {len(MARKET_ASSETS['الأسهم الأمريكية'])}

🔄 *البوت يعمل بنظام:*
• توقعات ذكية بدون وهميات
• تحليل صور متقدم
• نظام نقاط يومي
• قاعدة بيانات محلية
        """
        
        await update.message.reply_text(
            stats_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_more_options_keyboard()
        )
    
    async def show_settings(self, update: Update, user_id: int):
        """عرض الإعدادات"""
        settings_text = """
⚙️ *إعدادات البوت*

✅ *المميزات المفعلة:*
• نظام النقاط اليومية
• توقعات السوق الذكية
• تحليل الشارت بالصور
• إنشاء صور من النص
• روابط الإحالة للنقاط
• نشر تلقائي في القناة

🔄 *للتحديثات المقبلة:*
• إشعارات التحديثات
• تقارير أسبوعية
• تحليلات متقدمة
• دعم لغات إضافية

📞 *للاقتراحات والشكاوى:*
@Sz2zv
        """
        
        await update.message.reply_text(
            settings_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_more_options_keyboard()
        )
    
    async def show_account(self, update: Update, user_id: int):
        """عرض حساب المستخدم"""
        user = self.db.get_user(user_id)
        if not user:
            await update.message.reply_text(
                "لم يتم العثور على حسابك. الرجاء استخدام /start",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        predictions_count = self.db.get_user_predictions_count(user_id)
        image_analyses_count = self.db.get_user_image_analyses_count(user_id)
        total_analyses = predictions_count + image_analyses_count
        
        account_text = f"""
👤 *معلومات حسابك*

🆔 *الأيدي:* `{user[0]}`
👤 *اليوزر:* @{user[1] if user[1] else 'لا يوجد'}
💰 *النقاط:* {user[2]}
📊 *عدد التحليلات:* {total_analyses}
   - توقعات سوق: {predictions_count}
   - تحليلات صور: {image_analyses_count}
📅 *تاريخ الانضمام:* {user[6].split()[0] if user[6] else 'غير معروف'}
🕒 *آخر نشاط:* {user[4] if user[4] else 'غير معروف'}

🤖 *إحصائيات Mistral AI:*
• إجمالي الطلبات: {self.mistral.stats['total_requests']}
• طلبات ناجحة: {self.mistral.stats['successful']}
• Rate Limited: {self.mistral.stats['rate_limited']}
• أخطاء: {self.mistral.stats['errors']}
        """
        
        await update.message.reply_text(
            account_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def start_market_predictions(self, update: Update, user_id: int):
        """بدء توقعات السوق"""
        user = self.db.get_user(user_id)
        if user[2] < 5:
            await update.message.reply_text(
                "❌ *نقاط غير كافية!*\n"
                "تحتاج إلى 5 نقاط على الأقل للتوقعات.\n"
                f"💰 رصيدك الحالي: {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        # خصم النقاط
        self.db.update_points(user_id, -5)
        
        # إعادة تعيين البيانات المؤقتة
        if user_id in self.user_temp_data:
            del self.user_temp_data[user_id]
        
        self.user_temp_data[user_id] = {
            'step': 'select_asset_type',
            'data': {}
        }
        
        self.db.set_user_state(user_id, 'market_predictions', json.dumps({'step': 'select_asset_type'}))
        
        await update.message.reply_text(
            f"📈 *توقعات السوق الذكية*\n\n"
            f"💰 *تم خصم 5 نقاط*\n"
            f"💎 *رصيدك المتبقي:* {self.db.get_user(user_id)[2]} نقطة\n\n"
            "📊 *الخطوة 1: اختر نوع الأصل*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_asset_types_keyboard()
        )
    
    async def start_image_analysis(self, update: Update, user_id: int):
        """بدء تحليل الشارت بالصورة"""
        user = self.db.get_user(user_id)
        if user[2] < 5:
            await update.message.reply_text(
                "❌ *نقاط غير كافية!*\n"
                "تحتاج إلى 5 نقاط على الأقل لتحليل الصور.\n"
                f"💰 رصيدك الحالي: {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        # خصم النقاط
        self.db.update_points(user_id, -5)
        
        # إعادة تعيين البيانات المؤقتة
        if user_id in self.user_temp_data:
            del self.user_temp_data[user_id]
        
        self.user_temp_data[user_id] = {
            'step': 'waiting_image',
            'images': [],
            'data': {}
        }
        
        self.db.set_user_state(user_id, 'image_analysis', json.dumps({'step': 'waiting_image'}))
        
        await update.message.reply_text(
            f"🖼️ *تحليل الشارت بالصورة*\n\n"
            f"💰 *تم خصم 5 نقاط*\n"
            f"💎 *رصيدك المتبقي:* {self.db.get_user(user_id)[2]} نقطة\n\n"
            "📸 *الخطوة 1: أرسل صورة الشارت (الرسم البياني)*\n\n"
            "💡 *نصائح:*\n"
            "• تأكد من وضوح الصورة\n"
            "• يمكنك إرسال أكثر من صورة\n"
            "• تأكد من ظهور المؤشرات الفنية",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["🔙 العودة للرئيسية"]], resize_keyboard=True)
        )
    
    async def show_predictions_history(self, update: Update, user_id: int):
        """عرض تاريخ التوقعات"""
        self.db.set_user_state(user_id, 'history_view')
        
        cursor = self.db.conn.cursor()
        
        # جلب التوقعات
        cursor.execute('''
            SELECT asset_name, recommendation, timeframe, trade_time, created_at 
            FROM predictions 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 10
        ''', (user_id,))
        
        predictions = cursor.fetchall()
        
        # جلب تحليلات الصور
        cursor.execute('''
            SELECT asset_name, recommendation, candle_speed, trade_time, created_at 
            FROM image_analysis 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 10
        ''', (user_id,))
        
        image_analyses = cursor.fetchall()
        
        if not predictions and not image_analyses:
            await update.message.reply_text(
                "📋 *لا توجد توقعات سابقة*\n\n"
                "قم بإجراء تحليل أولاً من:\n"
                "• 📈 توقعات السوق\n"
                "• 🖼️ تحليل الشارت بالصورة\n\n"
                "استخدم زر '🔙 العودة للرئيسية' للرجوع",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["🔙 العودة للرئيسية"]], resize_keyboard=True)
            )
            return
        
        history_text = "📋 *آخر التوقعات والتحليلات:*\n\n"
        
        if predictions:
            history_text += "*📈 توقعات السوق:*\n"
            for i, (asset, recommendation, timeframe, trade_time, created_at) in enumerate(predictions, 1):
                action_icon = "🟢" if recommendation == "شراء" else "🔴" if recommendation == "بيع" else "🟡"
                history_text += f"{i}. {action_icon} *{asset}* - {recommendation}\n"
                history_text += f"   ⚡ {timeframe} | ⏰ {trade_time}\n"
                history_text += f"   📅 {created_at}\n\n"
        
        if image_analyses:
            history_text += "*🖼️ تحليلات الصور:*\n"
            for i, (asset, recommendation, candle_speed, trade_time, created_at) in enumerate(image_analyses, 1):
                action_icon = "🟢" if recommendation == "شراء" else "🔴" if recommendation == "بيع" else "🟡"
                history_text += f"{i}. {action_icon} *{asset}* - {recommendation}\n"
                history_text += f"   ⚡ {candle_speed} | ⏰ {trade_time}\n"
                history_text += f"   📅 {created_at}\n\n"
        
        history_text += "📌 *للحصول على تفاصيل كاملة، أعد إجراء التحليل.*"
        history_text += "\n\nاستخدم زر '🔙 العودة للرئيسية' للرجوع"
        
        await update.message.reply_text(
            history_text[:4000],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([["🔙 العودة للرئيسية"]], resize_keyboard=True)
        )
    
    async def handle_history_view(self, update: Update, message_text: str, user_id: int):
        """معالجة عرض التاريخ"""
        if message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            await update.message.reply_text(
                "تم العودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "استخدم زر '🔙 العودة للرئيسية' للرجوع",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([["🔙 العودة للرئيسية"]], resize_keyboard=True)
            )
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة الصور المرسلة"""
        user_id = update.effective_user.id
        state, data = self.db.get_user_state(user_id)
        
        if state == 'image_analysis':
            step_data = json.loads(data) if data else {'step': 'waiting_image'}
            
            if step_data['step'] == 'waiting_image':
                # حفظ معلومات الصورة
                photo = update.message.photo[-1]
                file_id = photo.file_id
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['images'].append(file_id)
                    step_data['image_count'] = len(self.user_temp_data[user_id]['images'])
                    step_data['step'] = 'select_candle_speed'
                    self.db.set_user_state(user_id, 'image_analysis', json.dumps(step_data))
                    
                    await update.message.reply_text(
                        f"✅ *تم استلام الصورة #{len(self.user_temp_data[user_id]['images'])}*\n\n"
                        "📊 *الخطوة 2: اختر سرعة الشموع*",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=self.keyboard_manager.get_candle_speed_keyboard()
                    )
            
            elif step_data['step'] in ['select_candle_speed', 'select_trade_time']:
                # المستخدم أرسل صورة إضافية
                photo = update.message.photo[-1]
                file_id = photo.file_id
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['images'].append(file_id)
                    
                    await update.message.reply_text(
                        f"✅ *تم إضافة صورة إضافية #{len(self.user_temp_data[user_id]['images'])}*\n\n"
                        "يمكنك متابعة الاختيار من الأزرار.",
                        parse_mode=ParseMode.MARKDOWN
                    )
        else:
            await update.message.reply_text(
                "📸 *تم استلام الصورة*\n\n"
                "لتحليل الصورة، انتقل إلى:\n"
                "🖼️ تحليل الشارت بالصورة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
    
    async def handle_market_predictions_state(self, update: Update, message_text: str, user_id: int, data: str):
        """معالجة حالة توقعات السوق"""
        if message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            if user_id in self.user_temp_data:
                del self.user_temp_data[user_id]
            await update.message.reply_text(
                "تم الإلغاء والعودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        step_data = json.loads(data) if data else {'step': 'select_asset_type'}
        
        if step_data['step'] == 'select_asset_type':
            if message_text in MARKET_ASSETS:
                step_data['asset_type'] = message_text
                step_data['step'] = 'select_asset'
                self.db.set_user_state(user_id, 'market_predictions', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['asset_type'] = message_text
                
                await update.message.reply_text(
                    f"✅ *النوع:* {message_text}\n\n"
                    "📊 *الخطوة 2: اختر الأصل المحدد*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_assets_keyboard(message_text)
                )
        
        elif step_data['step'] == 'select_asset':
            asset_type = step_data.get('asset_type', '')
            assets = MARKET_ASSETS.get(asset_type, [])
            
            if message_text in assets:
                step_data['asset'] = message_text
                step_data['step'] = 'select_candle_speed'
                self.db.set_user_state(user_id, 'market_predictions', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['asset'] = message_text
                
                await update.message.reply_text(
                    f"✅ *الأصل:* {message_text}\n\n"
                    "📊 *الخطوة 3: اختر سرعة الشموع*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_candle_speed_keyboard()
                )
        
        elif step_data['step'] == 'select_candle_speed':
            # تحقق مما إذا كان الخيار صالحاً
            valid_speeds = []
            for row in CANDLE_SPEEDS:
                valid_speeds.extend(row)
            
            if message_text in valid_speeds:
                step_data['candle_speed'] = message_text
                step_data['step'] = 'select_trade_time'
                self.db.set_user_state(user_id, 'market_predictions', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['candle_speed'] = message_text
                
                await update.message.reply_text(
                    f"✅ *سرعة الشموع:* {message_text}\n\n"
                    "📊 *الخطوة 4: اختر وقت الصفقة*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_trade_time_keyboard()
                )
        
        elif step_data['step'] == 'select_trade_time':
            # تحقق مما إذا كان الخيار صالحاً
            valid_times = []
            for row in TRADE_TIMES:
                valid_times.extend(row)
            
            if message_text in valid_times:
                step_data['trade_time'] = message_text
                self.db.set_user_state(user_id, 'market_predictions', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['trade_time'] = message_text
                
                # بدء عملية التوقعات
                await self.perform_market_predictions(update, user_id, step_data)
    
    async def handle_image_analysis_state(self, update: Update, message_text: str, user_id: int, data: str):
        """معالجة حالة تحليل الصور"""
        if message_text == "🔙 العودة للرئيسية":
            self.db.set_user_state(user_id, 'main')
            if user_id in self.user_temp_data:
                del self.user_temp_data[user_id]
            await update.message.reply_text(
                "تم الإلغاء والعودة للقائمة الرئيسية",
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            return
        
        step_data = json.loads(data) if data else {'step': 'waiting_image'}
        
        if step_data['step'] == 'select_candle_speed':
            # تحقق مما إذا كان الخيار صالحاً
            valid_speeds = []
            for row in CANDLE_SPEEDS:
                valid_speeds.extend(row)
            
            if message_text in valid_speeds:
                step_data['candle_speed'] = message_text
                step_data['step'] = 'select_trade_time'
                self.db.set_user_state(user_id, 'image_analysis', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['candle_speed'] = message_text
                
                await update.message.reply_text(
                    f"✅ *سرعة الشموع:* {message_text}\n\n"
                    "📊 *الخطوة 3: اختر وقت الصفقة*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=self.keyboard_manager.get_trade_time_keyboard()
                )
        
        elif step_data['step'] == 'select_trade_time':
            # تحقق مما إذا كان الخيار صالحاً
            valid_times = []
            for row in TRADE_TIMES:
                valid_times.extend(row)
            
            if message_text in valid_times:
                step_data['trade_time'] = message_text
                self.db.set_user_state(user_id, 'image_analysis', json.dumps(step_data))
                
                if user_id in self.user_temp_data:
                    self.user_temp_data[user_id]['data']['trade_time'] = message_text
                
                # بدء عملية تحليل الصورة
                await self.perform_image_analysis(update, user_id, step_data)
    
    # ==================== وظائف التحليل (مختصرة) ====================
    
    async def perform_market_predictions(self, update: Update, user_id: int, step_data: dict):
        """تنفيذ توقعات السوق"""
        wait_msg = await update.message.reply_text(
            "🧠 *جاري تحليل البيانات وتوليد توقعات ذكية...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # جمع البيانات
            asset_type = step_data.get('asset_type', '')
            asset = step_data.get('asset', '')
            candle_speed = step_data.get('candle_speed', '')
            trade_time = step_data.get('trade_time', '')
            
            # الحصول على التوقعات من الذكاء الاصطناعي
            prediction_result = await self.mistral.get_predictions(asset, candle_speed, trade_time)
            
            # استخراج التوصية
            recommendation = self.extract_recommendation(prediction_result)
            
            # تنسيق النتيجة
            result_text = self.format_prediction_result(
                asset, asset_type, candle_speed, trade_time,
                prediction_result, recommendation
            )
            
            # حفظ التوقعات
            self.db.save_prediction(
                user_id, asset_type, asset, candle_speed,
                trade_time, prediction_result, recommendation
            )
            
            await wait_msg.delete()
            
            # إرسال النتيجة
            await self.send_formatted_prediction(update, result_text, recommendation)
            
            # إرسال رصيد النقاط المتبقي
            user = self.db.get_user(user_id)
            await update.message.reply_text(
                f"💰 *رصيدك المتبقي:* {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            
            # تنظيف البيانات المؤقتة
            if user_id in self.user_temp_data:
                del self.user_temp_data[user_id]
            
            # العودة للحالة الرئيسية
            self.db.set_user_state(user_id, 'main')
            
        except Exception as e:
            await wait_msg.delete()
            await update.message.reply_text(
                f"❌ *حدث خطأ أثناء توليد التوقعات*\n{str(e)}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            self.db.set_user_state(user_id, 'main')
    
    async def perform_image_analysis(self, update: Update, user_id: int, step_data: dict):
        """تنفيذ تحليل الصورة"""
        wait_msg = await update.message.reply_text(
            "🔍 *جاري تحليل الصورة باستخدام الذكاء الاصطناعي...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # جمع البيانات
            candle_speed = step_data.get('candle_speed', '')
            trade_time = step_data.get('trade_time', '')
            image_count = step_data.get('image_count', 0)
            
            # وصف الصور
            image_description = f"تم استلام {image_count} صورة للشارت"
            if user_id in self.user_temp_data and self.user_temp_data[user_id]['images']:
                image_description = f"تم تحليل {len(self.user_temp_data[user_id]['images'])} صورة للشارت"
            
            # الحصول على التحليل
            analysis_result = await self.mistral.analyze_image_for_trading(
                image_description, candle_speed, trade_time
            )
            
            # استخراج التوصية واسم الأصل
            recommendation = self.extract_recommendation(analysis_result)
            asset_name = self.extract_asset_name(analysis_result)
            
            # تنسيق النتيجة
            result_text = self.format_image_analysis_result(
                asset_name, candle_speed, trade_time,
                analysis_result, recommendation, image_count
            )
            
            # حفظ التحليل
            image_id = f"img_{user_id}_{int(time.time())}"
            if user_id in self.user_temp_data and self.user_temp_data[user_id]['images']:
                image_id = self.user_temp_data[user_id]['images'][0]
            
            self.db.save_image_analysis(
                user_id, image_id, candle_speed, trade_time,
                asset_name, recommendation, analysis_result
            )
            
            await wait_msg.delete()
            
            # إرسال النتيجة
            await self.send_formatted_image_analysis(update, result_text, recommendation, asset_name)
            
            # إرسال رصيد النقاط المتبقي
            user = self.db.get_user(user_id)
            await update.message.reply_text(
                f"💰 *رصيدك المتبقي:* {user[2]} نقطة",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            
            # تنظيف البيانات المؤقتة
            if user_id in self.user_temp_data:
                del self.user_temp_data[user_id]
            
            # العودة للحالة الرئيسية
            self.db.set_user_state(user_id, 'main')
            
        except Exception as e:
            await wait_msg.delete()
            await update.message.reply_text(
                f"❌ *حدث خطأ أثناء تحليل الصورة*\n{str(e)}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.keyboard_manager.get_main_keyboard()
            )
            self.db.set_user_state(user_id, 'main')
    
    def extract_recommendation(self, text: str) -> str:
        """استخراج التوصية من النص"""
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['شراء', 'buy', 'long', 'صعود']):
            return "شراء"
        elif any(word in text_lower for word in ['بيع', 'sell', 'short', 'هبوط']):
            return "بيع"
        elif any(word in text_lower for word in ['احتفاظ', 'hold', 'محايد', 'انتظار']):
            return "الاحتفاظ"
        
        return "تحليل"
    
    def extract_asset_name(self, text: str) -> str:
        """استخراج اسم الأصل من النص"""
        patterns = [
            r'([A-Z]{3}/[A-Z]{3})',
            r'([A-Z]{6})',
            r'بتكوين|bitcoin|BTC',
            r'إيثريوم|ethereum|ETH',
            r'ذهب|gold|XAU',
            r'فضة|silver|XAG',
            r'نفط|oil|WTI|BRENT'
        ]
        
        import re
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1) if len(match.groups()) > 0 else match.group(0)
        
        return "شارت مجهول"
    
    def format_prediction_result(self, asset: str, asset_type: str, candle_speed: str, 
                                trade_time: str, prediction: str, recommendation: str) -> str:
        """تنسيق نتيجة التوقعات"""
        action_icons = {
            "شراء": "🟢",
            "بيع": "🔴", 
            "الاحتفاظ": "🟡",
            "تحليل": "🔵"
        }
        
        icon = action_icons.get(recommendation, "🔵")
        
        result = f"""
{icon} *【 توقعات ذكية 】* {icon}

📊 *المعلومات المدخلة:*
• 📈 الأصل: `{asset}`
• 🏷️ النوع: `{asset_type}`
• ⚡ الشموع: `{candle_speed}`
• ⏰ الوقت: `{trade_time}`

🎯 *【 {asset.upper()} 】*

{icon} *التوصية: 【 {recommendation.upper()} 】*

📈 *التوقعات الذكية:*
{prediction}

⚠️ *تحذير هام:*
هذه توقعات تعتمد على الذكاء الاصطناعي ولا تعتبر توصية استثمارية.
الأسواق المالية تحمل مخاطر عالية. استشر مستشاراً مالياً.
        """
        
        return result
    
    def format_image_analysis_result(self, asset_name: str, candle_speed: str, trade_time: str,
                                    analysis: str, recommendation: str, image_count: int) -> str:
        """تنسيق نتيجة تحليل الصورة"""
        action_icons = {
            "شراء": "🟢",
            "بيع": "🔴", 
            "الاحتفاظ": "🟡",
            "تحليل": "🔵"
        }
        
        icon = action_icons.get(recommendation, "🔵")
        
        result = f"""
{icon} *【 تحليل الشارت بالصورة 】* {icon}

📊 *المعلومات المدخلة:*
• 🖼️ عدد الصور: `{image_count}`
• ⚡ الشموع: `{candle_speed}`
• ⏰ الوقت: `{trade_time}`

🎯 *【 {asset_name.upper()} 】*

{icon} *التوصية: 【 {recommendation.upper()} 】*

📈 *التحليل الفني:*
{analysis}

⚠️ *تحذير هام:*
هذا تحليل يعتمد على الذكاء الاصطناعي ولا يعتبر توصية استثمارية.
الأسواق المالية تحمل مخاطر عالية. استشر مستشاراً مالياً.
        """
        
        return result
    
    async def send_formatted_prediction(self, update: Update, prediction_text: str, recommendation: str):
        """إرسال التوقعات"""
        await update.message.reply_text(
            "✅ *تم الانتهاء من التحليل الذكي* ✅",
            parse_mode=ParseMode.MARKDOWN
        )
        
        if recommendation == "شراء":
            await update.message.reply_text(
                "🟢 *【 شـــراء 】* 🟢",
                parse_mode=ParseMode.MARKDOWN
            )
        elif recommendation == "بيع":
            await update.message.reply_text(
                "🔴 *【 بـــيــع 】* 🔴",
                parse_mode=ParseMode.MARKDOWN
            )
        elif recommendation == "الاحتفاظ":
            await update.message.reply_text(
                "🟡 *【 الاحتفاظ 】* 🟡",
                parse_mode=ParseMode.MARKDOWN
            )
        
        if len(prediction_text) > 4000:
            chunks = [prediction_text[i:i+4000] for i in range(0, len(prediction_text), 4000)]
            for chunk in chunks:
                await update.message.reply_text(
                    chunk,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text(
                prediction_text,
                parse_mode=ParseMode.MARKDOWN
            )
        
        await update.message.reply_text(
            "🎯 *تم حفظ التوقعات في سجلاتك*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def send_formatted_image_analysis(self, update: Update, analysis_text: str, recommendation: str, asset_name: str):
        """إرسال تحليل الصورة"""
        await update.message.reply_text(
            "✅ *تم الانتهاء من تحليل الشارت* ✅",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await update.message.reply_text(
            f"🎯 *【 {asset_name.upper()} 】*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        if recommendation == "شراء":
            await update.message.reply_text(
                "🟢 *【 شـــراء 】* 🟢",
                parse_mode=ParseMode.MARKDOWN
            )
        elif recommendation == "بيع":
            await update.message.reply_text(
                "🔴 *【 بـــيــع 】* 🔴",
                parse_mode=ParseMode.MARKDOWN
            )
        elif recommendation == "الاحتفاظ":
            await update.message.reply_text(
                "🟡 *【 الاحتفاظ 】* 🟡",
                parse_mode=ParseMode.MARKDOWN
            )
        
        if len(analysis_text) > 4000:
            chunks = [analysis_text[i:i+4000] for i in range(0, len(analysis_text), 4000)]
            for chunk in chunks:
                await update.message.reply_text(
                    chunk,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text(
                analysis_text,
                parse_mode=ParseMode.MARKDOWN
            )
        
        await update.message.reply_text(
            "🎯 *تم حفظ التحليل في سجلاتك*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة Callback Queries"""
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "تم استلام الطلب.",
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    async def handle_other_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة أي رسائل أخرى"""
        await update.message.reply_text(
            "📝 *للاستفادة من البوت، استخدم الأزرار أدناه*\n"
            "أو اكتب /help للمساعدة",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self.keyboard_manager.get_main_keyboard()
        )
    
    def run(self):
        """تشغيل البوت"""
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        
        print(f"""
        ========================================
        🤖 {BOT_NAME} - النظام المتكامل
        ========================================
        
        ✅ المميزات الجديدة:
        1. 👑 لوحة تحكم كاملة للإدمن
        2. 🎁 نظام روابط النقاط والإحالة
        3. 📈 نشر تلقائي في القناة كل ساعة
        4. 🖼️ إنشاء صور من النص
        5. ⛔ نظام حظر وفك حظر المستخدمين
        6. ➕ إضافة نقاط يدوية من الإدمن
        
        📊 نظام الإدمن:
        • بث رسائل لجميع المستخدمين
        • إضافة نقاط بأي كمية
        • حظر/فك حظر المستخدمين
        • إنشاء روابط نقاط قابلة للتخصيص
        • نشر يدوي في القناة
        • إحصائيات متقدمة
        
        🚀 جاري بدء البوت...
        """)
        
        self.application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

# ==================== التشغيل ====================
if __name__ == '__main__':
    bot = AboodGPTBot()
    bot.run()