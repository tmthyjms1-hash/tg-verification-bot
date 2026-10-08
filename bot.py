import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Load secure keys from hosting environment
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_GROUP_ID = int(os.environ.get('ADMIN_GROUP_ID'))
VIP_LINK = os.environ.get('VIP_LINK')

bot = telebot.TeleBot(BOT_TOKEN)

# Simple temporary dictionary to prevent double submissions while bot runs
submitted_users = set()

# 1. Welcome Intro Message
@bot.message_handler(commands=['start'])
def send_welcome(message):
    intro_text = (
        "Welcome to the **VIP Verification Bot**! 🚀\n\n"
        "To get instant, free access to our Premium VIP Betting Channel, "
        "please provide proof that you are registered under our official promo code.\n\n"
        "👉 **Please reply by typing your Betting Account ID or sending a screenshot of your profile.**\n\n"
        "⚠️ *Note: You can only submit your details ONCE. Make sure your info is correct.*"
    )
    bot.send_message(message.chat.id, intro_text, parse_mode='Markdown')

# 2. Capture Text or Screenshot & Forward to Admins
@bot.message_handler(content_types=['text', 'photo'])
def handle_submission(message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "No Username"

    # Check if user already submitted
    if user_id in submitted_users:
        bot.reply_to(message, "❌ You have already submitted your request. Please wait for an admin to review it.")
        return

    # Create Approve/Reject buttons for the admin chat
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    )

    # Inform the Admin Group
    bot.send_message(ADMIN_GROUP_ID, f"📩 **New Submission from {username} (ID: {user_id}):**")
    
    # Forward the content directly so admins see the text or image
    bot.forward_message(ADMIN_GROUP_ID, message.chat.id, message.message_id)
    
    # Send the action buttons right below the forwarded message
    bot.send_message(ADMIN_GROUP_ID, "Action required:", reply_markup=markup)

    # Lock the user out from sending more messages
    submitted_users.add(user_id)
    bot.reply_to(message, "✅ Thank you! Your ID has been sent to our team. You will be notified here once verified.")

# 3. Handle Admin Button Clicks (Approve / Reject)
@bot.callback_query_handler(func=lambda call: True)
def admin_action(call):
    action, target_user_id = call.data.split("_")
    target_user_id = int(target_user_id)

    if action == "approve":
        success_msg = f"🎉 **Verification Successful!**\n\nWelcome to the team. Click the link below to join the VIP Channel instantly:\n{VIP_LINK}"
        try:
            bot.send_message(target_user_id, success_msg, parse_mode='Markdown')
            bot.edit_message_text(f"✅ Approved by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        except Exception:
            bot.edit_message_text("⚠️ User blocked the bot, couldn't send link.", call.message.chat.id, call.message.message_id)

    elif action == "reject":
        fail_msg = "❌ **Verification Failed.**\n\nYour Betting ID was not found under our promo code tree. Please ensure you typed it correctly or re-register under our link."
        try:
            bot.send_message(target_user_id, fail_msg, parse_mode='Markdown')
            bot.edit_message_text(f"❌ Rejected by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        except Exception:
            bot.edit_message_text("⚠️ User blocked the bot.", call.message.chat.id, call.message.message_id)

# Keep bot running
bot.infinity_polling()
