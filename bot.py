
import os
import re
import json
import time
import sqlite3
import logging
import threading

import telebot
import gspread
from flask import Flask, jsonify
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2.service_account import Credentials


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID_RAW = os.environ.get("ADMIN_GROUP_ID")
VIP_LINK = os.environ.get("VIP_LINK")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_WORKSHEET_NAME = os.environ.get(
    "GOOGLE_WORKSHEET_NAME", "Registrations"
)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)

# Temporary storage: no Render disk required.
DB_PATH = os.environ.get(
    "DB_PATH", "/tmp/wxc_verification.db"
)

PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")

if not ADMIN_GROUP_ID_RAW:
    raise RuntimeError("Missing ADMIN_GROUP_ID environment variable.")

if not VIP_LINK:
    raise RuntimeError("Missing VIP_LINK environment variable.")

try:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_RAW)
except ValueError:
    raise RuntimeError("ADMIN_GROUP_ID must be a numeric Telegram chat ID.")


# --------------------------------------------------
# LOGGING AND BOT SETUP
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

awaiting_account_id = set()
awaiting_lock = threading.Lock()


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def get_db_connection():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                telegram_user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                account_id TEXT,
                status TEXT NOT NULL,
                admin_message_id INTEGER,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.commit()

    logger.info("Database initialized at %s", DB_PATH)


def get_verification(user_id):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM verifications WHERE telegram_user_id = ?",
            (user_id,)
        ).fetchone()


def save_verification(
    user_id,
    username,
    first_name,
    account_id,
    status,
    admin_message_id=None
):
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO verifications (
                telegram_user_id, username, first_name,
                account_id, status, admin_message_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                account_id = excluded.account_id,
                status = excluded.status,
                admin_message_id = excluded.admin_message_id,
                updated_at = excluded.updated_at
        """, (
            user_id,
            username,
            first_name,
            account_id,
            status,
            admin_message_id,
            int(time.time())
        ))
        conn.commit()


def set_verification_status(user_id, status):
    with get_db_connection() as conn:
        cursor = conn.execute("""
            UPDATE verifications
            SET status = ?, updated_at = ?
            WHERE telegram_user_id = ? AND status = 'pending'
        """, (status, int(time.time()), user_id))
        conn.commit()
        return cursor.rowcount == 1


# --------------------------------------------------
# GOOGLE SHEETS
# Player IDs start in cell E11.
# --------------------------------------------------

def connect_to_google_sheet():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID.")

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON.")

    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes
    )

    client = gspread.authorize(credentials)

    # Accept either a full spreadsheet URL or just its ID.
    sheet_id = GOOGLE_SHEET_ID.strip()

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id)
    if match:
        sheet_id = match.group(1)

    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)


def lookup_account_in_sheet(account_id):
    """
    Reads player IDs from E11 downward.

    Returns:
        ("found", None)
        ("not_found", None)
        ("error", None)
    """
    try:
        worksheet = connect_to_google_sheet()
        player_ids = worksheet.get("E11:E")
        submitted_id = str(account_id).strip()

        for row in player_ids:
            if not row:
                continue

            sheet_account_id = str(row[0]).strip()

            if sheet_account_id and sheet_account_id == submitted_id:
                return "found", None

        return "not_found", None

    except Exception:
        logger.exception("Google Sheets lookup failed.")
        return "error", None


# --------------------------------------------------
# TELEGRAM KEYBOARDS
# --------------------------------------------------

def start_keyboard(user_id):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            "🚀 Start Verification",
            callback_data=f"begin_{user_id}"
        )
    )
    return keyboard


def admin_review_keyboard(user_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"review:approve:{user_id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"review:reject:{user_id}"
        )
    )
    return keyboard


# --------------------------------------------------
# /START
# --------------------------------------------------

@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.from_user.id

    existing = get_verification(user_id)

    if existing:
        status = existing["status"]

        if status == "approved":
            bot.reply_to(
                message,
                "✅ You have already been verified.\n\n"
                f"VIP access: {VIP_LINK}"
            )
            return

        if status == "pending":
            bot.reply_to(
                message,
                "⏳ Your verification is pending admin review. "
                "Please wait for an update."
            )
            return

        if status == "rejected":
            bot.reply_to(
                message,
                "❌ Your verification was rejected. "
                "Please contact an administrator if you believe "
                "this was a mistake."
            )
            return

    welcome = (
        "Welcome to WXC Verification!\n\n"
        "Verify your 1XBET account to access the VIP group.\n\n"
        "Press the button below to begin."
    )

    bot.send_message(
        message.chat.id,
        welcome,
        reply_markup=start_keyboard(user_id)
    )


# --------------------------------------------------
# BEGIN VERIFICATION
# --------------------------------------------------

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("begin_")
)
def begin_verification(call):
    try:
        target_user_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "Invalid verification request."
        )
        return

    if call.from_user.id != target_user_id:
        bot.answer_callback_query(
            call.id,
            "Please start verification from your own chat.",
            show_alert=True
        )
        return

    existing = get_verification(target_user_id)

    if existing:
        status = existing["status"]

        if status == "approved":
            bot.answer_callback_query(
                call.id, "You are already verified."
            )
            bot.send_message(
                call.message.chat.id,
                f"✅ You are verified.\nVIP access: {VIP_LINK}"
            )
            return

        if status == "pending":
            bot.answer_callback_query(
                call.id, "Your verification is pending review."
            )
            return

        if status == "rejected":
            bot.answer_callback_query(
                call.id, "Your verification was rejected.",
                show_alert=True
            )
            return

    with awaiting_lock:
        awaiting_account_id.add(target_user_id)

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "Please send your 1XBET account ID.\n\n"
        "Make sure it is your account ID, not your username "
        "or password.\n\n"
        "🔒 Never send your password or OTP."
    )


# --------------------------------------------------
# RECEIVE ACCOUNT ID
# --------------------------------------------------

@bot.message_handler(
    func=lambda message: (
        message.from_user is not None
        and message.chat.type == "private"
        and message.from_user.id in awaiting_account_id
    )
)
def receive_account_id(message):
    user = message.from_user
    user_id = user.id
    account_id = (message.text or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", account_id):
        bot.reply_to(
            message,
            "That ID format doesn't look valid. Please send your "
            "account ID using 3–64 letters, numbers, underscores, "
            "or hyphens."
        )
        return

    existing = get_verification(user_id)

    if existing and existing["status"] in (
        "pending", "approved", "rejected"
    ):
        with awaiting_lock:
            awaiting_account_id.discard(user_id)

        bot.reply_to(
            message,
            "You already have a verification record. "
            "Please wait for the existing review or contact an admin."
        )
        return

    with awaiting_lock:
        awaiting_account_id.discard(user_id)

    bot.send_message(
        message.chat.id,
        "🔎 Checking your account ID. Please wait..."
    )

    result, _ = lookup_account_in_sheet(account_id)

    if result == "found":
        save_verification(
            user_id=user_id,
            username=user.username or "",
            first_name=user.first_name or "",
            account_id=account_id,
            status="approved"
        )

        bot.send_message(
            message.chat.id,
            "✅ Your account ID was found in the registration list!\n\n"
            f"VIP access: {VIP_LINK}"
        )

        logger.info("Automatically approved Telegram user %s", user_id)
        return

    # Not found or lookup failed: send to Admin GC for review.
    save_verification(
        user_id=user_id,
        username=user.username or "",
        first_name=user.first_name or "",
        account_id=account_id,
        status="pending"
    )

    if result == "not_found":
        reason = "Account ID was not found in the registration list."
    else:
        reason = (
            "Google Sheets lookup failed. Manual verification is needed."
        )

    username_display = (
        f"@{user.username}" if user.username else "(no username)"
    )

    admin_text = (
        "📝 WXC VERIFICATION REVIEW\n\n"
        f"Name: {user.first_name or 'Unknown'}\n"
        f"Username: {username_display}\n"
        f"Telegram ID: {user_id}\n"
        f"Submitted account ID: {account_id}\n\n"
        f"Reason: {reason}"
    )

    try:
        admin_message = bot.send_message(
            ADMIN_GROUP_ID,
            admin_text,
            reply_markup=admin_review_keyboard(user_id)
        )

        save_verification(
            user_id=user_id,
            username=user.username or "",
            first_name=user.first_name or "",
            account_id=account_id,
            status="pending",
            admin_message_id=admin_message.message_id
        )

        bot.send_message(
            message.chat.id,
            "🕒 Your account ID has been sent to our admins "
            "for manual review. Please wait for confirmation."
        )

    except Exception:
        logger.exception("Could not notify Admin GC.")

        bot.send_message(
            message.chat.id,
            "⚠️ We couldn't notify the admin group right now. "
            "Please contact an administrator for help."
        )


# --------------------------------------------------
# ADMIN REVIEW
# --------------------------------------------------

def is_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Could not check admin status.")
        return False


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("review:")
)
def review_verification(call):
    parts = call.data.split(":")

    if len(parts) != 3:
        bot.answer_callback_query(
            call.id, "Invalid review request."
        )
        return

    _, action, user_id_raw = parts

    try:
        target_user_id = int(user_id_raw)
    except ValueError:
        bot.answer_callback_query(
            call.id, "Invalid user ID."
        )
        return

    if call.message.chat.id != ADMIN_GROUP_ID:
        bot.answer_callback_query(
            call.id, "This action is only allowed in the Admin GC.",
            show_alert=True
        )
        return

    if not is_admin(ADMIN_GROUP_ID, call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "Only Admin GC administrators can review requests.",
            show_alert=True
        )
        return

    if action not in ("approve", "reject"):
        bot.answer_callback_query(
            call.id, "Unknown action."
        )
        return

    new_status = "approved" if action == "approve" else "rejected"

    if not set_verification_status(target_user_id, new_status):
        bot.answer_callback_query(
            call.id,
            "This request has already been processed or no longer exists.",
            show_alert=True
        )
        return

    if new_status == "approved":
        user_message = (
            "✅ Your verification has been approved!\n\n"
            f"VIP access: {VIP_LINK}"
        )
        admin_result = "✅ APPROVED"
    else:
        user_message = (
            "❌ Your verification request was rejected. "
            "Please contact an administrator if you believe "
            "this was a mistake."
        )
        admin_result = "❌ REJECTED"

    try:
        bot.send_message(target_user_id, user_message)
    except Exception:
        logger.exception(
            "Could not notify Telegram user %s", target_user_id
        )

    try:
        bot.edit_message_text(
            f"{call.message.text}\n\n{admin_result} by "
            f"{call.from_user.first_name}.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception:
        logger.exception("Could not update the admin review message.")

    bot.answer_callback_query(call.id, admin_result)


# --------------------------------------------------
# FLASK HEALTH ENDPOINTS FOR UPTIMEROBOT
# --------------------------------------------------

@app.route("/")
def home():
    return "WXC Verification Bot is alive!", 200


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "WXC Verification Bot"
    }), 200


def run_web_server():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


# --------------------------------------------------
# STARTUP
# --------------------------------------------------

if __name__ == "__main__":
    initialize_database()

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    logger.info("Starting Telegram bot polling.")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )
        except Exception:
            logger.exception(
                "Telegram polling stopped unexpectedly. Retrying in 5s."
            )
            time.sleep(5)
