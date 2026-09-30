import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os, time, math, subprocess, aiohttp, uuid, glob, shutil
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
    InputMediaPhoto, InputMediaVideo
)
import yt_dlp
from pyrogram.errors import MessageNotModified, FloodWait
from yt_dlp.networking.impersonate import ImpersonateTarget

# ==========================================
# 0. FIREBASE DATABASE SETUP
# ==========================================
FIREBASE_URL = "https://fastdldbot-default-rtdb.firebaseio.com"

async def get_coins(user_id):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FIREBASE_URL}/users/{user_id}/coins.json") as resp:
                data = await resp.json()
                if data is None:
                    await update_coins_db(user_id, 5, is_set=True)
                    return 5
                return int(data)
    except Exception:
        return 0

async def update_coins_db(user_id, amount, is_set=False):
    try:
        new_coins = amount if is_set else (await get_coins(user_id)) + amount
        async with aiohttp.ClientSession() as session:
            async with session.put(f"{FIREBASE_URL}/users/{user_id}/coins.json", json=new_coins) as resp:
                return new_coins
    except Exception:
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

# ==========================================
# 2. HELPER FUNCTIONS & DOWNLOADERS
# ==========================================
def format_bytes(size):
    if size is None: return '0 B'
    try: size = int(size)
    except: return '0 B'
    if not size: return '0 B'
    power = 2**10
    n = 0
    dic_powerN = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power: size /= power; n += 1
    return f"{round(size, 2)} {dic_powerN[n]}"

def generate_thumbnail(video_path, thumbnail_path):
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "00:00:50", "-i", video_path, "-vframes", "1", "-q:v", "2", "-vf", "scale=320:-1", thumbnail_path, "-y"]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0: return thumbnail_path
    except Exception: pass
    return None

def get_formats(url):
    ydl_opts = {'socket_timeout': 15, 'retries': 3, 'quiet': True, 'noplaylist': True, 'impersonate': ImpersonateTarget.from_str('chrome'), 'extractor_args': {'youtube': ['player_client=ios,android']}, 'http_headers': {'User-Agent': 'Mozilla/5.0'}}
    try:
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
    except Exception as e:
        if "No video" in str(e) or "video formats found" in str(e):
            return [0], "Instagram/Gallery" 
        raise e

def run_gallery_dl(url):
    uid = str(uuid.uuid4())
    out_dir = f"downloads/{uid}"
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["gallery-dl", "-d", out_dir, url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = []
    for ext in ('*.jpg', '*.jpeg', '*.png', '*.webp', '*.mp4'):
        files.extend(glob.glob(os.path.join(out_dir, "**", ext), recursive=True))
    return files, out_dir

class CancelledError(Exception): pass
class MyLogger(object):
    def __init__(self, msg_id): self.msg_id = msg_id
    def debug(self, msg):
        if GLOBAL_CANCEL or (self.msg_id and CANCEL_TASKS.get(self.msg_id)): raise CancelledError("Cancelled")
    def warning(self, msg): pass
    def error(self, msg): pass

def download_with_ytdlp(url, msg, selected_res, loop):
    global GLOBAL_CANCEL
    msg_id = msg.id if msg else None
    if GLOBAL_CANCEL or (msg_id and CANCEL_TASKS.get(msg_id)): return None, None
    last_edit_time = [0]
    
    def progress_hook(d):
        if GLOBAL_CANCEL or (msg_id and CANCEL_TASKS.get(msg_id)): raise CancelledError("Cancelled")
        if msg.chat.id < 0: return
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_edit_time[0] > 5.0: 
                last_edit_time[0] = current_time
                downloaded = d.get('downloaded_bytes'); downloaded = downloaded if downloaded else 0
                total = d.get('total_bytes') or d.get('total_bytes_estimate'); total = total if total else 0
                speed = d.get('speed'); speed = speed if speed else 0
                
                if total > 0:
                    percentage = downloaded * 100 / total
                    progress = "[{0}{1}]".format(''.join(["█" for i in range(math.floor(percentage / 10))]), ''.join(["░" for i in range(10 - math.floor(percentage / 10))]))
                    text = f"⚡ **Downloading...**\n📊 {progress} **{round(percentage, 2)}%**\n📦 **Size:** {format_bytes(downloaded)} / {format_bytes(total)}\n⚡ **Speed:** {format_bytes(speed)}/s"
                else:
                    text = f"⚡ **Downloading...**\n📦 {format_bytes(downloaded)}\n⚡ {format_bytes(speed)}/s"
                asyncio.run_coroutine_threadsafe(msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg_id}")]])), loop)

    ydl_opts = {'socket_timeout': 15, 'retries': 3, 'fragment_retries': 3, 'outtmpl': '%(id)s.%(ext)s', 'format': f'bestvideo[height<={selected_res}]+bestaudio/best[height<={selected_res}]/best', 'merge_output_format': 'mp4', 'fixup': 'never', 'quiet': True, 'noplaylist': True, 'impersonate': ImpersonateTarget.from_str('chrome'), 'extractor_args': {'youtube': ['player_client=ios,android']}, 'external_downloader': 'aria2c', 'external_downloader_args': ['-c', '-x', '16', '-s', '16', '-k', '1M', '--connect-timeout=15', '--timeout=20', '--max-tries=5'], 'logger': MyLogger(msg_id) if msg_id else MyLogger("none"), 'progress_hooks': [progress_hook]}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not filename.endswith('.mp4') and os.path.exists(filename):
                new_filename = filename.rsplit('.', 1)[0] + '.mp4'
                os.rename(filename, new_filename)
                filename = new_filename
            return info, filename
    except CancelledError: return None, "CANCELLED"
    except Exception as e: return None, f"YTDLP_ERROR: {str(e)}"

async def progress_bar(current, total, msg, start_time, action="Uploading"):
    global GLOBAL_CANCEL
    if GLOBAL_CANCEL or STOP_UPLOAD.get(msg.id): raise Exception("Upload Cancelled")
    if msg.chat.id < 0: return
    now = time.time()
    diff = now - start_time
    if diff < 1: return 
    if round(diff % 3.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        time_to_completion = round((total - current) / speed) if speed > 0 else 0
        progress = "[{0}{1}]".format(''.join(["█" for i in range(math.floor(percentage / 10))]), ''.join(["░" for i in range(10 - math.floor(percentage / 10))]))
        text = f"🚀 **{action}...**\n📊 {progress} **{round(percentage, 2)}%**\n📦 **Size:** {format_bytes(current)} / {format_bytes(total)}\n⚡ **Speed:** {format_bytes(speed)}/s"
        try: await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")]]))
        except: pass

async def process_queue():
    global GLOBAL_CANCEL
    while True:
        task = await download_queue.get()
        url, chat_id, msg, selected_res = task  
        GLOBAL_CANCEL = False
        if url in queue_display: queue_display.remove(url) 
        if CANCEL_TASKS.get(msg.id) or GLOBAL_CANCEL: download_queue.task_done(); continue

        try:
            cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")]]) if msg.chat.id > 0 else None
            
            # GALLERY DL LOGIC
            if int(selected_res) == 0:
                await msg.edit_text("📸 Fetching Gallery/Images...", reply_markup=cancel_markup)
                files, out_dir = await asyncio.to_thread(run_gallery_dl, url)
                
                if not files:
                    raise Exception("No images/videos found in this post.")
                
                media_group = []
                for f in files:
                    if f.lower().endswith('.mp4'): media_group.append(InputMediaVideo(f))
                    else: media_group.append(InputMediaPhoto(f))
                
                await msg.edit_text("📤 Uploading Album/Carousel...", reply_markup=cancel_markup)
                for i in range(0, len(media_group), 10):
                    await app.send_media_group(chat_id, media_group[i:i+10])
                    await asyncio.sleep(2)
                
                shutil.rmtree(out_dir, ignore_errors=True)
                await msg.delete()
                download_queue.task_done()
                continue
            
            # NORMAL YT-DLP LOGIC
            await msg.edit_text(f"⚡ Downloading locally...\nQuality: {selected_res}p", reply_markup=cancel_markup)
            current_loop = asyncio.get_running_loop()
            info, filename = await asyncio.to_thread(download_with_ytdlp, url, msg, selected_res, current_loop)
            
            if filename == "CANCELLED" or CANCEL_TASKS.get(msg.id) or GLOBAL_CANCEL: raise Exception("Cancelled by user")
            if not info and filename and filename.startswith("YTDLP_ERROR:"): raise Exception(filename.replace("YTDLP_ERROR: ", ""))
            if not filename: raise Exception("Download failed.")

            thumb_path = f"thumb_{msg.id}.jpg"
            thumb = generate_thumbnail(filename, thumb_path)
            local_caption = f"**🎬 Title:** {info.get('title', 'Unknown')}\n**🌐 Website:** {info.get('extractor_key', 'Unknown')}\n**⚙️ Quality:** {selected_res}p\n**🔗 Source:** [Original Link]({url})"

            await msg.edit_text("📤 Uploading...", reply_markup=cancel_markup)
            start_time = time.time()
            upload_success = False
            for attempt in range(3):
                if CANCEL_TASKS.get(msg.id) or STOP_UPLOAD.get(msg.id) or GLOBAL_CANCEL: raise Exception("Upload Cancelled")
                try:
                    await asyncio.wait_for(app.send_video(chat_id=chat_id, video=filename, thumb=thumb, caption=local_caption, supports_streaming=True, progress=progress_bar, progress_args=(msg, start_time, "Uploading")), timeout=900)
                    upload_success = True; break
                except asyncio.TimeoutError: pass
                except FloodWait as e: await asyncio.sleep(e.value + 3)
                except Exception as e:
                    if "Upload Cancelled" in str(e): raise e
                    await asyncio.sleep(5)

            if upload_success: await msg.delete()
            else: raise Exception("Upload failed after multiple attempts.")
            if os.path.exists(filename): os.remove(filename)
            if thumb and os.path.exists(thumb): os.remove(thumb)

        except Exception as e:
            # 🔥 REFUND LOGIC: Koi bhi error aayi toh coin wapas de do
            await update_coins_db(chat_id, 1) # Refund 1 Coin
            
            if "Cancelled" in str(e) or GLOBAL_CANCEL:
                 try: await msg.edit_text("❌ Download Cancelled.\n🪙 **1 Coin Refunded!**")
                 except: pass
                 subprocess.run(["pkill", "-f", "aria2c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                 try: await msg.edit_text(f"❌ Error: {str(e)}\n🪙 **1 Coin Refunded!**")
                 except: pass
                 
        finally:
            if msg.id in CANCEL_TASKS: del CANCEL_TASKS[msg.id]
            if msg.id in STOP_UPLOAD: del STOP_UPLOAD[msg.id]
            try: download_queue.task_done()
            except: pass

# ==========================================
# 3. FIREBASE BACKGROUND BRIDGE
# ==========================================
async def process_webapp_checking(user_id, url):
    async with aiohttp.ClientSession() as session:
        await session.patch(f"{FIREBASE_URL}/tasks/{user_id}.json", json={"step": "processing_check"})
        try:
            res_list, website = await asyncio.to_thread(get_formats, url)
            if not res_list: res_list = [360, 480, 720, 1080]
        except Exception as e:
            res_list = [360, 480, 720, 1080]
            website = "Media Link"
        try:
            await session.patch(f"{FIREBASE_URL}/tasks/{user_id}.json", json={
                "step": "choosing",
                "qualities": res_list,
                "website": website
            })
        except: pass

async def process_webapp_downloading(user_id, url, res):
    async with aiohttp.ClientSession() as session:
        await session.patch(f"{FIREBASE_URL}/tasks/{user_id}.json", json={"step": "completed"})
    try:
        quality_text = "Images/Gallery" if int(res) == 0 else f"{res}p"
        msg = await app.send_message(int(user_id), f"✅ Link Received from WebApp!\n**Quality:** {quality_text}\n⏳ Processing...")
        URL_CACHE[msg.id] = url
        queue_display.append(url)
        await download_queue.put((url, int(user_id), msg, int(res)))
    except Exception as e:
        print("❌ Telegram Send Error:", e)

async def firebase_polling():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{FIREBASE_URL}/tasks.json") as resp:
                    tasks = await resp.json()
                    if tasks:
                        if isinstance(tasks, list): task_items = enumerate(tasks)
                        elif isinstance(tasks, dict): task_items = tasks.items()
                        else: task_items = []
                            
                        for user_id, task in task_items:
                            if not task or not isinstance(task, dict): continue
                            step = task.get("step")
                            if step == "checking": asyncio.create_task(process_webapp_checking(user_id, task.get("url")))
                            elif step == "downloading": asyncio.create_task(process_webapp_downloading(user_id, task.get("url"), task.get("res")))
        except Exception: pass
        await asyncio.sleep(1.5)

# ==========================================
# 4. TELEGRAM COMMANDS
# ==========================================
@app.on_message(filters.command("start"))
async def start(client, message):
    # 🔴 YAHAN APNA ASLI BLOGGER WALA LINK DALEIN 🔴
    BLOGGER_URL = "https://aapka-blogger-link.blogspot.com"
    markup = ReplyKeyboardMarkup([[KeyboardButton("🎬 Open Downloader App", web_app=WebAppInfo(url=BLOGGER_URL))]], resize_keyboard=True)
    coins = await get_coins(message.from_user.id)
    text = (f"Hello! Main Smart WebApp Downloader hoon.\n\n🪙 **Your Coins:** {coins}\n*(1 Download = 1 Coin. Get free coins by watching ads!)*\n\n👇 Niche diye gaye **Open Downloader App** button pe click karo!")
    await message.reply_text(text, reply_markup=markup)

@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_callback(client, callback_query):
    msg_id = int(callback_query.data.split("_")[1])
    CANCEL_TASKS[msg_id] = True; STOP_UPLOAD[msg_id] = True 
    await callback_query.answer("Cancelling task... Please wait!", show_alert=True)

@app.on_message(filters.command("cancelall"))
async def cancel_all(client, message):
    global queue_display, GLOBAL_CANCEL
    GLOBAL_CANCEL = True
    queue_display.clear()
    while not download_queue.empty():
        try: download_queue.get_nowait(); download_queue.task_done()
        except: pass
    subprocess.run(["pkill", "-f", "aria2c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for msg_id in list(URL_CACHE.keys()) + list(CANCEL_TASKS.keys()) + list(STOP_UPLOAD.keys()):
        CANCEL_TASKS[msg_id] = True; STOP_UPLOAD[msg_id] = True
    await message.reply_text("🗑️️ Pura queue WIPE OUT kar diya gaya hai! ✅")
    await asyncio.sleep(2)
    GLOBAL_CANCEL = False

if __name__ == "__main__":
    print("Bot is running with Complete UI & Refund Logic!")
    loop = asyncio.get_event_loop()
    loop.create_task(process_queue())
    loop.create_task(firebase_polling()) 
    app.run()
