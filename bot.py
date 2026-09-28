import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import time
import math
import subprocess
import json
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
import yt_dlp
from pyrogram.errors import MessageNotModified, FloodWait
from yt_dlp.networking.impersonate import ImpersonateTarget

# ==========================================
# 1. BOT CREDENTIALS
# ==========================================
API_ID = int(os.environ.get("API_ID", 0))        
API_HASH = os.environ.get("API_HASH", "")    
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ==========================================
# 2. QUEUE, CANCEL MANAGER & URL CACHE
# ==========================================
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
    while size > power:
        size /= power
        n += 1
    return f"{round(size, 2)} {dic_powerN[n]}"

def generate_thumbnail(video_path, thumbnail_path):
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", "00:00:50", "-i", video_path, 
            "-vframes", "1", "-q:v", "2", 
            "-vf", "scale=320:-1", 
            thumbnail_path, "-y"
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
            return thumbnail_path
            
        cmd[5] = "00:00:02"
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
            return thumbnail_path
            
    except Exception:
        pass
    return None

# ==========================================
# 3. LIVE PROGRESS BAR ENGINE (WITH CANCEL)
# ==========================================
async def progress_bar(current, total, msg, start_time, action="Uploading"):
    global GLOBAL_CANCEL
    if GLOBAL_CANCEL or STOP_UPLOAD.get(msg.id):
        raise Exception("Upload Cancelled")
        
    if msg.chat.id < 0:
        return

    now = time.time()
    diff = now - start_time
    if diff < 1: return 
    
    if round(diff % 3.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        time_to_completion = round((total - current) / speed) if speed > 0 else 0
        
        progress = "[{0}{1}]".format(
            ''.join(["█" for i in range(math.floor(percentage / 10))]),
            ''.join(["░" for i in range(10 - math.floor(percentage / 10))])
        )
        
        text = f"🚀 **{action}...**\n\n"
        text += f"📊 {progress} **{round(percentage, 2)}%**\n"
        text += f"📦 **Size:** {format_bytes(current)} / {format_bytes(total)}\n"
        text += f"⚡ **Speed:** {format_bytes(speed)}/s\n"
        text += f"⏳ **ETA:** {time_to_completion} Seconds"
        
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")]]
        )
        
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)

# ==========================================
# 4. YT-DLP CORE (RESOLUTION & BYPASS)
# ==========================================
class CancelledError(Exception): pass

class MyLogger(object):
    def __init__(self, msg_id):
        self.msg_id = msg_id
    def debug(self, msg):
        global GLOBAL_CANCEL
        if GLOBAL_CANCEL or (self.msg_id and CANCEL_TASKS.get(self.msg_id)):
             raise CancelledError("Download cancelled")
    def warning(self, msg): pass
    def error(self, msg): pass

def get_formats(url):
    ydl_opts = {
        'socket_timeout': 15, 'retries': 3, 'quiet': True, 'noplaylist': True,
        'impersonate': ImpersonateTarget.from_str('chrome'),
        'extractor_args': {'youtube': ['player_client=ios,android']},
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    }
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

def extract_info_only(url, selected_res):
    ydl_opts = {
        'socket_timeout': 15, 'retries': 3, 'format': f'best[height<={selected_res}]', 
        'quiet': True, 'noplaylist': True,
        'impersonate': ImpersonateTarget.from_str('chrome'),
        'extractor_args': {'youtube': ['player_client=ios,android']},
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_with_ytdlp(url, msg, selected_res, loop):
    global GLOBAL_CANCEL
    msg_id = msg.id if msg else None
    if GLOBAL_CANCEL or (msg_id and CANCEL_TASKS.get(msg_id)): return None, None
    
    last_edit_time = [0]
    
    def progress_hook(d):
        global GLOBAL_CANCEL
        if GLOBAL_CANCEL or (msg_id and CANCEL_TASKS.get(msg_id)):
            raise CancelledError("Download Cancelled")
            
        if msg.chat.id < 0: return

        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_edit_time[0] > 5.0: 
                last_edit_time[0] = current_time
                
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                speed = d.get('speed', 0)
                
                if total > 0:
                    percentage = downloaded * 100 / total
                    time_to_completion = round((total - downloaded) / speed) if speed and speed > 0 else 0
                    
                    progress = "[{0}{1}]".format(
                        ''.join(["█" for i in range(math.floor(percentage / 10))]),
                        ''.join(["░" for i in range(10 - math.floor(percentage / 10))])
                    )
                    text = f"⚡ **Downloading locally...**\n⚙️ Quality: {selected_res}p\n\n"
                    text += f"📊 {progress} **{round(percentage, 2)}%**\n"
                    text += f"📦 **Size:** {format_bytes(downloaded)} / {format_bytes(total)}\n"
                    text += f"⚡ **Speed:** {format_bytes(speed)}/s\n"
                    text += f"⏳ **ETA:** {time_to_completion} Seconds"
                else:
                    text = f"⚡ **Downloading locally...**\n⚙️ Quality: {selected_res}p\n\n"
                    text += f"📦 **Downloaded:** {format_bytes(downloaded)}\n"
                    text += f"⚡ **Speed:** {format_bytes(speed)}/s\n"
                    
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg_id}")]])
                
                async def edit_message():
                    try: await msg.edit_text(text, reply_markup=reply_markup)
                    except: pass
                asyncio.run_coroutine_threadsafe(edit_message(), loop)

    ydl_opts = {
        'socket_timeout': 15, 'retries': 3, 'fragment_retries': 3,
        'outtmpl': '%(id)s.%(ext)s',
        'format': f'bestvideo[height<={selected_res}]+bestaudio/best[height<={selected_res}]/best',
        'merge_output_format': 'mp4', 'fixup': 'never', 'quiet': True, 'noplaylist': True,
        'impersonate': ImpersonateTarget.from_str('chrome'),
        'extractor_args': {'youtube': ['player_client=ios,android']},
        'external_downloader': 'aria2c',
        'external_downloader_args': ['-c', '-x', '16', '-s', '16', '-k', '1M', '--connect-timeout=15', '--timeout=20', '--max-tries=5'],
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
        'logger': MyLogger(msg_id) if msg_id else MyLogger("none"),
        'progress_hooks': [progress_hook] 
    }
    
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

# ==========================================
# 5. BACKGROUND WORKER (QUEUE SYSTEM)
# ==========================================
async def process_queue():
    global GLOBAL_CANCEL
    while True:
        task = await download_queue.get()
        url, chat_id, msg, selected_res = task  
        GLOBAL_CANCEL = False
        
        if url in queue_display: queue_display.remove(url) 

        if msg is None:
            try:
                msg = await app.send_message(chat_id, "⏳ Processing WebApp Request...")
                URL_CACHE[msg.id] = url
            except Exception:
                download_queue.task_done()
                continue
        
        if CANCEL_TASKS.get(msg.id) or GLOBAL_CANCEL:
            try: await msg.edit_text("❌ Task Cancelled before starting.")
            except: pass
            download_queue.task_done()
            continue

        try:
            cancel_markup = None
            if msg.chat.id > 0: 
                cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")]])
            
            await msg.edit_text("🔍 Checking Direct Link...", reply_markup=cancel_markup)
            
            try: info_direct = await asyncio.to_thread(extract_info_only, url, selected_res)
            except Exception as e: info_direct = None
            
            if CANCEL_TASKS.get(msg.id) or GLOBAL_CANCEL: raise Exception("Cancelled by user")

            direct_url = info_direct.get('url') if info_direct else None
            title = info_direct.get('title', 'Unknown Title') if info_direct else 'Unknown Title'
            website = info_direct.get('extractor_key', 'Unknown Website') if info_direct else 'Unknown Website'
            
            caption_text = (
                f"**🎬 Title:** {title}\n"
                f"**🌐 Website:** {website}\n"
                f"**⚙️ Quality:** {selected_res}p\n"
                f"**🔗 Source:** [Original Link]({url})"
            )
            
            if direct_url:
                direct_success = False
                try:
                    await msg.edit_text("🚀 Trying Direct Upload (Superfast)...", reply_markup=cancel_markup)
                    for _ in range(2):
                        if GLOBAL_CANCEL: raise Exception("Global Cancel triggered")
                        try:
                            await asyncio.wait_for(
                                app.send_video(chat_id=chat_id, video=direct_url, caption=caption_text, supports_streaming=True), timeout=60 
                            )
                            direct_success = True
                            break
                        except asyncio.TimeoutError: pass
                        except FloodWait as e: await asyncio.sleep(e.value + 2)
                        except Exception: await asyncio.sleep(2)
                except Exception: pass 

                if direct_success:
                    await msg.delete()
                    if msg.id in CANCEL_TASKS: del CANCEL_TASKS[msg.id]
                    if msg.id in STOP_UPLOAD: del STOP_UPLOAD[msg.id]
                    download_queue.task_done()
                    await asyncio.sleep(2.5)
                    continue 

            await msg.edit_text(f"⚡ Downloading locally...\nQuality: {selected_res}p", reply_markup=cancel_markup)
            current_loop = asyncio.get_running_loop()
            info, filename = await asyncio.to_thread(download_with_ytdlp, url, msg, selected_res, current_loop)
            
            if filename == "CANCELLED" or CANCEL_TASKS.get(msg.id) or GLOBAL_CANCEL: raise Exception("Cancelled by user")
            if not info and filename and filename.startswith("YTDLP_ERROR:"): raise Exception(filename.replace("YTDLP_ERROR: ", ""))
            if not filename: raise Exception("Download failed due to an unknown issue.")

            thumb_path = f"thumb_{msg.id}.jpg"
            thumb = generate_thumbnail(filename, thumb_path)

            local_title = info.get('title', 'Unknown Title')
            local_website = info.get('extractor_key', 'Unknown Website')
            local_caption = (f"**🎬 Title:** {local_title}\n**🌐 Website:** {local_website}\n**⚙️ Quality:** {selected_res}p\n**🔗 Source:** [Original Link]({url})")

            await msg.edit_text("📤 Uploading...", reply_markup=cancel_markup)
            start_time = time.time()
            
            upload_success = False
            for attempt in range(3):
                if CANCEL_TASKS.get(msg.id) or STOP_UPLOAD.get(msg.id) or GLOBAL_CANCEL: raise Exception("Upload Cancelled")
                try:
                    await asyncio.wait_for(
                        app.send_video(
                            chat_id=chat_id, video=filename, thumb=thumb, caption=local_caption,
                            supports_streaming=True, progress=progress_bar, progress_args=(msg, start_time, "Uploading")
                        ), timeout=900 
                    )
                    upload_success = True
                    break
                except asyncio.TimeoutError: pass
                except FloodWait as e: await asyncio.sleep(e.value + 3)
                except Exception as e:
                    if "Upload Cancelled" in str(e): raise e
                    await asyncio.sleep(5)

            if upload_success: await msg.delete()
            else: await msg.edit_text("❌ Upload failed after multiple attempts (Telegram Timeout).")

            if os.path.exists(filename): os.remove(filename)
            if thumb and os.path.exists(thumb): os.remove(thumb)

        except Exception as e:
            if "Cancelled" in str(e) or GLOBAL_CANCEL:
                 try: await msg.edit_text("❌ Video Download/Upload Rok Diya Gaya Hai.")
                 except: pass
                 subprocess.run(["pkill", "-f", "aria2c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                 try: await msg.edit_text(f"❌ Error: {str(e)}")
                 except: pass
            
            try:
                if 'filename' in locals() and os.path.exists(filename) and not filename.startswith("YTDLP_ERROR:"): os.remove(filename)
                if 'thumb_path' in locals() and os.path.exists(thumb_path): os.remove(thumb_path)
            except: pass
        finally:
            if msg.id in CANCEL_TASKS: del CANCEL_TASKS[msg.id]
            if msg.id in STOP_UPLOAD: del STOP_UPLOAD[msg.id]

        try: download_queue.task_done()
        except ValueError: pass
        await asyncio.sleep(2.5)


# ==========================================
# 6. TELEGRAM COMMANDS & HANDLERS
# ==========================================

@app.on_message(filters.command("start"))
async def start(client, message):
    # 🔴 YAHAN APNA ASLI BLOGGER WALA LINK DAAL DO 🔴
    BLOGGER_URL = "https://unidl.blogspot.com"
    
    markup = ReplyKeyboardMarkup(
        [[KeyboardButton("🎬 Open Downloader App", web_app=WebAppInfo(url=BLOGGER_URL))]],
        resize_keyboard=True
    )
    
    await message.reply_text(
        "Hello! Main Premium Downloader hoon.\n\n"
        "👇 Niche diye gaye **Open Downloader App** button pe click karo aur seedha URL daalkar download karo!\n"
        "Ya phir mujhe yaha chat me bhi link bhej sakte ho.",
        reply_markup=markup
    )

@app.on_message(filters.command("queue"))
async def show_queue(client, message):
    if not queue_display:
        await message.reply_text("📭 Queue bilkul khaali hai! Koi naya link bhejein.")
        return
    text = "**📋 Line me lagi hui videos:**\n\n"
    for i, url in enumerate(queue_display): text += f"{i+1}. {url}\n"
    await message.reply_text(text)

@app.on_message(filters.command("cancelall"))
async def cancel_all(client, message):
    global queue_display, GLOBAL_CANCEL
    GLOBAL_CANCEL = True
    queue_display.clear()
    
    while not download_queue.empty():
        try: download_queue.get_nowait(); download_queue.task_done()
        except: pass
            
    subprocess.run(["pkill", "-f", "aria2c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    for msg_id in list(URL_CACHE.keys()) + list(CANCEL_TASKS.keys()) + list(STOP_UPLOAD.keys()):
        CANCEL_TASKS[msg_id] = True; STOP_UPLOAD[msg_id] = True
        
    await message.reply_text("🗑️ **BAM!** Pura queue aur current download WIPE OUT kar diya gaya hai! ✅")
    await asyncio.sleep(2)
    GLOBAL_CANCEL = False

# MAGIC HANDLER: Ye WebApp se data aane par aur normal text links aane par dono ke liye kaam karega
@app.on_message(filters.private & ~filters.command(["start", "queue", "cancelall"]))
async def handle_messages(client, message):
    
    # CASE 1: Data Telegram WebApp se aaya hai (Blogger)
    if getattr(message, "web_app_data", None):
        try:
            data = json.loads(message.web_app_data.data)
            url = data.get("url")
            res = int(data.get("res", 720))
            
            msg = await message.reply_text(f"✅ WebApp se link mil gaya!\n**Link:** {url}\n**Quality:** {res}p\n⏳ Processing...")
            URL_CACHE[msg.id] = url
            queue_display.append(url)
            await download_queue.put((url, message.chat.id, msg, res))
            
        except Exception as e:
            await message.reply_text(f"❌ WebApp Error: {str(e)}")
        return

    # CASE 2: User ne direct message me link bheja hai (Normal Flow)
    if not message.text: return
    
    lines = message.text.split('\n')
    auto_quality = None
    first_line = lines[0].strip()
    
    if first_line.isdigit():
        auto_quality = int(first_line)
        lines = lines[1:] 

    if auto_quality:
        valid_urls = [u.strip() for u in lines if u.strip().startswith("http")]
        if not valid_urls: return
        for url in valid_urls:
            queue_display.append(url)
            await download_queue.put((url, message.chat.id, None, auto_quality))
        await message.reply_text(f"✅ **Bulk Queue Active:** {len(valid_urls)} links added.")
        return

    for url in lines:
        url = url.strip()
        if not url.startswith("http"): continue
        msg = await message.reply_text(f"🔍 Fetching quality options... Please wait!")
        URL_CACHE[msg.id] = url
        try:
            res_list, website = await asyncio.to_thread(get_formats, url)
            if not res_list: res_list = [360, 480, 720, 1080] 
            
            buttons = []
            row = []
            for res in res_list:
                row.append(InlineKeyboardButton(f"🎬 {res}p", callback_data=f"res_{res}_{msg.id}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row: buttons.append(row) 
            
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{msg.id}")])
            reply_markup = InlineKeyboardMarkup(buttons)
            await msg.edit_text(f"**🔗 Link:** {url}\n**🌐 Source:** {website}\n\n👇 **Select Quality to Download:**", reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as e:
            await msg.edit_text(f"❌ Error fetching qualities: {str(e)}")


@app.on_callback_query(filters.regex(r"^res_"))
async def select_resolution(client, callback_query):
    data = callback_query.data.split("_")
    selected_res = int(data[1])
    msg_id = int(data[2])
    
    url = URL_CACHE.get(msg_id)
    if not url:
        await callback_query.answer("Error: Link purana ho gaya hai, kripya wapas link bhejein.", show_alert=True)
        return
    
    position = len(queue_display) + 1
    queue_display.append(url)
    await download_queue.put((url, callback_query.message.chat.id, callback_query.message, selected_res))
    await callback_query.message.edit_text(f"⏳ Line me lag gaya!\n(Position: {position} | Selected Quality: {selected_res}p)")

@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_callback(client, callback_query):
    msg_id = int(callback_query.data.split("_")[1])
    CANCEL_TASKS[msg_id] = True
    STOP_UPLOAD[msg_id] = True 
    await callback_query.answer("Cancelling task... Please wait!", show_alert=True)
    try: await callback_query.message.edit_text("❌ Task Cancelled.")
    except: pass

# ==========================================
# 7. BOT RUNNER (NO WEB SERVER NEEDED)
# ==========================================
if __name__ == "__main__":
    print("========================================")
    print("Bot is running seamlessly on Railway!")
    print("Feature Active: WebApp integration (No domain needed)")
    print("========================================")
    
    loop = asyncio.get_event_loop()
    loop.create_task(process_queue())
    app.run()
