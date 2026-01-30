import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
from datetime import datetime, timezone
import requests
import time

# ================== CONFIG ==================
BOT_TOKEN = "8556247369:AAGJvTvad1aCfWk8Sh9vI5LsK4qtj3YQrLY"  # yahan apna token daal sakte ho
ADMIN_IDS = [7968177079]

DEFAULT_CREDITS = 10
LOOKUP_COST = 1

# ✅ FIXED: Force channel join - Channel ID aur Link dalo
CHANNEL_LINK = ""  # Apne channel ka link daalo
CHANNEL_ID = ""  # Channel username ya ID (example: -1001234567890)

bot = telebot.TeleBot(BOT_TOKEN)

# ================== DATABASE SETUP ==================
conn = sqlite3.connect("bott.db", check_same_thread=False)
cur = conn.cursor()


def ensure_column(table: str, col_def: str):
    col_name = col_def.split()[0]
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if col_name not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        conn.commit()


def init_db():
    # users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()

    ensure_column("users", "username TEXT")
    ensure_column("users", "credits INTEGER DEFAULT 0")
    ensure_column("users", "referred_by INTEGER")
    ensure_column("users", "is_banned INTEGER DEFAULT 0")
    # NEW: unlimited search expiry (epoch seconds)
    ensure_column("users", "unlimited_until INTEGER DEFAULT 0")

    # history table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            result TEXT,
            created_at TEXT
        )
    """)
    conn.commit()

    # NEW: settings table (for global unlimited etc.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


init_db()

# ================== SETTINGS HELPERS ==================


def get_setting(key: str):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_setting(key: str, value: str):
    if get_setting(key) is None:
        cur.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    else:
        cur.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    conn.commit()


def now_ts() -> int:
    return int(time.time())


# ================== USER HELPERS ==================


def get_or_create_user(user_id, username=None, referred_by=None):
    cur.execute("SELECT user_id, username, credits, referred_by, is_banned, unlimited_until FROM users WHERE user_id = ?",
                (user_id,))
    row = cur.fetchone()
    if row:
        if username and row[1] != username:
            cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
        return row

    cur.execute(
        "INSERT INTO users (user_id, username, credits, referred_by, is_banned, unlimited_until) "
        "VALUES (?, ?, ?, ?, 0, 0)",
        (user_id, username, DEFAULT_CREDITS, referred_by)
    )
    conn.commit()
    cur.execute("SELECT user_id, username, credits, referred_by, is_banned, unlimited_until FROM users WHERE user_id = ?",
                (user_id,))
    return cur.fetchone()


def set_credits(user_id, amount):
    cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def add_credits(user_id, amount):
    cur.execute("UPDATE users SET credits = COALESCE(credits, 0) + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def remove_credits(user_id, amount):
    cur.execute(
        "UPDATE users SET credits = MAX(COALESCE(credits, 0) - ?, 0) WHERE user_id = ?",
        (amount, user_id)
    )
    conn.commit()


def get_credits(user_id):
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def set_ban_status(user_id, status: bool):
    cur.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if status else 0, user_id))
    conn.commit()


def is_banned(user_id):
    cur.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return bool(row[0]) if row and row[0] is not None else False


def save_history(user_id, query, result):
    cur.execute(
        "INSERT INTO history (user_id, query, result, created_at) VALUES (?, ?, ?, ?)",
        (user_id, query, result[:1000], datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def get_history(user_id, limit=10):
    cur.execute(
        "SELECT query, result, created_at FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    return cur.fetchall()


# ================== UNLIMITED HELPERS ==================


def get_user_unlimited_until(user_id: int) -> int:
    cur.execute("SELECT unlimited_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def set_user_unlimited(user_id: int, minutes: int):
    expiry = now_ts() + minutes * 60
    cur.execute("UPDATE users SET unlimited_until = ? WHERE user_id = ?", (expiry, user_id))
    conn.commit()
    return expiry


def clear_user_unlimited(user_id: int):
    cur.execute("UPDATE users SET unlimited_until = 0 WHERE user_id = ?", (user_id,))
    conn.commit()


def get_global_unlimited_until() -> int:
    val = get_setting("global_unlimited_until")
    try:
        return int(val) if val is not None else 0
    except ValueError:
        return 0


def set_global_unlimited(minutes: int):
    expiry = now_ts() + minutes * 60
    set_setting("global_unlimited_until", str(expiry))
    return expiry


def clear_global_unlimited():
    set_setting("global_unlimited_until", "0")


def is_unlimited_user(user_id: int) -> bool:
    now = now_ts()
    user_until = get_user_unlimited_until(user_id)
    global_until = get_global_unlimited_until()
    return (user_until > now) or (global_until > now)


# ---------------- FORCE SUB ----------------

def is_user_in_channel(user_id: int) -> bool:
    """✅ FIXED: Check karo user channel me hai ya nahi"""
    if not CHANNEL_ID:  # Agar channel ID nahi hai to skip karo
        return True
        
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        print(f"Channel check error: {e}")
        return False  # Agar error aaye to allow nahi karo


def send_force_sub(chat_id: int):
    """✅ FIXED: Channel join karne ke liye button"""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🔔 Join Channel", url=CHANNEL_LINK)
    )
    kb.row(
        InlineKeyboardButton("✅ Joined, Check Again", callback_data="check_sub")
    )
    
    # ✅ CORRECT: Pehle message bhejo, phir callback handle karo
    msg = bot.send_message(
        chat_id,
        "📢 Bot use karne ke liye pehle hamara official channel join karein.\n\n"
        "Channel join karne ke baad 'Joined, Check Again' dabayein.\n\n"
        f"Channel: {CHANNEL_LINK}",
        reply_markup=kb
    )
    return msg


def ensure_user_record_from_obj(user_obj):
    """Force-sub pass karne ke baad user DB me ho ye ensure karta hai."""
    user_id = user_obj.id
    username = user_obj.username
    get_or_create_user(user_id, username=username, referred_by=None)


# ================== UI KEYBOARDS ==================

def main_menu(is_admin=False):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🇮🇳 INDIA NUMBER INFO", callback_data="number_info"),
        InlineKeyboardButton("🇵🇰 PAKISTAN NUMBER INFO", callback_data="pak_number_info"),
    )
    kb.row(
        InlineKeyboardButton("🆔 AADHAAR LOOKUP", callback_data="aadhaar_lookup"),
        InlineKeyboardButton("🧾 GST DETAILS", callback_data="gst_lookup"),
    )
    kb.row(
        InlineKeyboardButton("🪪 CNIC (PAKISTAN ID)", callback_data="cnic_lookup"),
    )
    kb.row(
        InlineKeyboardButton("🚗 VEHICLE (RC) LOOKUP", callback_data="rc_lookup"),
    )
    kb.row(
        InlineKeyboardButton("🪪 PAN CARD DETAILS", callback_data="pan_lookup"),
        InlineKeyboardButton("🏦 IFSC BANK DETAIL", callback_data="ifsc_lookup"),
    )
    kb.row(
        InlineKeyboardButton("🏛 PINCODE", callback_data="pincode_lookup"),
        InlineKeyboardButton("🏙 CITY", callback_data="city_lookup"),
    )
    kb.row(
        InlineKeyboardButton("📸 INSTAGRAM PROFILE", callback_data="instagram_lookup"),
    )
    kb.row(
        InlineKeyboardButton("🎁 Referral", callback_data="referral"),
        InlineKeyboardButton("💳 My Credits", callback_data="my_credits"),
    )
    kb.row(
        InlineKeyboardButton("📜 My History", callback_data="my_history"),
    )
    if is_admin:
        kb.row(
            InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")
        )
    return kb


def admin_menu():
    kb = InlineKeyboardMarkup()
    # NEW ROW: Bonus + Status
    kb.row(
        InlineKeyboardButton("🎁 Bonus to All", callback_data="admin_bonus_all"),
        InlineKeyboardButton("📊 Status", callback_data="admin_status"),
    )
    # NEW ROW: Unlimited controls
    kb.row(
        InlineKeyboardButton("♾️ Unlimited (User)", callback_data="admin_unlimited_user"),
        InlineKeyboardButton("🌐 Unlimited (All)", callback_data="admin_unlimited_all"),
    )
    kb.row(
        InlineKeyboardButton("📜 Unlimited Users", callback_data="admin_unlimited_list"),
    )

    # OLD CONTROLS
    kb.row(
        InlineKeyboardButton("➕ Add Credit", callback_data="admin_add_credit"),
        InlineKeyboardButton("➖ Remove Credit", callback_data="admin_remove_credit"),
    )
    kb.row(
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
    )
    kb.row(
        InlineKeyboardButton("👥 All Users", callback_data="admin_all_users"),
    )
    kb.row(
        InlineKeyboardButton("🔒 Ban User", callback_data="admin_ban"),
        InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Back", callback_data="back_main")
    )
    return kb


# ================== STATE ==================
# india / pakistan / aadhaar / gst / pan / ifsc / pincode / city / rc / cnic / instagram
USER_STATE = {}   # {user_id: "..."}
ADMIN_STATE = {}  # {user_id: {"mode": "add_credit"/...}}

# ================== REAL LOOKUP FUNCTIONS ==================


def _format_value(val, indent=0):
    space = "  " * indent
    if isinstance(val, dict):
        lines = []
        for k, v in val.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{space}{k}:")
                lines.append(_format_value(v, indent + 1))
            else:
                lines.append(f"{space}{k}: {v}")
        return "\n".join(lines)
    elif isinstance(val, list):
        lines = []
        for idx, item in enumerate(val, start=1):
            if isinstance(item, (dict, list)):
                lines.append(f"{space}- [{idx}]")
                lines.append(_format_value(item, indent + 1))
            else:
                lines.append(f"{space}- {item}")
        return "\n".join(lines)
    else:
        return f"{space}{val}"


# ---- sensitive keys & cleaning ----
SENSITIVE_KEYS = {
    "credit_by", "credit", "credits", "credits_by", "credit_to",
    "developer", "developed_by", "dev", "dev_by", "dev_name",
    "powered_by", "powered", "hosted_by",
    "owner", "owner_name", "created_by", "creator", "maker", "made_by",
    "author", "author_name", "api_by", "api_owner", "api_provider",
    "provider", "provided_by", "generated_by", "maintained_by",
    "seller", "seller_name", "seller_by", "seller_id",
    "vendor", "vendor_name", "reseller", "reseller_name",
    "brand", "brand_name", "company", "company_name",
    "telegram", "telegram_id", "telegram_channel", "insta", "instagram",
    "whatsapp", "website", "site", "domain", "link", "url",
    "key", "api_key", "token", "client_id", "project", "project_id", "api_sell",
    "msg", "message", "note", "disclaimer",
    "footer", "signature", "credits_line"
    # ❌ DO NOT include "result" here
}


def _clean_data(obj):
    """
    JSON se developer / credit etc. hata deta hai (recursive).
    """
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if k in SENSITIVE_KEYS:
                continue
            cleaned[k] = _clean_data(v)
        return cleaned
    elif isinstance(obj, list):
        return [_clean_data(x) for x in obj]
    else:
        return obj


def fetch_from_api(api_url, number):
    """Single API se data fetch karta hai (error responses ignore + cleaning)."""
    try:
        response = requests.get(api_url, timeout=10)
        if response.status_code != 200:
            return None

        data = response.json()
        if not data or data == {}:
            return None

        if isinstance(data, dict):
            err_text = str(data.get("error", "")).lower()
            details_text = str(data.get("details", "")).lower()
            status_text = str(data.get("status", "")).lower()

            if "request failed" in err_text or "request failed" in details_text:
                return None
            if "error" in status_text and "success" not in status_text:
                return None
            if "success" in data and not data.get("success", True):
                return None
            # Check for boolean false
            if data.get("status") is False:
                return None

        cleaned = _clean_data(data)

        if cleaned in (None, {}, []):
            return None

        return cleaned

    except:
        return None


# ---------- INDIA NUMBER LOOKUP ----------
def lookup_india_number(mobile_number: str) -> str:
    """
    4 APIs se INDIA number ka data fetch karta hai ek ke baad ek.
    """
    number = mobile_number.strip().replace(" ", "").replace("-", "")

    if not number:
        return "Number khali hai."

    apis = [
        f"https://source-code-api.vercel.app/?num={number}",
        f"https://goku-numberr-info.vercel.app/api/seller/?mobile={number}&key=GOKU",
        f"https://niloy-number-info-api.vercel.app/api/seller?mobile={number}&key=Niloy",
        f"http://india.42web.io/number.php?number={number}"
    ]

    api_names = ["API-1", "API-2", "API-3", "API-4"]

    for idx, api_url in enumerate(apis):
        try:
            data = fetch_from_api(api_url, number)

            if data:
                pretty_raw = _format_value(data, 0)

                footer = (
                    "\n\n━━━━━━━━━━━━━━━━━━\n"
                    "⚡ Powered by Unknown Nobita\n"
                    "📡 @Unknown_Nobita_15\n"
                    "━━━━━━━━━━━━━━━━━━"
                )

                pretty_body = (
                    "📌 *Lookup Result (INDIA)*\n"
                    f"✅ *Result Found via {api_names[idx]}*\n\n"
                    + pretty_raw.replace("name:", "👤 Name:")
                                .replace("father_name:", "👨‍👧 Father:")
                                .replace("mobile:", "📱 Mobile:")
                                .replace("address:", "🏡 Address:")
                                .replace("id_number:", "🆔 Aadhaar:")
                                .replace("aadhaar:", "🆔 Aadhaar:")
                                .replace("aadhar:", "🆔 Aadhaar:")
                                .replace("adhar:", "🆔 Aadhaar:")
                                .replace("circle:", "🔁 Circle:")
                                .replace("email:", "📨 Email:")
                )

                result_text = pretty_body + footer

                if len(result_text) > 3800:
                    result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

                return result_text

        except Exception:
            continue

    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "🔗 https://t.me/OTP_RESELLER_01\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    return (
        "❌ *DATA NOT FOUND (INDIA)*\n\n"
        "⚠️ Is number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- PAKISTAN NUMBER LOOKUP ----------
def lookup_pakistan_number(mobile_number: str) -> str:
    """
    Pakistan number ke liye single API:
    https://pakistan-num-info.gauravcyber0.workers.dev/?pakistan={number}
    """
    number = mobile_number.strip().replace(" ", "").replace("-", "")

    if not number:
        return "Number khali hai."

    api_url = f"https://pakistan-num-info.gauravcyber0.workers.dev/?pakistan={number}"
    api_name = "Pakistan-API"

    try:
        data = fetch_from_api(api_url, number)

        if data:
            pretty_raw = _format_value(data, 0)

            footer = (
                "\n\n━━━━━━━━━━━━━━━━━━\n"
                "⚡ Powered by Unknown Nobita\n"
                "📡 @Unknown_Nobita_15\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            pretty_body = (
                "📌 *Lookup Result (PAKISTAN)*\n"
                f"✅ *Result Found via {api_name}*\n\n"
                + pretty_raw.replace("name:", "👤 Name:")
                            .replace("father_name:", "👨‍👧 Father:")
                            .replace("mobile:", "📱 Mobile:")
                            .replace("address:", "🏡 Address:")
                            .replace("id_number:", "🆔 ID Number:")
                            .replace("circle:", "🔁 Circle:")
                            .replace("email:", "📨 Email:")
            )

            result_text = pretty_body + footer

            if len(result_text) > 3800:
                result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

            return result_text

    except Exception:
        pass

    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "🔗 https://t.me/OTP_RESELLER_01\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    return (
        "❌ *DATA NOT FOUND (PAKISTAN)*\n\n"
        "⚠️ Is Pakistan number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- CNIC (PAKISTAN ID) LOOKUP ----------
def lookup_cnic_number(cnic: str) -> str:
    """
    CNIC (Computerized National Identity Card - Pakistan ID)
    API: https://cnic-info.gauravcyber0.workers.dev/?cnic={cnic}
    """
    cnic = cnic.strip()

    if not cnic:
        return "CNIC number khali hai."

    api_url = f"https://cnic-info.gauravcyber0.workers.dev/?cnic={cnic}"

    try:
        data = fetch_from_api(api_url, cnic)
    except Exception:
        data = None

    if not data:
        return _cnic_not_found()

    pretty_raw = _format_value(data, 0)

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Powered by Unknown Nobita\n"
        "📡 @Unknown_Nobita_15\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    pretty_body = (
        "🪪 *CNIC (Pakistan ID) Details*\n\n"
        + pretty_raw.replace("name:", "👤 Name:")
                    .replace("father_name:", "👨‍👧 Father:")
                    .replace("cnic:", "🪪 CNIC:")
                    .replace("dob:", "📅 DOB:")
                    .replace("address:", "🏡 Address:")
                    .replace("permanent_address:", "🏡 Permanent Address:")
                    .replace("temporary_address:", "🏠 Temporary Address:")
                    .replace("gender:", "⚧ Gender:")
                    .replace("mobile:", "📱 Mobile:")
    )

    result_text = pretty_body + footer

    if len(result_text) > 3800:
        result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

    return result_text


def _cnic_not_found() -> str:
    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    return (
        "❌ *DATA NOT FOUND (CNIC)*\n\n"
        "⚠️ Is CNIC number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- AADHAAR LOOKUP ----------
def lookup_aadhaar_number(aadhaar: str) -> str:
    """
    3 APIs se AADHAAR ka data fetch karta hai ek ke baad ek.
    """
    aadhaar = aadhaar.strip().replace(" ", "").replace("-", "")

    if not aadhaar:
        return "Aadhaar number khali hai."

    apis = [
        f"https://lookup.42web.io/id/?q={aadhaar}&format=json",
        f"https://suryansh.site/adhaar-info/api.php?key=suryansh&aadhaar={aadhaar}",
        f"https://niloy-api.vercel.app/api?key=Niloy&aadhaar={aadhaar}"
    ]

    api_names = ["AADHAAR-API-1", "AADHAAR-API-2", "AADHAAR-API-3"]

    for idx, api_url in enumerate(apis):
        try:
            data = fetch_from_api(api_url, aadhaar)

            if data:
                pretty_raw = _format_value(data, 0)

                footer = (
                    "\n\n━━━━━━━━━━━━━━━━━━\n"
                    "⚡ Powered by Unknown Nobita\n"
                    "📡 @Unknown_Nobita_15\n"
                    "━━━━━━━━━━━━━━━━━━"
                )

                pretty_body = (
                    "📌 *Aadhaar Lookup Result*\n"
                    f"✅ *Result Found via {api_names[idx]}*\n\n"
                    + pretty_raw.replace("name:", "👤 Name:")
                                .replace("father_name:", "👨‍👧 Father:")
                                .replace("mobile:", "📱 Mobile:")
                                .replace("address:", "🏡 Address:")
                                .replace("id_number:", "🆔 Aadhaar:")
                                .replace("aadhaar:", "🆔 Aadhaar:")
                                .replace("aadhar:", "🆔 Aadhaar:")
                                .replace("adhar:", "🆔 Aadhaar:")
                                .replace("dob:", "📅 DOB:")
                                .replace("gender:", "⚧ Gender:")
                                .replace("email:", "📨 Email:")
                )

                result_text = pretty_body + footer

                if len(result_text) > 3800:
                    result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

                return result_text

        except Exception:
            continue

    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "🔗 https://t.me/OTP_RESELLER_01\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    return (
        "❌ *DATA NOT FOUND (AADHAAR)*\n\n"
        "⚠️ Is Aadhaar number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- GST LOOKUP ----------
def lookup_gst_number(gst_no: str) -> str:
    """
    3 APIs se GST ka data fetch karta hai ek ke baad ek.
    """
    gst = gst_no.strip().replace(" ", "").upper()

    if not gst:
        return "GST number khali hai."

    apis = [
        f"https://gstdata.herokuapp.com/api/gst?number={gst}",
        f"https://hydrashop.in.net/gst.php?q={gst}&format=json",
        f"https://gstlookup.hideme.eu.org/?gstNumber={gst}"
    ]

    api_names = ["GST-API-1", "GST-API-2", "GST-API-3"]

    for idx, api_url in enumerate(apis):
        try:
            data = fetch_from_api(api_url, gst)

            if data:
                pretty_raw = _format_value(data, 0)

                footer = (
                    "\n\n━━━━━━━━━━━━━━━━━━\n"
                    "⚡ Powered by Unknown Nobita\n"
                    "📡 @Unknown_Nobita_15\n"
                    "━━━━━━━━━━━━━━━━━━"
                )

                pretty_body = (
                    "📌 *GST Lookup Result*\n"
                    f"✅ *Result Found via {api_names[idx]}*\n\n"
                    + pretty_raw.replace("gstin:", "🧾 GSTIN:")
                                .replace("gst_no:", "🧾 GSTIN:")
                                .replace("gst:", "🧾 GSTIN:")
                                .replace("trade_name:", "🏢 Trade Name:")
                                .replace("lgnm:", "👤 Legal Name:")
                                .replace("legal_name:", "👤 Legal Name:")
                                .replace("firm_name:", "👤 Firm Name:")
                                .replace("addr:", "🏡 Address:")
                                .replace("address:", "🏡 Address:")
                                .replace("state:", "🌍 State:")
                                .replace("sts:", "📌 Status:")
                                .replace("status:", "📌 Status:")
                                .replace("nature:", "💼 Nature of Business:")
                                .replace("email:", "📨 Email:")
                                .replace("mobile:", "📱 Mobile:")
                )

                result_text = pretty_body + footer

                if len(result_text) > 3800:
                    result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

                return result_text

        except Exception:
            continue

    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "🔗 https://t.me/OTP_RESELLER_01\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    return (
        "❌ *DATA NOT FOUND (GST)*\n\n"
        "⚠️ Is GST number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- PAN LOOKUP ----------
def lookup_pan_number(pan_no: str) -> str:
    """
    Single API se PAN ka data fetch karta hai:

    https://lookup.42web.io/pancard/?q={pan_no}&i=1
    """
    pan = pan_no.strip().replace(" ", "").upper()

    if not pan:
        return "PAN number khali hai."

    api_url = f"https://lookup.42web.io/pancard/?q={pan}&i=1"
    api_name = "PAN-API-42WEB"

    try:
        resp = requests.get(api_url, timeout=10)
    except Exception:
        return _pan_not_found_msg()

    if resp.status_code != 200:
        return _pan_not_found_msg()

    try:
        raw = resp.json()
    except Exception:
        return _pan_not_found_msg()

    data = _clean_data(raw)

    if not data or "data" not in data:
        return _pan_not_found_msg()

    d = data["data"]

    pan_number = d.get("PAN", pan)
    full_name = d.get("Full Name", "N/A")
    fname = d.get("First Name", "")
    mname = d.get("Middle Name", "")
    lname = d.get("Last Name", "")
    father = d.get("Father's Name", "")
    aadhaar_status = d.get("Aadhaar Seeding Status", "N/A")

    text = (
        "📌 *PAN Card Lookup Result*\n"
        f"🪪 *PAN:* `{pan_number}`\n"
        f"👤 *Full Name:* {full_name}\n"
        f"🧍 *First Name:* {fname}\n"
        f"✳️ *Middle Name:* {mname or 'N/A'}\n"
        f"🧔 *Last Name:* {lname}\n"
        f"👨‍👦 *Father's Name:* {father or 'N/A'}\n"
        f"🔗 *Aadhaar Seeding:* {aadhaar_status}\n"
    )

    footer = (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Powered by Unknown Nobita\n"
        "📡 @Unknown_Nobita_15\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    return text + footer


def _pan_not_found_msg() -> str:
    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    return (
        "❌ *DATA NOT FOUND (PAN)*\n\n"
        "⚠️ Is PAN number ke liye koi data nahi mila.\n"
        + footer_not_found
    )


# ---------- IFSC LOOKUP ----------
def lookup_ifsc_code(ifsc: str) -> str:
    """
    Single API se IFSC bank details fetch karta hai:
    https://ifsc.razorpay.com/{ifsc}
    """
    ifsc = ifsc.strip().upper()

    if not ifsc:
        return "IFSC code khali hai."

    api_url = f"https://ifsc.razorpay.com/{ifsc}"

    try:
        resp = requests.get(api_url, timeout=10)
    except Exception:
        return _ifsc_not_found()

    if resp.status_code != 200:
        return _ifsc_not_found()

    try:
        data = resp.json()
    except Exception:
        return _ifsc_not_found()

    if not data or data == {}:
        return _ifsc_not_found()

    data = _clean_data(data)
    pretty_raw = _format_value(data, 0)

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Powered by Unknown Nobita\n"
        "📡 @Unknown_Nobita_15\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    pretty_body = (
        "🏦 *IFSC Bank Details*\n\n"
        + pretty_raw.replace("BANK:", "🏛 Bank:")
                    .replace("BRANCH:", "🏢 Branch:")
                    .replace("ADDRESS:", "📍 Address:")
                    .replace("STATE:", "🌍 State:")
                    .replace("DISTRICT:", "📌 District:")
                    .replace("CITY:", "🏙 City:")
                    .replace("IFSC:", "🔗 IFSC Code:")
    )

    return pretty_body + footer


def _ifsc_not_found():
    return (
        "❌ *DATA NOT FOUND (IFSC)*\n\n"
        "⚠️ Is IFSC code ke liye koi bank information nahi mili.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


# ---------- PINCODE LOOKUP ----------
def lookup_pincode_number(pincode: str) -> str:
    """
    2 APIs se PINCODE ka data fetch karta hai ek ke baad ek.
    """
    pin = pincode.strip()

    if not pin:
        return "Pincode khali hai."

    apis = [
        f"https://api.postalpincode.in/pincode/{pin}",
        f"https://pin-code-info.gauravcyber0.workers.dev/?pincode={pin}",
    ]

    api_names = ["PIN-API-1", "PIN-API-2"]

    for idx, api_url in enumerate(apis):
        try:
            resp = requests.get(api_url, timeout=10)
        except Exception:
            continue

        if resp.status_code != 200:
            continue

        try:
            raw = resp.json()
        except Exception:
            continue

        cleaned = _clean_data(raw)

        # First API: official postalpincode (list with Status + PostOffice)
        if idx == 0:
            if not isinstance(cleaned, list) or not cleaned:
                continue
            first = cleaned[0]
            status = str(first.get("Status", "")).lower()
            # Success condition
            if status != "success":
                continue
            data_for_print = first
        else:
            # Second API: custom worker, accept non-empty
            if not cleaned:
                continue
            data_for_print = cleaned

        pretty_raw = _format_value(data_for_print, 0)

        footer = (
            "\n\n━━━━━━━━━━━━━━━━━━\n"
            "⚡ Powered by Unknown Nobita\n"
            "📡 @Unknown_Nobita_15\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        pretty_body = (
            "🏛 *Pincode Details*\n"
            f"✅ *Result Found via {api_names[idx]}*\n\n"
            + pretty_raw.replace("PostOffice:", "🏣 Post Offices:")
                        .replace("Name:", "🏷 Name:")
                        .replace("District:", "📌 District:")
                        .replace("State:", "🌍 State:")
                        .replace("Pincode:", "🔢 Pincode:")
                        .replace("Block:", "🧩 Block:")
                        .replace("Division:", "📍 Division:")
                        .replace("Region:", "🗺 Region:")
        )

        result_text = pretty_body + footer

        if len(result_text) > 3800:
            result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

        return result_text

    return _pincode_not_found()


def _pincode_not_found() -> str:
    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    return (
        "❌ *DATA NOT FOUND (PINCODE)*\n\n"
        "⚠️ Is Pincode ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- CITY LOOKUP ----------
def lookup_city_name(city: str) -> str:
    """
    CITY / Post Office name se data fetch:
    https://api.postalpincode.in/postoffice/{city}
    """
    city = city.strip()

    if not city:
        return "City name khali hai."

    api_url = f"https://api.postalpincode.in/postoffice/{city}"

    try:
        resp = requests.get(api_url, timeout=10)
    except Exception:
        return _city_not_found()

    if resp.status_code != 200:
        return _city_not_found()

    try:
        raw = resp.json()
    except Exception:
        return _city_not_found()

    cleaned = _clean_data(raw)

    # Response format: list with first element having Status + PostOffice
    if not isinstance(cleaned, list) or not cleaned:
        return _city_not_found()

    first = cleaned[0]
    status = str(first.get("Status", "")).lower()
    if status != "success":
        return _city_not_found()

    data_for_print = first

    pretty_raw = _format_value(data_for_print, 0)

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Powered by Unknown Nobita\n"
        "📡 @Unknown_Nobita_15\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    pretty_body = (
        "🏙 *City / Post Office Details*\n\n"
        + pretty_raw.replace("PostOffice:", "🏣 Post Offices:")
                    .replace("Name:", "🏷 Name:")
                    .replace("District:", "📌 District:")
                    .replace("State:", "🌍 State:")
                    .replace("Pincode:", "🔢 Pincode:")
                    .replace("Block:", "🧩 Block:")
                    .replace("Division:", "📍 Division:")
                    .replace("Region:", "🗺 Region:")
    )

    result_text = pretty_body + footer

    if len(result_text) > 3800:
        result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

    return result_text


def _city_not_found() -> str:
    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    return (
        "❌ *DATA NOT FOUND (CITY)*\n\n"
        "⚠️ Is city / post office ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- VEHICLE RC LOOKUP (NEW) ----------
def lookup_vehicle_rc(v_number: str) -> str:
    """
    2 APIs se Vehicle RC data fetch karta hai sequentially.
    1. https://vehicledata.herokuapp.com/api/vehicle?number={vno}
    2. http://india.42web.io/vehicle/?q={vno}&format=json
    """
    vno = v_number.strip().replace(" ", "").replace("-", "").upper()

    if not vno:
        return "Vehicle number khali hai."

    apis = [
        f"https://vehicledata.herokuapp.com/api/vehicle?number={vno}",
        f"http://india.42web.io/vehicle/?q={vno}&format=json"
    ]

    api_names = ["RC-API-1", "RC-API-2"]

    for idx, api_url in enumerate(apis):
        try:
            data = fetch_from_api(api_url, vno)

            if data:
                # API 2 specifically sometimes returns empty JSON or status false
                if idx == 1:
                    if not data or not data.get("status", True):
                        continue

                pretty_raw = _format_value(data, 0)

                footer = (
                    "\n\n━━━━━━━━━━━━━━━━━━\n"
                    "⚡ Powered by Unknown Nobita\n"
                    "📡 @Unknown_Nobita_15\n"
                    "━━━━━━━━━━━━━━━━━━"
                )

                # Replace common RC keys with emojis
                pretty_body = (
                    "🚗 *Vehicle RC Details*\n"
                    f"✅ *Result Found via {api_names[idx]}*\n\n"
                    + pretty_raw.replace("owner:", "👤 Owner Name:")
                                .replace("owner_name:", "👤 Owner Name:")
                                .replace("name:", "👤 Name:")
                                .replace("maker:", "🏭 Maker/Model:")
                                .replace("model:", "🚘 Model:")
                                .replace("fuel:", "⛽ Fuel Type:")
                                .replace("fuel_type:", "⛽ Fuel Type:")
                                .replace("rc_status:", "📜 RC Status:")
                                .replace("reg_date:", "📅 Registration Date:")
                                .replace("exp_date:", "⏳ Expiry Date:")
                                .replace("engine:", "⚙️ Engine No:")
                                .replace("chassis:", "🔢 Chassis No:")
                                .replace("financer:", "🏦 Financer:")
                                .replace("insurance:", "🛡 Insurance:")
                )

                result_text = pretty_body + footer

                if len(result_text) > 3800:
                    result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

                return result_text

        except Exception:
            continue

    footer_not_found = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
        "📡 *@Unknown_Nobita_15*\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    return (
        "❌ *DATA NOT FOUND (VEHICLE RC)*\n\n"
        "⚠️ Is gadi number ke liye koi information nahi mili.\n"
        + footer_not_found
    )


# ---------- INSTAGRAM PROFILE LOOKUP ----------
def lookup_instagram_profile(username: str) -> str:
    """
    Instagram profile ke liye API:
    https://instagram-info.gauravcyber0.workers.dev/?username={username}
    """
    uname = username.strip().lstrip("@")

    if not uname:
        return "Instagram username khali hai."

    api_url = f"https://instagram-info.gauravcyber0.workers.dev/?username={uname}"

    try:
        data = fetch_from_api(api_url, uname)
    except Exception:
        data = None

    if not data:
        footer_not_found = (
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ *ＰＯＷＥＲＥＤ  ＢＹ* 𓆩𝕌𝕟𝕜ⁿᵒʷⁿ 𝕟𝕠𝕓𝕚𝕥𝕒𓆪\n"
            "📡 *@Unknown_Nobita_15*\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        return (
            "❌ *DATA NOT FOUND (INSTAGRAM)*\n\n"
            "⚠️ Is username ke liye koi Instagram data nahi mila.\n"
            + footer_not_found
        )

    pretty_raw = _format_value(data, 0)

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Powered by Unknown Nobita\n"
        "📡 @Unknown_Nobita_15\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    pretty_body = (
        "📸 *Instagram Profile Info*\n\n"
        + pretty_raw.replace("username:", "👤 Username:")
                    .replace("full_name:", "📛 Full Name:")
                    .replace("name:", "📛 Name:")
                    .replace("biography:", "📝 Bio:")
                    .replace("followers:", "👥 Followers:")
                    .replace("following:", "👣 Following:")
                    .replace("posts:", "📸 Posts:")
                    .replace("is_verified:", "✔️ Verified:")
                    .replace("category:", "🏷 Category:")
    )

    result_text = pretty_body + footer

    if len(result_text) > 3800:
        result_text = result_text[:3800] + "\n\n… (trimmed)" + footer

    return result_text


# ================== COMMAND HANDLERS ==================


@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id

    # ✅ FIXED: Force join check - agar channel nahi hai to skip karo
    if CHANNEL_ID and not is_user_in_channel(user_id):
        send_force_sub(message.chat.id)
        return

    username = message.from_user.username

    # Referral
    args = message.text.split()
    referred_by = None    # Referral id

    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id:
                referred_by = ref_id
        except ValueError:
            pass

    get_or_create_user(user_id, username=username, referred_by=referred_by)

    # referral bonus
    if referred_by:
        add_credits(referred_by, 2)

    if is_banned(user_id):
        bot.reply_to(message, "🚫 Aapko is bot se ban kiya gaya hai. Admin se contact karein.")
        return

    text = (
    f"🔥 𓆩ᵁⁿᵏⁿᵒʷⁿ ᴺᵒᵇⁱᵗᵃ 𝗦𝘆𝘀𝘁𝗲𝗺 𝗕𝗼𝘁 𝗺𝗲𝗶𝗻 𝗔𝗮𝗽𝗸𝗮 𝗗𝗶𝗹 𝗦𝗲 𝗦𝘄𝗮𝗴𝗮𝘁 hai, {message.from_user.first_name}! 🔥\n"
    "╔══════════════════════╗\n"
    "👋 Namaste, Legend!\n"
    "╚══════════════════════╝\n\n"
    "✨ Aap ab enter ho chuke ho ek *AI powered Information World* me,\n"
    "jahan sirf **1 click** me milti hai *verified details* ⚡\n\n"
    "🚀 Yahaan Aap Kya-Kya Kar Sakte Ho?\n\n"
    "📌 *Features Menu* 👇\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🇮🇳 INDIA NUMBER INFO\n"
    "🇵🇰 PAKISTAN NUMBER INFO\n"
    "🆔 AADHAAR LOOKUP\n"
    "🧾 GST DETAILS\n"
    "🪪 CNIC (PAKISTAN ID)\n"
    "🚗 VEHICLE RC INFO\n"
    "🪪 PAN CARD DETAILS\n"
    "🏦 IFSC BANK DETAIL\n"
    "🏛 PINCODE INFO\n"
    "🏙 CITY DETAILS\n"
    "📸 INSTAGRAM PROFILE\n"
    "💳 CREDITS BALANCE CHECK\n"
    "🎁 REFERRAL SE FREE CREDITS\n"
    "📜 HISTORY & REPORT LOG\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "⚠️ *IMPORTANT NOTE*\n"
    "🔐 Ye bot sirf *legal, educational & informational* purpose ke liye banaya gaya hai.\n"
    "❌ Illegal use par account block ho sakta hai!\n\n"
    "💡 Bas niche diye gaye options me se kisi ek button par click karo,\n"
    "aur AI aapko *turant result* provide karega 👇\n\n"
    "𓆩ᵁⁿᵏⁿᵒʷⁿ ᴺᵒᵇⁱᵗᵃ𓆪ꪾ — *The Future of Information* ⚡\n"
    "📡 Powered By @Unknown_Nobita_15"
)

    is_admin = user_id in ADMIN_IDS
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin)
    )


# ================== CALLBACK HANDLER ==================


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data

    # ✅ FIXED: check_sub for force join
    if data == "check_sub":
        bot.answer_callback_query(call.id, "Checking subscription...")
        if CHANNEL_ID and is_user_in_channel(user_id):
            ensure_user_record_from_obj(call.from_user)
            is_admin = user_id in ADMIN_IDS
            bot.send_message(
                call.message.chat.id,
                "✅ Subscription verify ho gaya. Ab aap bot use kar sakte hain.",
                reply_markup=main_menu(is_admin)
            )
        else:
            send_force_sub(call.message.chat.id)
        return

    # ✅ FIXED: Baaki sab pe force join check - agar channel ID nahi hai to skip
    if CHANNEL_ID and not is_user_in_channel(user_id):
        bot.answer_callback_query(call.id, "Pehle channel join karein!")
        send_force_sub(call.message.chat.id)
        return

    ensure_user_record_from_obj(call.from_user)

    if is_banned(user_id) and user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "You are banned.")
        return

    # MAIN MENU ACTIONS
    if data == "number_info":
        USER_STATE[user_id] = "awaiting_india"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🇮🇳 INDIA mobile number bhejein (sirf digits, jaise 6200303551).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: Hamari 4 INDIA APIs check karengi, thoda time lagega..."
        )

    elif data == "pak_number_info":
        USER_STATE[user_id] = "awaiting_pakistan"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🇵🇰 Pakistan number bhejein (format: 923xxxxxxxxx).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: Pakistan API se data fetch hoga..."
        )

    elif data == "cnic_lookup":
        USER_STATE[user_id] = "awaiting_cnic"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🪪 Pakistan CNIC number bhejein (jaise 42101-1234567-1).\n"
            "CNIC ka matlab hai *Computerized National Identity Card* – Pakistan ka ID number.\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: CNIC API se data fetch hoga..."
        )

    elif data == "aadhaar_lookup":
        USER_STATE[user_id] = "awaiting_aadhaar"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🆔 12 digit Aadhaar number bhejein (sirf digits, jaise 123412341234).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: 3 AADHAAR APIs se data check hoga..."
        )

    elif data == "gst_lookup":
        USER_STATE[user_id] = "awaiting_gst"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🧾 15 digit GST number bhejein (jaise 22AAAAA0000A1Z5).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: 3 GST APIs me data check hoga..."
        )

    elif data == "rc_lookup":
        USER_STATE[user_id] = "awaiting_rc"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🚗 Gadi ka number bhejein (jaise DL01AB1234).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: Vehicle RC APIs se data fetch hoga..."
        )

    elif data == "pan_lookup":
        USER_STATE[user_id] = "awaiting_pan"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🪪 PAN number bhejein (jaise ABCDE1234F).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: PAN API se data fetch hoga..."
        )

    elif data == "ifsc_lookup":
        USER_STATE[user_id] = "awaiting_ifsc"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏦 IFSC code bhejein (jaise SBIN0005943).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: Razorpay IFSC API se data fetch hoga..."
        )

    elif data == "pincode_lookup":
        USER_STATE[user_id] = "awaiting_pincode"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏛 6 digit Pincode bhejein (jaise 110001).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: 2 PINCODE APIs me data check hoga..."
        )

    elif data == "city_lookup":
        USER_STATE[user_id] = "awaiting_city"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏙 City / Post Office ka naam bhejein (jaise: Connaught Place).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: CITY API se data fetch hoga..."
        )

    elif data == "instagram_lookup":
        USER_STATE[user_id] = "awaiting_instagram"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📸 Instagram username bhejein (jaise: nobita ya @nobita).\n"
            f"Har lookup me {LOOKUP_COST} credit katega.\n\n"
            "⚠️ Note: Instagram info API se data fetch hoga..."
        )

    elif data == "referral":
        bot.answer_callback_query(call.id)
        ref_link = f"https://t.me/{bot.get_me().username}?start={user_id}"
        text = (
            "🎁 Referral Program\n\n"
            "Doston ko ye link bhejein. Jab wo bot start karenge, "
            "unko free credits milenge aur aapko +2 credits milenge.\n\n"
            f"{ref_link}"
        )
        bot.send_message(call.message.chat.id, text)

    elif data == "my_credits":
        bot.answer_callback_query(call.id)
        credits = get_credits(user_id)
        if is_unlimited_user(user_id):
            msg = (
                f"♾️ *Unlimited Mode Active*\n\n"
                f"💳 Aapke account me {credits} credits hain,\n"
                f"lekin abhi lookups pe credit *nahi* katega (unlimited)."
            )
        else:
            msg = f"💳 Aapke paas abhi {credits} credits hain."
        bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")

    elif data == "my_history":
        bot.answer_callback_query(call.id)
        rows = get_history(user_id, limit=10)
        if not rows:
            bot.send_message(call.message.chat.id, "📜 Abhi tak koi history nahi mili.")
        else:
            lines = ["📜 Last 10 lookups:"]
            for q, res, dt in rows:
                lines.append(f"- {q} @ {dt}")
            bot.send_message(call.message.chat.id, "\n".join(lines))

    # ADMIN PANEL
    elif data == "admin_panel":
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Admin only.")
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "🛠 Admin Panel",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=admin_menu()
        )

    elif data == "back_main":
        is_admin = user_id in ADMIN_IDS
        bot.edit_message_text(
            "🏠 Main Menu",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=main_menu(is_admin)
        )

    # ADMIN ACTIONS
    elif data in [
        "admin_add_credit", "admin_remove_credit", "admin_broadcast",
        "admin_ban", "admin_unban", "admin_all_users",
        "admin_bonus_all", "admin_status",
        "admin_unlimited_user", "admin_unlimited_all", "admin_unlimited_list"
    ]:
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Admin only.")
            return
        bot.answer_callback_query(call.id)

        if data == "admin_add_credit":
            ADMIN_STATE[user_id] = {"mode": "add_credit"}
            bot.send_message(call.message.chat.id, "➕ User ID aur credits bhejein (format: user_id credits).")

        elif data == "admin_remove_credit":
            ADMIN_STATE[user_id] = {"mode": "remove_credit"}
            bot.send_message(call.message.chat.id, "➖ User ID aur credits bhejein (format: user_id credits).")

        elif data == "admin_ban":
            ADMIN_STATE[user_id] = {"mode": "ban"}
            bot.send_message(call.message.chat.id, "🔒 Ban karne ke liye user ID bhejein.")

        elif data == "admin_unban":
            ADMIN_STATE[user_id] = {"mode": "unban"}
            bot.send_message(call.message.chat.id, "🔓 Unban karne ke liye user ID bhejein.")

        elif data == "admin_broadcast":
            ADMIN_STATE[user_id] = {"mode": "broadcast"}
            bot.send_message(call.message.chat.id, "📢 Broadcast message bhejein (plain text).")

        elif data == "admin_all_users":
            # All Users ka clean code
            cur.execute("SELECT user_id, username, credits, unlimited_until FROM users ORDER BY user_id")
            rows = cur.fetchall()

            if not rows:
                bot.send_message(call.message.chat.id, "👥 Abhi tak koi user register nahi hai.")
            else:
                header = "👥 All Users List:\n(user_id | username | credits | unlimited)\n\n"
                chunk = header
                now = now_ts()

                for uid, uname, cr, un_till in rows:
                    # username format @username / -
                    uname = f"@{uname}" if uname else "-"

                    # unlimited_time ko int me safely convert karo
                    try:
                        un_till_int = int(un_till) if un_till is not None else 0
                    except (ValueError, TypeError):
                        un_till_int = 0

                    if un_till_int > now:
                        rem_min = (un_till_int - now) // 60
                        un_str = f"{rem_min} min"
                    else:
                        un_str = "-"

                    line = f"{uid} | {uname} | {cr} cr | {un_str}\n"

                    # 4096 char limit se pehle chunk bhej dena
                    if len(chunk) + len(line) > 3900:
                        bot.send_message(call.message.chat.id, chunk)
                        chunk = ""

                    chunk += line

                if chunk:
                    bot.send_message(call.message.chat.id, chunk)

        elif data == "admin_bonus_all":
            ADMIN_STATE[user_id] = {"mode": "bonus_all"}
            bot.send_message(
                call.message.chat.id,
                "🎁 *Bonus to All*\n\n"
                "Kitne credits sabhi users ko dene hain? Sirf number bhejein.\n"
                "Example: `5` (sab ke account me +5 credits)",
                parse_mode="Markdown"
            )

        elif data == "admin_status":
            # STATS
            cur.execute("SELECT COUNT(*), SUM(COALESCE(credits,0)) FROM users")
            total_users, total_credits = cur.fetchone()
            total_users = total_users or 0
            total_credits = total_credits or 0

            cur.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
            banned_users = cur.fetchone()[0] or 0

            cur.execute("SELECT COUNT(DISTINCT user_id) FROM history")
            active_users = cur.fetchone()[0] or 0

            cur.execute("SELECT COUNT(*) FROM users WHERE COALESCE(credits,0) = 0")
            zero_credit_users = cur.fetchone()[0] or 0

            cur.execute("SELECT user_id, credits FROM users ORDER BY credits DESC LIMIT 1")
            top_row = cur.fetchone()
            if top_row:
                top_user_id, top_user_credits = top_row
                top_line = f"🥇 *Top Credits User:* `{top_user_id}` ({top_user_credits} cr)"
            else:
                top_line = "🥇 *Top Credits User:* -"

            avg_credits = (total_credits / total_users) if total_users > 0 else 0

            # Unlimited stats
            now = now_ts()
            cur.execute("SELECT COUNT(*) FROM users WHERE unlimited_until > ?", (now,))
            unlimited_users = cur.fetchone()[0] or 0
            global_until = get_global_unlimited_until()
            if global_until > now:
                rem_min = int((global_until - now) / 60)
                global_line = f"🌐 *Global Unlimited:* ON (approx {rem_min} min left)"
            else:
                global_line = "🌐 *Global Unlimited:* OFF"

            msg = (
                "📊 *Bot Status Overview*\n\n"
                f"👥 *Total Registered Users:* {total_users}\n"
                f"✅ *Active Users (min 1 lookup):* {active_users}\n"
                f"⛔ *Banned Users:* {banned_users}\n"
                f"💳 *Total Credits in System:* {total_credits}\n"
                f"📈 *Avg Credits/User:* {avg_credits:.2f}\n"
                f"🪫 *Zero Credit Users:* {zero_credit_users}\n"
                f"{top_line}\n\n"
                f"♾️ *Unlimited Users (personal):* {unlimited_users}\n"
                f"{global_line}"
            )
            bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")

        elif data == "admin_unlimited_user":
            ADMIN_STATE[user_id] = {"mode": "unlimited_user"}
            bot.send_message(
                call.message.chat.id,
                "♾️ *Unlimited (User)*\n\n"
                "User ko unlimited search dene ke liye:\n"
                "`user_id minutes`\n\n"
                "Example: `123456789 60` (is user ko 60 minutes ke liye unlimited)",
                parse_mode="Markdown"
            )

        elif data == "admin_unlimited_all":
            ADMIN_STATE[user_id] = {"mode": "unlimited_all"}
            bot.send_message(
                call.message.chat.id,
                "🌐 *Unlimited (All)*\n\n"
                "Kitne minutes ke liye sabhi users ko unlimited search dena hai?\n"
                "Sirf minutes bhejein. Example: `30`",
                parse_mode="Markdown"
            )

        elif data == "admin_unlimited_list":
            now = now_ts()
            cur.execute(
                "SELECT user_id, username, unlimited_until FROM users WHERE unlimited_until > ? ORDER BY unlimited_until",
                (now,)
            )
            rows = cur.fetchall()
            global_until = get_global_unlimited_until()

            lines = ["📜 *Unlimited Users List*"]
            if global_until > now:
                rem_min = int((global_until - now) / 60)
                lines.append(f"🌐 Global Unlimited: *ON* (approx {rem_min} min left)")
            else:
                lines.append("🌐 Global Unlimited: *OFF*")

            if not rows:
                lines.append("\n♾️ Abhi kisi user pe personal unlimited active nahi hai.")
            else:
                lines.append("\n👥 *Personal Unlimited Users:*")
                for uid, uname, un_till in rows:
                    rem_sec = un_till - now
                    rem_min = int(rem_sec / 60)
                    rem_hr = int(rem_min / 60)
                    if rem_hr > 0:
                        rem_str = f"{rem_hr} hr {rem_min % 60} min"
                    else:
                        rem_str = f"{rem_min} min"
                    uname = f"@{uname}" if uname else "-"
                    lines.append(f"• `{uid}` ({uname}) → {rem_str} left")

                lines.append(
                    "\n❌ Kisi user ka unlimited band karne ke liye uska `user_id` bhejein.\n"
                    "Example: `123456789`"
                )
                ADMIN_STATE[user_id] = {"mode": "unlimited_cancel"}

            bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="Markdown")


# ================== MESSAGE HANDLERS (STATE) ==================


@bot.message_handler(func=lambda m: USER_STATE.get(m.from_user.id) in (
        "awaiting_india", "awaiting_pakistan", "awaiting_aadhaar",
        "awaiting_gst", "awaiting_pan", "awaiting_ifsc",
        "awaiting_pincode", "awaiting_city", "awaiting_rc", "awaiting_cnic",
        "awaiting_instagram"))
def handle_number_lookup(message):
    user_id = message.from_user.id

    # ✅ FIXED: Force join check
    if CHANNEL_ID and not is_user_in_channel(user_id):
        USER_STATE.pop(user_id, None)
        send_force_sub(message.chat.id)
        return

    ensure_user_record_from_obj(message.from_user)

    number = message.text.strip()

    if is_banned(user_id):
        bot.reply_to(message, "🚫 Aap banned hain.")
        return

    unlimited_active = is_unlimited_user(user_id)

    if not unlimited_active:
        credits = get_credits(user_id)
        if credits < LOOKUP_COST:
            bot.reply_to(message, "❌ Aapke paas enough credits nahi hain. Pehle credits add karwayein.")
            USER_STATE[user_id] = None
            return

    mode = USER_STATE.get(user_id)

    # Animated loading message (mode ke hisaab se text)
    if mode == "awaiting_india":
        loading_msg = bot.reply_to(message, "🔍 INDIA APIs me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🔎", "📞"]
    elif mode == "awaiting_pakistan":
        loading_msg = bot.reply_to(message, "🔍 PAKISTAN API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🔎"]
    elif mode == "awaiting_cnic":
        loading_msg = bot.reply_to(message, "🔍 CNIC API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🪪"]
    elif mode == "awaiting_aadhaar":
        loading_msg = bot.reply_to(message, "🔍 AADHAAR APIs me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🔎", "🆔"]
    elif mode == "awaiting_gst":
        loading_msg = bot.reply_to(message, "🔍 GST APIs me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🔎", "🧾"]
    elif mode == "awaiting_pan":
        loading_msg = bot.reply_to(message, "🔍 PAN API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🔎", "🪪"]
    elif mode == "awaiting_ifsc":
        loading_msg = bot.reply_to(message, "🔍 IFSC API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🏦"]
    elif mode == "awaiting_pincode":
        loading_msg = bot.reply_to(message, "🔍 PINCODE APIs me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🏛"]
    elif mode == "awaiting_city":
        loading_msg = bot.reply_to(message, "🔍 CITY API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "🏙"]
    elif mode == "awaiting_rc":
        loading_msg = bot.reply_to(message, "🔍 Checking RC details...")
        loading_emojis = ["🔍", "📡", "🚗", "📜"]
    elif mode == "awaiting_instagram":
        loading_msg = bot.reply_to(message, "🔍 INSTAGRAM API me search ho raha hai...")
        loading_emojis = ["🔍", "📡", "📸"]
    else:
        loading_msg = bot.reply_to(message, "🔍 Searching...")
        loading_emojis = ["🔍", "📡"]

    # Animated loading
    for i in range(len(loading_emojis)):
        try:
            bot.edit_message_text(
                f"{loading_emojis[i]} Searching...",
                chat_id=message.chat.id,
                message_id=loading_msg.message_id
            )
            time.sleep(0.5)
        except Exception:
            pass

    # Actual lookup
    if mode == "awaiting_india":
        result_text = lookup_india_number(number)
    elif mode == "awaiting_pakistan":
        result_text = lookup_pakistan_number(number)
    elif mode == "awaiting_cnic":
        result_text = lookup_cnic_number(number)
    elif mode == "awaiting_aadhaar":
        result_text = lookup_aadhaar_number(number)
    elif mode == "awaiting_gst":
        result_text = lookup_gst_number(number)
    elif mode == "awaiting_pan":
        result_text = lookup_pan_number(number)
    elif mode == "awaiting_ifsc":
        result_text = lookup_ifsc_code(number)
    elif mode == "awaiting_pincode":
        result_text = lookup_pincode_number(number)
    elif mode == "awaiting_city":
        result_text = lookup_city_name(number)
    elif mode == "awaiting_rc":
        result_text = lookup_vehicle_rc(number)
    elif mode == "awaiting_instagram":
        result_text = lookup_instagram_profile(number)
    else:
        result_text = "❌ Invalid state. Dubara try karein."

    if not unlimited_active:
        remove_credits(user_id, LOOKUP_COST)

    save_history(user_id, number, result_text)

    # Final result with animated loading message removal
    try:
        bot.delete_message(message.chat.id, loading_msg.message_id)
    except Exception:
        pass

    bot.send_message(message.chat.id, result_text, parse_mode="Markdown", disable_web_page_preview=False)

    remaining = get_credits(user_id)
    if unlimited_active:
        bot.send_message(
            message.chat.id,
            f"♾️ *Unlimited ON* – is lookup par koi credit nahi kata.\n"
            f"💳 Credits (display only): {remaining}",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(message.chat.id, f"💳 Remaining credits: {remaining}")
    USER_STATE[user_id] = None


@bot.message_handler(func=lambda m: m.from_user.id in ADMIN_STATE)
def handle_admin_state(message):
    user_id = message.from_user.id

    # ✅ FIXED: Force join check
    if CHANNEL_ID and not is_user_in_channel(user_id):
        ADMIN_STATE.pop(user_id, None)
        send_force_sub(message.chat.id)
        return

    state = ADMIN_STATE.get(user_id)
    if not state:
        return

    mode = state.get("mode")

    if mode in ["add_credit", "remove_credit"]:
        try:
            uid_str, amount_str = message.text.split()
            target_id = int(uid_str)
            amount = int(amount_str)
        except Exception:
            bot.reply_to(message, "Format galat hai. Example: 123456789 10")
            return

        if mode == "add_credit":
            add_credits(target_id, amount)
            bot.reply_to(message, f"{target_id} ko +{amount} credits de diye gaye.")
        else:
            remove_credits(target_id, amount)
            bot.reply_to(message, f"{target_id} se -{amount} credits hata diye gaye.")

        ADMIN_STATE.pop(user_id, None)

    elif mode == "broadcast":
        text = message.text
        cur.execute("SELECT user_id FROM users")
        all_users = cur.fetchall()
        success = 0
        for (uid,) in all_users:
            try:
                bot.send_message(uid, f"📢 Broadcast:\n\n{text}")
                success += 1
            except Exception:
                pass
        bot.reply_to(message, f"Broadcast complete. Sent to {success} users.")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "ban":
        try:
            target_id = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "User ID number me bhejein.")
            return
        set_ban_status(target_id, True)
        bot.reply_to(message, f"User {target_id} ko ban kar diya gaya.")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "unban":
        try:
            target_id = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "User ID number me bhejein.")
            return
        set_ban_status(target_id, False)
        bot.reply_to(message, f"User {target_id} ko unban kar diya gaya.")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "bonus_all":
        # Bonus credits to all users
        try:
            bonus = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "Sirf number me credits bhejein. Example: 5")
            return

        if bonus <= 0:
            bot.reply_to(message, "Bonus 0 se bada hona chahiye.")
            return

        # Fetch current balances then update
        cur.execute("SELECT user_id, COALESCE(credits,0) FROM users")
        rows = cur.fetchall()

        cur.execute("UPDATE users SET credits = COALESCE(credits,0) + ?", (bonus,))
        conn.commit()

        # Notify users
        sent = 0
        for uid, old_cr in rows:
            new_balance = old_cr + bonus
            try:
                bot.send_message(
                    uid,
                    "🎁 *Bonus Credit Update*\n\n"
                    f"✨ Aapke account me *{bonus}* bonus credits add kiye gaye hain!\n"
                    f"💳 New Balance: *{new_balance}*\n\n"
                    "🤝 Thanks for using 𓆩ᵁⁿᵏⁿᵒʷⁿ ᴺᵒᵇⁱᵗᵃ𓆪ꪾ Bot 💙",
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception:
                pass

        bot.reply_to(message, f"✅ Bonus +{bonus} credits sabhi users ko de diya gaya.\n📨 Notifications sent: {sent}")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "unlimited_user":
        try:
            uid_str, min_str = message.text.split()
            target_id = int(uid_str)
            minutes = int(min_str)
        except Exception:
            bot.reply_to(message, "Format galat hai. Example: 123456789 60")
            return

        if minutes <= 0:
            bot.reply_to(message, "Minutes 0 se bade hone chahiye.")
            return

        expiry = set_user_unlimited(target_id, minutes)
        rem_min = minutes
        bot.reply_to(
            message,
            f"♾️ User `{target_id}` ko {rem_min} minutes ke liye *Unlimited Search* de diya gaya.",
            parse_mode="Markdown"
        )
        # Notify target user
        try:
            bot.send_message(
                target_id,
                "♾️ *Unlimited Search Activated*\n\n"
                f"⏰ Aapko *{minutes} minutes* ke liye unlimited search de di gayi hai.\n"
                "🔍 Is duration me aapke credits *nahi* katenge.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        ADMIN_STATE.pop(user_id, None)

    elif mode == "unlimited_all":
        try:
            minutes = int(message.text.strip())
        except Exception:
            bot.reply_to(message, "Sirf minutes number me bhejein. Example: 30")
            return

        if minutes <= 0:
            bot.reply_to(message, "Minutes 0 se bade hone chahiye.")
            return

        expiry = set_global_unlimited(minutes)
        rem_min = minutes
        bot.reply_to(
            message,
            f"🌐 *Global Unlimited ON*\n\n"
            f"Sabh users ko {rem_min} minutes ke liye unlimited search de diya gaya.\n"
            "Is duration me kisi ka credit nahi katega.",
            parse_mode="Markdown"
        )

        # Notify all users
        cur.execute("SELECT user_id FROM users")
        all_users = cur.fetchall()
        sent = 0
        for (uid,) in all_users:
            try:
                bot.send_message(
                    uid,
                    "🌐 *Global Unlimited Search*\n\n"
                    f"♾️ Aapko abhi *{minutes} minutes* ke liye unlimited search mil rahi hai.\n"
                    "🔍 Is time me sabhi lookups free honge (credits cut nahi honge).",
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception:
                pass

        ADMIN_STATE.pop(user_id, None)

    elif mode == "unlimited_cancel":
        try:
            target_id = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "User ID number me bhejein. Example: 123456789")
            return

        clear_user_unlimited(target_id)
        bot.reply_to(
            message,
            f"❌ User `{target_id}` ka *Unlimited Search* off kar diya gaya.",
            parse_mode="Markdown"
        )
        try:
            bot.send_message(
                target_id,
                "❌ *Unlimited Search Disabled*\n\n"
                "Aapka unlimited search mode ab band kar diya gaya hai.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        ADMIN_STATE.pop(user_id, None)


# ================== FALLBACK ==================


@bot.message_handler(func=lambda m: True)
def fallback(message):
    user_id = message.from_user.id

    # ✅ FIXED: Force join check
    if CHANNEL_ID and not is_user_in_channel(user_id):
        send_force_sub(message.chat.id)
        return

    ensure_user_record_from_obj(message.from_user)

    # Ban check (admin ko allow)
    if is_banned(user_id) and user_id not in ADMIN_IDS:
        bot.reply_to(message, "🚫 Aap banned hain.")
        return

    text = (
    f"🔥 𓆩ᵁⁿᵏⁿᵒʷⁿ ᴺᵒᵇⁱᵗᵃ System Bot me Swagat hai, {message.from_user.first_name}! 🔥\n"
    "╔═══════════════╗\n"
    "👋 Namaste, Legend!\n"
    "╚═══════════════╝\n\n"
    "✨ Aap enter ho chuke ho AI Powered Information Zone me,\n"
    "jahan **1 click = instant verified details** ⚡\n\n"
    "⚠️ *IMPORTANT*\n"
    "Ye bot sirf **legal & educational** use ke liye hai.\n\n"
    "👇 Kisi bhi button par click karein &\n"
    "**turant result payein** 🚀\n\n"
    "𓆩ᵁⁿᵏⁿᵒʷⁿ ᴺᵒᵇⁱᵗᵃ𓆪ꪾ — *The Future of Information* ⚡\n"
    "📡 Powered by @Unknown_Nobita_15\n"
    "━━━━━━━━━━━━━━━━"
)

    is_admin = user_id in ADMIN_IDS
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin)
    )


# ================== RUN ==================
if __name__ == "__main__":
    print("Bot started...")
    bot.infinity_polling(skip_pending=True)