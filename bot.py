
import os
import re
import json
import time
import sqlite3
import logging
import threading

import telebot
import gspread

from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIGURATION — SET THESE IN RENDER ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_GROUP_ID_RAW = os.environ.get("ADMIN_GROUP_ID", "").strip()
VIP_LINK = os.environ.get("VIP_LINK", "").strip()

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_WORKSHEET_NAME = os.environ.get(
    "GOOGLE_WORKSHEET_NAME", "Registrations"
).strip()

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", ""
).strip()

DB_PATH = os.environ.get("DB_PATH", "/var/data/wxc_verification.db")
PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# VALIDATE CONFIGURATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN.")

if not ADMIN_GROUP_ID_RAW:
    raise RuntimeError("Missing ADMIN_GROUP_ID.")

if not VIP_LINK:
    raise RuntimeError("Missing VIP_LINK.")

if not GOOGLE_SHEET_ID:
    raise RuntimeError("Missing GOOGLE_SHEET_ID.")

if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON.")

try:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_GROUP_ID must be a numeric Telegram chat ID.") from exc


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)

awaiting_account_id = set()
awaiting_lock = threading.Lock()


# ============================================================
# SQLITE DATABASE
# ============================================================

def get_db_connection():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(directory, exist_ok=True)

    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                first_name TEXT,
                account_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                admin_message_id INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_verifications_account_id
            ON verifications(account_id)
        """)
        conn.commit()


def get_verification(user_id):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM verifications WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()


def save_verification(
    user_id,
    username,
    first_name,
    account_id,
    status,
):
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO verifications (
                telegram_user_id,
                telegram_username,
                first_name,
                account_id,
                status,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                first_name = excluded.first_name,
                account_id = excluded.account_id,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id,
            username or "",
            first_name or "",
            account_id or "",
            status,
        ))
        conn.commit()


def set_admin_message_id(user_id, message_id):
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE verifications
            SET admin_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE telegram_user_id = ?
        """, (message_id, user_id))
        conn.commit()


def update_status_if_pending(user_id, new_status):
    """Change status only if the case is still awaiting review."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            UPDATE verifications
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE telegram_user_id = ? AND status = 'pending'
        """, (new_status, user_id))
        conn.commit()
        return cursor.rowcount == 1


initialize_database()


# ============================================================
# GOOGLE SHEETS
# Sheet needs only one column headed "Account ID".
# A successful match means the user is already registered.
# ============================================================

def extract_sheet_id(value):
    """Accept a spreadsheet ID or a complete Google Sheets URL."""
    if "docs.google.com/spreadsheets" not in value:
        return value

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)

    if not match:
        raise ValueError("Could not extract spreadsheet ID from URL.")

    return match.group(1)


def connect_to_google_sheet():
    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ],
    )

    client = gspread.authorize(credentials)

    spreadsheet_id = extract_sheet_id(GOOGLE_SHEET_ID)
    spreadsheet = client.open_by_key(spreadsheet_id)

    return spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())



def lookup_account_in_sheet(account_id):
    """
    Checks player IDs in column E, starting at E11.
    Returns:
        ("found", None)      ID exists in the list.
        ("not_found", None)  ID is not in the list.
        ("error", None)      Google Sheets lookup failed.
    """
    try:
        worksheet = connect_to_google_sheet()

        # Read column E starting from row 11.
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


# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def start_keyboard(user_id):
    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            "🚀 Start Verification",
            callback_data=f"begin_{user_id}",
        )
    )

    return keyboard


def admin_review_keyboard(user_id):
    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"review:approve:{user_id}",
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"review:reject:{user_id}",
        ),
    )

    return keyboard


# ============================================================
# MESSAGE HELPERS
# ============================================================

def safe_notify_user(user_id, message):
    try:
        bot.send_message(user_id, message)
    except Exception:
        logger.exception("Could not message user %s.", user_id)


def notify_admin_group(user, account_id, reason):
    username = (
        f"@{user.username}"
        if user.username
        else "(no Telegram username)"
    )

    text = (
        "🟡 WXC VERIFICATION — MANUAL REVIEW\n\n"
        f"Name: {user.first_name or 'Unknown'}\n"
        f"Username: {username}\n"
        f"Telegram ID: {user.id}\n"
        f"Submitted Account ID: {account_id}\n"
        f"Reason: {reason}\n\n"
        "The submitted ID was not automatically verified. "
        "Please review this case."
    )

    message = bot.send_message(
        ADMIN_GROUP_ID,
        text,
        reply_markup=admin_review_keyboard(user.id),
    )

    set_admin_message_id(user.id, message.message_id)


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    record = get_verification(user_id)

    if record:
        if record["status"] == "approved":
            bot.send_message(
                message.chat.id,
                "✅ You have already been verified!\n\n"
                f"Your VIP link: {VIP_LINK}",
                disable_web_page_preview=True,
            )
            return

        if record["status"] == "pending":
            bot.send_message(
                message.chat.id,
                "⏳ Your verification is awaiting manual review. "
                "Please wait for an administrator.",
            )
            return

        if record["status"] == "rejected":
            bot.send_message(
                message.chat.id,
                "❌ Your previous submission was rejected.\n\n"
                "If you believe this is a mistake, please contact "
                "an administrator.",
            )
            return

    welcome_text = (
        "👋 Welcome to WXC Verification!\n\n"
        "Verify your 1XBET account to gain access to our VIP community.\n\n"
        "Submit your 1XBET Account ID. We will check it against our "
        "registration list.\n\n"
        "⚠️ Never share your password or OTP.\n\n"
        "Press the button below to begin."
    )

    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=start_keyboard(user_id),
    )


# ============================================================
# START VERIFICATION BUTTON
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("begin_")
)
def begin_verification(call):
    try:
        target_user_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid verification request.")
        return

    if call.from_user.id != target_user_id:
        bot.answer_callback_query(
            call.id,
            "This button belongs to another user.",
            show_alert=True,
        )
        return

    record = get_verification(target_user_id)

    if record:
        if record["status"] == "approved":
            bot.answer_callback_query(call.id, "You are already verified.")
            safe_notify_user(
                target_user_id,
                f"✅ You are already verified!\n\nVIP link: {VIP_LINK}",
            )
            return

        if record["status"] == "pending":
            bot.answer_callback_query(
                call.id,
                "Your case is awaiting manual review.",
                show_alert=True,
            )
            return

        if record["status"] == "rejected":
            bot.answer_callback_query(
                call.id,
                "Your previous submission was rejected. Contact an admin.",
                show_alert=True,
            )
            return

    with awaiting_lock:
        awaiting_account_id.add(target_user_id)

    bot.answer_callback_query(call.id)

    bot.send_message(
        target_user_id,
        "🔎 Please send your 1XBET Account ID as a message.\n\n"
        "Make sure you send your account ID, not your username or password.\n"
        "You can usually find your ID in your account/profile section.\n\n"
        "⚠️ Never share your password or OTP.",
    )


# ============================================================
# RECEIVE ACCOUNT ID
# ============================================================

@bot.message_handler(
    content_types=["text"],
    func=lambda message: (
        message.chat.type == "private"
        and not message.text.startswith("/")
    ),
)
def receive_account_id(message):
    user_id = message.from_user.id

    with awaiting_lock:
        is_waiting = user_id in awaiting_account_id

    if not is_waiting:
        return

    account_id = message.text.strip()

    # Adjust this validation if your provider uses a different ID format.
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", account_id):
        bot.send_message(
            message.chat.id,
            "⚠️ That doesn't look like a valid Account ID.\n\n"
            "Please send only your account ID using letters, numbers, "
            "hyphens, or underscores. Do not send your password or OTP.",
        )
        return

    existing_record = get_verification(user_id)

    if existing_record and existing_record["status"] in {
        "pending",
        "approved",
        "rejected",
    }:
        with awaiting_lock:
            awaiting_account_id.discard(user_id)

        bot.send_message(
            message.chat.id,
            "You already have a verification record. Use /start to "
            "check your status, or contact an administrator if needed.",
        )
        return

    with awaiting_lock:
        awaiting_account_id.discard(user_id)

    bot.send_message(
        message.chat.id,
        "⏳ Checking your Account ID against the latest registration list...",
    )

    lookup_status, _ = lookup_account_in_sheet(account_id)

    # FOUND: automatically approve.
    if lookup_status == "found":
        save_verification(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            account_id=account_id,
            status="approved",
        )

        bot.send_message(
            message.chat.id,
            "✅ Verification successful!\n\n"
            "Your Account ID is on our registration list.\n\n"
            f"Welcome to the VIP community!\n{VIP_LINK}",
            disable_web_page_preview=True,
        )
        return

    # NOT FOUND or SHEET ERROR: manual review.
    if lookup_status == "not_found":
        reason = "Account ID was not found in the current registration list."
    else:
        reason = (
            "Google Sheets could not be checked. Manual review is required."
        )

    save_verification(
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        account_id=account_id,
        status="pending",
    )

    try:
        notify_admin_group(
            message.from_user,
            account_id,
            reason,
        )
    except Exception:
        logger.exception("Could not notify Admin GC.")

        bot.send_message(
            message.chat.id,
            "⚠️ Your submission was saved, but the Admin GC could not be "
            "notified. Please contact an administrator directly.",
        )
        return

    bot.send_message(
        message.chat.id,
        "🟡 Your Account ID needs manual review.\n\n"
        "Your submission has been forwarded to the verification team. "
        "Please wait for an administrator to approve or reject it.",
    )


# ============================================================
# ADMIN REVIEW PERMISSIONS
# ============================================================

def is_admin_group_administrator(user_id):
    try:
        member = bot.get_chat_member(ADMIN_GROUP_ID, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        logger.exception("Could not verify Admin GC permissions.")
        return False


# ============================================================
# ADMIN APPROVE / REJECT BUTTONS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("review:")
)
def handle_admin_review(call):
    if call.message.chat.id != ADMIN_GROUP_ID:
        bot.answer_callback_query(
            call.id,
            "This action is only available in the configured Admin GC.",
            show_alert=True,
        )
        return

    if not is_admin_group_administrator(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "Only Admin GC administrators can review submissions.",
            show_alert=True,
        )
        return

    parts = call.data.split(":")

    if len(parts) != 3:
        bot.answer_callback_query(call.id, "Invalid review action.")
        return

    action = parts[1]

    try:
        target_user_id = int(parts[2])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid user ID.")
        return

    if action not in {"approve", "reject"}:
        bot.answer_callback_query(call.id, "Unknown review action.")
        return

    record = get_verification(target_user_id)

    if not record:
        bot.answer_callback_query(
            call.id,
            "Verification record not found.",
            show_alert=True,
        )
        return

    if record["status"] != "pending":
        bot.answer_callback_query(
            call.id,
            f"This case has already been handled ({record['status']}).",
            show_alert=True,
        )
        return

    new_status = "approved" if action == "approve" else "rejected"

    # Atomic database update prevents two admins handling the same case.
    if not update_status_if_pending(target_user_id, new_status):
        bot.answer_callback_query(
            call.id,
            "This case has already been handled by another admin.",
            show_alert=True,
        )
        return

    if action == "approve":
        safe_notify_user(
            target_user_id,
            "✅ Your verification has been approved by an administrator!\n\n"
            f"Welcome to the VIP community.\n{VIP_LINK}",
        )

        result_text = (
            "✅ APPROVED BY ADMIN\n"
            f"Reviewed by: {call.from_user.first_name}"
        )
        callback_message = "User approved."

    else:
        safe_notify_user(
            target_user_id,
            "❌ Your verification has been rejected by an administrator.\n\n"
            "If you believe this is a mistake, please contact the "
            "verification team.",
        )

        result_text = (
            "❌ REJECTED BY ADMIN\n"
            f"Reviewed by: {call.from_user.first_name}"
        )
        callback_message = "User rejected."

    try:
        original_text = call.message.text or "Verification submission"

        bot.edit_message_text(
            f"{original_text}\n\n{result_text}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        logger.exception("Could not update Admin GC review message.")

    bot.answer_callback_query(call.id, callback_message)


# ============================================================
# FLASK HEALTH CHECK FOR RENDER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "WXC Verification Bot is alive!", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("WXC Verification Bot is starting.")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
            )
        except Exception:
            logger.exception("Telegram polling stopped unexpectedly.")
            time.sleep(5)
