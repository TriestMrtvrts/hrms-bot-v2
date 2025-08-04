import os
import requests
import json
import threading
from flask import Flask, request
from flask_cors import CORS
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ========== Настройки ==========

BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
# =================================

app = Flask(__name__)
CORS(app)  # чтобы разрешить fetch из браузера

# В памяти
subscribers  = set()
leads        = {}   # lead_id → {name,phone,taken_by,taker_name,result}
msg_map      = {}   # lead_id → {chat_id: message_id}
lead_counter = 0

ASK_RESULT = range(1)


# —— Telegram handlers ——
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Чтобы получать заявки, отправь:\n"
        "/auth <ключ-пароль>"
    )

async def auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args    = ctx.args
    if len(args) != 1 or args[0] != ADMIN_PASSWORD:
        return await update.message.reply_text("❌ Неверный пароль.")
    if chat_id in subscribers:
        return await update.message.reply_text("✅ Вы уже авторизованы.")
    subscribers.add(chat_id)
    await update.message.reply_text("✅ Успешно авторизованы! Заявки буду приходить сюда.")

async def take_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    lead_id = int(query.data.split("|")[1])
    lead    = leads[lead_id]

    if lead["taken_by"] is not None:
        return await query.answer("🚫 Уже взято.", show_alert=True)

    lead["taken_by"]   = query.from_user.id
    lead["taker_name"] = query.from_user.full_name

    for cid, mid in msg_map[lead_id].items():
        await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)

    ctx.user_data["current_lead"] = lead_id
    await ctx.bot.send_message(
        chat_id=query.from_user.id,
        text="✏️ Вы взяли заявку. Напишите, пожалуйста, результат обзвона:"
    )
    return ASK_RESULT

async def save_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lead_id = ctx.user_data["current_lead"]
    text    = update.message.text
    lead    = leads[lead_id]
    lead["result"] = text

    for cid, mid in msg_map[lead_id].items():
        new_text = (
            f"📞 Заявка #{lead_id}\n"
            f"Имя: {lead['name']}\n"
            f"Телефон: {lead['phone']}\n"
            f"Взял: {lead['taker_name']}\n"
            f"Результат: {lead['result']}"
        )
        await ctx.bot.edit_message_text(chat_id=cid, message_id=mid, text=new_text)

    await update.message.reply_text("✅ Результат сохранён.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# —— Flask endpoint ——
@app.route("/submit", methods=["POST"])
def submit():
    global lead_counter
    data = request.get_json()
    name  = data.get("name")
    phone = data.get("phone")

    # Новый ID
    lead_id = lead_counter
    lead_counter += 1

    # Сохраняем в памяти
    leads[lead_id] = {
        "name": name, "phone": phone,
        "taken_by": None, "taker_name": None, "result": None
    }
    msg_map[lead_id] = {}

    # Шлём всем подписчикам через HTTP-API
    for chat_id in subscribers:
        text = f"📞 Заявка #{lead_id}\nИмя: {name}\nТелефон: {phone}"
        reply_markup = {
            "inline_keyboard": [
                [ { "text": "✅ Взять", "callback_data": f"take|{lead_id}" } ]
            ]
        }
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_markup": json.dumps(reply_markup)
            }
        )
        result = resp.json().get("result", {})
        msg_id = result.get("message_id")
        if msg_id:
            msg_map[lead_id][chat_id] = msg_id

    return {"status": "ok"}


def main():
    # 1) Создаём Telegram Application (и берём у него bot)
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.bot    = application.bot

    # 2) Запускаем Flask в фоне
    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 8000))
        ),
        daemon=True
    ).start()

    # 3) Регистрируем хендлеры в application
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("auth", auth))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(take_callback, pattern=r"^take\|\d+$")],
        states={ ASK_RESULT: [ MessageHandler(filters.TEXT & ~filters.COMMAND, save_result) ] },
        fallbacks=[ CommandHandler("cancel", cancel) ]
    )
    application.add_handler(conv)

    # 4) Стартуем polling
    application.run_polling()

if __name__ == "__main__":
    main()
