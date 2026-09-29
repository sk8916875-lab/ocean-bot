import csv
import html
import re
import urllib.request
import json
import os
import threading
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.txt"
DB = BASE / "tac_full.csv"
CACHE = BASE / "live_cache.csv"
STATS_FILE = BASE / "stats.json"

LOCAL_DB_URL = "https://raw.githubusercontent.com/MoazEb/tac-database/main/tac_full.csv"
FCCID_URL = "https://fccid.io/TAC/{}"

ADMIN_ID = 1689869561
ADMIN_USERNAME = "thetpaing2021"

# Keyboard တွင်ပေါ်မည့် စာသားများ
BTN_MODEL = "📱 ဖုန်းမော်ဒယ် စစ်ရန် (IMEI)"
BTN_ADMIN = "💬 Admin သို့ ဆက်သွယ်ရန် ✨"

# ==========================================
# Flask Web Server အပိုင်း (Render.com အတွက်)
# ==========================================
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Ocean Mobile IMEI Check Bot is Running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)
# ==========================================

def get_token():
    # နောက်ပိုင်း Render သုံးရင် Environment Variable ကနေ တိုက်ရိုက်ယူပါမယ်
    token_env = os.environ.get("BOT_TOKEN")
    if token_env:
        return token_env

    if not CONFIG.exists():
        return ""
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""

def normalize_imei(text):
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 15 else None

def luhn_ok(imei):
    total = 0
    for i, ch in enumerate(imei):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

def download_local_db():
    tmp = DB.with_suffix(".tmp")
    req = urllib.request.Request(LOCAL_DB_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    if tmp.stat().st_size < 100000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Local DB download too small.")
    tmp.replace(DB)

def local_lookup(tac):
    if not DB.exists(): return None
    try:
        with DB.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers: return None
            headers = [str(h).strip().lower() for h in headers]
            tac_idx = brand_idx = model_idx = -1
            for i, h in enumerate(headers):
                if h in ["tac", "tac_code", "type allocation code"]: tac_idx = i
                elif h in ["brand", "manufacturer", "maker", "company"]: brand_idx = i
                elif h in ["model", "device", "specs", "name", "device_model"]: model_idx = i
            if tac_idx == -1: return None
            for row in reader:
                if len(row) > tac_idx and row[tac_idx].strip() == str(tac):
                    brand = row[brand_idx].strip() if brand_idx != -1 and len(row) > brand_idx else "Unknown"
                    model = row[model_idx].strip() if model_idx != -1 and len(row) > model_idx else "Unknown"
                    return {"brand": brand, "model": model}
    except Exception:
        pass
    return None

def cache_load():
    data = {}
    if CACHE.exists():
        with CACHE.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
            for row in csv.DictReader(f):
                if row.get("tac"): data[row["tac"]] = row
    return data

def cache_save(tac, brand, model):
    data = cache_load()
    data[tac] = {"tac": tac, "brand": brand, "model": model}
    with CACHE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["tac", "brand", "model"])
        w.writeheader()
        for row in data.values(): w.writerow(row)

def live_fccid_lookup(tac):
    url = FCCID_URL.format(tac)
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n", strip=True)
        if "IMEI Type Allocation Code" not in text: return None
        title = ""
        for h in soup.find_all("h1"):
            t = h.get_text(" ", strip=True)
            if t and t != "IMEI Type Allocation Code":
                title = t
                break
        def after_label(label):
            for node in soup.find_all(string=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)):
                if node.parent and node.parent.find_next():
                    val = node.parent.find_next().get_text(" ", strip=True)
                    if val and val.lower() != label.lower(): return val
            m = re.search(rf"\b{re.escape(label)}\s+([^\n]+)", text, re.I)
            return m.group(1).strip() if m else ""
        brand = after_label("Brand")
        model = after_label("Model")
        if not model:
            m = re.search(r"Device overview\s+TAC\s*"+re.escape(tac)+r"\s+Model\s+([^\n]+)", text, re.I)
            if m: model = m.group(1).strip()
        if not model: model = title
        if not brand: brand = ""
        if not model or model.lower() in {"search", "imei type allocation code"}: return None
        cache_save(tac, brand, model)
        return {"brand": brand, "model": model}
    except Exception:
        return None

def lookup(tac):
    cached = cache_load().get(tac)
    if cached: return {"brand": cached.get("brand", ""), "model": cached.get("model", "")}
    local = local_lookup(tac)
    if local: return local
    try:
        live = live_fccid_lookup(tac)
        if live: return live
    except Exception:
        pass
    return None

def update_stats(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {}
    if STATS_FILE.exists():
        with open(STATS_FILE, "r") as f:
            try: stats = json.load(f)
            except: pass
    if today not in stats: stats[today] = {"users": [], "checks": 0}
    if user_id not in stats[today]["users"]: stats[today]["users"].append(user_id)
    stats[today]["checks"] += 1
    with open(STATS_FILE, "w") as f: json.dump(stats, f)

def get_reply_keyboard():
    keyboard = [
        [KeyboardButton(BTN_MODEL), KeyboardButton(BTN_ADMIN)]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 <b>Ocean IMEI Check Bot</b> မှ ကြိုဆိုပါတယ်။\n\n"
        "အောက်ပါ Menu မှ သင်စစ်ဆေးလိုသော ဝန်ဆောင်မှုကို ရွေးချယ်ပါ 👇",
        reply_markup=get_reply_keyboard(),
        parse_mode="HTML"
    )
    
async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Local database update လုပ်နေပါတယ်... ခဏစောင့်ပါ။")
    try:
        download_local_db()
        size_mb = DB.stat().st_size / 1024 / 1024
        await update.message.reply_text(
            f"✅ Local database update ပြီးပါပြီ။\n"
            f"📦 Size: {size_mb:.1f} MB"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Local database download မအောင်မြင်ပါ။ Error: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == BTN_MODEL:
        await update.message.reply_text("📱 ဖုန်းမော်ဒယ် စစ်ဆေးရန် အဆင်သင့်ဖြစ်ပါပြီ။\nIMEI ဂဏန်း (၁၅) လုံးကို ပို့ပေးပါ ခင်ဗျာ။")
        return
    elif text == BTN_ADMIN:
        admin_btn = [[InlineKeyboardButton("👨‍💻 Admin သို့ စာပို့ရန်", url=f"https://t.me/{ADMIN_USERNAME}")]]
        await update.message.reply_text("အောက်ပါ ခလုတ်ကိုနှိပ်၍ Admin သို့ တိုက်ရိုက် ဆက်သွယ်နိုင်ပါသည်။ 👇", reply_markup=InlineKeyboardMarkup(admin_btn))
        return

    imei = normalize_imei(text)
    if not imei:
        await update.message.reply_text("❌ IMEI က ဂဏန်း 15 လုံး ဖြစ်ရပါမယ်။")
        return

    if not luhn_ok(imei):
        await update.message.reply_text("⚠️ IMEI Check Digit မကိုက်ညီပါ။ နံပါတ်ကို ပြန်လည်စစ်ဆေးပါ။")
        return

    tac = imei[:8]
    result = lookup(tac)
    update_stats(update.effective_user.id)

    if not result:
        await update.message.reply_text(
            f"❓ Device မတွေ့ပါ။\n\n"
            f"<b>IMEI:</b> <code>{imei}</code>\n"
            f"<b>TAC:</b> <code>{tac}</code>\n\n"
            f"ဒီ TAC အချက်အလက်ကို Database ထဲမှာ ရှာမတွေ့သေးပါ။\n\n🌊 <b>Ocean Mobile TGI</b>",
            parse_mode="HTML"
        )
        return

    brand = result.get("brand", "").strip()
    model = result.get("model", "").strip()
    brand_line = brand if brand and brand.lower() != "unknown" else "Not listed"

    card_text = (
        f"✨📱 <b>DEVICE INFO</b> 📱✨\n\n"
        f"🏷️ <b>Brand :</b> {brand_line}\n"
        f"💎 <b>Model :</b> {model}\n"
        f"🔍 <b>IMEI  :</b> <code>{imei}</code>\n\n"
        f"🎀 <i>Ocean Mobile TGI</i> 🌊💙"
    )

    await update.message.reply_text(card_text, parse_mode="HTML")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {}
    if STATS_FILE.exists():
        with open(STATS_FILE, "r") as f:
            try: stats = json.load(f)
            except: pass
    if today in stats:
        text = f"📊 <b>ယနေ့ ({today}) အသုံးပြုမှုစာရင်း</b>\n\n👤 လာရောက်သုံးစွဲသူ: {len(stats[today]['users'])} ဦး\n🔎 စစ်ဆေးမှု: {stats[today]['checks']} ကြိမ်\n\n🌊 <b>Ocean Mobile TGI</b>"
    else:
        text = f"📊 ယနေ့ ({today}) အတွက် လာရောက်စစ်ဆေးသူ မရှိသေးပါ။"
    await update.message.reply_text(text, parse_mode="HTML")

def main():
    token = get_token()
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("BOT_TOKEN မထည့်ရသေးပါ။")
        return
    if not DB.exists():
        try: download_local_db()
        except: pass

    # Web Server ကို နောက်ကွယ် (Background Thread) မှ Run ခြင်း
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("OceanIMEICheckBot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()