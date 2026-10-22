import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "bookings.sqlite3").strip() or "bookings.sqlite3"
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Tehran").strip() or "Asia/Tehran"
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "15"))
SESSION_MINUTES = 90
WINDOW_SECONDS = 48 * 60 * 60

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set BOT_TOKEN.")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing. Put your numeric Telegram user ID in .env.")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from exc

TZ = ZoneInfo(TIMEZONE_NAME)
BOT_USERNAME = ""
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("futsal-booking")

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
STATUS_FA = {
    "open": "خالی",
    "held": "در انتظار پرداخت",
    "pending": "رسید در انتظار تأیید",
    "booked": "رزرو قطعی",
}


def normalize_digits(value: str) -> str:
    return value.translate(DIGIT_MAP).replace("٬", "").replace(",", "").strip()


def fa(value) -> str:
    return str(value).translate(FA_DIGITS)


def money(amount: int) -> str:
    return fa(f"{amount:,}") + " تومان"


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    with closing(db_connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts INTEGER NOT NULL UNIQUE,
                end_ts INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                held_by INTEGER,
                hold_until INTEGER,
                customer_id INTEGER,
                customer_username TEXT,
                customer_name TEXT,
                payment_type TEXT,
                amount INTEGER,
                total_price INTEGER,
                receipt_file_id TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        slot_columns = {row["name"] for row in con.execute("PRAGMA table_info(slots)").fetchall()}
        if "total_price" not in slot_columns:
            con.execute("ALTER TABLE slots ADD COLUMN total_price INTEGER")
        con.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('price', '500000')")
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('deposit', '0')")
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('payment_instructions', '')")
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('channel_message_id', '')")


def get_setting(key: str, default: str = "") -> str:
    with closing(db_connect()) as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(db_connect()) as con:
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def now_ts() -> int:
    return int(time.time())


def now_local() -> datetime:
    return datetime.now(TZ)


def jalali_datetime_to_ts(date_text: str, time_text: str) -> int:
    date_text = normalize_digits(date_text)
    time_text = normalize_digits(time_text)
    year, month, day = [int(part) for part in date_text.split("-")]
    hour, minute = [int(part) for part in time_text.split(":")]
    gregorian_naive = jdatetime.datetime(year, month, day, hour, minute).togregorian()
    return int(gregorian_naive.replace(tzinfo=TZ).timestamp())


def jalali_date_string(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ)
    jd = jdatetime.date.fromgregorian(date=dt.date())
    return fa(f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d}")


def day_label(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ)
    return f"{WEEKDAYS_FA[dt.weekday()]} {jalali_date_string(ts)}"


def time_label(ts: int) -> str:
    return fa(datetime.fromtimestamp(ts, TZ).strftime("%H:%M"))


def slot_label(row: sqlite3.Row, include_day: bool = True) -> str:
    start = int(row["start_ts"])
    end = int(row["end_ts"])
    prefix = day_label(start) + "، " if include_day else ""
    return f"{prefix}{time_label(start)} تا {time_label(end)}"


def available_slots(limit_seconds: int = WINDOW_SECONDS):
    now = now_ts()
    with closing(db_connect()) as con:
        return con.execute(
            "SELECT * FROM slots WHERE status='open' AND start_ts>? AND start_ts<=? "
            "ORDER BY start_ts LIMIT 100",
            (now, now + limit_seconds),
        ).fetchall()


def all_slots_in_window():
    now = now_ts()
    with closing(db_connect()) as con:
        return con.execute(
            "SELECT * FROM slots WHERE start_ts>? AND start_ts<=? ORDER BY start_ts LIMIT 100",
            (now, now + WINDOW_SECONDS),
        ).fetchall()


def expire_holds() -> int:
    now = now_ts()
    with closing(db_connect()) as con:
        cur = con.execute(
            """UPDATE slots SET status='open', held_by=NULL, hold_until=NULL,
               customer_id=NULL, customer_username=NULL, customer_name=NULL,
               payment_type=NULL, amount=NULL, receipt_file_id=NULL
               WHERE status='held' AND hold_until IS NOT NULL AND hold_until < ?""",
            (now,),
        )
        return cur.rowcount


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def user_label(user) -> str:
    name = (user.full_name or "کاربر تلگرام").replace("\n", " ").strip()
    username = f"@{user.username}" if user.username else "ندارد"
    return f"{name} ({username})"


def clear_slot_assignment_sql():
    return """held_by=NULL, hold_until=NULL, customer_id=NULL,
              customer_username=NULL, customer_name=NULL, payment_type=NULL,
              amount=NULL, total_price=NULL, receipt_file_id=NULL"""


async def safe_refresh_channel(bot) -> None:
    if not CHANNEL_ID:
        return
    try:
        await refresh_channel(bot)
    except Exception:
        logger.exception("Could not refresh channel availability post. Check CHANNEL_ID and bot admin rights.")


async def refresh_channel(bot) -> None:
    global BOT_USERNAME
    expire_holds()
    if not BOT_USERNAME:
        BOT_USERNAME = (await bot.get_me()).username or ""
    if not BOT_USERNAME:
        logger.warning("Bot username unavailable; channel post not updated.")
        return

    slots = available_slots()
    price = int(get_setting("price", "500000") or "500000")
    deposit = int(get_setting("deposit", "0") or "0")
    lines = ["🥅 وقت‌های خالی سالن فوتسال", "", f"تعرفه هر سانس ۹۰ دقیقه‌ای: {money(price)}"]
    if 0 < deposit < price:
        lines.append(f"امکان رزرو با بیعانه: {money(deposit)}")
    lines.extend(["", "برای رزرو، روی دکمه وقت دلخواه بزنید و در گفت‌وگوی خصوصی ربات ادامه دهید.",
                  f"زمان نگهداری موقت وقت: {fa(HOLD_MINUTES)} دقیقه؛ رزرو پس از بررسی رسید توسط مدیر نهایی می‌شود."])

    keyboard_rows = []
    for row in slots[:80]:
        sid = int(row["id"])
        label = f"{day_label(int(row['start_ts']))} | {time_label(int(row['start_ts']))}"
        url = f"https://t.me/{BOT_USERNAME}?start=slot_{sid}"
        keyboard_rows.append([InlineKeyboardButton(label, url=url)])
    if slots:
        text = "\n".join(lines)
    else:
        text = "\n".join(lines + ["", "در حال حاضر برای ۴۸ ساعت آینده وقت آزادی ثبت نشده است."])
    markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    stored_id = get_setting("channel_message_id", "").strip()
    if stored_id:
        try:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=int(stored_id),
                text=text,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("Editing channel post failed; attempting to create a new one: %s", exc)

    message = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    set_setting("channel_message_id", str(message.message_id))


async def post_init(application: Application) -> None:
    global BOT_USERNAME
    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    await safe_refresh_channel(application.bot)
    if application.job_queue is not None:
        application.job_queue.run_repeating(expire_task, interval=60, first=15, name="expire-holds")
    else:
        logger.warning("JobQueue missing; install requirements with the job-queue extra.")


async def expire_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    changed = expire_holds()
    if changed:
        await safe_refresh_channel(context.bot)


async def admin_guard(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        if update.effective_message:
            await update.effective_message.reply_text("این دستور فقط برای مدیر سالن فعال است.")
        return False
    return True


def make_customer_slot_keyboard(rows):
    buttons = []
    for row in rows:
        label = f"{day_label(int(row['start_ts']))} | {time_label(int(row['start_ts']))}–{time_label(int(row['end_ts']))}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"select:{int(row['id'])}")])
    buttons.append([InlineKeyboardButton("به‌روزرسانی وقت‌ها", callback_data="list_slots")])
    return InlineKeyboardMarkup(buttons)


def payment_keyboard(slot_id: int):
    with closing(db_connect()) as con:
        row = con.execute("SELECT total_price FROM slots WHERE id=?", (slot_id,)).fetchone()
    price = int((row["total_price"] if row and row["total_price"] else get_setting("price", "500000")) or "500000")
    deposit = int(get_setting("deposit", "0") or "0")
    buttons = [[InlineKeyboardButton(f"پرداخت کامل ({money(price)})", callback_data=f"payfull:{slot_id}")]]
    if 0 < deposit < price:
        buttons.append([InlineKeyboardButton(f"پرداخت بیعانه ({money(deposit)})", callback_data=f"paydep:{slot_id}")])
    buttons.append([InlineKeyboardButton("انصراف و آزادکردن وقت", callback_data=f"release:{slot_id}")])
    return InlineKeyboardMarkup(buttons)


async def send_payment_choices(message, slot_id: int, start_ts: int, hold_until: int) -> None:
    deadline = datetime.fromtimestamp(hold_until, TZ).strftime("%H:%M")
    text = (
        f"وقت انتخابی شما موقتاً نگه داشته شد:\n{day_label(start_ts)}، "
        f"{time_label(start_ts)} تا {time_label(start_ts + SESSION_MINUTES * 60)}\n\n"
        f"تا ساعت {fa(deadline)} فرصت دارید روش پرداخت را انتخاب و رسید را ارسال کنید. "
        "پس از ارسال رسید، مدیر آن را بررسی می‌کند."
    )
    await message.reply_text(text, reply_markup=payment_keyboard(slot_id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if context.args and context.args[0].startswith("slot_"):
        try:
            slot_id = int(context.args[0].split("_", 1)[1])
        except ValueError:
            await update.effective_message.reply_text("لینک رزرو معتبر نیست. از فهرست وقت‌های خالی دوباره انتخاب کنید.")
            return
        result = reserve_slot(slot_id, user)
        if result[0] == "ok":
            _, start_ts, hold_until = result
            await send_payment_choices(update.effective_message, slot_id, start_ts, hold_until)
            await safe_refresh_channel(context.bot)
        else:
            messages = {
                "unavailable": "این وقت همین حالا توسط فرد دیگری گرفته شده یا دیگر در بازه رزرو نیست. فهرست را به‌روز کنید.",
                "not_configured": "ربات هنوز برای دریافت پرداخت آماده نشده است. لطفاً کمی بعد دوباره تلاش کنید.",
            }
            await update.effective_message.reply_text(messages.get(result[0], "رزرو انجام نشد. دوباره تلاش کنید."))
            await show_availability(update, context, edit=False)
        return

    price = int(get_setting("price", "500000") or "500000")
    deposit = int(get_setting("deposit", "0") or "0")
    text = (
        "به ربات رزرو سالن فوتسال خوش آمدید.\n"
        f"مدت هر سانس: {fa(SESSION_MINUTES)} دقیقه\n"
        f"هزینه کامل: {money(price)}\n"
    )
    if 0 < deposit < price:
        text += f"بیعانه قابل پرداخت: {money(deposit)}\n"
    text += "\nبرای مشاهده و رزرو وقت‌های خالی در ۴۸ ساعت آینده، دکمه زیر را بزنید."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("مشاهده وقت‌های خالی", callback_data="list_slots")]])
    await update.effective_message.reply_text(text, reply_markup=keyboard)


def reserve_slot(slot_id: int, user) -> tuple:
    now = now_ts()
    instructions = get_setting("payment_instructions", "").strip()
    if not instructions:
        return ("not_configured",)
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} "
            "WHERE status='held' AND hold_until IS NOT NULL AND hold_until < ?",
            (now,),
        )
        row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not row or row["status"] != "open" or int(row["start_ts"]) <= now or int(row["start_ts"]) > now + WINDOW_SECONDS:
            con.execute("COMMIT")
            return ("unavailable",)
        # A user can keep only one temporary hold at a time.
        con.execute(
            f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} WHERE status='held' AND held_by=? AND id<>?",
            (user.id, slot_id),
        )
        hold_until = now + HOLD_MINUTES * 60
        username = user.username or ""
        name = user.full_name or "کاربر تلگرام"
        price_row = con.execute("SELECT value FROM settings WHERE key='price'").fetchone()
        slot_price = int(price_row["value"] if price_row else "500000")
        con.execute(
            """UPDATE slots SET status='held', held_by=?, hold_until=?, customer_id=?,
               customer_username=?, customer_name=?, payment_type=NULL, amount=NULL,
               total_price=?, receipt_file_id=NULL WHERE id=? AND status='open'""",
            (user.id, hold_until, user.id, username, name, slot_price, slot_id),
        )
        updated = con.execute("SELECT status, start_ts FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not updated or updated["status"] != "held":
            con.execute("ROLLBACK")
            return ("unavailable",)
        con.execute("COMMIT")
        return ("ok", int(updated["start_ts"]), hold_until)
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


async def show_availability(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    expire_holds()
    rows = available_slots()
    if not rows:
        text = "در ۴۸ ساعت آینده وقت خالی ثبت نشده است. بعداً دوباره بررسی کنید."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("به‌روزرسانی", callback_data="list_slots")]])
    else:
        text = "وقت‌های خالی (هر سانس ۹۰ دقیقه):\n\nبرای انتخاب، روی وقت دلخواه بزنید."
        markup = make_customer_slot_keyboard(rows)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)
    await safe_refresh_channel(context.bot)


async def availability_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_availability(update, context, edit=False)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user = update.effective_user

    if data == "list_slots":
        await show_availability(update, context, edit=True)
        return

    if data.startswith("select:"):
        try:
            slot_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text("شناسه وقت معتبر نیست.")
            return
        result = reserve_slot(slot_id, user)
        if result[0] == "ok":
            _, start_ts, hold_until = result
            await query.edit_message_text(
                f"وقت {day_label(start_ts)}، {time_label(start_ts)} انتخاب شد.",
                reply_markup=None,
            )
            await send_payment_choices(query.message, slot_id, start_ts, hold_until)
            await safe_refresh_channel(context.bot)
        elif result[0] == "not_configured":
            await query.edit_message_text("ربات هنوز برای دریافت پرداخت آماده نشده است. لطفاً بعداً دوباره تلاش کنید.")
        else:
            await query.edit_message_text("این وقت دیگر آزاد نیست. فهرست را به‌روز کنید.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("به‌روزرسانی", callback_data="list_slots")]]))
        return

    if data.startswith("payfull:") or data.startswith("paydep:"):
        is_deposit = data.startswith("paydep:")
        try:
            slot_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        now = now_ts()
        con = db_connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not row or row["status"] != "held" or int(row["held_by"] or 0) != user.id or int(row["hold_until"] or 0) < now:
                con.execute("COMMIT")
                await query.edit_message_text("مهلت نگه‌داشتن این وقت تمام شده یا وقت دیگر در اختیار شما نیست. فهرست را دوباره باز کنید.")
                await safe_refresh_channel(context.bot)
                return
            price = int(row["total_price"] or get_setting("price", "500000") or "500000")
            deposit = int(get_setting("deposit", "0") or "0")
            if is_deposit and not (0 < deposit < price):
                con.execute("COMMIT")
                await query.edit_message_text("پرداخت بیعانه در حال حاضر فعال نیست. فهرست را دوباره باز کنید.")
                return
            amount = deposit if is_deposit else price
            pay_type = "deposit" if is_deposit else "full"
            con.execute("UPDATE slots SET payment_type=?, amount=? WHERE id=? AND status='held' AND held_by=?",
                        (pay_type, amount, slot_id, user.id))
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        instructions = get_setting("payment_instructions", "").strip()
        await query.edit_message_text(
            f"مبلغ قابل پرداخت: {money(amount)}\n\nاطلاعات پرداخت:\n{instructions}\n\n"
            "بعد از پرداخت، تصویر رسید را همین‌جا در گفت‌وگوی ربات ارسال کنید. "
            "تا پیش از ارسال رسید، وقت فقط به‌صورت موقت نگه داشته می‌شود."
        , reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("انصراف و آزادکردن وقت", callback_data=f"release:{slot_id}")]]))
        await safe_refresh_channel(context.bot)
        return

    if data.startswith("release:"):
        try:
            slot_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        con = db_connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if row and row["status"] == "held" and int(row["held_by"] or 0) == user.id:
                con.execute(f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} WHERE id=?", (slot_id,))
                con.execute("COMMIT")
                await query.edit_message_text("وقت آزاد شد. اگر قصد رزرو ندارید، کار دیگری لازم نیست انجام دهید.")
                await safe_refresh_channel(context.bot)
            else:
                con.execute("COMMIT")
                await query.edit_message_text("این وقت دیگر در اختیار شما نیست یا رسید آن برای بررسی ارسال شده است.")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return

    if data.startswith("approve:") or data.startswith("reject:"):
        if not is_admin(user.id if user else None):
            await query.answer("فقط مدیر سالن می‌تواند رسید را بررسی کند.", show_alert=True)
            return
        approve = data.startswith("approve:")
        try:
            slot_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        con = db_connect()
        customer_id = None
        row_copy = None
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not row or row["status"] != "pending":
                con.execute("COMMIT")
                await query.edit_message_reply_markup(reply_markup=None)
                await query.answer("این رسید قبلاً بررسی شده یا دیگر معتبر نیست.", show_alert=True)
                return
            customer_id = int(row["customer_id"] or 0)
            row_copy = dict(row)
            if approve:
                con.execute("UPDATE slots SET status='booked', hold_until=NULL WHERE id=?", (slot_id,))
            else:
                con.execute(f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} WHERE id=?", (slot_id,))
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        if customer_id:
            if approve:
                amount = int(row_copy.get("amount") or 0)
                price = int(row_copy.get("total_price") or get_setting("price", "500000") or "500000")
                paid_type = row_copy.get("payment_type")
                text = (
                    "✅ رزرو شما توسط مدیر تأیید و قطعی شد.\n"
                    f"زمان: {day_label(int(row_copy['start_ts']))}، "
                    f"{time_label(int(row_copy['start_ts']))} تا {time_label(int(row_copy['end_ts']))}\n"
                    f"مبلغ ثبت‌شده: {money(amount)}\n"
                )
                if paid_type == "deposit":
                    text += f"مانده مبلغ ({money(max(0, price - amount))}) طبق توافق باید تسویه شود."
                try:
                    await context.bot.send_message(chat_id=customer_id, text=text)
                except TelegramError:
                    logger.warning("Could not notify customer %s", customer_id)
                await query.message.reply_text(f"رزرو سانس #{slot_id} تأیید شد.")
            else:
                try:
                    await context.bot.send_message(
                        chat_id=customer_id,
                        text="رسید پرداخت تأیید نشد و وقت آزاد شد. برای رزرو دوباره، فهرست وقت‌های خالی را باز کنید.",
                    )
                except TelegramError:
                    logger.warning("Could not notify customer %s", customer_id)
                await query.message.reply_text(f"رسید سانس #{slot_id} رد شد و وقت آزاد شد.")
        await safe_refresh_channel(context.bot)
        return


async def receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.photo:
        return
    now = now_ts()
    file_id = message.photo[-1].file_id
    con = db_connect()
    slot = None
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} "
            "WHERE status='held' AND hold_until IS NOT NULL AND hold_until < ?",
            (now,),
        )
        row = con.execute(
            "SELECT * FROM slots WHERE status='held' AND held_by=? AND hold_until>=? AND payment_type IS NOT NULL ORDER BY hold_until DESC LIMIT 1",
            (user.id, now),
        ).fetchone()
        if row:
            con.execute("UPDATE slots SET status='pending', hold_until=NULL, receipt_file_id=? WHERE id=?",
                        (file_id, int(row["id"])))
            slot = dict(row)
            slot["receipt_file_id"] = file_id
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()

    if not slot:
        pending = None
        with closing(db_connect()) as check_con:
            pending = check_con.execute(
                "SELECT id FROM slots WHERE status='pending' AND customer_id=? ORDER BY start_ts LIMIT 1",
                (user.id,),
            ).fetchone()
        if pending:
            await message.reply_text("رسید شما قبلاً دریافت شده و در انتظار بررسی مدیر است.")
        else:
            await message.reply_text("برای ارسال رسید، ابتدا یک وقت انتخاب و روش پرداخت را مشخص کنید. با /start وقت‌های خالی را ببینید.")
        await safe_refresh_channel(context.bot)
        return

    payment_label = "بیعانه" if slot.get("payment_type") == "deposit" else "پرداخت کامل"
    username = f"@{slot.get('customer_username')}" if slot.get("customer_username") else "ندارد"
    caption = (
        f"🧾 رسید پرداخت جدید برای بررسی مدیر\n"
        f"سانس: #{slot['id']}\n"
        f"زمان: {day_label(int(slot['start_ts']))}، {time_label(int(slot['start_ts']))} تا {time_label(int(slot['end_ts']))}\n"
        f"مشتری: {slot.get('customer_name') or 'کاربر تلگرام'} ({username})\n"
        f"شناسه تلگرام مشتری: {slot.get('customer_id')}\n"
        f"نوع پرداخت: {payment_label}\n"
        f"مبلغ اعلام‌شده: {money(int(slot.get('amount') or 0))}"
    )
    admin_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید و قطعی‌کردن", callback_data=f"approve:{slot['id']}"),
        InlineKeyboardButton("❌ رد رسید و آزادکردن", callback_data=f"reject:{slot['id']}"),
    ]])
    try:
        await context.bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=caption, reply_markup=admin_markup)
    except TelegramError:
        logger.exception("Could not send receipt to admin; slot remains pending.")
        await message.reply_text("رسید ذخیره شد، اما ارسال آن به مدیر با خطا روبه‌رو شد. لطفاً با مدیر تماس بگیرید.")
        return
    await message.reply_text("رسید دریافت شد و وقت تا زمان بررسی مدیر از دسترس دیگران خارج است. پس از تأیید، پیام قطعی‌شدن رزرو را دریافت می‌کنید.")
    await safe_refresh_channel(context.bot)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"شناسه عددی تلگرام شما: {user.id}")


async def helpadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    text = (
        "دستورهای مدیریت سالن (فقط مدیر):\n\n"
        "/addslot تاریخ ساعت — افزودن یک سانس ۹۰ دقیقه‌ای\n"
        "نمونه: /addslot 1405-07-30 18:00\n\n"
        "/addslots تاریخ ساعت_شروع ساعت_پایان — ساخت سانس‌های پشت‌سرهم ۹۰ دقیقه‌ای\n"
        "نمونه: /addslots 1405-07-30 16:00 23:30\n\n"
        "/delslot شناسه — حذف یک وقت خالی\n"
        "/cancel شناسه — لغو رزرو/درخواست و آزادکردن همان وقت\n"
        "/slots — مشاهده وضعیت سانس‌های ۴۸ ساعت آینده\n"
        "/setprice مبلغ — تعیین هزینه کامل به تومان\n"
        "/setdeposit مبلغ — تعیین بیعانه به تومان؛ عدد 0 بیعانه را غیرفعال می‌کند\n"
        "/setpayment متن — تعیین شماره کارت/اطلاعات پرداخت\n"
        "/publish — به‌روزرسانی پیام کانال\n"
        "/myid — نمایش شناسه تلگرام\n\n"
        "تاریخ‌ها شمسی هستند. برای تغییر ساعت یک وقت خالی، آن را حذف و دوباره با ساعت جدید اضافه کنید."
    )
    await update.effective_message.reply_text(text)


async def addslot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("فرمت: /addslot 1405-07-30 18:00\nتاریخ شمسی و ساعت ۲۴ساعته را وارد کنید.")
        return
    try:
        start = jalali_datetime_to_ts(context.args[0], context.args[1])
    except Exception:
        await update.effective_message.reply_text("تاریخ یا ساعت درست نیست. نمونه: /addslot 1405-07-30 18:00")
        return
    now = now_ts()
    end = start + SESSION_MINUTES * 60
    if start <= now:
        await update.effective_message.reply_text("زمان شروع باید در آینده باشد.")
        return
    if start > now + WINDOW_SECONDS:
        await update.effective_message.reply_text("فقط می‌توانید برای ۴۸ ساعت آینده سانس تعریف کنید.")
        return
    with closing(db_connect()) as con:
        conflict = con.execute(
            "SELECT id FROM slots WHERE start_ts < ? AND end_ts > ? LIMIT 1",
            (end, start),
        ).fetchone()
        if conflict:
            await update.effective_message.reply_text(f"این زمان با سانس #{conflict['id']} تداخل دارد؛ ثبت نشد.")
            return
        try:
            cur = con.execute(
                "INSERT INTO slots(start_ts,end_ts,status,created_at) VALUES(?,?, 'open', ?)",
                (start, end, now),
            )
        except sqlite3.IntegrityError:
            await update.effective_message.reply_text("برای این ساعت قبلاً سانسی ثبت شده است.")
            return
        slot_id = cur.lastrowid
    await update.effective_message.reply_text(f"سانس #{slot_id} ثبت شد:\n{day_label(start)}، {time_label(start)} تا {time_label(end)}")
    await safe_refresh_channel(context.bot)


async def addslots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 3:
        await update.effective_message.reply_text("فرمت: /addslots 1405-07-30 16:00 23:30\nسانس‌های ۹۰ دقیقه‌ای پشت‌سرهم تا ساعت پایان ساخته می‌شوند.")
        return
    try:
        start = jalali_datetime_to_ts(context.args[0], context.args[1])
        finish = jalali_datetime_to_ts(context.args[0], context.args[2])
    except Exception:
        await update.effective_message.reply_text("تاریخ یا ساعت درست نیست. نمونه: /addslots 1405-07-30 16:00 23:30")
        return
    if finish <= start:
        await update.effective_message.reply_text("ساعت پایان باید بعد از ساعت شروع باشد.")
        return
    now = now_ts()
    if start <= now:
        await update.effective_message.reply_text("ساعت شروع باید در آینده باشد.")
        return
    if start > now + WINDOW_SECONDS:
        await update.effective_message.reply_text("شروع برنامه خارج از بازه ۴۸ ساعت آینده است.")
        return
    candidates = []
    cursor = start
    while cursor + SESSION_MINUTES * 60 <= finish and cursor <= now + WINDOW_SECONDS:
        candidates.append((cursor, cursor + SESSION_MINUTES * 60))
        cursor += SESSION_MINUTES * 60
    if not candidates:
        await update.effective_message.reply_text("در بازه واردشده سانس کامل ۹۰ دقیقه‌ای جا نمی‌گیرد.")
        return
    added, skipped = [], []
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        for slot_start, slot_end in candidates:
            conflict = con.execute(
                "SELECT id FROM slots WHERE start_ts < ? AND end_ts > ? LIMIT 1",
                (slot_end, slot_start),
            ).fetchone()
            if conflict:
                skipped.append((slot_start, int(conflict["id"])))
                continue
            try:
                cur = con.execute(
                    "INSERT INTO slots(start_ts,end_ts,status,created_at) VALUES(?,?, 'open', ?)",
                    (slot_start, slot_end, now),
                )
                added.append(int(cur.lastrowid))
            except sqlite3.IntegrityError:
                skipped.append((slot_start, 0))
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    answer = f"{fa(len(added))} سانس ثبت شد. شناسه‌ها: " + (", ".join(fa(i) for i in added) if added else "ندارد")
    if skipped:
        answer += f"\n{fa(len(skipped))} سانس به‌دلیل تداخل یا تکراری‌بودن رد شد."
    await update.effective_message.reply_text(answer)
    await safe_refresh_channel(context.bot)


async def delslot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 1 or not normalize_digits(context.args[0]).isdigit():
        await update.effective_message.reply_text("فرمت: /delslot 12")
        return
    slot_id = int(normalize_digits(context.args[0]))
    with closing(db_connect()) as con:
        row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not row:
            await update.effective_message.reply_text("این شناسه پیدا نشد.")
            return
        if row["status"] != "open":
            await update.effective_message.reply_text("این وقت خالی نیست. ابتدا با /cancel شناسه رزرو/درخواست را لغو کنید.")
            return
        con.execute("DELETE FROM slots WHERE id=?", (slot_id,))
    await update.effective_message.reply_text(f"سانس #{slot_id} حذف شد.")
    await safe_refresh_channel(context.bot)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 1 or not normalize_digits(context.args[0]).isdigit():
        await update.effective_message.reply_text("فرمت: /cancel 12")
        return
    slot_id = int(normalize_digits(context.args[0]))
    con = db_connect()
    customer_id = None
    was_found = False
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if row:
            was_found = True
            customer_id = int(row["customer_id"] or 0) or None
            con.execute(f"UPDATE slots SET status='open', {clear_slot_assignment_sql()} WHERE id=?", (slot_id,))
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    if not was_found:
        await update.effective_message.reply_text("این شناسه پیدا نشد.")
        return
    if customer_id:
        try:
            await context.bot.send_message(chat_id=customer_id,
                                           text="مدیر سالن این رزرو/درخواست را لغو کرد و وقت دوباره آزاد شد. برای هماهنگی با مدیر تماس بگیرید.")
        except TelegramError:
            pass
    await update.effective_message.reply_text(f"رزرو/درخواست سانس #{slot_id} لغو شد و وقت آزاد شد. برای حذف کامل وقت، /delslot {slot_id} را بزنید.")
    await safe_refresh_channel(context.bot)


async def slots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    expire_holds()
    rows = all_slots_in_window()
    if not rows:
        await update.effective_message.reply_text("در ۴۸ ساعت آینده سانسی ثبت نشده است.")
        return
    lines = ["وضعیت سانس‌های ۴۸ ساعت آینده:"]
    for row in rows:
        status = STATUS_FA.get(row["status"], row["status"])
        line = f"#{row['id']} | {slot_label(row)} | {status}"
        if row["status"] in ("held", "pending", "booked"):
            customer = row["customer_name"] or "نامشخص"
            username = f"@{row['customer_username']}" if row["customer_username"] else "بدون نام کاربری"
            line += f" | {customer} ({username})"
            if row["amount"]:
                line += f" | {money(int(row['amount']))}"
        lines.append(line)
    await update.effective_message.reply_text("\n".join(lines))


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("فرمت: /setprice 500000  (مبلغ به تومان)")
        return
    value = normalize_digits(context.args[0])
    if not value.isdigit() or int(value) <= 0:
        await update.effective_message.reply_text("مبلغ باید عددی بزرگ‌تر از صفر باشد.")
        return
    set_setting("price", str(int(value)))
    await update.effective_message.reply_text(f"هزینه کامل هر سانس روی {money(int(value))} تنظیم شد.")
    await safe_refresh_channel(context.bot)


async def setdeposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("فرمت: /setdeposit 150000  (مبلغ به تومان؛ برای غیرفعال‌سازی /setdeposit 0)")
        return
    value = normalize_digits(context.args[0])
    if not value.isdigit():
        await update.effective_message.reply_text("مبلغ بیعانه باید عددی و صفر یا بیشتر باشد.")
        return
    amount = int(value)
    price = int(get_setting("price", "500000") or "500000")
    if amount >= price and amount != 0:
        await update.effective_message.reply_text("بیعانه باید از هزینه کامل کمتر باشد. برای دریافت کل مبلغ، بیعانه را با /setdeposit 0 غیرفعال کنید.")
        return
    set_setting("deposit", str(amount))
    result = "بیعانه غیرفعال شد؛ مشتری باید هزینه کامل را بپردازد." if amount == 0 else f"بیعانه روی {money(amount)} تنظیم شد."
    await update.effective_message.reply_text(result)
    await safe_refresh_channel(context.bot)


async def setpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    instructions = " ".join(context.args).strip()
    if not instructions:
        await update.effective_message.reply_text("فرمت: /setpayment شماره کارت: ... | به نام: ...\nاطلاعات را در گفت‌وگوی خصوصی ربات وارد کنید.")
        return
    set_setting("payment_instructions", instructions)
    await update.effective_message.reply_text("اطلاعات پرداخت ذخیره شد. این اطلاعات در کانال نمایش داده نمی‌شود و فقط پس از انتخاب وقت به مشتری نشان داده می‌شود.")


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update):
        return
    if not CHANNEL_ID:
        await update.effective_message.reply_text("CHANNEL_ID در فایل .env تنظیم نشده است.")
        return
    await safe_refresh_channel(context.bot)
    await update.effective_message.reply_text("درخواست به‌روزرسانی کانال ارسال شد. اگر تغییری نمی‌بینید، دسترسی ادمین ربات به کانال را بررسی کنید.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)


def main() -> None:
    init_db()
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("availability", availability_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("helpadmin", helpadmin_command))
    application.add_handler(CommandHandler("addslot", addslot_command))
    application.add_handler(CommandHandler("addslots", addslots_command))
    application.add_handler(CommandHandler("delslot", delslot_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("slots", slots_command))
    application.add_handler(CommandHandler("setprice", setprice_command))
    application.add_handler(CommandHandler("setdeposit", setdeposit_command))
    application.add_handler(CommandHandler("setpayment", setpayment_command))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, receipt_handler))
    application.add_error_handler(error_handler)
    logger.info("Futsal booking bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
