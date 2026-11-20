import os
import html
import time
import shutil
import signal
import zipfile
import sqlite3
import subprocess
from io import BytesIO
import qrcode
import telebot
from telebot import types

# ================= CONFIGURATION =================
BOT_TOKEN = "9112885103:AAGgshN_V6Xk5OTEwEMwtSotdA8uylHQT-Y"
ADMIN_PASSWORD = "743405"
UPI_ID = "8016922924@ibl"
SUPPORT_USERNAME = "@ai_brain_box_2"
BASE_DIR = os.path.abspath("hosted_bots")
DB_PATH = "bot_host.db"

os.makedirs(BASE_DIR, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# State management: {user_id: {"action": "...", "data": {...}}}
user_states = {}
running_processes = {}  # {bot_db_id: subprocess.Popen}


# ================= DATABASE SETUP =================
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            plan TEXT DEFAULT 'Free',
            base_limit INTEGER DEFAULT 3,
            extra_files INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bot_name TEXT,
            file_type TEXT,
            work_dir TEXT,
            entry_file TEXT,
            log_path TEXT,
            pid INTEGER DEFAULT 0,
            status TEXT DEFAULT 'stopped'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            invite_link TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sub_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            photo_id TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn


db = init_db()


def db_exec(query, params=(), fetch=False, fetchone=False):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute(query, params)
    res = None
    if fetchone:
        res = c.fetchone()
    elif fetch:
        res = c.fetchall()
    conn.commit()
    conn.close()
    return res


def ensure_user(user):
    u = db_exec("SELECT user_id FROM users WHERE user_id=?", (user.id,), fetchone=True)
    full_name = user.first_name or "User"
    username = user.username or "N/A"
    if not u:
        db_exec(
            "INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (user.id, username, full_name),
        )
    else:
        db_exec(
            "UPDATE users SET username=?, full_name=? WHERE user_id=?",
            (username, full_name, user.id),
        )


def get_user(user_id):
    return db_exec(
        "SELECT user_id, username, full_name, plan, base_limit, extra_files, is_admin FROM users WHERE user_id=?",
        (user_id,),
        fetchone=True,
    )


def get_user_bot_count(user_id):
    res = db_exec("SELECT COUNT(*) FROM bots WHERE user_id=?", (user_id,), fetchone=True)
    return res[0] if res else 0


# ================= KEYBOARDS =================
def user_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🚀 New bot host")
    markup.row("🤖 My bots", "📜 Bots logs")
    markup.row("💎 Subscription")
    markup.row("🆘 Support", "👤 Account")
    markup.row("🔐 Admin login")
    return markup


def admin_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🤖 All Bots", "📁 All Files")
    markup.row("👥 Users", "📩 Sub Requests")
    markup.row("💎 Sub Users", "➕ Add Channel")
    markup.row("⚙️ Manage Channels", "📢 Send Notification")
    markup.row("🎁 Send Extra Plan", "➖ Remove Extra Files")
    markup.row("🔙 Back to User Panel")
    return markup


# ================= CHANNEL VERIFICATION =================
def check_user_joined(user_id):
    channels = db_exec("SELECT channel_id, invite_link FROM channels", fetch=True)
    if not channels:
        return True, []
    not_joined = []
    for ch_id, link in channels:
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status in ["left", "kicked"]:
                not_joined.append((ch_id, link))
        except Exception:
            not_joined.append((ch_id, link))
    return len(not_joined) == 0, not_joined


def send_verify_prompt(chat_id, channels):
    markup = types.InlineKeyboardMarkup()
    for idx, (ch_id, link) in enumerate(channels, 1):
        url = link if link.startswith("http") else f"https://t.me/{ch_id.replace('@', '')}"
        markup.add(types.InlineKeyboardButton(f"📢 Join Official Channel {idx}", url=url))
    markup.add(types.InlineKeyboardButton("✅ Verify Membership", callback_data="verify_join"))
    bot.send_message(
        chat_id,
        "🔒 <b>Access Restricted — Channel Verification Required</b>\n\n"
        "Welcome to our <b>Cloud Bot Hosting Platform</b>! Before you can access the dashboard and host your bots for free, "
        "you must join our official channel(s) listed below.\n\n"
        "👉 <b>Step 1:</b> Click each <i>Join Official Channel</i> button below and join.\n"
        "👉 <b>Step 2:</b> Return here and press <b>✅ Verify Membership</b> to unlock full access.",
        reply_markup=markup,
    )


# ================= BOT PROCESS RUNNER =================
def detect_entry_file(work_dir, uploaded_filename):
    ext = os.path.splitext(uploaded_filename)[1].lower()
    if ext in [".py", ".js", ".php"]:
        return uploaded_filename, ext

    priority = ["bot.py", "main.py", "app.py", "index.js", "bot.js", "index.php", "bot.php"]
    for root, _, files in os.walk(work_dir):
        for p in priority:
            if p in files:
                rel = os.path.relpath(os.path.join(root, p), work_dir)
                return rel, os.path.splitext(p)[1].lower()
    for root, _, files in os.walk(work_dir):
        for f in files:
            fext = os.path.splitext(f)[1].lower()
            if fext in [".py", ".js", ".php"]:
                rel = os.path.relpath(os.path.join(root, f), work_dir)
                return rel, fext
    return None, None


def start_bot_process(bot_id):
    row = db_exec(
        "SELECT id, work_dir, entry_file, file_type, log_path, pid FROM bots WHERE id=?",
        (bot_id,),
        fetchone=True,
    )
    if not row:
        return False, "Bot record not found in database."
    _, work_dir, entry_file, ftype, log_path, _ = row
    stop_bot_process(bot_id)

    cmd_map = {
        ".py": ["python3", entry_file],
        ".js": ["node", entry_file],
        ".php": ["php", entry_file],
    }
    cmd = cmd_map.get(ftype)
    if not cmd:
        return False, "Unsupported runtime file type."

    try:
        log_file = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        running_processes[bot_id] = proc
        db_exec("UPDATE bots SET pid=?, status='running' WHERE id=?", (proc.pid, bot_id))
        return True, proc.pid
    except Exception as e:
        return False, str(e)


def stop_bot_process(bot_id):
    proc = running_processes.pop(bot_id, None)
    if proc and proc.poll() is None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass
    row = db_exec("SELECT pid FROM bots WHERE id=?", (bot_id,), fetchone=True)
    if row and row[0]:
        try:
            os.kill(row[0], signal.SIGTERM)
        except Exception:
            pass
    db_exec("UPDATE bots SET status='stopped', pid=0 WHERE id=?", (bot_id,))


# ================= /start & VERIFY =================
@bot.message_handler(commands=["start"])
def handle_start(message):
    user_states.pop(message.from_user.id, None)
    ensure_user(message.from_user)
    joined, not_joined = check_user_joined(message.from_user.id)
    if not joined:
        send_verify_prompt(message.chat.id, not_joined)
        return
    name = html.escape(message.from_user.first_name or "Developer")
    bot.send_message(
        message.chat.id,
        f"👋 <b>Welcome, {name}!</b>\n\n"
        f"🚀 You are now connected to the <b>Telegram Cloud Bot Hosting Panel</b>. "
        f"Here you can deploy, run, monitor, and manage your bots 24/7 right inside Telegram.\n\n"
        f"📌 <b>What You Can Do:</b>\n"
        f"• <b>🚀 New bot host:</b> Upload <code>.py</code>, <code>.js</code>, <code>.php</code>, or <code>.zip</code> bot projects.\n"
        f"• <b>🤖 My bots:</b> View status, restart, or delete your hosted bots.\n"
        f"• <b>📜 Bots logs:</b> Inspect live console output and error logs.\n"
        f"• <b>💎 Subscription:</b> Upgrade your hosting slots anytime.\n\n"
        f"👇 Select an option from the menu below to get started:",
        reply_markup=user_main_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "verify_join")
def handle_verify_callback(call):
    ensure_user(call.from_user)
    joined, _ = check_user_joined(call.from_user.id)
    if not joined:
        bot.answer_callback_query(
            call.id,
            "❌ Verification Failed! Please make sure you have joined all required channels first.",
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id, "✅ Membership Verified Successfully!")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    name = html.escape(call.from_user.first_name or "Developer")
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>Verification Successful!</b>\n\n"
        f"Thank you for joining, <b>{name}</b>! Your hosting dashboard is now unlocked.\n"
        f"🆓 Your <b>Free Plan (3 Bot Slots)</b> has been activated automatically.\n\n"
        f"👇 Use the menu below to host your first bot!",
        reply_markup=user_main_menu(),
    )


# ================= TEXT MENU HANDLERS =================
@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid = message.from_user.id
    text = message.text.strip()
    ensure_user(message.from_user)

    u = get_user(uid)
    if not u[6]:
        joined, not_joined = check_user_joined(uid)
        if not joined:
            send_verify_prompt(message.chat.id, not_joined)
            return

    menu_buttons = [
        "🚀 New bot host", "🤖 My bots", "📜 Bots logs", "💎 Subscription",
        "🆘 Support", "👤 Account", "🔐 Admin login",
        "🤖 All Bots", "📁 All Files", "👥 Users", "📩 Sub Requests",
        "💎 Sub Users", "➕ Add Channel", "⚙️ Manage Channels",
        "📢 Send Notification", "🎁 Send Extra Plan", "➖ Remove Extra Files", "🔙 Back to User Panel"
    ]
    if text in menu_buttons:
        user_states.pop(uid, None)

    # ---- STATE HANDLERS ----
    if uid in user_states:
        state = user_states[uid]
        action = state["action"]

        if action == "admin_pass":
            user_states.pop(uid, None)
            if text == ADMIN_PASSWORD:
                db_exec("UPDATE users SET is_admin=1 WHERE user_id=?", (uid,))
                bot.send_message(
                    message.chat.id,
                    "🔓 <b>Admin Authentication Successful!</b>\n\n"
                    "Welcome to the <b>Administrator Control Panel</b>. You now have full access to manage users, "
                    "hosted bots, subscription requests, force-join channels, and global broadcasts.",
                    reply_markup=admin_main_menu(),
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "❌ <b>Access Denied!</b>\nThe administrator password you entered is incorrect. Please try again.",
                )
            return

        elif action == "add_channel":
            user_states.pop(uid, None)
            parts = [p.strip() for p in text.split("|")]
            ch_id = parts[0]
            link = parts[1] if len(parts) > 1 else f"https://t.me/{ch_id.replace('@', '')}"
            try:
                db_exec("INSERT OR REPLACE INTO channels (channel_id, invite_link) VALUES (?, ?)", (ch_id, link))
                bot.send_message(
                    message.chat.id,
                    f"✅ <b>Force-Join Channel Added Successfully!</b>\n\n"
                    f"📢 <b>Channel ID/Username:</b> <code>{html.escape(ch_id)}</code>\n"
                    f"🔗 <b>Invite Link:</b> {html.escape(link)}\n\n"
                    f"Users will now be required to join this channel before using the bot.",
                )
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ <b>Failed to Add Channel:</b>\n<code>{html.escape(str(e))}</code>")
            return

        elif action == "broadcast":
            user_states.pop(uid, None)
            users = db_exec("SELECT user_id FROM users", fetch=True)
            sent = 0
            failed = 0
            for (target_uid,) in users:
                try:
                    bot.send_message(
                        target_uid,
                        f"📢 <b>System Announcement</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n\n"
                        f"{html.escape(text)}\n\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"<i>— Cloud Bot Hosting Team</i>",
                    )
                    sent += 1
                except Exception:
                    failed += 1
            bot.send_message(
                message.chat.id,
                f"📢 <b>Broadcast Completed!</b>\n\n"
                f"✅ <b>Delivered:</b> {sent} users\n"
                f"❌ <b>Failed/Blocked:</b> {failed} users",
            )
            return

        elif action == "extra_plan_target":
            state["data"]["target"] = text
            state["action"] = "extra_plan_count"
            bot.send_message(
                message.chat.id,
                "🔢 <b>Enter Extra File Slots</b>\n\n"
                "How many <b>extra bot file slots</b> would you like to add to this target? Please enter a valid number (e.g., <code>2</code>):",
            )
            return

        elif action == "extra_plan_count":
            user_states.pop(uid, None)
            if not text.isdigit():
                bot.send_message(message.chat.id, "❌ <b>Invalid Input!</b> Please enter a positive whole number.")
                return
            count = int(text)
            target = state["data"]["target"].lower()
            if target == "all":
                db_exec("UPDATE users SET extra_files = extra_files + ?", (count,))
                bot.send_message(
                    message.chat.id,
                    f"🎁 <b>Extra Slots Granted to All Users!</b>\n\n"
                    f"Every registered user has received <b>+{count} extra file slot(s)</b>.",
                )
            elif target.isdigit():
                db_exec("UPDATE users SET extra_files = extra_files + ? WHERE user_id=?", (count, int(target)))
                bot.send_message(
                    message.chat.id,
                    f"🎁 <b>Extra Slots Granted!</b>\n\n"
                    f"User <code>{target}</code> has received <b>+{count} extra file slot(s)</b>.",
                )
                try:
                    bot.send_message(
                        int(target),
                        f"🎁 <b>Good News!</b>\n\nThe administrator has gifted you <b>+{count} extra bot hosting slot(s)</b>! "
                        f"Check your <b>👤 Account</b> tab to view your updated limit.",
                    )
                except Exception:
                    pass
            else:
                bot.send_message(message.chat.id, "❌ <b>Invalid Target!</b> Enter a valid numeric User ID or type <code>all</code>.")
            return

        elif action == "remove_extra_target":
            state["data"]["target"] = text
            state["action"] = "remove_extra_count"
            bot.send_message(
                message.chat.id,
                "🔢 <b>Enter Slots to Remove</b>\n\n"
                "How many extra file slots do you want to deduct? Please enter a valid number:",
            )
            return

        elif action == "remove_extra_count":
            user_states.pop(uid, None)
            if not text.isdigit():
                bot.send_message(message.chat.id, "❌ <b>Invalid Input!</b> Please enter a positive whole number.")
                return
            count = int(text)
            target = state["data"]["target"].lower()
            if target == "all":
                db_exec("UPDATE users SET extra_files = MAX(0, extra_files - ?)", (count,))
                bot.send_message(
                    message.chat.id,
                    f"➖ <b>Extra Slots Removed from All Users!</b>\n\n"
                    f"Up to <b>{count} extra file slot(s)</b> have been deducted from all users.",
                )
            elif target.isdigit():
                db_exec("UPDATE users SET extra_files = MAX(0, extra_files - ?) WHERE user_id=?", (count, int(target)))
                bot.send_message(
                    message.chat.id,
                    f"➖ <b>Extra Slots Removed!</b>\n\n"
                    f"Deducted <b>{count} extra file slot(s)</b> from User <code>{target}</code>.",
                )
            else:
                bot.send_message(message.chat.id, "❌ <b>Invalid Target!</b> Enter a valid numeric User ID or type <code>all</code>.")
            return

    # ---- USER PANEL BUTTONS ----
    if text == "🚀 New bot host":
        total_limit = u[4] + u[5]
        used = get_user_bot_count(uid)
        if used >= total_limit:
            bot.send_message(
                message.chat.id,
                f"⚠️ <b>Hosting Limit Reached!</b>\n\n"
                f"You are currently using <b>{used} out of {total_limit}</b> available bot slots on your <b>{u[3]} Plan</b>.\n\n"
                f"💡 To host more bots, either delete an unused bot from <b>🤖 My bots</b> or upgrade your plan from <b>💎 Subscription</b>.",
            )
            return
        user_states[uid] = {"action": "upload_bot", "data": {}}
        bot.send_message(
            message.chat.id,
            f"📤 <b>Upload Your Bot File</b>\n\n"
            f"Please send your bot source file as a <b>Document</b> right here.\n\n"
            f"✅ <b>Supported Formats:</b>\n"
            f"• <code>.py</code> (Python 3)\n"
            f"• <code>.js</code> (Node.js)\n"
            f"• <code>.php</code> (PHP)\n"
            f"• <code>.zip</code> (Compressed Project Archive — make sure your main file is named <code>bot.py</code>, <code>main.py</code>, <code>index.js</code>, or <code>index.php</code>)\n\n"
            f"📊 <b>Available Slots:</b> {total_limit - used} remaining",
        )

    elif text == "🤖 My bots":
        bots = db_exec("SELECT id, bot_name, file_type, entry_file, pid, status FROM bots WHERE user_id=?", (uid,), fetch=True)
        if not bots:
            bot.send_message(
                message.chat.id,
                "📭 <b>No Hosted Bots Found</b>\n\n"
                "You haven't deployed any bots yet. Click <b>🚀 New bot host</b> in the menu to upload and run your first bot!",
            )
            return
        bot.send_message(message.chat.id, f"🤖 <b>Your Hosted Bots ({len(bots)}):</b>\nManage, restart, or remove your bots below:")
        for b_id, b_name, ftype, entry, pid, status in bots:
            icon = "🟢 Running" if status == "running" else "🔴 Stopped"
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("🔄 Restart Bot", callback_data=f"restart_bot_{b_id}"),
                types.InlineKeyboardButton("📜 View Logs", callback_data=f"view_log_{b_id}"),
            )
            markup.add(types.InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_bot_{b_id}"))
            bot.send_message(
                message.chat.id,
                f"🤖 <b>Bot Name:</b> {html.escape(b_name)}\n"
                f"🆔 <b>Bot ID:</b> <code>#{b_id}</code>\n"
                f"⚙️ <b>Runtime:</b> <code>{ftype}</code> (Entry: <code>{html.escape(entry)}</code>)\n"
                f"📡 <b>Status:</b> {icon} | <b>PID:</b> <code>{pid}</code>",
                reply_markup=markup,
            )

    elif text == "📜 Bots logs":
        bots = db_exec("SELECT id, bot_name, status FROM bots WHERE user_id=?", (uid,), fetch=True)
        if not bots:
            bot.send_message(
                message.chat.id,
                "📭 <b>No Logs Available</b>\n\nYou do not have any hosted bots yet. Upload a bot first to monitor its runtime logs.",
            )
            return
        markup = types.InlineKeyboardMarkup()
        for b_id, b_name, status in bots:
            dot = "🟢" if status == "running" else "🔴"
            markup.add(types.InlineKeyboardButton(f"{dot} {b_name} (#{b_id})", callback_data=f"view_log_{b_id}"))
        bot.send_message(
            message.chat.id,
            "📜 <b>Bot Console Logs</b>\n\nSelect a bot from the list below to inspect its latest runtime output and error logs:",
            reply_markup=markup,
        )

    elif text == "💎 Subscription":
        total_limit = u[4] + u[5]
        used = get_user_bot_count(uid)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Upgrade to ₹9 Plan (5 Bot Slots)", callback_data="buy_9rs"))
        bot.send_message(
            message.chat.id,
            f"💎 <b>Bot Hosting Subscription Plans</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆓 <b>Free Plan (Auto-Activated):</b>\n"
            f"• Host up to <b>3 Bot Files</b> simultaneously\n"
            f"• Live Console Logs & Instant Restart\n\n"
            f"🌟 <b>Premium Plan — Only ₹9:</b>\n"
            f"• Host up to <b>5 Bot Files</b> simultaneously\n"
            f"• Priority Process Stability & Instant Support\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Your Current Status:</b>\n"
            f"• Active Plan: <b>{u[3]}</b>\n"
            f"• Usage: <b>{used} / {total_limit} Files</b>\n\n"
            f"👇 Click the button below to upgrade via UPI!",
            reply_markup=markup,
        )

    elif text == "🆘 Support":
        bot.send_message(
            message.chat.id,
            f"🆘 <b>Customer & Technical Support</b>\n\n"
            f"Need help deploying your bot, fixing runtime errors, or verifying your payment? "
            f"Our support team is always ready to assist you!\n\n"
            f"👨‍💻 <b>Official Support Contact:</b> {SUPPORT_USERNAME}\n\n"
            f"💡 <i>Tip: When messaging support, please include your User ID (<code>{uid}</code>) and a screenshot or log of your issue for faster assistance.</i>",
        )

    elif text == "👤 Account":
        total_limit = u[4] + u[5]
        used = get_user_bot_count(uid)
        remaining = max(0, total_limit - used)
        caption = (
            f"👤 <b>Your Hosting Account Profile</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📛 <b>Full Name:</b> {html.escape(u[2])}\n"
            f"🆔 <b>User ID:</b> <code>{u[0]}</code>\n"
            f"🔗 <b>Username:</b> @{html.escape(u[1])}\n"
            f"💎 <b>Current Plan:</b> {u[3]}\n"
            f"📦 <b>Base Plan Limit:</b> {u[4]} Files\n"
            f"🎁 <b>Extra Bonus Slots:</b> +{u[5]} Files\n"
            f"📊 <b>Total Capacity:</b> {total_limit} Files\n"
            f"🤖 <b>Active Bots Hosted:</b> {used} Files\n"
            f"🟢 <b>Available Slots Left:</b> {remaining} Files\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        try:
            photos = bot.get_user_profile_photos(uid, limit=1)
            if photos.total_count > 0:
                bot.send_photo(message.chat.id, photos.photos[0][-1].file_id, caption=caption)
                return
        except Exception:
            pass
        bot.send_message(message.chat.id, caption)

    elif text == "🔐 Admin login":
        user_states[uid] = {"action": "admin_pass", "data": {}}
        bot.send_message(
            message.chat.id,
            "🔐 <b>Administrator Login</b>\n\nPlease enter the <b>Admin Password</b> to access the management dashboard:",
        )

    # ---- ADMIN PANEL BUTTONS ----
    elif u[6] == 1:
        if text == "🔙 Back to User Panel":
            bot.send_message(
                message.chat.id,
                "🔙 Switched back to the <b>User Hosting Panel</b>.",
                reply_markup=user_main_menu(),
            )

        elif text == "🤖 All Bots":
            bots = db_exec("SELECT id, user_id, bot_name, file_type, status, pid FROM bots", fetch=True)
            if not bots:
                bot.send_message(message.chat.id, "📭 There are currently no hosted bots on the server.")
                return
            bot.send_message(message.chat.id, f"🤖 <b>All Hosted Bots ({len(bots)} total):</b>")
            for b_id, owner_id, b_name, ftype, status, pid in bots:
                icon = "🟢 Running" if status == "running" else "🔴 Stopped"
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_bot_{b_id}"),
                    types.InlineKeyboardButton("📜 Logs", callback_data=f"view_log_{b_id}"),
                    types.InlineKeyboardButton("🗑 Delete", callback_data=f"delete_bot_{b_id}"),
                )
                bot.send_message(
                    message.chat.id,
                    f"🤖 <b>{html.escape(b_name)}</b> (<code>{ftype}</code>)\n"
                    f"🆔 Bot ID: <code>#{b_id}</code> | Owner: <code>{owner_id}</code>\n"
                    f"📡 Status: {icon} | PID: <code>{pid}</code>",
                    reply_markup=markup,
                )

        elif text == "📁 All Files":
            bots = db_exec("SELECT id, user_id, bot_name, entry_file, file_type FROM bots", fetch=True)
            if not bots:
                bot.send_message(message.chat.id, "📭 No files have been uploaded yet.")
                return
            msg = f"📁 <b>All Uploaded Project Files ({len(bots)}):</b>\n\n"
            for b_id, owner_id, b_name, entry, ftype in bots:
                msg += f"• <b>#{b_id}</b> | {html.escape(b_name)} → <code>{html.escape(entry)}</code> (Owner: <code>{owner_id}</code>)\n"
            bot.send_message(message.chat.id, msg[:4000])

        elif text == "👥 Users":
            users = db_exec("SELECT user_id, full_name, plan, base_limit, extra_files FROM users", fetch=True)
            msg = f"👥 <b>Registered Users Overview (Total: {len(users)}):</b>\n\n"
            for user_id, name, plan, blim, elim in users[:50]:
                msg += f"• <code>{user_id}</code> | {html.escape(name)} | <b>{plan}</b> (Limit: {blim}+{elim})\n"
            bot.send_message(message.chat.id, msg[:4000])

        elif text == "📩 Sub Requests":
            reqs = db_exec("SELECT id, user_id, amount, photo_id FROM sub_requests WHERE status='pending'", fetch=True)
            if not reqs:
                bot.send_message(message.chat.id, "✅ <b>All Caught Up!</b> There are no pending subscription payment requests.")
                return
            for req_id, r_uid, amt, photo_id in reqs:
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("✅ Approve Payment", callback_data=f"sub_approve_{req_id}"),
                    types.InlineKeyboardButton("❌ Reject Payment", callback_data=f"sub_reject_{req_id}"),
                )
                bot.send_photo(
                    message.chat.id,
                    photo_id,
                    caption=(
                        f"📩 <b>Pending Subscription Request #{req_id}</b>\n\n"
                        f"👤 <b>User ID:</b> <code>{r_uid}</code>\n"
                        f"💰 <b>Amount Paid:</b> ₹{amt}\n"
                        f"📦 <b>Requested Plan:</b> Premium (5 Files)\n\n"
                        f"Please verify the payment screenshot above and choose an action:"
                    ),
                    reply_markup=markup,
                )

        elif text == "💎 Sub Users":
            paid = db_exec("SELECT user_id, full_name, base_limit, extra_files FROM users WHERE plan='Paid'", fetch=True)
            if not paid:
                bot.send_message(message.chat.id, "📭 There are no active Paid subscription users yet.")
                return
            msg = f"💎 <b>Active Paid Subscribers ({len(paid)}):</b>\n\n"
            for p_uid, name, blim, elim in paid:
                msg += f"• <code>{p_uid}</code> | {html.escape(name)} | Limit: {blim}+{elim} Files\n"
            bot.send_message(message.chat.id, msg[:4000])

        elif text == "➕ Add Channel":
            user_states[uid] = {"action": "add_channel", "data": {}}
            bot.send_message(
                message.chat.id,
                "➕ <b>Add Force-Join Channel</b>\n\n"
                "Send the channel username or <code>ChatID|InviteLink</code>.\n\n"
                "📌 <b>Examples:</b>\n"
                "• Public channel: <code>@yourchannel</code>\n"
                "• Private channel: <code>-1001234567890|https://t.me/+InviteCode</code>\n\n"
                "⚠️ <i>Important: You must add this bot as an Administrator in that channel so it can verify user memberships.</i>",
            )

        elif text == "⚙️ Manage Channels":
            channels = db_exec("SELECT id, channel_id, invite_link FROM channels", fetch=True)
            if not channels:
                bot.send_message(message.chat.id, "📭 No force-join channels are configured.")
                return
            bot.send_message(message.chat.id, f"⚙️ <b>Configured Force-Join Channels ({len(channels)}):</b>")
            for cid, ch_name, link in channels:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🗑 Remove Channel", callback_data=f"del_channel_{cid}"))
                bot.send_message(
                    message.chat.id,
                    f"📢 <b>Channel:</b> <code>{html.escape(ch_name)}</code>\n🔗 <b>Link:</b> {html.escape(link)}",
                    reply_markup=markup,
                )

        elif text == "📢 Send Notification":
            user_states[uid] = {"action": "broadcast", "data": {}}
            bot.send_message(
                message.chat.id,
                "📢 <b>Global Broadcast Notification</b>\n\n"
                "Type the announcement message you want to send to <b>all registered users</b>:",
            )

        elif text == "🎁 Send Extra Plan":
            user_states[uid] = {"action": "extra_plan_target", "data": {}}
            bot.send_message(
                message.chat.id,
                "🎁 <b>Grant Extra File Slots</b>\n\n"
                "Enter the target <b>User ID</b> to gift extra slots to a specific user, or type <code>all</code> to grant slots to every user:",
            )

        elif text == "➖ Remove Extra Files":
            user_states[uid] = {"action": "remove_extra_target", "data": {}}
            bot.send_message(
                message.chat.id,
                "➖ <b>Deduct Extra File Slots</b>\n\n"
                "Enter the target <b>User ID</b>, or type <code>all</code> to deduct extra slots from every user:",
            )


# ================= DOCUMENT UPLOAD (HOST BOT) =================
@bot.message_handler(content_types=["document"])
def handle_document(message):
    uid = message.from_user.id
    state = user_states.get(uid)
    if not state or state["action"] != "upload_bot":
        return

    user_states.pop(uid, None)
    u = get_user(uid)
    total_limit = u[4] + u[5]
    if get_user_bot_count(uid) >= total_limit:
        bot.send_message(message.chat.id, "⚠️ <b>Hosting Limit Reached!</b> Please upgrade your plan or delete an existing bot.")
        return

    doc = message.document
    filename = doc.file_name
    ext = os.path.splitext(filename)[1].lower()
    if ext not in [".py", ".js", ".php", ".zip"]:
        bot.send_message(
            message.chat.id,
            "❌ <b>Unsupported File Format!</b>\n\n"
            "Only <code>.py</code>, <code>.js</code>, <code>.php</code>, and <code>.zip</code> files are supported. Please upload a valid file.",
        )
        return

    prog_msg = bot.send_message(
        message.chat.id,
        "⏳ <b>Deploying Your Bot...</b>\n\n<code>[░░░░░░░░░░] 0%</code> — Downloading file from Telegram servers...",
    )
    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)

    bot_folder = os.path.join(BASE_DIR, f"{uid}_{int(time.time())}")
    os.makedirs(bot_folder, exist_ok=True)
    save_path = os.path.join(bot_folder, filename)
    with open(save_path, "wb") as f:
        f.write(downloaded)

    steps = [
        ("<code>[███░░░░░░░] 30%</code> — Verifying file integrity & workspace..."),
        ("<code>[██████░░░░] 60%</code> — Extracting archive & detecting entry point..."),
        ("<code>[█████████░] 90%</code> — Launching runtime process on server..."),
    ]
    for bar in steps:
        time.sleep(0.7)
        try:
            bot.edit_message_text(f"⏳ <b>Deploying Your Bot...</b>\n\n{bar}", message.chat.id, prog_msg.message_id)
        except Exception:
            pass

    if ext == ".zip":
        try:
            with zipfile.ZipFile(save_path, "r") as zip_ref:
                zip_ref.extractall(bot_folder)
        except Exception as e:
            bot.edit_message_text(
                f"❌ <b>Deployment Failed!</b>\nCould not extract the ZIP archive:\n<code>{html.escape(str(e))}</code>",
                message.chat.id,
                prog_msg.message_id,
            )
            shutil.rmtree(bot_folder, ignore_errors=True)
            return

    entry_file, run_ext = detect_entry_file(bot_folder, filename)
    if not entry_file:
        bot.edit_message_text(
            "❌ <b>No Executable Entry File Found!</b>\n\n"
            "We could not find any <code>.py</code>, <code>.js</code>, or <code>.php</code> script inside your ZIP archive. "
            "Please check your project folder and upload again.",
            message.chat.id,
            prog_msg.message_id,
        )
        shutil.rmtree(bot_folder, ignore_errors=True)
        return

    log_path = os.path.join(bot_folder, "bot.log")
    db_exec(
        "INSERT INTO bots (user_id, bot_name, file_type, work_dir, entry_file, log_path) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, filename, run_ext, bot_folder, entry_file, log_path),
    )
    bot_id = db_exec("SELECT last_insert_rowid()", fetchone=True)[0]
    ok, info = start_bot_process(bot_id)

    time.sleep(0.5)
    if ok:
        bot.edit_message_text(
            f"✅ <b><code>[██████████] 100%</code> — Deployment Complete!</b>\n\n"
            f"🚀 <b>Your bot is now live and running!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Bot Name:</b> {html.escape(filename)}\n"
            f"🆔 <b>Bot ID:</b> <code>#{bot_id}</code>\n"
            f"📄 <b>Entry File:</b> <code>{html.escape(entry_file)}</code>\n"
            f"⚙️ <b>Runtime:</b> <code>{run_ext}</code>\n"
            f"🔢 <b>Process ID (PID):</b> <code>{info}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 Use <b>🤖 My bots</b> to restart/delete or <b>📜 Bots logs</b> to inspect live console output.",
            message.chat.id,
            prog_msg.message_id,
        )
    else:
        bot.edit_message_text(
            f"⚠️ <b>Uploaded, But Failed to Start</b>\n\n"
            f"Your file was saved, however the server returned an error while starting the process:\n"
            f"<code>{html.escape(str(info))}</code>",
            message.chat.id,
            prog_msg.message_id,
        )


# ================= PHOTO UPLOAD (PAYMENT SCREENSHOT) =================
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id
    state = user_states.get(uid)
    if not state or state["action"] != "upload_payment":
        return
    user_states.pop(uid, None)
    photo_id = message.photo[-1].file_id
    db_exec("INSERT INTO sub_requests (user_id, amount, photo_id) VALUES (?, ?, ?)", (uid, 9, photo_id))
    req_id = db_exec("SELECT last_insert_rowid()", fetchone=True)[0]
    bot.send_message(
        message.chat.id,
        f"✅ <b>Payment Proof Submitted Successfully! (Request #{req_id})</b>\n\n"
        f"Your payment screenshot has been forwarded to our administrators for verification. "
        f"Once approved, your <b>₹9 Premium Plan (5 File Slots)</b> will be activated automatically and you will receive a confirmation message here!",
    )

    admins = db_exec("SELECT user_id FROM users WHERE is_admin=1", fetch=True)
    for (adm_id,) in admins:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Approve Payment", callback_data=f"sub_approve_{req_id}"),
            types.InlineKeyboardButton("❌ Reject Payment", callback_data=f"sub_reject_{req_id}"),
        )
        try:
            bot.send_photo(
                adm_id,
                photo_id,
                caption=(
                    f"🆕 <b>New Subscription Payment Request #{req_id}</b>\n\n"
                    f"👤 <b>User ID:</b> <code>{uid}</code>\n"
                    f"💰 <b>Amount:</b> ₹9\n"
                    f"📦 <b>Plan:</b> Premium (5 Bot Slots)\n\n"
                    f"Verify the screenshot and approve or reject below:"
                ),
                reply_markup=markup,
            )
        except Exception:
            pass


# ================= INLINE CALLBACKS =================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    uid = call.from_user.id

    if data == "buy_9rs":
        upi_url = f"upi://pay?pa={UPI_ID}&pn=CloudBotHost&am=9&cu=INR"
        qr = qrcode.make(upi_url)
        bio = BytesIO()
        qr.save(bio, format="PNG")
        bio.seek(0)
        user_states[uid] = {"action": "upload_payment", "data": {}}
        bot.answer_callback_query(call.id)
        bot.send_photo(
            call.message.chat.id,
            bio,
            caption=(
                f"💳 <b>Upgrade to Premium Plan — ₹9 (5 Bot Slots)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📌 <b>UPI ID:</b> <code>{UPI_ID}</code>\n"
                f"💰 <b>Payable Amount:</b> ₹9 INR\n\n"
                f"📝 <b>How to Complete Your Purchase:</b>\n"
                f"1️⃣ Scan the QR code above or pay <b>₹9</b> to the UPI ID <code>{UPI_ID}</code> using any UPI app (PhonePe, GPay, Paytm).\n"
                f"2️⃣ Take a clear screenshot of the completed payment.\n"
                f"3️⃣ Send the <b>payment screenshot (Image)</b> directly in this chat.\n\n"
                f"⏳ <i>Waiting for your payment screenshot...</i>"
            ),
        )

    elif data.startswith("restart_bot_"):
        b_id = int(data.split("_")[-1])
        ok, info = start_bot_process(b_id)
        if ok:
            bot.answer_callback_query(call.id, f"🔄 Bot #{b_id} Restarted Successfully! (PID: {info})", show_alert=True)
        else:
            bot.answer_callback_query(call.id, f"❌ Restart Failed: {info}", show_alert=True)

    elif data.startswith("delete_bot_"):
        b_id = int(data.split("_")[-1])
        row = db_exec("SELECT work_dir FROM bots WHERE id=?", (b_id,), fetchone=True)
        stop_bot_process(b_id)
        if row and os.path.exists(row[0]):
            shutil.rmtree(row[0], ignore_errors=True)
        db_exec("DELETE FROM bots WHERE id=?", (b_id,))
        bot.answer_callback_query(call.id, f"🗑 Bot #{b_id} has been stopped and deleted.", show_alert=True)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

    elif data.startswith("view_log_"):
        b_id = int(data.split("_")[-1])
        row = db_exec("SELECT bot_name, log_path FROM bots WHERE id=?", (b_id,), fetchone=True)
        if not row or not os.path.exists(row[1]):
            bot.answer_callback_query(call.id, "📭 No log file found for this bot yet.", show_alert=True)
            return
        with open(row[1], "r", encoding="utf-8", errors="ignore") as f:
            logs = f.read().strip()
        bot.answer_callback_query(call.id)
        if not logs:
            bot.send_message(
                call.message.chat.id,
                f"📜 <b>Console Logs — {html.escape(row[0])} (#{b_id})</b>\n\n<i>The log file is currently empty. No output or errors have been recorded yet.</i>",
            )
        elif len(logs) > 3500:
            with open(row[1], "rb") as f:
                bot.send_document(
                    call.message.chat.id,
                    f,
                    caption=f"📜 <b>Full Console Log File — {html.escape(row[0])} (#{b_id})</b>",
                )
        else:
            bot.send_message(
                call.message.chat.id,
                f"📜 <b>Console Logs — {html.escape(row[0])} (#{b_id}):</b>\n<pre>{html.escape(logs)}</pre>",
            )

    elif data.startswith("sub_approve_"):
        req_id = int(data.split("_")[-1])
        req = db_exec("SELECT user_id, status FROM sub_requests WHERE id=?", (req_id,), fetchone=True)
        if not req or req[1] != "pending":
            bot.answer_callback_query(call.id, "⚠️ This request has already been processed.")
            return
        target_uid = req[0]
        db_exec("UPDATE sub_requests SET status='approved' WHERE id=?", (req_id,))
        db_exec("UPDATE users SET plan='Paid', base_limit=5 WHERE user_id=?", (target_uid,))
        bot.answer_callback_query(call.id, "✅ Payment Approved!")
        bot.edit_message_caption(
            f"✅ <b>Approved Request #{req_id}</b>\nUser <code>{target_uid}</code> has been upgraded to the <b>Paid Plan (5 Files)</b>.",
            call.message.chat.id,
            call.message.message_id,
        )
        try:
            bot.send_message(
                target_uid,
                "🎉 <b>Congratulations! Your Subscription is Now Active!</b>\n\n"
                "Your ₹9 payment has been verified and approved by an administrator.\n"
                "💎 <b>Active Plan:</b> Paid Plan (5 Bot Hosting Slots)\n\n"
                "You can now host up to 5 bots simultaneously from <b>🚀 New bot host</b>!",
            )
        except Exception:
            pass

    elif data.startswith("sub_reject_"):
        req_id = int(data.split("_")[-1])
        req = db_exec("SELECT user_id, status FROM sub_requests WHERE id=?", (req_id,), fetchone=True)
        if not req or req[1] != "pending":
            bot.answer_callback_query(call.id, "⚠️ This request has already been processed.")
            return
        target_uid = req[0]
        db_exec("UPDATE sub_requests SET status='rejected' WHERE id=?", (req_id,))
        bot.answer_callback_query(call.id, "❌ Payment Rejected!")
        bot.edit_message_caption(
            f"❌ <b>Rejected Request #{req_id}</b>\nUser ID: <code>{target_uid}</code>",
            call.message.chat.id,
            call.message.message_id,
        )
        try:
            bot.send_message(
                target_uid,
                f"❌ <b>Payment Verification Failed (Request #{req_id})</b>\n\n"
                f"Unfortunately, your recent payment screenshot could not be verified and was rejected. "
                f"If you believe this is a mistake, please contact {SUPPORT_USERNAME} with your transaction details.",
            )
        except Exception:
            pass

    elif data.startswith("del_channel_"):
        cid = int(data.split("_")[-1])
        db_exec("DELETE FROM channels WHERE id=?", (cid,))
        bot.answer_callback_query(call.id, "🗑 Channel removed from force-join list.")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass


print("🤖 Cloud Hosting Bot is running...")
bot.infinity_polling(skip_pending=True)
