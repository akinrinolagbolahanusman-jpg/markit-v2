"""
Mark-it V2 — Telegram reminder bot with Stars subscriptions.

Tiers:
  free  -> 3 active reminders, text only, no snooze
  tier1 -> 10 Stars/month: pictorial reminders, 5-min snooze, saved timezone
  tier2 -> 50 Stars/month: everything in tier1 + unlimited reminders

Extra features beyond a normal phone alarm:
  - Daily / weekly recurring reminders, not just one-offs
  - Inline Done / Snooze / Delete buttons when a reminder fires
  - A "Done" streak counter (consecutive completed reminders)
  - Admin stats: Stars revenue this month / this year, users per tier
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("markit")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
TEST_CODE = os.environ.get("TEST_CODE", "")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://your-service.onrender.com

FREE_LIMIT = 3

# ---- conversation states for /add ----
ASK_MESSAGE, ASK_TIME, ASK_RECURRENCE, ASK_WEEKDAY, ASK_PHOTO = range(5)

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------- helpers ----------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def build_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        ["➕ Add Reminder", "📋 My Reminders"],
        ["🔥 Streak", "🌟 Upgrade"],
    ]
    if is_admin(user_id):
        keyboard.append(["🛠 Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def month_start_ts() -> int:
    now = datetime.now(dt_timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def year_start_ts() -> int:
    now = datetime.now(dt_timezone.utc)
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


# ---------------- basic commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_or_create_user(update.effective_user.id)
    if user["utc_offset"] is None:
        await update.message.reply_text(
            "Welcome to Mark-it! 👋\n\n"
            "Before your first reminder, tell me your UTC offset so reminders fire "
            "at the right time for you.\n\n"
            "e.g. Lagos is +1, London is +0, New York is -5.\n"
            "Just send it like: +1"
        )
        context.user_data["awaiting_offset"] = True
        return
    await send_menu(update)


async def send_menu(update: Update):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "What would you like to do? Use the buttons below, or these commands:\n\n"
        "/add - new reminder\n"
        "/myreminders - view & manage reminders\n"
        "/streak - your Done streak\n"
        "/upgrade - see premium tiers\n"
        "/timezone - change your UTC offset",
        reply_markup=build_keyboard(user_id),
    )


async def handle_offset_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_offset"):
        return
    text = update.message.text.strip()
    try:
        offset = float(text)
        assert -12 <= offset <= 14
    except Exception:
        await update.message.reply_text("Didn't catch that — send it like +1 or -5.")
        return
    db.set_utc_offset(update.effective_user.id, offset)
    context.user_data["awaiting_offset"] = False
    await update.message.reply_text(f"Got it — UTC{'+' if offset >= 0 else ''}{offset} saved.")
    await send_menu(update)


async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_offset"] = True
    await update.message.reply_text("Send your new UTC offset, e.g. +1 or -5.")


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_or_create_user(update.effective_user.id)
    await update.message.reply_text(f"🔥 Current streak: {user['streak']} reminder(s) completed in a row.")


# ---------------- add reminder conversation ----------------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_or_create_user(update.effective_user.id)
    tier = db.get_effective_tier(user)
    if tier != "tier2" and db.count_active_reminders(user["user_id"]) >= FREE_LIMIT:
        await update.message.reply_text(
            f"You've hit the {FREE_LIMIT}-reminder limit for your tier.\n"
            "Upgrade to Tier 2 for unlimited reminders — /upgrade"
        )
        return ConversationHandler.END
    await update.message.reply_text("What should the reminder say?")
    return ASK_MESSAGE


async def add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_reminder"] = {"message": update.message.text.strip()}
    await update.message.reply_text("What time? (24h format, e.g. 14:30)")
    return ASK_TIME


async def add_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        hour, minute = map(int, text.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        await update.message.reply_text("Didn't catch that — send time like 14:30.")
        return ASK_TIME
    context.user_data["new_reminder"]["hour"] = hour
    context.user_data["new_reminder"]["minute"] = minute

    keyboard = [
        [
            InlineKeyboardButton("Once", callback_data="rec_once"),
            InlineKeyboardButton("Daily", callback_data="rec_daily"),
            InlineKeyboardButton("Weekly", callback_data="rec_weekly"),
        ]
    ]
    await update.message.reply_text("How often?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_RECURRENCE


async def add_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split("_")[1]  # once | daily | weekly
    context.user_data["new_reminder"]["recurrence"] = choice

    if choice == "weekly":
        keyboard = [[InlineKeyboardButton(name, callback_data=f"wd_{i}")
                     for i, name in enumerate(WEEKDAY_NAMES[:4])],
                    [InlineKeyboardButton(name, callback_data=f"wd_{i+4}")
                     for i, name in enumerate(WEEKDAY_NAMES[4:])]]
        await query.edit_message_text("Which day?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_WEEKDAY

    return await maybe_ask_photo(update, context)


async def add_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    weekday = int(query.data.split("_")[1])
    context.user_data["new_reminder"]["weekday"] = weekday
    await query.edit_message_text(f"Set for every {WEEKDAY_NAMES[weekday]}.")
    return await maybe_ask_photo(update, context)


async def maybe_ask_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_or_create_user(update.effective_user.id)
    tier = db.get_effective_tier(user)
    chat = update.effective_chat
    if tier in ("tier1", "tier2"):
        await chat.send_message(
            "Want to attach a picture to this reminder? Send one now, or /skip."
        )
        return ASK_PHOTO
    return await finish_add(update, context, chat)


async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    context.user_data["new_reminder"]["photo_file_id"] = photo.file_id
    return await finish_add(update, context, update.effective_chat)


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await finish_add(update, context, update.effective_chat)


async def finish_add(update: Update, context: ContextTypes.DEFAULT_TYPE, chat):
    r = context.user_data.pop("new_reminder")
    db.add_reminder(
        user_id=update.effective_user.id,
        message=r["message"],
        hour=r["hour"],
        minute=r["minute"],
        recurrence=r.get("recurrence", "once"),
        weekday=r.get("weekday"),
        photo_file_id=r.get("photo_file_id"),
    )
    await chat.send_message(f"✅ Reminder set: \"{r['message']}\" at {r['hour']:02d}:{r['minute']:02d}.")
    return ConversationHandler.END


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_reminder", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------- list / delete reminders ----------------

async def myreminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_reminders(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No active reminders. Use /add to make one.")
        return
    for r in rows:
        rec = r["recurrence"]
        when = f"{r['hour']:02d}:{r['minute']:02d}"
        if rec == "weekly":
            when += f" every {WEEKDAY_NAMES[r['weekday']]}"
        elif rec == "daily":
            when += " daily"
        keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data=f"del_{r['id']}")]]
        await update.message.reply_text(
            f"⏰ {when} — {r['message']}", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Deleted")
    reminder_id = int(query.data.split("_")[1])
    db.delete_reminder(reminder_id, update.effective_user.id)
    await query.edit_message_text("🗑 Reminder deleted.")


# ---------------- upgrade / Stars payments ----------------

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Tier 1 — 10 ⭐/month", callback_data="buy_tier1")],
        [InlineKeyboardButton("Tier 2 — 50 ⭐/month", callback_data="buy_tier2")],
    ]
    await update.message.reply_text(
        "🌟 *Mark-it Premium*\n\n"
        "*Tier 1 — 10 Stars/month*\n"
        "• Pictorial reminders\n• 5-min snooze\n• Saved timezone\n\n"
        "*Tier 2 — 50 Stars/month*\n"
        "• Everything in Tier 1\n• Unlimited reminders\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tier = query.data.split("_")[1]  # tier1 | tier2
    price = 10 if tier == "tier1" else 50
    label = "Mark-it Tier 1 (1 month)" if tier == "tier1" else "Mark-it Tier 2 (1 month)"

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=label,
        description="Unlocks premium reminder features for 30 days.",
        payload=f"markit_{tier}_{update.effective_user.id}",
        provider_token="",  # empty string required for Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label, price)],  # Stars have no decimal subunits
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    _, tier, user_id_str = payload.split("_")
    user_id = int(user_id_str)
    stars = update.message.successful_payment.total_amount

    db.set_tier(user_id, tier, days=30)
    db.record_payment(user_id, stars, tier)

    await update.message.reply_text(
        f"🎉 Payment received — {tier.replace('tier', 'Tier ')} unlocked for 30 days. Thank you!"
    )


# ---------------- admin ----------------

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    month_total, month_count = db.stats_since(month_start_ts())
    year_total, year_count = db.stats_since(year_start_ts())
    tiers = db.users_by_tier()
    total = db.total_users()

    msg = (
        "📊 *Mark-it Admin Stats*\n\n"
        f"This month: {month_total} ⭐ ({month_count} payments)\n"
        f"This year: {year_total} ⭐ ({year_count} payments)\n\n"
        f"Total users: {total}\n"
        f"Free: {tiers.get('free', 0)} | Tier 1: {tiers.get('tier1', 0)} | Tier 2: {tiers.get('tier2', 0)}\n\n"
        f"To test premium on your own account: /testcode <code>"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def testcode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # silently ignore for non-admins — command isn't acknowledged at all
    if not TEST_CODE:
        await update.message.reply_text("No TEST_CODE set on the server yet.")
        return
    if not context.args or context.args[0] != TEST_CODE:
        await update.message.reply_text("Invalid code.")
        return

    user = db.get_or_create_user(user_id)
    current_tier = db.get_effective_tier(user)
    if current_tier == "free":
        db.set_tier(user_id, "tier2", days=3650)
        await update.message.reply_text(
            "✅ Test premium ON — Tier 2 unlocked on your account for testing.\n"
            "Run /testcode again with the same code to turn it back off."
        )
    else:
        db.set_free(user_id)
        await update.message.reply_text("✅ Test premium OFF — back to free tier.")


# ---------------- reminder firing ----------------

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(dt_timezone.utc)
    for r in db.all_active_reminders():
        if r["fired_today"]:
            continue
        user = db.get_or_create_user(r["user_id"])
        offset = user["utc_offset"] or 0
        local_now = now_utc + timedelta(hours=offset)

        if r["recurrence"] == "weekly" and local_now.weekday() != r["weekday"]:
            continue
        if local_now.hour == r["hour"] and local_now.minute == r["minute"]:
            await fire_reminder(context, r, user)


async def fire_reminder(context, r, user):
    tier = db.get_effective_tier(user)
    buttons = [InlineKeyboardButton("✅ Done", callback_data=f"done_{r['id']}")]
    if tier in ("tier1", "tier2"):
        buttons.append(InlineKeyboardButton("💤 Snooze 5m", callback_data=f"snooze_{r['id']}"))
    buttons.append(InlineKeyboardButton("🗑 Delete", callback_data=f"del_{r['id']}"))
    markup = InlineKeyboardMarkup([buttons])

    try:
        if r["photo_file_id"] and tier in ("tier1", "tier2"):
            await context.bot.send_photo(
                chat_id=r["user_id"], photo=r["photo_file_id"],
                caption=f"⏰ {r['message']}", reply_markup=markup,
            )
        else:
            await context.bot.send_message(
                chat_id=r["user_id"], text=f"⏰ {r['message']}", reply_markup=markup
            )
    except Exception as e:
        log.warning(f"Failed to fire reminder {r['id']}: {e}")
        return

    db.mark_fired(r["id"])
    if r["recurrence"] == "once":
        db.deactivate_once_reminder(r["id"])


async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Nice work 🔥")
    db.increment_streak(update.effective_user.id)
    await query.edit_message_reply_markup(reply_markup=None)


async def snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Snoozed 5 min")
    reminder_id = int(query.data.split("_")[1])
    r = db.get_reminder(reminder_id)
    await query.edit_message_reply_markup(reply_markup=None)
    context.job_queue.run_once(
        lambda ctx: fire_reminder(ctx, r, db.get_or_create_user(r["user_id"])),
        when=300,
    )


async def midnight_reset(context: ContextTypes.DEFAULT_TYPE):
    db.reset_fired_flags()


# ---------------- entrypoint ----------------

def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex("^➕ Add Reminder$"), add_start),
        ],
        states={
            ASK_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_message)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_time)],
            ASK_RECURRENCE: [CallbackQueryHandler(add_recurrence, pattern="^rec_")],
            ASK_WEEKDAY: [CallbackQueryHandler(add_weekday, pattern="^wd_")],
            ASK_PHOTO: [
                MessageHandler(filters.PHOTO, add_photo),
                CommandHandler("skip", skip_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(CommandHandler("streak", streak_cmd))
    app.add_handler(CommandHandler("myreminders", myreminders))
    app.add_handler(CommandHandler("upgrade", upgrade_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("testcode", testcode_cmd))
    app.add_handler(add_conv)

    # persistent keyboard buttons — must be registered before the catch-all text handler below
    app.add_handler(MessageHandler(filters.Regex("^📋 My Reminders$"), myreminders))
    app.add_handler(MessageHandler(filters.Regex("^🔥 Streak$"), streak_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🌟 Upgrade$"), upgrade_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🛠 Admin Panel$"), stats_cmd))

    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done_"))
    app.add_handler(CallbackQueryHandler(snooze_callback, pattern="^snooze_"))

    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # catch-all text handler for the timezone-offset reply (must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_offset_reply))

    app.job_queue.run_repeating(check_reminders, interval=60, first=5)
    app.job_queue.run_daily(midnight_reset, time=datetime.min.time())

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
