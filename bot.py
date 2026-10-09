import os
import threading
import logging

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)

# -----------------------------
# ENVIRONMENT VARIABLES
# -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = os.environ.get("ADMIN_GROUP_ID")
VIP_LINK = os.environ.get("VIP_LINK")

if not BOT_TOKEN or not ADMIN_GROUP_ID or not VIP_LINK:
    raise ValueError(
        "Missing environment variables: "
        "BOT_TOKEN, ADMIN_GROUP_ID, or VIP_LINK"
    )

ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)

bot = telebot.TeleBot(BOT_TOKEN)

# -----------------------------
# FLASK SERVER FOR RENDER
# -----------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_web_server, daemon=True).start()

# -----------------------------
# TEMPORARY USER TRACKING
# Note: These sets reset when the
# service restarts.
# -----------------------------
submitted_users = set()
verified_users = set()

# -----------------------------
# RE-REGISTRATION DETAILS
# -----------------------------
REREGISTRATION_LINK = "https://esportslinks.one/1xdotaph/"
PROMO_CODE = "1XDOTAPH"

# -----------------------------
# /START COMMAND
# -----------------------------
@bot.message_handler(commands=["start"], chat_types=["private"])
def start(message):
    user_id = message.from_user.id

    if user_id in verified_users:
        bot.send_message(
            message.chat.id,
            "✅ You are already verified!"
        )
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🚀 Start Verification",
            callback_data=f"begin_{user_id}"
        )
    )

    
    
    bot.send_message(
        message.chat.id,
        "To get access to our exclusive channel, please provide your 1XBET account ID \n"
        "to ensure you are registered under our official promo codes: \n"
        "<b><i> 1XDOTAPH - JUSTML - FOCUSFIRE - ALOWXC </i></b> \n\n"
        "Our team will check your ID against our system and send your invite link as soon as you are verified!"
        parse_mode="HTML",
        reply_markup=markup
    )
    

# -----------------------------
# START VERIFICATION BUTTON
# -----------------------------
@bot.callback_query_handler(
    func=lambda call: call.data.startswith("begin_")
)
def begin_verification(call):
    user_id = call.from_user.id

    # Only the user who received the button can use it.
    if user_id != int(call.data.split("_", 1)[1]):
        bot.answer_callback_query(
            call.id,
            "This button belongs to another user.",
            show_alert=True
        )
        return

    if user_id in verified_users:
        bot.answer_callback_query(
            call.id,
            "You are already verified!",
            show_alert=True
        )
        return

    if user_id in submitted_users:
        bot.answer_callback_query(
            call.id,
            "Your submission is already being processed.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        "📝 Please send your Betting Account ID or "
        "the required verification proof here.\n\n"
        "You can send it as text or a photo."
    )

# -----------------------------
# HANDLE USER SUBMISSIONS
# Private chats only.
# -----------------------------
@bot.message_handler(
    content_types=["text", "photo"],
    chat_types=["private"]
)
def handle_submission(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Ignore commands handled elsewhere.
    if message.content_type == "text" and message.text.startswith("/"):
        return

    if user_id in verified_users:
        bot.send_message(
            chat_id,
            "✅ You are already verified. "
            "You don't need to submit again."
        )
        return

    if user_id in submitted_users:
        bot.send_message(
            chat_id,
            "⏳ Your submission is already being processed. "
            "Please wait for the Admin team's decision."
        )
        return

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "No username set"
    )

    full_name = message.from_user.full_name or "Unknown"

    admin_text = (
        "📩 NEW VERIFICATION SUBMISSION\n\n"
        f"👤 Name: {full_name}\n"
        f"🔹 Username: {username}\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"💬 Chat ID: {chat_id}\n\n"
        "Review the user's submission below."
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approve_{user_id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject_{user_id}"
        )
    )

    try:
        # Send applicant details to the Admin GC.
        bot.send_message(
            ADMIN_GROUP_ID,
            admin_text
        )

        # Forward the original submission to the Admin GC.
        bot.forward_message(
            ADMIN_GROUP_ID,
            chat_id,
            message.message_id
        )

        # Send the approval/rejection buttons.
        bot.send_message(
            ADMIN_GROUP_ID,
            f"Admin decision for Telegram ID: {user_id}",
            reply_markup=markup
        )

        # Mark submitted only after the admin messages succeed.
        submitted_users.add(user_id)

        bot.send_message(
            chat_id,
            "✅ Your submission has been sent to the Admin team.\n\n"
            "Please wait while your verification is reviewed."
        )

    except Exception as e:
        logging.exception("Failed to process submission for %s", user_id)

        bot.send_message(
            chat_id,
            "⚠️ We couldn't send your submission to the Admin team. "
            "Please try again later."
        )

# -----------------------------
# APPROVE / REJECT BUTTONS
# -----------------------------
@bot.callback_query_handler(
    func=lambda call: (
        call.data.startswith("approve_")
        or call.data.startswith("reject_")
    )
)
def handle_admin_decision(call):
    try:
        action, target_user_id_text = call.data.split("_", 1)
        target_user_id = int(target_user_id_text)
    except (ValueError, AttributeError):
        bot.answer_callback_query(
            call.id,
            "Invalid action.",
            show_alert=True
        )
        return

    # Answer each callback only once.
    if target_user_id in verified_users:
        bot.answer_callback_query(
            call.id,
            "This user is already verified.",
            show_alert=True
        )
        return

    if target_user_id not in submitted_users:
        bot.answer_callback_query(
            call.id,
            "This submission is no longer pending.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    if action == "approve":
        try:
            bot.send_message(
                target_user_id,
                "🎉 Congratulations! Your verification has been approved.\n\n"
                f"🔗 Here is your VIP link:\n{VIP_LINK}"
            )

            verified_users.add(target_user_id)
            submitted_users.discard(target_user_id)

            bot.edit_message_text(
                f"✅ APPROVED\nTelegram ID: {target_user_id}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup()
            )

        except Exception as e:
            logging.exception(
                "Failed to notify approved user %s",
                target_user_id
            )

            bot.send_message(
                ADMIN_GROUP_ID,
                "⚠️ Could not notify user "
                f"{target_user_id}.\n"
                f"Error: {str(e)}\n\n"
                "They may need to open the bot and press Start."
            )

    elif action == "reject":
        try:
            bot.send_message(
                target_user_id,
                "❌ Unfortunately, your verification was rejected.\n\n"
                "You may register again using the link below.\n\n"
                f"🔗 Re-registration link: {REREGISTRATION_LINK}\n"
                f"🎟 Promo code: {PROMO_CODE}"
            )

            submitted_users.discard(target_user_id)

            bot.edit_message_text(
                f"❌ REJECTED\nTelegram ID: {target_user_id}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup()
            )

        except Exception as e:
            logging.exception(
                "Failed to notify rejected user %s",
                target_user_id
            )

            bot.send_message(
                ADMIN_GROUP_ID,
                "⚠️ Could not notify rejected user "
                f"{target_user_id}.\n"
                f"Error: {str(e)}"
            )

# -----------------------------
# RUN BOT
# -----------------------------
if __name__ == "__main__":
    logging.info("Starting Telegram bot...")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
