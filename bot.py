import os
import re
import json
import time
import html
import sqlite3
import logging
import threading
import unicodedata
from decimal import Decimal, InvalidOperation

import telebot
import gspread

from flask import Flask, jsonify
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID_RAW = os.environ.get("ADMIN_GROUP_ID")
VIP_LINK = os.environ.get("VIP_LINK", "")
REREGISTRATION_LINK = os.environ.get("REREGISTRATION_LINK", "")
PROMO_CODE = os.environ.get("PROMO_CODE", "")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_WORKSHEET_NAME = os.environ.get(
    "GOOGLE_WORKSHEET_NAME", "Registrations"
)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)

DB_PATH = os.environ.get("DB_PATH", "/tmp/wxc_verification.db")
PORT = int(os.environ.get("PORT", "10000"))

SHEET_CACHE_TTL = 60

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN environment variable.")

if not ADMIN_GROUP_ID_RAW:
    raise ValueError("Missing ADMIN_GROUP_ID environment variable.")

if not VIP_LINK:
    raise ValueError("Missing VIP_LINK environment variable.")

if not GOOGLE_SHEET_ID:
    raise ValueError("Missing GOOGLE_SHEET_ID environment variable.")

if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise ValueError(
        "Missing GOOGLE_SERVICE_ACCOUNT_JSON environment variable."
    )

try:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_RAW)
except ValueError as exc:
    raise ValueError("ADMIN_GROUP_ID must be an integer.") from exc


# ============================================================
# LOGGING AND INITIALIZATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# In-memory state: resets whenever the process restarts.
awaiting_account_id = set()


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_db_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                telegram_user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                account_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                admin_message_id INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                attempt_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(verifications)"
            ).fetchall()
        }

        if "attempt_count" not in columns:
            connection.execute("""
                ALTER TABLE verifications
                ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0
            """)

    logging.info("Database initialized at %s", DB_PATH)


def get_verification(user_id):
    with get_db_connection() as connection:
        row = connection.execute("""
            SELECT *
            FROM verifications
            WHERE telegram_user_id = ?
        """, (user_id,)).fetchone()

    return dict(row) if row else None


def save_verification(
    user_id,
    username,
    first_name,
    account_id,
    status,
    admin_message_id=None,
    attempt_count=0
):
    with get_db_connection() as connection:
        connection.execute("""
            INSERT INTO verifications (
                telegram_user_id,
                username,
                first_name,
                account_id,
                status,
                admin_message_id,
                updated_at,
                attempt_count
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                account_id = excluded.account_id,
                status = excluded.status,
                admin_message_id = excluded.admin_message_id,
                updated_at = CURRENT_TIMESTAMP,
                attempt_count = excluded.attempt_count
        """, (
            user_id,
            username,
            first_name,
            account_id,
            status,
            admin_message_id,
            attempt_count
        ))


def set_verification_status(
    user_id,
    status,
    admin_message_id=None,
    attempt_count=None
):
    with get_db_connection() as connection:
        if attempt_count is None:
            connection.execute("""
                UPDATE verifications
                SET status = ?,
                    admin_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ?
            """, (status, admin_message_id, user_id))
        else:
            connection.execute("""
                UPDATE verifications
                SET status = ?,
                    admin_message_id = ?,
                    attempt_count = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ?
            """, (
                status,
                admin_message_id,
                attempt_count,
                user_id
            ))


# ============================================================
# GOOGLE SHEETS AND CACHING
# ============================================================

_sheet_lock = threading.Lock()

_sheet_cache = {
    "account_ids": None,
    "loaded_at": 0
}


def connect_to_google_sheet():
    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]
    )

    client = gspread.authorize(credentials)

    spreadsheet_id = GOOGLE_SHEET_ID.strip()

    # Accept either the spreadsheet ID or its full URL.
    match = re.search(
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        spreadsheet_id
    )

    if match:
        spreadsheet_id = match.group(1)

    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        available_tabs = [
            sheet.title for sheet in spreadsheet.worksheets()
        ]
        logging.error(
            "Worksheet '%s' not found. Available tabs: %s",
            GOOGLE_WORKSHEET_NAME,
            available_tabs
        )
        raise

    return worksheet


def normalize_account_id(value):
    """Normalize account IDs consistently from Google Sheets and Telegram.

    Handles surrounding/hidden Unicode whitespace, non-breaking spaces,
    numeric cells returned as floats (for example, 123456.0), thousands
    separators in numeric-formatted cells, and scientific-notation strings.
    It deliberately preserves letters, underscores, and hyphens.
    """
    if value is None:
        return ""

    # Convert numeric values without retaining an unnecessary trailing .0.
    if isinstance(value, bool):
        text = str(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if value.is_integer():
            text = str(int(value))
        else:
            text = format(value, ".15g")
    else:
        text = str(value)

    text = unicodedata.normalize("NFKC", text)
    # Remove invisible characters that can be introduced by copying/pasting.
    for invisible in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(invisible, "")
    text = text.replace("\u00a0", " ").strip()

    # Numeric IDs sometimes arrive formatted as 123,456 or 123456.0.
    numeric_text = text.replace(",", "")
    if re.fullmatch(r"[+-]?\d+(?:\.0+)?", numeric_text):
        if "." in numeric_text:
            numeric_text = numeric_text.split(".", 1)[0]
        text = numeric_text
    elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+", text):
        # Convert a scientific-notation string to ordinary decimal notation.
        try:
            decimal_value = Decimal(text)
            if decimal_value.is_finite() and decimal_value == decimal_value.to_integral_value():
                text = format(decimal_value.quantize(Decimal("1")), "f")
        except (InvalidOperation, ValueError):
            pass

    return text.strip().casefold()


def load_account_ids_from_sheet():
    worksheet = connect_to_google_sheet()

    # Account IDs are in column E, starting from row 11.
    # FORMATTED_VALUE preserves the displayed ID; normalize_account_id then
    # handles common number-format differences before comparing IDs.
    values = worksheet.get("E11:E", value_render_option="FORMATTED_VALUE")

    account_ids = set()
    nonempty_rows = 0

    for row in values:
        if row and row[0] is not None:
            normalized_id = normalize_account_id(row[0])
            if normalized_id:
                account_ids.add(normalized_id)
                nonempty_rows += 1

    logging.info(
        "Loaded %d unique account IDs from Google Sheets worksheet '%s' (range E11:E; %d non-empty rows).",
        len(account_ids),
        worksheet.title,
        nonempty_rows
    )

    return account_ids


def get_account_ids():
    now = time.monotonic()
    cached_ids = _sheet_cache["account_ids"]
    loaded_at = _sheet_cache["loaded_at"]

    if (
        cached_ids is not None
        and now - loaded_at < SHEET_CACHE_TTL
    ):
        return cached_ids

    with _sheet_lock:
        now = time.monotonic()
        cached_ids = _sheet_cache["account_ids"]
        loaded_at = _sheet_cache["loaded_at"]

        if (
            cached_ids is not None
            and now - loaded_at < SHEET_CACHE_TTL
        ):
            return cached_ids

        # Only replace the cache after a successful refresh.
        fresh_ids = load_account_ids_from_sheet()

        _sheet_cache["account_ids"] = fresh_ids
        _sheet_cache["loaded_at"] = time.monotonic()

        return fresh_ids


def lookup_account_in_sheet(account_id):
    try:
        account_ids = get_account_ids()
        normalized_id = normalize_account_id(account_id)

        if normalized_id and normalized_id in account_ids:
            logging.info(
                "Account ID lookup matched (submitted length=%d; loaded IDs=%d).",
                len(normalized_id),
                len(account_ids)
            )
            return "found", None

        # Avoid printing the full account ID into logs.
        logging.warning(
            "Account ID lookup did not match (submitted length=%d; loaded IDs=%d; normalized length=%d).",
            len(str(account_id).strip()),
            len(account_ids),
            len(normalized_id)
        )
        return "not_found", None

    except Exception as exc:
        logging.exception("Google Sheets lookup failed.")
        return "error", str(exc)


# ============================================================
# USER MESSAGES
# ============================================================

WELCOME_MESSAGE = (
    "Welcome to the <b>WXC Verification Bot</b>! 🚀\n\n"
    "To get instant, free access to our "
    "<b>Exclusive WXC Channel</b>, "
    "please provide proof that you are registered under "
    "our official promo code.\n\n"
    "<b>👇 Press Start Verification to begin. 👇</b>"
)

ACCOUNT_ID_INSTRUCTIONS = (
    "📝 Please type your 1X Account ID.\n\n"
    "How to find your 1X Account ID?\n"
    "1. Log in to your account.\n"
    "2. Tap your <b>Profile/Account Icon</b>.\n"
    "3. Look for your <b>Account Number</b> in your profile details.\n"
    "4. Copy your ID and send it here for verification.\n\n"
    "⚠️ <b>PLEASE SEND YOUR ACCOUNT ID ONLY.</b> "
    "Never share your <b>Password or OTP</b>."
)

PENDING_MESSAGE = (
    "⏳ Your submission is already being processed.\n\n"
    "Please wait for the Admin team's decision."
)


def approved_welcome_message():
    return (
        "🎉 <b>Welcome to the Exclusive WXC Channel!</b> 🏆\n\n"
        "Congratulations! Your verification is complete, "
        "and you're now ready to join the WXC community! 🚀\n\n"
        "Get ready to enjoy exclusive perks, including:\n\n"
        "🎁 <b>Exclusive Raffles &amp; Prizes</b> — "
        "Get a chance to win exciting rewards!\n"
        "🎯 <b>Free Bets &amp; Promotions</b> — "
        "Watch out for special offers and giveaways!\n"
        "📊 <b>Betting Tips &amp; Insights</b> — "
        "Stay updated with tips and match analysis.\n"
        "🔥 <b>Exclusive Updates</b> — "
        "Don't miss upcoming events and community activities!\n\n"
        "👇 <b>Click the link below to join your "
        "Exclusive WXC Channel:</b>\n\n"
        f"🔗 {html.escape(VIP_LINK)}\n\n"
        "💚 Welcome aboard! 🚀"
    )


def registration_instructions():
    return (
        "❌ Unfortunately, your verification has failed.\n\n"
        "You may register using the link below and make sure "
        "to use our promo code.\n\n"
        f"🔗 Registration link: {html.escape(REREGISTRATION_LINK)}\n"
        f"🎟 Promo code: {html.escape(PROMO_CODE)}\n\n"
        "Once you have successfully registered using our "
        "promo code, please type in your account ID again."
    )


def rejection_message():
    return (
        "❌ Unfortunately, your verification was rejected.\n\n"
        "You may register again using the link below.\n\n"
        f"🔗 Re-registration link: {html.escape(REREGISTRATION_LINK)}\n"
        f"🎟 Promo code: {html.escape(PROMO_CODE)}\n\n"
        "Once you have successfully registered using our "
        "promo code, please type in your account ID again."
    )


# ============================================================
# KEYBOARDS
# ============================================================

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
    keyboard = InlineKeyboardMarkup()

    keyboard.row(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approve_{user_id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject_{user_id}"
        )
    )

    return keyboard


# ============================================================
# ADMIN NOTIFICATIONS
# ============================================================

def notify_admin_success(user, account_id):
    username = (
        f"@{html.escape(user.username)}"
        if user.username
        else "Not set"
    )

    message = (
        "✅ <b>ACCOUNT ID SUCCESSFULLY VERIFIED</b>\n\n"
        f"👤 Name: {html.escape(user.first_name or 'Unknown')}\n"
        f"📱 Username: {username}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🎮 Account ID: <code>{html.escape(account_id)}</code>\n"
        "📋 Status: Automatically Approved"
    )

    try:
        bot.send_message(
            ADMIN_GROUP_ID,
            message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception:
        logging.exception(
            "Verification succeeded, but the success notification "
            "could not be sent to the Admin GC."
        )


def send_manual_review_request(user, account_id, reason):
    username = (
        f"@{html.escape(user.username)}"
        if user.username
        else "Not set"
    )

    message = (
        "🔎 <b>VERIFICATION REQUIRES MANUAL REVIEW</b>\n\n"
        f"👤 Name: {html.escape(user.first_name or 'Unknown')}\n"
        f"📱 Username: {username}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🎮 Account ID: <code>{html.escape(account_id)}</code>\n"
        f"📝 Reason: {html.escape(reason)}\n\n"
        "Please review this member's verification."
    )

    return bot.send_message(
        ADMIN_GROUP_ID,
        message,
        parse_mode="HTML",
        reply_markup=admin_review_keyboard(user.id)
    )


def ask_for_account_id(user_id):
    bot.send_message(
        user_id,
        ACCOUNT_ID_INSTRUCTIONS,
        parse_mode="HTML"
    )


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.from_user.id
    verification = get_verification(user_id)

    if verification:
        status = verification["status"]

        if status == "approved":
            # Already verified: don't allow another submission.
            awaiting_account_id.discard(user_id)
            bot.send_message(
                user_id,
                approved_welcome_message(),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return

        if status == "pending":
            awaiting_account_id.discard(user_id)
            bot.send_message(user_id, PENDING_MESSAGE)
            return

        if status == "failed":
            awaiting_account_id.add(user_id)
            bot.send_message(
                user_id,
                registration_instructions(),
                parse_mode="HTML"
            )
            return

        if status == "rejected":
            awaiting_account_id.add(user_id)
            bot.send_message(
                user_id,
                rejection_message(),
                parse_mode="HTML"
            )
            return

    bot.send_message(
        user_id,
        WELCOME_MESSAGE,
        parse_mode="HTML",
        reply_markup=start_keyboard(user_id)
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
        bot.answer_callback_query(
            call.id,
            "Invalid request.",
            show_alert=True
        )
        return

    if call.from_user.id != target_user_id:
        bot.answer_callback_query(
            call.id,
            "This button belongs to another user.",
            show_alert=True
        )
        return

    verification = get_verification(target_user_id)

    if verification:
        status = verification["status"]

        if status == "approved":
            bot.answer_callback_query(
                call.id,
                "You are already verified."
            )
            bot.send_message(
                target_user_id,
                approved_welcome_message(),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return

        if status == "pending":
            bot.answer_callback_query(
                call.id,
                "Your submission is already being processed."
            )
            bot.send_message(target_user_id, PENDING_MESSAGE)
            return

    awaiting_account_id.add(target_user_id)

    bot.answer_callback_query(call.id, "Verification started.")
    ask_for_account_id(target_user_id)


# ============================================================
# RECEIVE ACCOUNT ID AND HANDLE REPEAT MESSAGES
# ============================================================

@bot.message_handler(
    content_types=["text"],
    func=lambda message: (
        message.chat.type == "private"
        and message.from_user is not None
        and not message.text.startswith("/")
    )
)
def receive_account_id(message):
    user = message.from_user
    user_id = user.id

    existing = get_verification(user_id)

    # Approved users cannot submit again. Telegram does not let
    # bots prevent a user from sending a message, so the bot ignores it.
    if existing and existing["status"] == "approved":
        awaiting_account_id.discard(user_id)
        return

    # Always tell pending users their submission is being processed.
    if existing and existing["status"] == "pending":
        awaiting_account_id.discard(user_id)
        bot.send_message(user_id, PENDING_MESSAGE)
        return

    # Do not process unsolicited messages before verification starts.
    if user_id not in awaiting_account_id:
        bot.send_message(
            user_id,
            "Please press <b>Start Verification</b> to begin.",
            parse_mode="HTML",
            reply_markup=start_keyboard(user_id)
        )
        return

    account_id = message.text.strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", account_id):
        bot.send_message(
            user_id,
            "⚠️ That account ID format doesn't look valid.\n\n"
            "Please send only your account ID using letters, "
            "numbers, underscores, or hyphens (3–64 characters)."
        )
        return

    previous_attempts = (
        existing.get("attempt_count", 0)
        if existing
        else 0
    )
    attempt_count = previous_attempts + 1

    result, error_message = lookup_account_in_sheet(account_id)

    # --------------------------------------------------------
    # SUCCESSFUL GOOGLE SHEETS MATCH
    # --------------------------------------------------------

    if result == "found":
        save_verification(
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            account_id=account_id,
            status="approved",
            admin_message_id=None,
            attempt_count=attempt_count
        )

        awaiting_account_id.discard(user_id)

        # Send the full welcome message and VIP link.
        try:
            bot.send_message(
                user_id,
                approved_welcome_message(),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            logging.exception(
                "Account ID verified, but the welcome message "
                "could not be delivered to user %s.",
                user_id
            )

        # Notify the Admin GC after a successful automatic match.
        notify_admin_success(user, account_id)

        logging.info(
            "Automatically verified user %s with account ID %s.",
            user_id,
            account_id
        )
        return

    # --------------------------------------------------------
    # FIRST FAILED LOOKUP
    # --------------------------------------------------------

    if result == "not_found" and attempt_count == 1:
        save_verification(
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            account_id=account_id,
            status="failed",
            admin_message_id=None,
            attempt_count=attempt_count
        )

        # Keep the user eligible to submit again.
        awaiting_account_id.add(user_id)

        bot.send_message(
            user_id,
            registration_instructions(),
            parse_mode="HTML"
        )
        return

    # --------------------------------------------------------
    # MANUAL REVIEW: SECOND FAILURE OR SHEETS ERROR
    # --------------------------------------------------------

    if result == "error":
        reason = (
            "Google Sheets lookup error; manual verification "
            "is required."
        )

        logging.error(
            "Google Sheets error for user %s: %s",
            user_id,
            error_message
        )
    else:
        reason = (
            "Account ID was not found after the registration retry."
        )

    try:
        admin_message = send_manual_review_request(
            user,
            account_id,
            reason
        )
    except Exception:
        logging.exception(
            "Could not send manual review request to Admin GC."
        )

        save_verification(
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            account_id=account_id,
            status="failed",
            admin_message_id=None,
            attempt_count=attempt_count
        )

        awaiting_account_id.add(user_id)

        bot.send_message(
            user_id,
            "⚠️ We couldn't forward your submission for review "
            "right now. Please try again in a little while."
        )
        return

    save_verification(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        account_id=account_id,
        status="pending",
        admin_message_id=admin_message.message_id,
        attempt_count=attempt_count
    )

    awaiting_account_id.discard(user_id)

    bot.send_message(
        user_id,
        PENDING_MESSAGE
    )


# ============================================================
# ADMIN APPROVE / REJECT
# ============================================================

def is_admin(user_id):
    try:
        member = bot.get_chat_member(
            ADMIN_GROUP_ID,
            user_id
        )
        return member.status in ("administrator", "creator")

    except Exception:
        logging.exception("Could not check admin permissions.")
        return False


@bot.callback_query_handler(
    func=lambda call: (
        call.data.startswith("approve_")
        or call.data.startswith("reject_")
    )
)
def review_verification(call):
    if (
        call.message is None
        or call.message.chat.id != ADMIN_GROUP_ID
    ):
        bot.answer_callback_query(
            call.id,
            "This action is only available in the Admin GC.",
            show_alert=True
        )
        return

    if not is_admin(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "You are not authorized to review verifications.",
            show_alert=True
        )
        return

    try:
        action, user_id_text = call.data.split("_", 1)
        target_user_id = int(user_id_text)
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id,
            "Invalid review request.",
            show_alert=True
        )
        return

    verification = get_verification(target_user_id)

    if not verification:
        bot.answer_callback_query(
            call.id,
            "Verification record not found.",
            show_alert=True
        )
        return

    if verification["status"] != "pending":
        bot.answer_callback_query(
            call.id,
            "This verification has already been processed.",
            show_alert=True
        )
        return

    account_id = verification["account_id"]
    reviewer = html.escape(
        call.from_user.first_name or "Admin"
    )

    if action == "approve":
        set_verification_status(
            target_user_id,
            "approved",
            admin_message_id=None
        )

        awaiting_account_id.discard(target_user_id)

        try:
            bot.send_message(
                target_user_id,
                approved_welcome_message(),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            logging.exception(
                "Could not send approval message to user %s.",
                target_user_id
            )

        updated_message = (
            "✅ <b>VERIFICATION APPROVED</b>\n\n"
            f"🆔 Telegram ID: <code>{target_user_id}</code>\n"
            f"🎮 Account ID: <code>{html.escape(account_id)}</code>\n"
            f"👮 Reviewed by: {reviewer}"
        )

        try:
            bot.edit_message_text(
                updated_message,
                chat_id=ADMIN_GROUP_ID,
                message_id=call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            logging.exception(
                "Could not update the Admin GC review message."
            )

        bot.answer_callback_query(
            call.id,
            "Verification approved."
        )

        logging.info(
            "Admin %s approved user %s.",
            call.from_user.id,
            target_user_id
        )
        return

    if action == "reject":
        set_verification_status(
            target_user_id,
            "rejected",
            admin_message_id=None,
            attempt_count=0
        )

        # Allow the member to submit a new account ID.
        awaiting_account_id.add(target_user_id)

        try:
            bot.send_message(
                target_user_id,
                rejection_message(),
                parse_mode="HTML"
            )
        except Exception:
            logging.exception(
                "Could not send rejection message to user %s.",
                target_user_id
            )

        updated_message = (
            "❌ <b>VERIFICATION REJECTED</b>\n\n"
            f"🆔 Telegram ID: <code>{target_user_id}</code>\n"
            f"🎮 Account ID: <code>{html.escape(account_id)}</code>\n"
            f"👮 Reviewed by: {reviewer}"
        )

        try:
            bot.edit_message_text(
                updated_message,
                chat_id=ADMIN_GROUP_ID,
                message_id=call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            logging.exception(
                "Could not update the Admin GC review message."
            )

        bot.answer_callback_query(
            call.id,
            "Verification rejected."
        )

        logging.info(
            "Admin %s rejected user %s.",
            call.from_user.id,
            target_user_id
        )


# ============================================================
# FLASK HEALTH CHECKS
# ============================================================

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "WXC Verification Bot is alive!", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "WXC Verification Bot"
    }), 200


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


# ============================================================
# STARTUP AND POLLING
# ============================================================

if __name__ == "__main__":
    initialize_database()

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )
    flask_thread.start()

    logging.info("Starting Telegram bot polling.")
    logging.info(
        "Account ID matching uses normalized text from column E starting at row 11. "
        "For long numeric IDs, ensure Google Sheets has not rounded the underlying value."
    )

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )

        except Exception:
            logging.exception(
                "Telegram polling stopped unexpectedly. "
                "Retrying in 5 seconds."
            )
            time.sleep(5)
