import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os, time, math, subprocess, json, aiohttp
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
import yt_dlp
from pyrogram.errors import MessageNotModified, FloodWait
from yt_dlp.networking.impersonate import ImpersonateTarget

# ==========================================
# 0. FIREBASE DATABASE SETUP (REST API)
# ==========================================
# Aapka Firebase Database URL
FIREBASE_URL = "https://fastdldbot-default-rtdb.firebaseio.com"

async def get_coins(user_id):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FIREBASE_URL}/users/{user_id}/coins.json") as resp:
                data = await resp.json()
                if data is None:
                    # Naya user aaya hai, 5 coins de do
                    await update_coins_db(user_id, 5, is_set=True)
                    return 5
                return int(data)
    except Exception as e:
        print("Firebase Error:", e)
        return 0

async def update_coins_db(user_id, amount, is_set=False):
    try:
        if is_set:
            new_coins = amount
        else:
            current = await get_coins(user_id)
            new_coins = current + amount
            
        async with aiohttp.ClientSession() as session:
            async with session.put(f"{FIREBASE_URL}/users/{user_id}/coins.json", json=new_coins) as resp:
                return new_coins
    except Exception as e:
        print("Firebase PUT Error:", e)
        return 0

# ==========================================
# 1. BOT CREDENTIALS
# ==========================================
API_ID = int(os.environ.get("API_ID", 0))        
API_HASH = os.environ.get("API_HASH", "")    
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

download_queue = asyncio.Queue()
queue_display = [] 
CANCEL_TASKS = {}
STOP_UPLOAD = {} 
URL_CACHE = {} 
GLOBAL_CANCEL = False

def format_bytes(size):
    size = int(size)
    if not size: return '0 B'
    power = 2**10
    n = 0
    dic_powerN = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power: size /= power; n += 1
    return f"{round(size, 2)} {dic_powerN[n]}"

def get_formats(url):
    ydl_opts = {'socket_timeout': 15, 'retries': 3, 'quiet': True, 'noplaylist': True, 'impersonate': ImpersonateTarget.from_str('chrome'), 'extractor_args': {'youtube': ['player_client=ios,android']}, 'http_headers': {'User-Agent': 'Mozilla/5.0'}}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get('formats', [])
        resolutions = set()
        for f in formats:
            h = f.get('height')
            if h and isinstance(h, int) and h >= 144: resolutions.add(h)
        common_res = [144, 240, 360, 480, 720, 1080, 1440, 2160]
        available_res = sorted([r for r in resolutions if r in common_res])
        if not available_res: available_res = sorted(list(resolutions)) 
        return available_res, info.get('extractor_key', 'Unknown Website')

# ==========================================
# 2. TELEGRAM HANDLERS
# ==========================================
@app.on_message(filters.command("start"))
async def start(client, message):
    # 🔴 APNA ASLI BLOGGER WALA LINK DALEIN 🔴
    BLOGGER_URL = "https://unidl.blogspot.com"
    
    markup = ReplyKeyboardMarkup(
        [[KeyboardButton("🎬 Open Downloader App", web_app=WebAppInfo(url=BLOGGER_URL))]],
        resize_keyboard=True
    )
    
    coins = await get_coins(message.from_user.id)
    
    text = (
        "Hello! Main Premium Downloader hoon.\n\n"
        f"🪙 **Your Coins:** {coins}\n"
        "*(1 Download = 1 Coin. Get free coins by watching ads!)*\n\n"
        "👇 Niche diye gaye **Open Downloader App** button pe click karo!"
    )
    await message.reply_text(text, reply_markup=markup)


@app.on_message(filters.private & ~filters.command(["start"]))
async def handle_requests(client, message):
    user_id = message.from_user.id
    url = None
    
    # URL kahan se aaya? WebApp se ya Direct Text se?
    if getattr(message, "web_app_data", None):
        url = message.web_app_data.data
    elif message.text and message.text.startswith("http"):
        url = message.text.strip()
        
    if url:
        # User ke Coins Firebase se check karo
        coins = await get_coins(user_id)
        
        if coins <= 0:
            await message.reply_text("❌ Aapke paas 0 Coins hain! Please **Open Downloader App** pe click karein aur free Coins claim karein.")
            return
            
        # 1 Coin cut karo Firebase me
        await update_coins_db(user_id, -1)
        remaining = coins - 1
        
        msg = await message.reply_text(f"🔍 Checking link...\n🪙 Remaining Coins: {remaining}")
        URL_CACHE[msg.id] = url
        
        try:
            res_list, website = await asyncio.to_thread(get_formats, url)
            if not res_list: res_list = [360, 480, 720, 1080] 
            
            buttons = []
            row = []
            for res in res_list:
                row.append(InlineKeyboardButton(f"🎬 {res}p", callback_data=f"res_{res}_{msg.id}"))
                if len(row) == 2:
                    buttons.append(row); row = []
            if row: buttons.append(row) 
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")])
            
            await msg.edit_text(f"**🔗 Link:** {url}\n**🌐 Source:** {website}\n\n👇 **Select Quality:**", reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
        except Exception as e:
            # Agar Error aaya, toh Coin wapas de do
            await update_coins_db(user_id, 1)
            await msg.edit_text(f"❌ Error: {str(e)}\n(Coin Refunded)")

# (Baaki aapka original select_resolution, download_with_ytdlp, aur process_queue logic wahi rahega)

if __name__ == "__main__":
    print("Bot is running with Firebase Database!")
    app.run()
    
