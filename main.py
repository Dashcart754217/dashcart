# ============================================
# STEP 1: Config + Database + Helpers
# ============================================

import os
import sqlite3
import logging
import asyncio
import random
import string
import hmac
import hashlib
import time
from datetime import datetime, timezone

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
ADMIN_ID = 8575581503
FORCE_JOIN_CHANNEL = "@dashcartstore"
SUPPORT_USERNAME = "@dashcartsupport_bot"
BEP20_ADDRESS = "0x36fbc5e0dcfc7eedab4fda4c403015677d62069b"
BINANCE_PAY_ID = "516100411"
STORE_NAME = "DashCart"
DB_NAME = "/data/dashcart_store.db"
ANNOUNCE_CHANNEL = "@dashcartstore"
BOT_USERNAME = "dashcart_bot"

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")       # Optional
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET") # Optional

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN environment variable is missing!")

# ====================== DATABASE ======================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs("/data", exist_ok=True)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        balance REAL DEFAULT 0.0,
        total_spent REAL DEFAULT 0.0,
        total_orders INTEGER DEFAULT 0,
        joined_at TEXT,
        is_banned INTEGER DEFAULT 0,
        last_active TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        duration TEXT,
        price REAL NOT NULL,
        emoji TEXT,
        is_active INTEGER DEFAULT 1,
        description TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS stock (
        stock_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'AVAILABLE',
        reserved_by INTEGER,
        reserved_at TEXT,
        delivered_at TEXT,
        order_id TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER,
        product_id INTEGER,
        quantity INTEGER,
        total_amount REAL,
        payment_method TEXT,
        status TEXT DEFAULT 'PENDING',
        txid TEXT,
        binance_ref TEXT,
        created_at TEXT,
        approved_at TEXT,
        delivered_at TEXT,
        admin_note TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS wallet_deposits (
        deposit_id TEXT PRIMARY KEY,
        user_id INTEGER,
        amount REAL,
        payment_method TEXT,
        status TEXT DEFAULT 'PENDING',
        txid TEXT,
        binance_ref TEXT,
        created_at TEXT,
        approved_at TEXT,
        admin_note TEXT
    )''')

    try:
        c.execute("ALTER TABLE products ADD COLUMN description TEXT DEFAULT ''")
    except:
        pass

    # No default products
    conn.commit()
    conn.close()
    print("✅ Database initialized (no products)")

# ====================== HELPERS ======================
def generate_order_id():
    return f"DC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"

def generate_deposit_id():
    return f"DEP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"

def is_admin(user_id):
    return user_id == ADMIN_ID

def format_money(amount):
    return f"${amount:.2f}"

def get_current_time():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_txid_already_used(txid: str) -> bool:
    if not txid:
        return False
    txid = txid.strip().lower()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT order_id FROM orders WHERE LOWER(COALESCE(txid,'')) = ? OR LOWER(COALESCE(binance_ref,'')) = ?", (txid, txid))
    if c.fetchone():
        conn.close()
        return True
    c.execute("SELECT deposit_id FROM wallet_deposits WHERE LOWER(COALESCE(txid,'')) = ? OR LOWER(COALESCE(binance_ref,'')) = ?", (txid, txid))
    if c.fetchone():
        conn.close()
        return True
    conn.close()
    return False

def normalize_payment_proof(proof: str) -> str:
    if not proof:
        return ""
    s = proof.strip().lower()
    for prefix in [
        "off-chain transfer", "offchain transfer", "off chain transfer",
        "transaction id:", "txid:", "order id:", "order:", "binance pay:", "payment id:",
    ]:
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s.replace(" ", "")

async def check_binance_deposit(txid_or_ref: str, expected_amount: float) -> dict:
    try:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return {"found": False, "message": "API keys not configured"}

        raw = (txid_or_ref or "").strip()
        search = normalize_payment_proof(raw)
        if not search:
            return {"found": False, "message": "Empty proof"}

        timestamp = int(time.time() * 1000)

        async def signed_get(path: str):
            query = f"timestamp={timestamp}&limit=100"
            signature = hmac.new(
                BINANCE_API_SECRET.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            url = f"https://api.binance.com{path}?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return None, f"HTTP {resp.status}: {body[:150]}"
                    try:
                        return await resp.json(), None
                    except Exception:
                        return None, "Invalid JSON from Binance"

        def amount_ok(amount: float) -> bool:
            try:
                amount = float(amount or 0)
            except Exception:
                return False
            if amount <= 0:
                return True
            return abs(amount - expected_amount) <= 0.05 or amount >= expected_amount

        # Check BEP20 deposits
        deposits, err1 = await signed_get("/sapi/v1/capital/deposit/hisrec")
        if isinstance(deposits, list):
            for dep in deposits:
                tx_id = str(dep.get("txId", "")).lower().replace(" ", "")
                try:
                    amount = float(dep.get("amount", 0) or 0)
                except Exception:
                    amount = 0.0
                if dep.get("status") == 1 and tx_id and (search == tx_id or search in tx_id or tx_id in search):
                    if amount_ok(amount):
                        return {
                            "found": True,
                            "message": f"BEP20 deposit found: {amount}",
                            "amount": amount if amount > 0 else expected_amount
                        }

        # Check Binance Pay transactions
        pay_raw, err2 = await signed_get("/sapi/v1/pay/transactions")
        pay_list = []
        if isinstance(pay_raw, dict) and isinstance(pay_raw.get("data"), list):
            pay_list = pay_raw.get("data")
        elif isinstance(pay_raw, list):
            pay_list = pay_raw

        for tx in pay_list:
            order_id = str(tx.get("orderId") or "").lower().replace(" ", "")
            transaction_id = str(tx.get("transactionId") or "").lower().replace(" ", "")
            note = str(tx.get("note") or "").lower()
            amount = 0.0
            for key in ("amount", "totalAmount", "amountValue", "receiveAmount"):
                if tx.get(key) is not None:
                    try:
                        amount = float(tx.get(key) or 0)
                        break
                    except Exception:
                        pass

            id_match = (
                (order_id and (search == order_id or search in order_id or order_id in search))
                or (transaction_id and (search == transaction_id or search in transaction_id or transaction_id in search))
                or (note and search in note.replace(" ", ""))
            )
            if id_match and amount_ok(amount):
                return {
                    "found": True,
                    "message": f"Binance Pay found: {order_id or transaction_id or search}",
                    "amount": amount if amount > 0 else expected_amount
                }

        msg = "Not found in BEP20 deposits or Binance Pay history"
        if err1:
            msg += f" | deposit_api: {err1}"
        if err2:
            msg += f" | pay_api: {err2}"
        return {"found": False, "message": msg}

    except Exception as e:
        return {"found": False, "message": f"Error: {str(e)}"}
# ============================================
# STEP 2: Force Join + Start + Main Menu + Profile / Support / Terms
# ============================================

async def check_membership(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Membership error: {e}")
        return False


async def force_join_message(update, context):
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}")],
        [InlineKeyboardButton("✅ Check Membership", callback_data="check_membership")]
    ]
    text = (
        f"🔒 <b>Channel Membership Required</b>\n\n"
        f"Welcome to <b>{STORE_NAME}</b>!\n\n"
        f"Please join our channel first:\n"
        f"<b>{FORCE_JOIN_CHANNEL}</b>\n\n"
        f"After joining, click ✅ Check Membership"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )


async def check_membership_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await check_membership(user_id, context):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO users (user_id, username, full_name, joined_at, last_active) VALUES (?, ?, ?, ?, ?)",
                (user_id, query.from_user.username, query.from_user.full_name, get_current_time(), get_current_time())
            )
        else:
            c.execute(
                "UPDATE users SET last_active = ?, username = ?, full_name = ? WHERE user_id = ?",
                (get_current_time(), query.from_user.username, query.from_user.full_name, user_id)
            )
        conn.commit()
        conn.close()
        await show_main_menu(update, context)
    else:
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        await force_join_message(update, context)


async def open_product_from_deep_link(update, context, product_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_id = ? AND is_active = 1", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()

    if not product or stock_count == 0:
        await update.message.reply_text("❌ Product not available or out of stock.")
        await show_main_menu(update, context)
        return

    context.user_data['selected_product_id'] = product_id
    context.user_data['selected_product'] = dict(product)

    text = (
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"⏱ Duration: <b>{product['duration']}</b>\n"
        f"💵 Price: <b>{format_money(product['price'])}</b> each\n"
        f"📦 Available: <b>{stock_count}</b>\n\n"
        f"Select quantity:"
    )
    max_qty = min(5, stock_count)
    keyboard, row = [], []
    for i in range(1, max_qty + 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"qty_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Shop", callback_data="shop")])
    await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def start_command(update, context):
    user_id = update.effective_user.id

    # Check if banned
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row["is_banned"]:
        conn.close()
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    conn.close()

    # Force join check
    if not await check_membership(user_id, context):
        await force_join_message(update, context)
        return

    # Register / update user
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute(
            "INSERT INTO users (user_id, username, full_name, joined_at, last_active) VALUES (?, ?, ?, ?, ?)",
            (user_id, update.effective_user.username, update.effective_user.full_name, get_current_time(), get_current_time())
        )
    else:
        c.execute(
            "UPDATE users SET last_active = ?, username = ?, full_name = ? WHERE user_id = ?",
            (get_current_time(), update.effective_user.username, update.effective_user.full_name, user_id)
        )
    conn.commit()
    conn.close()

    # Deep link support
    if context.args:
        if context.args[0].startswith("buy_"):
            try:
                product_id = int(context.args[0].replace("buy_", ""))
                await open_product_from_deep_link(update, context, product_id)
                return
            except Exception as e:
                print(f"Deep link error: {e}")
        elif context.args[0] == "shop":
            await shop_handler(update, context)
            return

    await show_main_menu(update, context)


async def show_main_menu(update, context):
    user = update.effective_user
    text = (
        f"🏠 <b>Welcome to {STORE_NAME}</b>\n\n"
        f"Hello <b>{user.first_name}</b>!\n"
        f"Choose an option below:"
    )
    keyboard = [
        [InlineKeyboardButton("🛍️ Shop Products", callback_data="shop")],
        [
            InlineKeyboardButton("👤 Profile", callback_data="profile"),
            InlineKeyboardButton("💰 Wallet", callback_data="wallet")
        ],
        [
            InlineKeyboardButton("📦 My Orders", callback_data="my_orders"),
            InlineKeyboardButton("🎧 Support", callback_data="support")
        ],
        [InlineKeyboardButton("📜 Terms & Info", callback_data="terms")]
    ]
    if user and is_admin(user.id):
        keyboard.append([InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )


async def main_menu_callback(update, context):
    await update.callback_query.answer()
    await show_main_menu(update, context)


async def profile_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()

    if not user:
        await query.edit_message_text("❌ User not found. Please /start again.")
        return

    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🆔 User ID: <code>{user['user_id']}</code>\n"
        f"👤 Name: <b>{user['full_name'] or 'N/A'}</b>\n"
        f"🔗 Username: @{user['username'] or 'None'}\n\n"
        f"💰 Balance: <b>{format_money(user['balance'])}</b>\n"
        f"🛒 Total Spent: <b>{format_money(user['total_spent'])}</b>\n"
        f"📦 Total Orders: <b>{user['total_orders']}</b>\n"
        f"📅 Joined: <b>{user['joined_at']}</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
    await query.edit_message_text(
        text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
    )


async def support_handler(update, context):
    text = (
        f"🎧 <b>Support</b>\n\n"
        f"Need help? Contact our support:\n\n"
        f"👤 {SUPPORT_USERNAME}\n\n"
        f"Please include your <b>Order ID</b> if you have any issue with an order."
    )
    clean_username = SUPPORT_USERNAME.replace('@', '').strip()
    keyboard = [
        [InlineKeyboardButton("📩 Contact Support", url=f"https://t.me/{clean_username}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        await update.callback_query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
    elif update.message:
        await update.message.reply_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )


async def terms_handler(update, context):
    query = update.callback_query
    await query.answer()
    text = (
        f"📜 <b>Terms & Information</b>\n\n"
        f"• All products are digital and delivered after payment approval.\n"
        f"• Payments are manual (BEP20 / Binance Pay).\n"
        f"• Auto-verification is available when possible.\n"
        f"• No refunds after the product has been delivered.\n"
        f"• Do not share your accounts.\n"
        f"• Support: {SUPPORT_USERNAME}\n\n"
        f"<b>{STORE_NAME}</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
    await query.edit_message_text(
        text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
    )


async def my_orders_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    page = 0
    if query.data.startswith("my_orders_page_"):
        try:
            page = int(query.data.replace("my_orders_page_", ""))
        except:
            page = 0

    per_page = 10
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]

    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        LIMIT ? OFFSET ?
    ''', (user_id, per_page, offset))
    orders = c.fetchall()

    if not orders and page == 0:
        conn.close()
        text = "📦 <b>My Orders</b>\n\nYou have no orders yet."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    text = f"📦 <b>My Orders</b> (Page {page + 1})\n\n"

    for order in orders:
        status_emoji = {
            "PENDING": "⏳",
            "APPROVED": "✅",
            "DELIVERED": "✅",
            "REJECTED": "❌",
            "CANCELLED": "🚫"
        }.get(order['status'], "❓")

        text += (
            f"{status_emoji} <b>{order['order_id']}</b>\n"
            f"{order['emoji']} {order['product_name']} × {order['quantity']}\n"
            f"💰 {format_money(order['total_amount'])} | {order['payment_method'].upper()}\n"
            f"📅 {order['created_at']}\n"
            f"Status: <b>{order['status']}</b>\n"
        )

        # Show credentials if delivered
        if order['status'] in ["APPROVED", "DELIVERED"]:
            c.execute(
                "SELECT content FROM stock WHERE order_id = ? AND status = 'DELIVERED'",
                (order['order_id'],)
            )
            items = c.fetchall()
            if items:
                text += "🔑 <b>Credentials:</b>\n"
                for item in items:
                    text += f"<code>{item['content']}</code>\n"

        text += "\n" + "─" * 20 + "\n\n"

    conn.close()

    if len(text) > 3900:
        text = text[:3800] + "\n\n..."

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"my_orders_page_{page-1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"my_orders_page_{page+1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])

    await query.edit_message_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


print("✅ Step 2 completed!")
print("➡️ Reply with 'next' for Step 3 (Shop + Payments)")
# ============================================
# STEP 3: Shop + Quantity + Payment Methods
# ============================================

async def shop_handler(update, context):
    query = update.callback_query
    page = 0
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        if query.data and query.data.startswith("shop_page_"):
            try:
                page = int(query.data.replace("shop_page_", ""))
            except:
                page = 0

    context.user_data['shop_page'] = page

    per_page = 6
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
    total = c.fetchone()[0]

    c.execute(
        "SELECT * FROM products WHERE is_active = 1 ORDER BY product_id LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    products = c.fetchall()

    if not products and page == 0:
        conn.close()
        text = "🛍️ <b>Shop Products</b>\n\nNo products available at the moment.\nPlease check back later."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
        if query:
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        elif update.message:
            await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if not products and page > 0:
        page = 0
        offset = 0
        context.user_data['shop_page'] = 0
        c.execute(
            "SELECT * FROM products WHERE is_active = 1 ORDER BY product_id LIMIT ? OFFSET ?",
            (per_page, offset)
        )
        products = c.fetchall()

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    if total_pages > 1:
        text = f"🛍️ <b>Shop Products</b> (Page {page + 1}/{total_pages})\n\nSelect a product:"
    else:
        text = "🛍️ <b>Shop Products</b>\n\nSelect a product:"

    keyboard = []

    for p in products:
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (p['product_id'],))
        stock_count = c.fetchone()[0]

        btn_text = f"{p['emoji']} {p['name']} ({p['duration']}) — {format_money(p['price'])}"
        if stock_count == 0:
            btn_text += " [Out of Stock]"

        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"product_{p['product_id']}")])

    conn.close()

    # Pagination navigation: Back & More buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"shop_page_{page - 1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("More ➡️", callback_data=f"shop_page_{page + 1}"))

    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    if query:
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def product_selected(update, context):
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.split("_")[1])

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()

    if not product or stock_count == 0:
        await query.edit_message_text("❌ Product not available or out of stock.")
        return

    context.user_data['selected_product_id'] = product_id
    context.user_data['selected_product'] = dict(product)

    desc = (product['description'] or "").strip()
    if not desc:
        desc = (
            f"✅ Premium digital access\n"
            f"✅ Validity: {product['duration']}\n"
            f"✅ Instant delivery after payment"
        )

    text = (
        f"{product['emoji']} <b>{product['name']} · {product['duration']}</b>\n\n"
        f"💰 <b>Price:</b> {format_money(product['price'])} / item\n\n"
        f"📋 <b>Plan Details</b>\n{desc}\n\n"
        f"📦 <b>Available stock:</b> {stock_count}\n\n"
        f"Delivery is automatic after payment confirmation."
    )
    current_page = context.user_data.get('shop_page', 0)
    keyboard = [
        [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy_now_{product_id}")],
        [InlineKeyboardButton("⬅️ Back to Store", callback_data=f"shop_page_{current_page}")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def buy_now_selected(update, context):
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.replace("buy_now_", ""))
    product = context.user_data.get('selected_product')

    if not product or product.get('product_id') != product_id:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
        product = c.fetchone()
        conn.close()
        if not product:
            await query.edit_message_text("❌ Product not found.")
            return
        context.user_data['selected_product'] = dict(product)
        context.user_data['selected_product_id'] = product_id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()

    if stock_count <= 0:
        await query.edit_message_text("❌ Out of stock.")
        return

    text = (
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"💰 {format_money(product['price'])} each\n"
        f"📦 Available: <b>{stock_count}</b>\n\n"
        f"Select quantity:"
    )
    max_qty = min(5, stock_count)
    keyboard, row = [], []
    for i in range(1, max_qty + 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"qty_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"product_{product_id}")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def quantity_selected(update, context):
    query = update.callback_query
    await query.answer()

    qty = int(query.data.split("_")[1])
    product = context.user_data.get('selected_product')

    if not product:
        await query.edit_message_text("❌ Session expired. Please start again from Shop.")
        return

    total = product['price'] * qty
    context.user_data['quantity'] = qty
    context.user_data['total_amount'] = total

    text = (
        f"🛒 <b>Order Summary</b>\n\n"
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"📦 Quantity: <b>{qty}</b>\n"
        f"💰 <b>Total: {format_money(total)}</b>\n\n"
        f"Select payment method:"
    )
    keyboard = [
        [InlineKeyboardButton("💰 Pay from Wallet", callback_data="pay_wallet")],
        [InlineKeyboardButton("🪙 Pay with BEP20", callback_data="pay_bep20")],
        [InlineKeyboardButton("🔵 Pay with Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"product_{product['product_id']}")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def pay_from_wallet(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')

    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired. Please start again from Shop.")
        return

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    balance = row['balance'] if row else 0.0

    if balance < total:
        conn.close()
        text = (
            f"❌ <b>Insufficient Balance</b>\n\n"
            f"Your balance: <b>{format_money(balance)}</b>\n"
            f"Required: <b>{format_money(total)}</b>"
        )
        keyboard = [
            [InlineKeyboardButton("💰 Go to Wallet", callback_data="wallet")],
            [InlineKeyboardButton("🔙 Back", callback_data="shop")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product['product_id'],))
    if c.fetchone()[0] < qty:
        conn.close()
        await query.edit_message_text("❌ Not enough stock left.")
        return

    order_id = generate_order_id()

    c.execute(
        "UPDATE users SET balance = balance - ?, total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?",
        (total, total, user_id)
    )
    c.execute(
        "INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, user_id, product['product_id'], qty, total, "wallet", "DELIVERED", get_current_time())
    )

    c.execute(
        "SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?",
        (product['product_id'], qty)
    )
    stocks = c.fetchall()

    delivered_items = []
    for stock in stocks:
        c.execute(
            "UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
            (user_id, get_current_time(), order_id, stock['stock_id'])
        )
        delivered_items.append(stock['content'])

    conn.commit()
    conn.close()

    # Normal delivery for all products
    delivery_text = (
        f"✅ <b>Payment Successful!</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"📦 Quantity: {qty}\n"
        f"💰 Paid: {format_money(total)} (from Wallet)\n\n"
        f"<b>Your Product(s):</b>\n\n"
    )
    for i, item in enumerate(delivered_items, 1):
        delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
    delivery_text += "⚠️ Keep this information safe!"

    keyboard = [
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=delivery_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    await send_purchase_announcement(context, product['name'], qty, product['emoji'])
    context.user_data.clear()


async def pay_bep20(update, context):
    query = update.callback_query
    await query.answer()

    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')

    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired.")
        return

    order_id = generate_order_id()
    context.user_data['pending_order_id'] = order_id
    context.user_data['payment_method'] = "bep20"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, query.from_user.id, product['product_id'], qty, total, "bep20", "PENDING", get_current_time())
    )
    conn.commit()
    conn.close()

    text = (
        f"🪙 <b>BEP20 Payment</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} {product['name']} × {qty}\n"
        f"💰 Amount: <b>{format_money(total)} USDT</b>\n\n"
        f"<b>Send exactly to this address:</b>\n"
        f"<code>{BEP20_ADDRESS}</code>\n\n"
        f"Network: <b>BEP20 (BSC)</b>\n\n"
        f"After payment, click the button below and submit your Transaction ID (TXID)."
    )
    keyboard = [
        [InlineKeyboardButton("✅ I Have Paid – Submit TXID", callback_data="submit_txid")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="shop")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def pay_binance(update, context):
    query = update.callback_query
    await query.answer()

    product = context.user_data.get('selected_product')
    qty = context.user_data.get('quantity')
    total = context.user_data.get('total_amount')

    if not product or not qty or total is None:
        await query.edit_message_text("❌ Session expired.")
        return

    order_id = generate_order_id()
    context.user_data['pending_order_id'] = order_id
    context.user_data['payment_method'] = "binance"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (order_id, user_id, product_id, quantity, total_amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, query.from_user.id, product['product_id'], qty, total, "binance", "PENDING", get_current_time())
    )
    conn.commit()
    conn.close()

    text = (
        f"🔵 <b>Binance Pay</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} {product['name']} × {qty}\n"
        f"💰 Amount: <b>{format_money(total)}</b>\n\n"
        f"<b>Pay to Binance ID:</b>\n"
        f"<code>{BINANCE_PAY_ID}</code>\n\n"
        f"After payment, click the button below and submit your Payment Reference."
    )
    keyboard = [
        [InlineKeyboardButton("✅ I Have Paid – Submit Reference", callback_data="submit_binance_ref")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="shop")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


print("✅ Step 3 completed!")
print("➡️ Reply with 'next' for Step 4 (Payment Proof + Wallet Deposit)")
# ============================================
# STEP 4: Payment Proof + Wallet Deposit System
# ============================================

async def ask_txid(update, context):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get('pending_order_id')
    if not order_id:
        await query.edit_message_text("❌ Session expired. Please start order again from Shop.")
        return
    context.user_data['waiting_for'] = "txid"
    text = (
        f"📝 <b>Submit TXID</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n\n"
        f"Please send your BEP20 Transaction ID now."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order_{order_id}")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def ask_binance_ref(update, context):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get('pending_order_id')
    if not order_id:
        await query.edit_message_text("❌ Session expired. Please start order again from Shop.")
        return
    context.user_data['waiting_for'] = "binance_ref"
    text = (
        f"📝 <b>Submit Binance Reference</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n\n"
        f"Please send your Binance Payment Reference now."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order_{order_id}")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def handle_payment_proof(update, context):
    if not update.message or not update.message.text:
        return

    waiting_for = context.user_data.get('waiting_for')
    order_id = context.user_data.get('pending_order_id')
    if waiting_for not in ["txid", "binance_ref"] or not order_id:
        return

    user_id = update.effective_user.id
    proof = update.message.text.strip()

    if len(proof) < 8:
        await update.message.reply_text("❌ Invalid proof. Please send a valid TXID or reference.")
        return

    if is_txid_already_used(proof):
        await update.message.reply_text("❌ <b>This TXID / Reference was already used.</b>", parse_mode=ParseMode.HTML)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ? AND status = 'PENDING'", (order_id, user_id))
    order = c.fetchone()

    if not order:
        conn.close()
        await update.message.reply_text("❌ Order not found or already processed.")
        context.user_data.clear()
        return

    if waiting_for == "txid":
        c.execute("UPDATE orders SET txid = ? WHERE order_id = ?", (proof, order_id))
        proof_type = "TXID"
    else:
        c.execute("UPDATE orders SET binance_ref = ? WHERE order_id = ?", (proof, order_id))
        proof_type = "Binance Reference"

    conn.commit()

    expected_amount = float(order['total_amount'])
    verification = await check_binance_deposit(proof, expected_amount)

    if verification.get("found"):
        product_id = order['product_id']
        qty = order['quantity']

        c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product_id, qty))
        stocks = c.fetchall()

        if len(stocks) < qty:
            conn.close()
            await update.message.reply_text("✅ Payment detected, but stock is low. Admin will deliver shortly.")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Auto-paid but low stock\n🆔 <code>{order_id}</code>\n📄 <code>{proof}</code>",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
            context.user_data.clear()
            return

        c.execute(
            "UPDATE orders SET status = 'DELIVERED', approved_at = ?, delivered_at = ? WHERE order_id = ?",
            (get_current_time(), get_current_time(), order_id)
        )
        c.execute(
            "UPDATE users SET total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?",
            (expected_amount, user_id)
        )

        delivered_items = []
        for stock in stocks:
            c.execute(
                "UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                (user_id, get_current_time(), order_id, stock['stock_id'])
            )
            delivered_items.append(stock['content'])

        c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
        product = c.fetchone()
        conn.commit()
        conn.close()

        delivery_text = (
            f"✅ <b>Payment Automatically Verified!</b>\n\n"
            f"🆔 Order ID: <code>{order_id}</code>\n"
            f"{product['emoji']} <b>{product['name']}</b>\n"
            f"📦 Quantity: {qty}\n\n"
            f"<b>Your Product(s):</b>\n\n"
        )
        for i, item in enumerate(delivered_items, 1):
            delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"
        delivery_text += "⚠️ Keep this information safe!"

        await update.message.reply_text(delivery_text, parse_mode=ParseMode.HTML)
        await send_purchase_announcement(context, product['name'], qty, product['emoji'])
        context.user_data.clear()
        return

    conn.close()
    await update.message.reply_text(
        f"⏳ <b>{proof_type} Submitted</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"📄 {proof_type}: <code>{proof}</code>\n\n"
        f"Auto-check: not confirmed yet.\n"
        f"Status: <b>Pending Admin Approval</b>",
        parse_mode=ParseMode.HTML
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔔 <b>New Payment Pending</b>\n\n"
                f"🆔 <code>{order_id}</code>\n"
                f"👤 <code>{user_id}</code>\n"
                f"💰 {format_money(order['total_amount'])}\n"
                f"📄 {proof_type}: <code>{proof}</code>\n"
                f"🔍 {verification.get('message', 'n/a')}"
            ),
            parse_mode=ParseMode.HTML
        )
    except:
        pass
    context.user_data.clear()


async def cancel_order(update, context):
    query = update.callback_query
    await query.answer()
    order_id = query.data.split("_")[-1]
    user_id = query.from_user.id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ? AND status = 'PENDING'", (order_id, user_id))
    if not c.fetchone():
        conn.close()
        await query.edit_message_text("❌ Order not found or cannot be cancelled.")
        return

    c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()
    context.user_data.clear()
    await query.edit_message_text(f"❌ <b>Order Cancelled</b>\n\n🆔 <code>{order_id}</code>", parse_mode=ParseMode.HTML)


# ====================== WALLET ======================
async def wallet_handler(update, context):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, total_spent FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    balance = row['balance'] if row else 0.0
    total_spent = row['total_spent'] if row else 0.0
    conn.close()

    text = (
        f"💰 <b>Your Wallet</b>\n\n"
        f"💵 Current Balance: <b>{format_money(balance)}</b>\n"
        f"🛒 Total Spent: <b>{format_money(total_spent)}</b>\n\n"
        f"Select an option below:"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Deposit Funds", callback_data="deposit")],
        [InlineKeyboardButton("📜 Wallet History", callback_data="wallet_history")],
        [
            InlineKeyboardButton("📥 Deposit History", callback_data="deposit_history"),
            InlineKeyboardButton("📤 Spending History", callback_data="spending_history")
        ],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]
    if query:
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_history_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    page = 0
    if query.data.startswith("deposit_history_page_"):
        try:
            page = int(query.data.replace("deposit_history_page_", ""))
        except:
            page = 0

    per_page = 5
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM wallet_deposits WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]

    c.execute(
        "SELECT * FROM wallet_deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, per_page, offset)
    )
    deposits = c.fetchall()
    conn.close()

    if not deposits and page == 0:
        text = "📥 <b>Deposit History</b>\n\nYou haven't made any deposits yet."
        keyboard = [
            [InlineKeyboardButton("➕ Deposit Now", callback_data="deposit")],
            [InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    text = f"📥 <b>Deposit History</b> (Page {page + 1}/{total_pages}) — Total: {total}\n\n"

    for dep in deposits:
        status_map = {
            "PENDING": "⏳ Pending",
            "APPROVED": "✅ Approved",
            "REJECTED": "❌ Rejected"
        }
        status_str = status_map.get(dep['status'], dep['status'])
        method_str = (dep['payment_method'] or 'Crypto').upper()

        proof_str = ""
        if dep['txid']:
            tx_short = dep['txid'][:20] + "..." if len(dep['txid']) > 20 else dep['txid']
            proof_str = f"\n🔗 TXID: <code>{tx_short}</code>"
        elif dep['binance_ref']:
            proof_str = f"\n🔗 Ref: <code>{dep['binance_ref']}</code>"

        text += (
            f"🆔 <code>{dep['deposit_id']}</code>\n"
            f"💰 Amount: <b>{format_money(dep['amount'])}</b>\n"
            f"💳 Method: <b>{method_str}</b>\n"
            f"📊 Status: <b>{status_str}</b>\n"
            f"📅 Date: {dep['created_at']}{proof_str}\n"
            f"────────────────────\n\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"deposit_history_page_{page - 1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("More ➡️", callback_data=f"deposit_history_page_{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def spending_history_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    page = 0
    if query.data.startswith("spending_history_page_"):
        try:
            page = int(query.data.replace("spending_history_page_", ""))
        except:
            page = 0

    per_page = 5
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status IN ('DELIVERED', 'APPROVED')",
        (user_id,)
    )
    total = c.fetchone()[0]

    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ? AND o.status IN ('DELIVERED', 'APPROVED')
        ORDER BY o.created_at DESC
        LIMIT ? OFFSET ?
    ''', (user_id, per_page, offset))
    spending = c.fetchall()

    c.execute("SELECT total_spent FROM users WHERE user_id = ?", (user_id,))
    user_row = c.fetchone()
    total_spent_val = user_row['total_spent'] if user_row else 0.0
    conn.close()

    if not spending and page == 0:
        text = "📤 <b>Spending History</b>\n\nYou haven't spent any funds yet."
        keyboard = [
            [InlineKeyboardButton("🛍️ Shop Products", callback_data="shop")],
            [InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    text = (
        f"📤 <b>Spending History</b> (Page {page + 1}/{total_pages})\n"
        f"🛒 Total Spent to Date: <b>{format_money(total_spent_val)}</b>\n\n"
    )

    for item in spending:
        prod_name = item['product_name'] or 'Product'
        emoji = item['emoji'] or '📦'
        method_str = item['payment_method'].upper() if item['payment_method'] else 'WALLET'

        text += (
            f"🆔 <code>{item['order_id']}</code>\n"
            f"{emoji} {prod_name} × {item['quantity']}\n"
            f"💸 Amount: <b>-{format_money(item['total_amount'])}</b>\n"
            f"💳 Method: <b>{method_str}</b>\n"
            f"📅 Date: {item['created_at']}\n"
            f"────────────────────\n\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"spending_history_page_{page - 1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("More ➡️", callback_data=f"spending_history_page_{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def wallet_history_handler(update, context):
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id

    page = 0
    if query and query.data.startswith("wallet_history_page_"):
        try:
            page = int(query.data.replace("wallet_history_page_", ""))
        except Exception:
            page = 0

    per_page = 5
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()

    # User balance and total spent
    c.execute("SELECT balance, total_spent FROM users WHERE user_id = ?", (user_id,))
    user_row = c.fetchone()
    balance = user_row['balance'] if user_row else 0.0
    total_spent_val = user_row['total_spent'] if user_row else 0.0

    # Total approved deposits
    c.execute("SELECT COALESCE(SUM(amount), 0) FROM wallet_deposits WHERE user_id = ? AND status = 'APPROVED'", (user_id,))
    total_deposited = c.fetchone()[0]

    # Fetch all deposits of this user
    c.execute(
        "SELECT deposit_id, amount, payment_method, status, created_at FROM wallet_deposits WHERE user_id = ?",
        (user_id,)
    )
    deposits = c.fetchall()

    # Fetch all orders / spending of this user
    c.execute('''
        SELECT o.order_id, o.total_amount, o.payment_method, o.status, o.created_at, o.quantity, p.name as product_name, p.emoji
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ? AND o.status IN ('DELIVERED', 'APPROVED')
    ''', (user_id,))
    spends = c.fetchall()
    conn.close()

    status_map = {
        "PENDING": "⏳ Pending",
        "APPROVED": "✅ Approved",
        "DELIVERED": "✅ Delivered",
        "REJECTED": "❌ Rejected",
        "CANCELLED": "❌ Cancelled"
    }

    all_tx = []
    for dep in deposits:
        all_tx.append({
            'type': 'DEPOSIT',
            'id': dep['deposit_id'],
            'amount': dep['amount'],
            'method': (dep['payment_method'] or 'Crypto').upper(),
            'status': status_map.get(dep['status'], dep['status']),
            'date': dep['created_at'] or '',
            'detail': ''
        })

    for sp in spends:
        prod_name = sp['product_name'] or 'Product'
        emoji = sp['emoji'] or '📦'
        all_tx.append({
            'type': 'SPEND',
            'id': sp['order_id'],
            'amount': sp['total_amount'],
            'method': (sp['payment_method'] or 'WALLET').upper(),
            'status': status_map.get(sp['status'], sp['status']),
            'date': sp['created_at'] or '',
            'detail': f"{emoji} {prod_name} × {sp['quantity']}"
        })

    # Sort by date descending (newest first)
    all_tx.sort(key=lambda x: str(x['date']), reverse=True)
    total = len(all_tx)

    if not all_tx and page == 0:
        text = (
            f"📜 <b>Wallet History</b>\n\n"
            f"💵 Current Balance: <b>{format_money(balance)}</b>\n\n"
            f"You don't have any deposit or spending activity yet."
        )
        keyboard = [
            [InlineKeyboardButton("➕ Deposit Funds", callback_data="deposit")],
            [InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")]
        ]
        if query:
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    page_items = all_tx[offset : offset + per_page]

    text = (
        f"📜 <b>Wallet History</b> (Page {page + 1}/{total_pages}) · Total: {total}\n"
        f"💵 Balance: <b>{format_money(balance)}</b> | 📥 In: <b>{format_money(total_deposited)}</b> | 📤 Out: <b>{format_money(total_spent_val)}</b>\n"
        f"────────────────────\n\n"
    )

    for item in page_items:
        if item['type'] == 'DEPOSIT':
            text += (
                f"🟢 <b>Deposit</b>: <b>+{format_money(item['amount'])}</b>\n"
                f"🆔 <code>{item['id']}</code>\n"
                f"💳 Method: <b>{item['method']}</b> | {item['status']}\n"
                f"📅 {item['date']}\n"
                f"────────────────────\n\n"
            )
        else:
            text += (
                f"🔴 <b>Spend</b>: <b>-{format_money(item['amount'])}</b>\n"
                f"🆔 <code>{item['id']}</code>\n"
                f"📦 {item['detail']}\n"
                f"💳 Method: <b>{item['method']}</b> | {item['status']}\n"
                f"📅 {item['date']}\n"
                f"────────────────────\n\n"
            )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"wallet_history_page_{page - 1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("More ➡️", callback_data=f"wallet_history_page_{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton("📥 Deposit History", callback_data="deposit_history"),
        InlineKeyboardButton("📤 Spending History", callback_data="spending_history")
    ])
    keyboard.append([InlineKeyboardButton("🔙 Back to Wallet", callback_data="wallet")])

    if query:
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_start(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for'] = "deposit_amount"
    text = "➕ <b>Deposit Funds</b>\n\nPlease enter the amount (example: `5` or `10.5`)"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="wallet")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def handle_deposit_amount(update, context):
    if not update.message or not update.message.text:
        return
    if context.user_data.get('waiting_for') != "deposit_amount":
        return

    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Invalid amount. Please send a valid number.")
        return

    context.user_data['deposit_amount'] = amount
    context.user_data['waiting_for'] = None

    text = f"💰 Deposit Amount: <b>{format_money(amount)}</b>\n\nSelect payment method:"
    keyboard = [
        [InlineKeyboardButton("🪙 BEP20", callback_data="deposit_bep20")],
        [InlineKeyboardButton("🔵 Binance Pay", callback_data="deposit_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]
    ]
    await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_bep20(update, context):
    query = update.callback_query
    await query.answer()
    amount = context.user_data.get('deposit_amount')
    if not amount:
        await query.edit_message_text("❌ Session expired.")
        return

    deposit_id = generate_deposit_id()
    context.user_data['pending_deposit_id'] = deposit_id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO wallet_deposits (deposit_id, user_id, amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (deposit_id, query.from_user.id, amount, "bep20", "PENDING", get_current_time())
    )
    conn.commit()
    conn.close()

    text = (
        f"🪙 <b>BEP20 Deposit</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"💰 Amount: <b>{format_money(amount)} USDT</b>\n\n"
        f"<code>{BEP20_ADDRESS}</code>\n\n"
        f"After payment, submit your TXID."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Submit TXID", callback_data="deposit_submit_txid")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_binance(update, context):
    query = update.callback_query
    await query.answer()
    amount = context.user_data.get('deposit_amount')
    if not amount:
        await query.edit_message_text("❌ Session expired.")
        return

    deposit_id = generate_deposit_id()
    context.user_data['pending_deposit_id'] = deposit_id

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO wallet_deposits (deposit_id, user_id, amount, payment_method, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (deposit_id, query.from_user.id, amount, "binance", "PENDING", get_current_time())
    )
    conn.commit()
    conn.close()

    text = (
        f"🔵 <b>Binance Deposit</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"💰 Amount: <b>{format_money(amount)}</b>\n\n"
        f"Pay to: <code>{BINANCE_PAY_ID}</code>\n\n"
        f"After payment, submit your Reference."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Submit Reference", callback_data="deposit_submit_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wallet")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def deposit_ask_txid(update, context):
    query = update.callback_query
    await query.answer()
    deposit_id = context.user_data.get('pending_deposit_id')
    if not deposit_id:
        await query.edit_message_text("❌ Session expired.")
        return
    context.user_data['waiting_for'] = "deposit_txid"
    await query.edit_message_text(
        f"📝 Submit TXID\n\n🆔 Deposit ID: <code>{deposit_id}</code>\n\nSend your TXID now.",
        parse_mode=ParseMode.HTML
    )


async def deposit_ask_binance(update, context):
    query = update.callback_query
    await query.answer()
    deposit_id = context.user_data.get('pending_deposit_id')
    if not deposit_id:
        await query.edit_message_text("❌ Session expired.")
        return
    context.user_data['waiting_for'] = "deposit_binance_ref"
    await query.edit_message_text(
        f"📝 Submit Binance Reference\n\n🆔 Deposit ID: <code>{deposit_id}</code>\n\nSend your reference now.",
        parse_mode=ParseMode.HTML
    )


async def handle_deposit_proof(update, context):
    if not update.message or not update.message.text:
        return

    waiting_for = context.user_data.get('waiting_for')
    deposit_id = context.user_data.get('pending_deposit_id')
    if waiting_for not in ["deposit_txid", "deposit_binance_ref"] or not deposit_id:
        return

    proof = update.message.text.strip()
    user_id = update.effective_user.id

    if len(proof) < 8:
        await update.message.reply_text("❌ Invalid proof.")
        return

    if is_txid_already_used(proof):
        await update.message.reply_text("❌ <b>This TXID / Reference was already used.</b>", parse_mode=ParseMode.HTML)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE deposit_id = ? AND user_id = ? AND status = 'PENDING'", (deposit_id, user_id))
    deposit = c.fetchone()

    if not deposit:
        conn.close()
        await update.message.reply_text("❌ Deposit not found.")
        context.user_data.clear()
        return

    if waiting_for == "deposit_txid":
        c.execute("UPDATE wallet_deposits SET txid = ? WHERE deposit_id = ?", (proof, deposit_id))
        proof_type = "TXID"
    else:
        c.execute("UPDATE wallet_deposits SET binance_ref = ? WHERE deposit_id = ?", (proof, deposit_id))
        proof_type = "Binance Reference"

    conn.commit()

    expected_amount = float(deposit['amount'])
    verification = await check_binance_deposit(proof, expected_amount)

    if verification.get("found"):
        c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (expected_amount, user_id))
        c.execute(
            "UPDATE wallet_deposits SET status = 'APPROVED', approved_at = ? WHERE deposit_id = ?",
            (get_current_time(), deposit_id)
        )
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance = c.fetchone()['balance']
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ <b>Deposit Automatically Verified!</b>\n\n"
            f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
            f"💰 Amount: <b>{format_money(expected_amount)}</b>\n"
            f"New Wallet Balance: <b>{format_money(new_balance)}</b>",
            parse_mode=ParseMode.HTML
        )
        context.user_data.clear()
        return

    conn.close()
    await update.message.reply_text(
        f"⏳ <b>{proof_type} submitted!</b>\n\n"
        f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
        f"Auto-check: not confirmed yet.\n"
        f"Waiting for admin approval.",
        parse_mode=ParseMode.HTML
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"💵 New Wallet Deposit\n"
                f"🆔 <code>{deposit_id}</code>\n"
                f"💰 {format_money(deposit['amount'])}\n"
                f"📄 {proof_type}: <code>{proof}</code>\n"
                f"🔍 {verification.get('message', 'n/a')}"
            ),
            parse_mode=ParseMode.HTML
        )
    except:
        pass
    context.user_data.clear()


print("✅ Step 4 completed!")
print("➡️ Reply with 'next' for Step 5 (Admin Panel)")
# ============================================
# STEP 5: Admin Panel
# ============================================

async def admin_command(update, context):
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id or not is_admin(user_id):
        if update.message:
            await update.message.reply_text("❌ Access denied. Only admins can use this command.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Access denied.", show_alert=True)
        return
    await show_admin_panel(update, context)


async def show_admin_panel(update, context):
    text = f"🛡️ <b>{STORE_NAME} – Admin Panel</b>\n\nChoose an option below:"
    keyboard = [
        [
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
            InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton("📦 Stock Menu", callback_data="admin_stock_menu"),
            InlineKeyboardButton("➕ Add Product", callback_data="admin_add_product")
        ],
        [
            InlineKeyboardButton("✏️ Edit Product", callback_data="admin_edit_product"),
            InlineKeyboardButton("🗑️ Remove Product", callback_data="admin_remove_product")
        ],
        [
            InlineKeyboardButton("💳 Direct Payments", callback_data="admin_direct_payments"),
            InlineKeyboardButton("💵 Wallet Deposits", callback_data="admin_wallet_deposits")
        ],
        [
            InlineKeyboardButton("📋 Order History", callback_data="admin_order_history"),
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
        ],
        [
            InlineKeyboardButton("🔥 1-Click Stock Broadcast", callback_data="admin_broadcast_stock")
        ],
        [
            InlineKeyboardButton("❌ Close Panel", callback_data="admin_close")
        ]
    ]
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        await update.callback_query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
    elif update.message:
        await update.message.reply_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )


async def admin_panel_callback(update, context):
    await update.callback_query.answer()
    if not is_admin(update.callback_query.from_user.id):
        return
    await show_admin_panel(update, context)


async def admin_close(update, context):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🛡️ Admin panel closed. Send /admin anytime to reopen.")


async def admin_stats(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    total_orders = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders WHERE status = 'PENDING'")
    pending = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status IN ('APPROVED', 'DELIVERED')")
    revenue = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM stock WHERE status = 'AVAILABLE'")
    stock = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM wallet_deposits WHERE status = 'PENDING'")
    pending_dep = c.fetchone()[0]
    conn.close()

    text = (
        f"📊 <b>Statistics – {STORE_NAME}</b>\n\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"📦 Total Orders: <b>{total_orders}</b>\n"
        f"⏳ Pending Orders: <b>{pending}</b>\n"
        f"💵 Pending Deposits: <b>{pending_dep}</b>\n"
        f"💰 Total Revenue: <b>{format_money(revenue)}</b>\n"
        f"📦 Available Stock: <b>{stock}</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


# ---------- USERS ----------
async def admin_users(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    page = 0
    if query.data.startswith("admin_users_page_"):
        page = int(query.data.replace("admin_users_page_", ""))

    per_page = 15
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute('''
        SELECT user_id, full_name, username, balance, total_orders
        FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?
    ''', (per_page, offset))
    users = c.fetchall()
    conn.close()

    text = f"👥 <b>All Users</b> (Page {page+1}) · Total: {total}\n\n"
    keyboard = [[InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user")]]

    for u in users:
        name = u['full_name'] or "No Name"
        uname = f"@{u['username']}" if u['username'] else "—"
        btn = f"{name[:18]} | {uname} | 💰{format_money(u['balance'])}"
        keyboard.append([InlineKeyboardButton(btn[:60], callback_data=f"view_user_{u['user_id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_page_{page-1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def view_user_profile(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    user_id = int(query.data.replace("view_user_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()

    if not user:
        await query.answer("User not found", show_alert=True)
        return

    banned = "Yes 🚫" if user['is_banned'] else "No"
    text = (
        f"👤 <b>User Profile</b>\n\n"
        f"🆔 <code>{user['user_id']}</code>\n"
        f"👤 {user['full_name'] or 'N/A'}\n"
        f"🔗 @{user['username'] or 'N/A'}\n"
        f"💰 Balance: <b>{format_money(user['balance'])}</b>\n"
        f"💸 Spent: <b>{format_money(user['total_spent'])}</b>\n"
        f"📦 Orders: <b>{user['total_orders']}</b>\n"
        f"🚫 Banned: <b>{banned}</b>"
    )
    keyboard = [
        [InlineKeyboardButton(f"📦 View Orders ({user['total_orders']})", callback_data=f"admin_user_orders_{user_id}_0")],
        [InlineKeyboardButton("➕ Add Balance", callback_data=f"add_balance_{user_id}"),
         InlineKeyboardButton("➖ Remove Balance", callback_data=f"remove_balance_{user_id}")],
        [InlineKeyboardButton("🚫 Ban", callback_data=f"ban_user_{user_id}"),
         InlineKeyboardButton("✅ Unban", callback_data=f"unban_user_{user_id}")],
        [InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def ban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("ban_user_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await query.answer("🚫 User banned!", show_alert=True)
    query.data = f"view_user_{user_id}"
    await view_user_profile(update, context)


async def unban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("unban_user_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ User unbanned!", show_alert=True)
    query.data = f"view_user_{user_id}"
    await view_user_profile(update, context)


async def add_balance_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("add_balance_", ""))
    context.user_data['admin_action'] = "add_balance"
    context.user_data['target_user_id'] = user_id
    context.user_data['waiting_for'] = "admin_balance_amount"
    await query.edit_message_text(
        f"➕ <b>Add Balance</b>\n\nUser: <code>{user_id}</code>\n\nSend the amount:",
        parse_mode=ParseMode.HTML
    )


async def remove_balance_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    user_id = int(query.data.replace("remove_balance_", ""))
    context.user_data['admin_action'] = "remove_balance"
    context.user_data['target_user_id'] = user_id
    context.user_data['waiting_for'] = "admin_balance_amount"
    await query.edit_message_text(
        f"➖ <b>Remove Balance</b>\n\nUser: <code>{user_id}</code>\n\nSend the amount:",
        parse_mode=ParseMode.HTML
    )


async def handle_admin_balance_amount(update, context):
    if context.user_data.get('waiting_for') != "admin_balance_amount":
        return
    if not is_admin(update.effective_user.id):
        return

    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please send a valid number.")
        return

    user_id = context.user_data.get('target_user_id')
    action = context.user_data.get('admin_action')

    conn = get_db_connection()
    c = conn.cursor()
    if action == "add_balance":
        c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (amount, user_id))
        msg = f"added <b>{format_money(amount)}</b> to"
    else:
        c.execute("UPDATE users SET balance = MAX(COALESCE(balance, 0) - ?, 0) WHERE user_id = ?", (amount, user_id))
        msg = f"removed <b>{format_money(amount)}</b> from"

    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()['balance']
    conn.commit()
    conn.close()
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ Successfully {msg} user <code>{user_id}</code>\n\nNew Balance: <b>{format_money(new_balance)}</b>",
        parse_mode=ParseMode.HTML
    )


async def admin_search_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "admin_search_user"
    await query.edit_message_text(
        "🔍 <b>Search User</b>\n\nSend User ID or Username (without @):",
        parse_mode=ParseMode.HTML
    )


async def handle_admin_search(update, context):
    if context.user_data.get('waiting_for') != "admin_search_user":
        return
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text.strip().lstrip("@")
    conn = get_db_connection()
    c = conn.cursor()
    if text.isdigit():
        c.execute("SELECT * FROM users WHERE user_id = ?", (int(text),))
    else:
        c.execute("SELECT * FROM users WHERE username = ?", (text,))
    user = c.fetchone()
    conn.close()
    context.user_data.clear()

    if not user:
        await update.message.reply_text("❌ User not found.")
        return

    banned = "Yes 🚫" if user['is_banned'] else "No"
    uid = user['user_id']
    profile = (
        f"👤 <b>User Profile</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 {user['full_name'] or 'N/A'}\n"
        f"🔗 @{user['username'] or 'N/A'}\n"
        f"💰 <b>{format_money(user['balance'])}</b>\n"
        f"📦 Orders: <b>{user['total_orders']}</b>\n"
        f"🚫 Banned: {banned}"
    )
    keyboard = [
        [InlineKeyboardButton(f"📦 View Orders ({user['total_orders']})", callback_data=f"admin_user_orders_{uid}_0")],
        [InlineKeyboardButton("➕ Add Balance", callback_data=f"add_balance_{uid}"),
         InlineKeyboardButton("➖ Remove", callback_data=f"remove_balance_{uid}")],
        [InlineKeyboardButton("🚫 Ban", callback_data=f"ban_user_{uid}"),
         InlineKeyboardButton("✅ Unban", callback_data=f"unban_user_{uid}")],
        [InlineKeyboardButton("🔙 Users", callback_data="admin_users")]
    ]
    await update.message.reply_text(profile, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_user_orders(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    data_parts = query.data.split("_")
    try:
        user_id = int(data_parts[3])
        page = int(data_parts[4]) if len(data_parts) > 4 else 0
    except (IndexError, ValueError):
        await query.answer("Invalid request", show_alert=True)
        return

    per_page = 5
    offset = page * per_page

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    target_user = c.fetchone()
    if not target_user:
        conn.close()
        await query.answer("User not found", show_alert=True)
        return

    c.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,))
    total_orders = c.fetchone()[0]

    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        LIMIT ? OFFSET ?
    ''', (user_id, per_page, offset))
    orders = c.fetchall()

    user_name = target_user['full_name'] or (f"@{target_user['username']}" if target_user['username'] else str(user_id))
    total_pages = (total_orders + per_page - 1) // per_page if total_orders > 0 else 1

    if not orders and page == 0:
        conn.close()
        text = (
            f"📋 <b>Order History — {user_name}</b>\n"
            f"🆔 User ID: <code>{user_id}</code>\n\n"
            f"ℹ️ This user has not placed any orders yet."
        )
        keyboard = [
            [InlineKeyboardButton("🔙 Back to User Profile", callback_data=f"view_user_{user_id}")],
            [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    text = (
        f"📋 <b>Order History — {user_name}</b>\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Total Orders: <b>{total_orders}</b> (Page {page + 1}/{total_pages})\n\n"
    )

    for order in orders:
        status_emoji = {
            "PENDING": "⏳",
            "APPROVED": "✅",
            "DELIVERED": "✅",
            "REJECTED": "❌",
            "CANCELLED": "🚫"
        }.get(order['status'], "❓")

        p_name = order['product_name'] or f"Product #{order['product_id']}"
        p_emoji = order['emoji'] or "📦"
        payment_info = (order['payment_method'] or 'N/A').upper()
        proof = order['txid'] or order['binance_ref'] or ""

        text += (
            f"{status_emoji} <b>Order ID:</b> <code>{order['order_id']}</code>\n"
            f"   {p_emoji} <b>{p_name} × {order['quantity']}</b>\n"
            f"   💰 <b>Amount:</b> {format_money(order['total_amount'])} | <b>Method:</b> {payment_info}\n"
            f"   📅 <b>Date:</b> {order['created_at']}\n"
            f"   📊 <b>Status:</b> <b>{order['status']}</b>\n"
        )
        if proof:
            text += f"   📄 <b>TxID/Ref:</b> <code>{proof}</code>\n"

        # Show delivered credentials if any
        if order['status'] in ["APPROVED", "DELIVERED"]:
            c.execute(
                "SELECT content FROM stock WHERE order_id = ? AND status = 'DELIVERED'",
                (order['order_id'],)
            )
            items = c.fetchall()
            if items:
                text += "   🔑 <b>Delivered Items:</b>\n"
                for item in items:
                    text += f"   • <code>{item['content']}</code>\n"

        text += "────────────────────────\n"

    conn.close()

    if len(text) > 3900:
        text = text[:3800] + "\n\n...(truncated)"

    # Pagination navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_user_orders_{user_id}_{page-1}"))
    if offset + per_page < total_orders:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_user_orders_{user_id}_{page+1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton("🔙 Back to User Profile", callback_data=f"view_user_{user_id}"),
        InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")
    ])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


# ---------- STOCK ----------
async def admin_stock_menu(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    keyboard = [
        [
            InlineKeyboardButton("📥 Add Stock", callback_data="admin_add_stock"),
            InlineKeyboardButton("🗑️ Clear Stock", callback_data="admin_clear_stock")
        ],
        [
            InlineKeyboardButton("📊 View Stock Levels", callback_data="admin_view_stock")
        ],
        [
            InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")
        ]
    ]
    await query.edit_message_text("📦 <b>Stock Management</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_view_stock(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()

    text = "📊 <b>Current Stock Levels</b>\n\n"
    for p in products:
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (p['product_id'],))
        available = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'DELIVERED'", (p['product_id'],))
        delivered = c.fetchone()[0]
        text += f"{p['emoji']} <b>{p['name']}</b>\n   ✅ Available: {available} | 📤 Delivered: {delivered}\n\n"
    conn.close()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_add_stock_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()

    if not products:
        await query.edit_message_text("❌ No products yet. Add a product first.", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    keyboard = [[InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"addstock_{p['product_id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")])
    await query.edit_message_text("📥 <b>Add Stock</b>\n\nSelect product:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_add_stock_product(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[1])
    context.user_data['add_stock_product_id'] = product_id
    context.user_data['waiting_for'] = "stock_file"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.close()

    text = (
        f"📥 <b>Add Stock</b>\n\n"
        f"Product: {product['emoji']} <b>{product['name']}</b>\n\n"
        f"Upload a <b>.txt</b> file (one account/item per line)."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock_menu")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def handle_stock_file(update, context):
    if not update.message or not update.message.document:
        return
    if context.user_data.get('waiting_for') != "stock_file":
        return
    if not is_admin(update.effective_user.id):
        return

    document = update.message.document
    if not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ Please upload a .txt file only.")
        return

    product_id = context.user_data.get('add_stock_product_id')
    if not product_id:
        await update.message.reply_text("❌ Session expired.")
        return

    file = await context.bot.get_file(document.file_id)
    content = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    if not lines:
        await update.message.reply_text("❌ File is empty.")
        return

    conn = get_db_connection()
    c = conn.cursor()
    added = 0
    for line in lines:
        c.execute("INSERT INTO stock (product_id, content, status) VALUES (?, ?, 'AVAILABLE')", (product_id, line))
        added += 1

    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    total_available = c.fetchone()[0]
    c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>Stock Added!</b>\n\n"
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"➕ Added: <b>{added}</b>\n"
        f"📦 Available now: <b>{total_available}</b>",
        parse_mode=ParseMode.HTML
    )
    await send_restock_announcement(context, dict(product), added, total_available)


async def admin_clear_stock_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()

    keyboard = [[InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"clearstock_{p['product_id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_stock_menu")])
    await query.edit_message_text("🗑️ <b>Clear Stock</b>\n\nSelect product:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_clear_stock_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    count = c.fetchone()[0]
    conn.close()

    text = f"⚠️ <b>Confirm Clear</b>\n\n{product['emoji']} <b>{product['name']}</b>\nWill delete: <b>{count}</b> available items"
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Clear", callback_data=f"confirm_clear_{product_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_stock_menu")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_clear_stock_execute(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (product_id,))
    deleted = c.rowcount
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()

    await query.edit_message_text(
        f"🗑️ Cleared <b>{deleted}</b> items from {product['emoji']} <b>{product['name']}</b>",
        parse_mode=ParseMode.HTML
    )


print("✅ Step 5 (Part 1) completed!")
print("➡️ Reply with 'next' for the remaining Admin features + final Step 6")
# ============================================
# STEP 5 Continued: Add / Edit / Remove Product + Payments + Broadcast
# ============================================

async def admin_add_product_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "new_product_name"
    await query.edit_message_text(
        "➕ <b>Add New Product</b>\n\nSend <b>Product Name</b>\nExample: <code>Netflix 4K</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        parse_mode=ParseMode.HTML
    )


async def handle_new_product(update, context):
    if not update.message or not update.message.text or not is_admin(update.effective_user.id):
        return

    waiting = context.user_data.get('waiting_for')
    text = update.message.text.strip()

    if waiting == "new_product_name":
        context.user_data['new_product_name'] = text
        context.user_data['waiting_for'] = "new_product_duration"
        await update.message.reply_text("⏱ Send <b>Duration</b>\nExample: <code>1 Month</code>", parse_mode=ParseMode.HTML)

    elif waiting == "new_product_duration":
        context.user_data['new_product_duration'] = text
        context.user_data['waiting_for'] = "new_product_price"
        await update.message.reply_text("💰 Send <b>Price</b>\nExample: <code>1.50</code>", parse_mode=ParseMode.HTML)

    elif waiting == "new_product_price":
        try:
            price = float(text)
            if price <= 0:
                raise ValueError
        except:
            await update.message.reply_text("❌ Invalid price.")
            return
        context.user_data['new_product_price'] = price
        context.user_data['waiting_for'] = "new_product_emoji"
        await update.message.reply_text("✨ Send an <b>Emoji</b>\nExample: <code>📺</code>", parse_mode=ParseMode.HTML)

    elif waiting == "new_product_emoji":
        name = context.user_data.get('new_product_name')
        duration = context.user_data.get('new_product_duration')
        price = context.user_data.get('new_product_price')
        emoji = text

        description = f"✅ Premium digital access\n✅ Validity: {duration}\n✅ Instant delivery after payment"

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO products (name, duration, price, emoji, is_active, description) VALUES (?, ?, ?, ?, 1, ?)",
            (name, duration, price, emoji, description)
        )
        product_id = c.lastrowid
        conn.commit()
        conn.close()
        context.user_data.clear()

        await update.message.reply_text(
            f"✅ <b>Product Added!</b>\n\n{emoji} <b>{name}</b>\n⏱ {duration}\n💰 {format_money(price)}",
            parse_mode=ParseMode.HTML
        )
        await send_new_product_announcement(context, name, duration, price, emoji, product_id)


async def admin_edit_product_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT product_id, name, emoji, price FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()

    if not products:
        await query.edit_message_text("❌ No products.", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['name']} | {format_money(p['price'])}", 
                              callback_data=f"editprod_{p['product_id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text("✏️ <b>Edit Product</b>\n\nSelect product:", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_edit_product_menu(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.replace("editprod_", ""))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    p = c.fetchone()
    conn.close()

    if not p:
        await query.edit_message_text("❌ Not found.")
        return

    context.user_data['edit_product_id'] = product_id
    text = (
        f"✏️ <b>Edit Product</b>\n\n"
        f"{p['emoji']} <b>{p['name']}</b>\n"
        f"⏱ {p['duration']}\n"
        f"💰 {format_money(p['price'])}\n\n"
        f"What do you want to edit?"
    )
    keyboard = [
        [InlineKeyboardButton("Name", callback_data="editfield_name"),
         InlineKeyboardButton("Duration", callback_data="editfield_duration")],
        [InlineKeyboardButton("Price", callback_data="editfield_price"),
         InlineKeyboardButton("Emoji", callback_data="editfield_emoji")],
        [InlineKeyboardButton("Description", callback_data="editfield_description")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_edit_product")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_edit_field_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    field = query.data.replace("editfield_", "")
    context.user_data['edit_field'] = field
    context.user_data['waiting_for'] = "edit_product_field"
    await query.edit_message_text(f"Send new <b>{field}</b> now:", parse_mode=ParseMode.HTML)


async def handle_edit_product_field(update, context):
    if context.user_data.get('waiting_for') != "edit_product_field":
        return
    if not is_admin(update.effective_user.id):
        return

    product_id = context.user_data.get('edit_product_id')
    field = context.user_data.get('edit_field')
    value = update.message.text.strip()

    if field == "price":
        try:
            value = float(value)
            if value <= 0:
                raise ValueError
        except:
            await update.message.reply_text("❌ Invalid price.")
            return

    allowed = {"name", "duration", "price", "emoji", "description"}
    if field not in allowed:
        await update.message.reply_text("❌ Invalid field.")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"UPDATE products SET {field} = ? WHERE product_id = ?", (value, product_id))
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(f"✅ Updated <b>{field}</b> successfully.", parse_mode=ParseMode.HTML)


async def admin_remove_product_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY product_id")
    products = c.fetchall()
    conn.close()

    if not products:
        await query.edit_message_text("❌ No products.", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"remove_product_{p['product_id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text("🗑️ <b>Remove Product</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_remove_product_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ?", (product_id,))
    stock_count = c.fetchone()[0]
    conn.close()

    text = f"⚠️ Delete {product['emoji']} <b>{product['name']}</b>?\nStock items: <b>{stock_count}</b>"
    keyboard = [
        [InlineKeyboardButton("✅ Delete Permanently", callback_data=f"confirm_remove_{product_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_remove_product")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_remove_product_execute(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[-1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    c.execute("DELETE FROM stock WHERE product_id = ?", (product_id,))
    c.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
    conn.commit()
    conn.close()

    await query.edit_message_text(f"🗑️ Deleted {product['emoji']} <b>{product['name']}</b>", parse_mode=ParseMode.HTML)


# ---------- DIRECT PAYMENTS ----------
async def admin_direct_payments(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji 
        FROM orders o JOIN products p ON o.product_id = p.product_id
        WHERE o.status = 'PENDING' AND o.payment_method IN ('bep20', 'binance')
        ORDER BY o.created_at ASC LIMIT 10
    ''')
    pending = c.fetchall()
    conn.close()

    if not pending:
        await query.edit_message_text("✅ No pending direct payments.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode=ParseMode.HTML)
        return

    text = f"💳 <b>Pending Payments</b> ({len(pending)})\n\n"
    keyboard = []
    for order in pending:
        proof = order['txid'] or order['binance_ref'] or "Not submitted"
        text += (
            f"🆔 <code>{order['order_id']}</code>\n"
            f"{order['emoji']} {order['product_name']} × {order['quantity']}\n"
            f"💰 {format_money(order['total_amount'])}\n"
            f"📄 <code>{proof}</code>\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ {order['order_id'][-6:]}", callback_data=f"approve_order_{order['order_id']}"),
            InlineKeyboardButton(f"❌ {order['order_id'][-6:]}", callback_data=f"reject_order_{order['order_id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def approve_order(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    order_id = query.data.replace("approve_order_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id = ? AND status = 'PENDING'", (order_id,))
    order = c.fetchone()

    if not order:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return

    product_id = order['product_id']
    qty = order['quantity']
    user_id = order['user_id']

    c.execute("SELECT stock_id, content FROM stock WHERE product_id = ? AND status = 'AVAILABLE' LIMIT ?", (product_id, qty))
    stocks = c.fetchall()

    if len(stocks) < qty:
        conn.close()
        await query.answer("Not enough stock!", show_alert=True)
        return

    c.execute("UPDATE orders SET status = 'DELIVERED', approved_at = ?, delivered_at = ? WHERE order_id = ?",
              (get_current_time(), get_current_time(), order_id))
    c.execute("UPDATE users SET total_spent = total_spent + ?, total_orders = total_orders + 1 WHERE user_id = ?",
              (order['total_amount'], user_id))

    delivered_items = []
    for stock in stocks:
        c.execute("UPDATE stock SET status = 'DELIVERED', reserved_by = ?, delivered_at = ?, order_id = ? WHERE stock_id = ?",
                  (user_id, get_current_time(), order_id, stock['stock_id']))
        delivered_items.append(stock['content'])

    c.execute("SELECT name, emoji FROM products WHERE product_id = ?", (product_id,))
    product = c.fetchone()
    conn.commit()
    conn.close()

    delivery_text = (
        f"✅ <b>Payment Approved!</b>\n\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"{product['emoji']} <b>{product['name']}</b>\n"
        f"📦 Quantity: {qty}\n\n"
        f"<b>Your Product(s):</b>\n\n"
    )
    for i, item in enumerate(delivered_items, 1):
        delivery_text += f"<b>#{i}</b>\n<code>{item}</code>\n\n"

    try:
        await context.bot.send_message(chat_id=user_id, text=delivery_text, parse_mode=ParseMode.HTML)
    except:
        pass

    await send_purchase_announcement(context, product['name'], qty, product['emoji'])
    await query.answer("✅ Approved!", show_alert=True)
    await admin_direct_payments(update, context)


async def reject_order(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    order_id = query.data.replace("reject_order_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM orders WHERE order_id = ? AND status = 'PENDING'", (order_id,))
    order = c.fetchone()

    if not order:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return

    c.execute("UPDATE orders SET status = 'REJECTED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

    try:
        await context.bot.send_message(
            chat_id=order['user_id'],
            text=f"❌ Payment Rejected\n🆔 <code>{order_id}</code>\nSupport: {SUPPORT_USERNAME}",
            parse_mode=ParseMode.HTML
        )
    except:
        pass

    await query.answer("❌ Rejected", show_alert=True)
    await admin_direct_payments(update, context)


# ---------- WALLET DEPOSITS ----------
async def admin_wallet_deposits(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 10")
    pending = c.fetchall()
    conn.close()

    if not pending:
        await query.edit_message_text("✅ No pending deposits.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
            parse_mode=ParseMode.HTML)
        return

    text = "💵 <b>Pending Deposits</b>\n\n"
    keyboard = []
    for dep in pending:
        proof = dep['txid'] or dep['binance_ref'] or "Not submitted"
        text += f"🆔 <code>{dep['deposit_id']}</code>\n💰 {format_money(dep['amount'])}\n📄 <code>{proof}</code>\n\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ {dep['deposit_id'][-6:]}", callback_data=f"approve_deposit_{dep['deposit_id']}"),
            InlineKeyboardButton(f"❌ {dep['deposit_id'][-6:]}", callback_data=f"reject_deposit_{dep['deposit_id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def approve_deposit(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    deposit_id = query.data.replace("approve_deposit_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM wallet_deposits WHERE deposit_id = ? AND status = 'PENDING'", (deposit_id,))
    deposit = c.fetchone()

    if not deposit:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return

    user_id = deposit['user_id']
    amount = float(deposit['amount'])

    c.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (amount, user_id))
    c.execute("UPDATE wallet_deposits SET status = 'APPROVED', approved_at = ? WHERE deposit_id = ?",
              (get_current_time(), deposit_id))
    conn.commit()
    conn.close()

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ <b>Deposit Approved!</b>\n\n💰 +{format_money(amount)}",
            parse_mode=ParseMode.HTML
        )
    except:
        pass

    await query.answer("✅ Credited!", show_alert=True)
    await admin_wallet_deposits(update, context)


async def reject_deposit(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    deposit_id = query.data.replace("reject_deposit_", "")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM wallet_deposits WHERE deposit_id = ? AND status = 'PENDING'", (deposit_id,))
    deposit = c.fetchone()

    if not deposit:
        conn.close()
        await query.answer("Already processed.", show_alert=True)
        return

    c.execute("UPDATE wallet_deposits SET status = 'REJECTED' WHERE deposit_id = ?", (deposit_id,))
    conn.commit()
    conn.close()

    try:
        await context.bot.send_message(
            chat_id=deposit['user_id'],
            text=f"❌ Deposit Rejected\nSupport: {SUPPORT_USERNAME}",
            parse_mode=ParseMode.HTML
        )
    except:
        pass

    await query.answer("❌ Rejected", show_alert=True)
    await admin_wallet_deposits(update, context)


# ---------- ORDER HISTORY + BROADCAST ----------
async def admin_order_history(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT o.*, p.name as product_name, p.emoji, u.full_name
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        JOIN users u ON o.user_id = u.user_id
        ORDER BY o.created_at DESC LIMIT 15
    ''')
    orders = c.fetchall()
    conn.close()

    if not orders:
        text = "📋 No orders yet."
    else:
        text = "📋 <b>Recent Orders</b>\n\n"
        for o in orders:
            se = {"PENDING": "⏳", "DELIVERED": "✅", "REJECTED": "❌", "CANCELLED": "🚫"}.get(o['status'], "❓")
            text += f"{se} <code>{o['order_id']}</code>\n{o['emoji']} {o['product_name']} × {o['quantity']}\n💰 {format_money(o['total_amount'])} | {o['status']}\n👤 {o['full_name']}\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_broadcast(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    text = (
        "📢 <b>Broadcast Menu</b>\n\n"
        "Choose an option below:\n\n"
        "🔥 <b>1-Click Stock Broadcast:</b>\n"
        "Automatically broadcasts all available products with live stock and prices to BOTH all bot users and the notification channel in 1 click!\n\n"
        "✍️ <b>Custom Broadcast:</b>\n"
        "Send your own custom announcement message to all users."
    )
    keyboard = [
        [InlineKeyboardButton("🔥 1-Click Stock Broadcast", callback_data="admin_broadcast_stock")],
        [InlineKeyboardButton("✍️ Write Custom Message", callback_data="admin_broadcast_custom")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def admin_broadcast_custom(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['waiting_for'] = "broadcast_message"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    await query.edit_message_text(
        "📢 <b>Custom Broadcast</b>\n\nSend the message you want to broadcast to all users:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def handle_broadcast_message(update, context):
    if context.user_data.get('waiting_for') != "broadcast_message":
        return
    if not is_admin(update.effective_user.id):
        return

    message = update.message.text
    context.user_data.clear()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = c.fetchall()
    conn.close()

    success = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user['user_id'], text=message, parse_mode=ParseMode.HTML)
            success += 1
            await asyncio.sleep(0.04)
        except:
            pass

    await update.message.reply_text(f"✅ Broadcast sent to <b>{success}</b> users.", parse_mode=ParseMode.HTML)


async def admin_broadcast_stock(update, context):
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if not is_admin(user_id):
        if query:
            await query.answer("❌ Access denied.", show_alert=True)
        return

    if context.bot_data.get('broadcasting_in_progress'):
        if query:
            await query.answer("⏳ Broadcast already in progress! Please wait.", show_alert=True)
        return

    context.bot_data['broadcasting_in_progress'] = True

    try:
        if query:
            await query.answer("🚀 Starting 1-click broadcast...", show_alert=False)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY product_id ASC")
        products = c.fetchall()

        stock_items = []
        for p in products:
            c.execute("SELECT COUNT(*) FROM stock WHERE product_id = ? AND status = 'AVAILABLE'", (p['product_id'],))
            avail = c.fetchone()[0]
            if avail > 0:
                stock_items.append((p, avail))

        if not stock_items:
            conn.close()
            msg = "❌ <b>No Available Stock!</b>\n\nThere are currently no active products with available stock to broadcast. Please add stock before broadcasting."
            if query:
                await query.answer("❌ No products in stock!", show_alert=True)
                await query.message.reply_text(msg, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            return

        # Prepare unique, attractive broadcast message with bold text & relevant emojis
        broadcast_lines = [
            "🔥 <b>SELLING FAST — LIVE STOCK UPDATE!</b> ⚡\n",
            "✨ <b>Exclusive Deals Available Right Now!</b>",
            "⚡ <i>Instant Auto-Delivery 24/7 • 100% Genuine & Working</i>\n",
            "━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]

        for p, count in stock_items:
            p_emoji = p['emoji'] if p['emoji'] else "⚡"
            dur_str = f" ({p['duration']})" if p['duration'] else ""
            price_str = format_money(p['price'])
            broadcast_lines.append(
                f"🔥 <b>Selling Fast</b> {p_emoji} <b>{p['name']}{dur_str} × {count} Available : {price_str}</b>"
            )

        broadcast_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        broadcast_lines.append("📦 <b>Limited Quantities — Grab Yours Before It Sells Out!</b>")
        broadcast_lines.append("🚀 <b>Instant Delivery:</b> Immediate credentials delivered right after payment!")
        broadcast_lines.append(f"🏪 <b>Store:</b> <b>{STORE_NAME}</b> | 🎧 <b>Support:</b> {SUPPORT_USERNAME}\n")
        broadcast_lines.append("👇 <b>Tap below to order now:</b>")

        broadcast_text = "\n".join(broadcast_lines)

        # 1. Send to Notification Channel
        channel_status = "Not sent"
        clean_bot = BOT_USERNAME.replace('@', '').strip()
        clean_support = SUPPORT_USERNAME.replace('@', '').strip()
        channel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Now / Open Store", url=f"https://t.me/{clean_bot}?start=shop")],
            [InlineKeyboardButton("🎧 Support", url=f"https://t.me/{clean_support}")]
        ])
        try:
            await context.bot.send_message(
                chat_id=ANNOUNCE_CHANNEL,
                text=broadcast_text,
                reply_markup=channel_keyboard,
                parse_mode=ParseMode.HTML
            )
            channel_status = f"✅ Posted to {ANNOUNCE_CHANNEL}"
        except Exception as e:
            channel_status = f"❌ Failed to post to channel: {e}"
            logger.error(f"Failed to post broadcast to channel {ANNOUNCE_CHANNEL}: {e}")

        # 2. Send to Bot Users
        c.execute("SELECT user_id FROM users WHERE is_banned = 0")
        users = c.fetchall()
        conn.close()

        bot_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Shop Products", callback_data="shop")]
        ])

        status_msg = None
        if query:
            status_msg = await query.message.reply_text(
                f"⏳ <b>Broadcasting stock to {len(users)} users and {ANNOUNCE_CHANNEL}...</b>",
                parse_mode=ParseMode.HTML
            )

        success = 0
        failed = 0
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u['user_id'],
                    text=broadcast_text,
                    reply_markup=bot_keyboard,
                    parse_mode=ParseMode.HTML
                )
                success += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1

        summary = (
            "🎉 <b>1-Click Stock Broadcast Completed!</b>\n\n"
            f"📢 <b>Notification Channel:</b>\n{channel_status}\n\n"
            f"👥 <b>Bot Users Broadcast:</b>\n"
            f"• Total registered: <b>{len(users)}</b>\n"
            f"• ✅ Successfully delivered: <b>{success}</b>\n"
            f"• ⚠️ Failed / Blocked: <b>{failed}</b>\n"
            f"• 📦 Available products featured: <b>{len(stock_items)}</b>"
        )
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]])

        if status_msg:
            try:
                await status_msg.edit_text(summary, reply_markup=back_kb, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=summary, reply_markup=back_kb, parse_mode=ParseMode.HTML)
        elif query:
            await query.message.reply_text(summary, reply_markup=back_kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(summary, reply_markup=back_kb, parse_mode=ParseMode.HTML)

    finally:
        context.bot_data['broadcasting_in_progress'] = False


# ---------- MISSING HELPERS (if not already added) ----------
async def send_purchase_announcement(context, product_name, quantity, emoji):
    try:
        await context.bot.send_message(
            chat_id=ANNOUNCE_CHANNEL,
            text=f"🛒 Someone just bought <b>{quantity}×</b> {emoji} <b>{product_name}</b>!!",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Announce error: {e}")


async def send_new_product_announcement(context, name, duration, price, emoji, product_id):
    try:
        text = (
            f"🆕 <b>New Product Available!</b>\n\n"
            f"{emoji} <b>{name}</b>\n"
            f"⏱ Duration: <b>{duration}</b>\n"
            f"💰 Price: <b>{format_money(price)}</b>\n\n"
            f"Tap Buy Now 👇"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{BOT_USERNAME}?start=buy_{product_id}")
        ]])
        await context.bot.send_message(chat_id=ANNOUNCE_CHANNEL, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"New product announce error: {e}")


async def send_restock_announcement(context, product, added, available):
    try:
        text = (
            f"📦 <b>Restock Alert!</b>\n\n"
            f"{product['emoji']} <b>{product['name']}</b>\n"
            f"➕ Added: <b>{added}</b>\n"
            f"📊 Available now: <b>{available}</b>\n"
            f"💰 Price: <b>{format_money(product['price'])}</b>\n\n"
            f"Tap Buy Now 👇"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{BOT_USERNAME}?start=buy_{product['product_id']}")
        ]])
        await context.bot.send_message(chat_id=ANNOUNCE_CHANNEL, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Restock announce error: {e}")


print("✅ All Admin functions completed!")
# ============================================
# STEP 6: Register All Handlers + Main
# ============================================

async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "🚀 Start / Restart Bot"),
            BotCommand("menu", "🏠 Main Menu"),
            BotCommand("wallet", "💰 Wallet"),
            BotCommand("support", "🎧 Support"),
            BotCommand("help", "ℹ️ Help & Support"),
        ])
        logger.info("Bot commands menu set successfully!")
    except Exception as e:
        logger.warning(f"Could not set default bot commands: {e}")

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "🚀 Start / Restart Bot"),
                BotCommand("menu", "🏠 Main Menu"),
                BotCommand("wallet", "💰 Wallet"),
                BotCommand("support", "🎧 Support"),
                BotCommand("help", "ℹ️ Help & Support"),
                BotCommand("admin", "🛡️ Admin Panel"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )
        logger.info("Admin commands menu set successfully!")
    except Exception as e:
        logger.warning(f"Could not set admin bot commands: {e}")


async def message_router(update, context):
    text = (update.message.text or "").strip().lower() if update.message else ""
    if text in ["start", "/start", "menu", "/menu", "restart", "home", "main menu"]:
        context.user_data.clear()
        await start_command(update, context)
        return
    elif text in ["help", "/help", "support", "/support"]:
        context.user_data.clear()
        await support_handler(update, context)
        return
    elif text in ["wallet", "/wallet"]:
        context.user_data.clear()
        await wallet_handler(update, context)
        return
    elif text in ["wallet history", "/wallethistory"]:
        context.user_data.clear()
        await wallet_history_handler(update, context)
        return
    elif text in ["admin", "/admin"]:
        context.user_data.clear()
        await admin_command(update, context)
        return

    waiting = context.user_data.get('waiting_for')
    if waiting in ["txid", "binance_ref"]:
        await handle_payment_proof(update, context)
    elif waiting == "deposit_amount":
        await handle_deposit_amount(update, context)
    elif waiting in ["deposit_txid", "deposit_binance_ref"]:
        await handle_deposit_proof(update, context)
    elif waiting == "admin_balance_amount":
        await handle_admin_balance_amount(update, context)
    elif waiting == "admin_search_user":
        await handle_admin_search(update, context)
    elif waiting == "broadcast_message":
        await handle_broadcast_message(update, context)
    elif waiting in ["new_product_name", "new_product_duration", "new_product_price", "new_product_emoji"]:
        await handle_new_product(update, context)
    elif waiting == "edit_product_field":
        await handle_edit_product_field(update, context)


def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("support", support_handler))
    application.add_handler(CommandHandler("help", support_handler))
    application.add_handler(CommandHandler("wallet", wallet_handler))
    application.add_handler(CommandHandler("admin", admin_command))

    # Force join
    application.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))

    # Main menu
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(shop_handler, pattern="^shop$"))
    application.add_handler(CallbackQueryHandler(shop_handler, pattern="^shop_page_"))
    application.add_handler(CallbackQueryHandler(profile_handler, pattern="^profile$"))
    application.add_handler(CallbackQueryHandler(wallet_handler, pattern="^wallet$"))
    application.add_handler(CallbackQueryHandler(wallet_history_handler, pattern="^wallet_history$"))
    application.add_handler(CallbackQueryHandler(wallet_history_handler, pattern="^wallet_history_page_"))
    application.add_handler(CallbackQueryHandler(deposit_history_handler, pattern="^deposit_history$"))
    application.add_handler(CallbackQueryHandler(deposit_history_handler, pattern="^deposit_history_page_"))
    application.add_handler(CallbackQueryHandler(spending_history_handler, pattern="^spending_history$"))
    application.add_handler(CallbackQueryHandler(spending_history_handler, pattern="^spending_history_page_"))
    application.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders$"))
    application.add_handler(CallbackQueryHandler(support_handler, pattern="^support$"))
    application.add_handler(CallbackQueryHandler(terms_handler, pattern="^terms$"))

    # Shop flow
    application.add_handler(CallbackQueryHandler(product_selected, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(buy_now_selected, pattern="^buy_now_"))
    application.add_handler(CallbackQueryHandler(quantity_selected, pattern="^qty_"))
    application.add_handler(CallbackQueryHandler(pay_from_wallet, pattern="^pay_wallet$"))
    application.add_handler(CallbackQueryHandler(pay_bep20, pattern="^pay_bep20$"))
    application.add_handler(CallbackQueryHandler(pay_binance, pattern="^pay_binance$"))
    application.add_handler(CallbackQueryHandler(ask_txid, pattern="^submit_txid$"))
    application.add_handler(CallbackQueryHandler(ask_binance_ref, pattern="^submit_binance_ref$"))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order_"))

    # Wallet deposit
    application.add_handler(CallbackQueryHandler(deposit_start, pattern="^deposit$"))
    application.add_handler(CallbackQueryHandler(deposit_bep20, pattern="^deposit_bep20$"))
    application.add_handler(CallbackQueryHandler(deposit_binance, pattern="^deposit_binance$"))
    application.add_handler(CallbackQueryHandler(deposit_ask_txid, pattern="^deposit_submit_txid$"))
    application.add_handler(CallbackQueryHandler(deposit_ask_binance, pattern="^deposit_submit_binance$"))

    # Admin
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_close, pattern="^admin_close$"))
    application.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users$"))
    application.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users_page_"))
    application.add_handler(CallbackQueryHandler(view_user_profile, pattern="^view_user_"))
    application.add_handler(CallbackQueryHandler(ban_user, pattern="^ban_user_"))
    application.add_handler(CallbackQueryHandler(unban_user, pattern="^unban_user_"))
    application.add_handler(CallbackQueryHandler(add_balance_start, pattern="^add_balance_"))
    application.add_handler(CallbackQueryHandler(remove_balance_start, pattern="^remove_balance_"))
    application.add_handler(CallbackQueryHandler(admin_search_user, pattern="^admin_search_user$"))
    application.add_handler(CallbackQueryHandler(admin_user_orders, pattern="^admin_user_orders_"))

    application.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders$"))
    application.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders_page_"))

    application.add_handler(CallbackQueryHandler(admin_stock_menu, pattern="^admin_stock_menu$"))
    application.add_handler(CallbackQueryHandler(admin_view_stock, pattern="^admin_view_stock$"))
    application.add_handler(CallbackQueryHandler(admin_add_stock_start, pattern="^admin_add_stock$"))
    application.add_handler(CallbackQueryHandler(admin_add_stock_product, pattern="^addstock_"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_start, pattern="^admin_clear_stock$"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_confirm, pattern="^clearstock_"))
    application.add_handler(CallbackQueryHandler(admin_clear_stock_execute, pattern="^confirm_clear_"))

    application.add_handler(CallbackQueryHandler(admin_add_product_start, pattern="^admin_add_product$"))
    application.add_handler(CallbackQueryHandler(admin_edit_product_start, pattern="^admin_edit_product$"))
    application.add_handler(CallbackQueryHandler(admin_edit_product_menu, pattern="^editprod_"))
    application.add_handler(CallbackQueryHandler(admin_edit_field_start, pattern="^editfield_"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_start, pattern="^admin_remove_product$"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_confirm, pattern="^remove_product_"))
    application.add_handler(CallbackQueryHandler(admin_remove_product_execute, pattern="^confirm_remove_"))

    application.add_handler(CallbackQueryHandler(admin_direct_payments, pattern="^admin_direct_payments$"))
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_order_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_order_"))
    application.add_handler(CallbackQueryHandler(admin_wallet_deposits, pattern="^admin_wallet_deposits$"))
    application.add_handler(CallbackQueryHandler(approve_deposit, pattern="^approve_deposit_"))
    application.add_handler(CallbackQueryHandler(reject_deposit, pattern="^reject_deposit_"))
    application.add_handler(CallbackQueryHandler(admin_order_history, pattern="^admin_order_history$"))
    application.add_handler(CallbackQueryHandler(admin_broadcast, pattern="^admin_broadcast$"))
    application.add_handler(CallbackQueryHandler(admin_broadcast_stock, pattern="^admin_broadcast_stock$"))
    application.add_handler(CallbackQueryHandler(admin_broadcast_custom, pattern="^admin_broadcast_custom$"))

    # Messages & Documents
    application.add_handler(MessageHandler(filters.Document.ALL, handle_stock_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    print("🚀 DashCart Bot is starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()