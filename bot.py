import os
import threading
import logging
import telebot
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# 1. LOGGING
# ==========================================

logging.basicConfig(level=logging.INFO)

# ==========================================
# 2. FLASK SERVER FOR RENDER
# ==========================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

server_thread = threading.Thread(target=run_web_server, daemon=True)
server_thread.start()

# ==========================================
# 3. BOT CONFIGURATION
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_GROUP_ID = os.environ.get("ADMIN_GROUP_ID")
VIP_LINK = os.environ.get("VIP_LINK")

if not BOT_TOKEN or not ADMIN_GROUP_ID or not VIP_LINK:
    raise ValueError(
        "Missing BOT_TOKEN, ADMIN_GROUP_ID, or VIP_LINK environment variable."
    )

ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)

bot = telebot.TeleBot(BOT_TOKEN)

# Temporary submission tracking (resets when the bot restarts)
submitted_users = set()

# ==========================================
# 4. WELCOME MESSAGE
# ==========================================

@bot.message_handler(commands=["start"], chat_types=["private"])
def send_welcome(message):
    intro_text = (
        "Welcome to the *WXC Verification Bot*! 🚀\n\n"
        "To get access to our WXC Exclusive TG Channel, "
        "please provide proof that you are registered under our official promo code.\n\n"
        "👉 *Please send your Betting Account ID.*\n\n"
        "⚠️ _You can only submit your details once. "
        "Make sure your information is correct._"
    )

    bot.send_message(
        message.chat.id,
        intro_text,
        parse_mode="Markdown"
    )

# ==========================================
# 5. HANDLE PRIVATE SUBMISSIONS ONLY
# ==========================================

@bot.message_handler(
    content_types=["text", "photo"],
    chat_types=["private"]
)
def handle_submission(message):
    user_id = message.from_user.id
    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "No Username"
    )

    # Prevent duplicate submissions while awaiting review
    if user_id in submitted_users:
        bot.reply_to(
            message,
            "❌ You have already submitted your request. "
            "Please wait for an admin to review it."
        )
        return

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
        # Send applicant details to the Admin group
        bot.send_message(
            ADMIN_GROUP_ID,
            f"📩 New Verification Submission\n\n"
            f"Username: {username}\n"
            f"Telegram User ID: {user_id}"
        )

        # Forward the user's submitted ID or photo
        bot.forward_message(
            ADMIN_GROUP_ID,
            message.chat.id,
            message.message_id
        )

        # Attach approval buttons
        bot.send_message(
            ADMIN_GROUP_ID,
            "Action required:",
            reply_markup=markup
        )

        submitted_users.add(user_id)

        bot.reply_to(
            message,
            "✅ Thank you! Your ID has been sent to our team. "
            "You will be notified here once verified."
        )

    except Exception:
        logging.exception("Submission forwarding failed for user %s", user_id)

        bot.reply_to(
            message,
            "⚠️ We couldn't send your submission to the team. "
            "Please try again later."
        )

# ==========================================
# 6. ADMIN APPROVE / REJECT BUTTONS
# ==========================================

@bot.callback_query_handler(
    func=lambda call: (
        call.data is not None
        and (
            call.data.startswith("approve_")
            or call.data.startswith("reject_")
        )
    )
)
def admin_action(call):
    try:
        action, target_user_id = call.data.split("_", 1)
        target_user_id = int(target_user_id)
    except (ValueError, AttributeError):
        bot.answer_callback_query(call.id, "Invalid action.")
        return

    bot.answer_callback_query(call.id)

    if action == "approve":
        success_msg = (
            "🎉 *Verification Successful!*\n\n"
            "Welcome to the team! Click the link below to join "
            "the VIP Channel:\n\n"
            f"{VIP_LINK}"
        )

        try:
            bot.send_message(
                target_user_id,
                success_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

            bot.edit_message_text(
                f"✅ Approved by {call.from_user.first_name}",
                call.message.chat.id,
                call.message.message_id
            )

        except Exception:
            logging.exception("Approval notification failed for %s", target_user_id)

            bot.send_message(
                ADMIN_GROUP_ID,
                f"⚠️ Could not notify user {target_user_id}. "
                "They may need to start the bot first."
            )

    elif action == "reject":
        fail_msg = (
            "❌ *Verification Failed.*\n\n"
            "Your Betting ID was not found under our promo code tree. "
            "Please check that you typed it correctly or register again.\n\n"
            "🔄 Send your correct Betting Account ID here to try again.\n\n"
            "🔗 *Registration Link:* https://esportslinks.one/1xdotaph/\n"
            "🏷 *PROMOCODE:* `1XDOTAPH`"
        )

        try:
            bot.send_message(
                target_user_id,
                fail_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

            # Allow the user to submit again
            submitted_users.discard(target_user_id)

            bot.edit_message_text(
                f"❌ Rejected by {call.from_user.first_name}",
                call.message.chat.id,
                call.message.message_id
            )

        except Exception:
            logging.exception("Rejection notification failed for %s", target_user_id)

            bot.send_message(
                ADMIN_GROUP_ID,
                f"⚠️ Could not notify user {target_user_id}."
            )

# ==========================================
# 7. START BOT
# ==========================================

if __name__ == "__main__":
    logging.info("WXC Verification Bot is starting...")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
