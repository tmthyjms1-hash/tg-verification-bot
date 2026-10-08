import os
import threading
import telebot
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 1. Setup web server to satisfy Render's port binding
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

server_thread = threading.Thread(target=run_web_server)
server_thread.daemon = True
server_thread.start()

# 2. Initialize Telegram Bot
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_GROUP_ID = int(os.environ.get('ADMIN_GROUP_ID'))
VIP_LINK = os.environ.get('VIP_LINK')

bot = telebot.TeleBot(BOT_TOKEN)
submitted_users = set()

# Welcome Message with perfectly closed quotes and formatting
@bot.message_handler(commands=['start'])
def send_welcome(message):
    intro_text = (
        "Welcome to the **WXC Verification Bot**! 🚀\n\n"
        "To get access to our WXC Exlusive TG Channel, "
        "please provide proof that you are registered under our official promo codes:\n\n"
        "**1XDOTAPH - ALOWXC - JUSTML - FOCUSFIRE**\n\n"
        "👉 **Please reply by typing your Betting Account ID.**\n\n"
        "⚠️ *Note: You can only submit your details ONCE. Make sure your info is correct.*"
    )
    bot.send_message(message.chat.id, intro_text, parse_mode='Markdown')

@bot.message_handler(content_types=['text', 'photo'])
def handle_submission(message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "No Username"

    if user_id in submitted_users:
        bot.reply_to(message, "❌ You have already submitted your request. Please wait for an admin to review it.")
        return

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    )

    bot.send_message(ADMIN_GROUP_ID, f"📩 **New Submission from {username} (ID: {user_id}):**")
    bot.forward_message(ADMIN_GROUP_ID, message.chat.id, message.message_id)
    bot.send_message(ADMIN_GROUP_ID, "Action required:", reply_markup=markup)

    submitted_users.add(user_id)
    bot.reply_to(message, "✅ Thank you! Your ID has been sent to our team. You will be notified here once verified.")

@bot.callback_query_handler(func=lambda call: True)
def admin_action(call):
    bot.answer_callback_query(call.id)
    
    action, target_user_id = call.data.split("_")
    target_user_id = int(target_user_id)

    if action == "approve":
        success_msg = f"🎉 **Verification Successful!**\n\nWelcome to the team. Click the link below to join the VIP Channel instantly:\n{VIP_LINK}"
        try:
            bot.send_message(target_user_id, success_msg, parse_mode='Markdown')
            bot.edit_message_text(f"✅ Approved by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        except Exception as e:
            bot.send_message(ADMIN_GROUP_ID, f"⚠️ Error approving {target_user_id}: {str(e)}")

    elif action == "reject":
        fail_msg = "❌ **Verification Failed.**\n\nYour Betting ID was not found under our promo code tree. Please ensure you typed it correctly or re-register under our link."
        try:
            bot.send_message(target_user_id, fail_msg, parse_mode='Markdown')
            bot.edit_message_text(f"❌ Rejected by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        except Exception as e:
            bot.send_message(ADMIN_GROUP_ID, f"⚠️ Error rejecting {target_user_id}: {str(e)}")

bot.infinity_polling()
