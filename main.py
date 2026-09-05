# 1. Add this at the very top of main.py
from keep_alive import keep_alive
import logging
import sqlite3
import hashlib
import asyncio
import io
import re
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from playwright.async_api import async_playwright


# ========================================================
# ADMIN CONFIGURATION
# ========================================================
ADMIN_ID = 6505123260
BOT_ACTIVE = True  # Global state for bot ON/OFF toggle

# -----------------------------------------
# 1. SETUP & CONFIGURATION
# -----------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
(CHOOSING_ACTION, SCHOOL, GPA_CURRENT, GPA_NEXT, GENDER, 
 DEPT_1, DEPT_2, CONFIRM, AWAITING_ADMIN_MSG, VERIFY_NAME) = range(10)

# Predefined ASTU Schools & Departments
SCHOOL_DEPARTMENTS = {
    "School of Applied Natural Science (#SoASN)": [
        "Applied Biology",
        "Applied Chemistry",
        "Applied Geology",
        "Applied Mathematics",
        "Applied Physics",
        "Industrial Chemistry",
        "Pharmacy"
    ],
    "School of Civil Engineering and Architecture (#SoCEA)": [
        "Architecture",
        "Civil Engineering",
        "Water Resource Engineering"
    ],
    "School of Mechanical, Chemical and Material Engineering (#SoMCME)": [
        "Chemical Engineering",
        "Material Science and engineering",
        "Mechanical Engineering"
    ],
    "School of Electrical Engineering and Computing (#SoEEC)": [
        "Computer Science and Engineering",
        "Electronics and communication engineering",
        "Software Engineering",
        "Electrical power and control engineering"
    ]
}

SCHOOL_MAP = {
    "SoASN": "School of Applied Natural Science (#SoASN)",
    "SoCEA": "School of Civil Engineering and Architecture (#SoCEA)",
    "SoMCME": "School of Mechanical, Chemical and Material Engineering (#SoMCME)",
    "SoEEC": "School of Electrical Engineering and Computing (#SoEEC)"
}

# Custom Filters
CMD_START = filters.Regex(r'^[\./]start$')
CMD_CANCEL = filters.Regex(r'^[\./]cancel$')
CMD_VIEW = filters.Regex(r'^[\./]view$')
CMD_USERS = filters.Regex(r'^[\./]users$')
CMD_ADMIN = filters.Regex(r'^[\./]admin$')
CUSTOM_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^[\./]')

def init_db():
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            hash_id TEXT PRIMARY KEY,
            school TEXT,
            gpa_current REAL,
            gpa_next REAL,
            gender TEXT,
            dept_first TEXT,
            dept_second TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN verified_name TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE users ADD COLUMN verified_at DATETIME")
        cursor.execute("ALTER TABLE users ADD COLUMN verified_school TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

# -----------------------------------------
# DATABASE BACKUP HELPER
# -----------------------------------------
async def send_db_backup(context: ContextTypes.DEFAULT_TYPE, caption: str = "📦 Automatic Database Backup"):
    """Securely sends the SQLite database to the ADMIN_ID."""
    if ADMIN_ID != 0:
        try:
            with open("astu_placement.db", "rb") as db_file:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=db_file,
                    caption=caption
                )
        except Exception as e:
            logger.error(f"Failed to send DB backup: {e}")

def get_user_hash(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()

def is_user_registered(user_id: int) -> bool:
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)

def is_user_verified(user_id: int) -> bool:
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return bool(res and res[0] == 1)

def has_user_submitted(user_id: int) -> bool:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM submissions WHERE hash_id = ?", (user_hash,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)

# -----------------------------------------
# 2. SECURITY & ACCESS CHECKERS
# -----------------------------------------

async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    try:
        member = await context.bot.get_chat_member(chat_id="@astuplacement", user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Membership check failed: {e}")
        return False

async def check_access_and_respond(msg_obj, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    global BOT_ACTIVE
    
    if not BOT_ACTIVE and user_id != ADMIN_ID:
        text = "🔴 *Maintenance Mode*\n\nThe bot is currently inactive. Please try again later."
        if hasattr(msg_obj, 'edit_message_text'):
            await msg_obj.edit_message_text(text, parse_mode="Markdown")
        else:
            await msg_obj.reply_text(text, parse_mode="Markdown")
        return False
        
    conn = sqlite3.connect("astu_placement.db")
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    
    if res and res[0] == 1:
        text = "⛔ *Banned*\n\nYou have been restricted from using this bot."
        if hasattr(msg_obj, 'edit_message_text'):
            await msg_obj.edit_message_text(text, parse_mode="Markdown")
        else:
            await msg_obj.reply_text(text, parse_mode="Markdown")
        return False
        
    subbed = await is_subscribed(context, user_id)
    if not subbed:
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/astuplacement")],
            [InlineKeyboardButton("✅ Verify Membership", callback_data="verify_sub")]
        ]
        text = "⚠️ *Access Restricted*\n\nYou must join our channel to use this bot!"
        markup = InlineKeyboardMarkup(keyboard)
        if hasattr(msg_obj, 'edit_message_text'):
            await msg_obj.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await msg_obj.reply_text(text, reply_markup=markup, parse_mode="Markdown")
        return False
        
    return True

async def verify_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = query.from_user
    
    subbed = await is_subscribed(context, user.id)
    if subbed:
        await query.answer("✅ Membership verified! Welcome.")
        return await show_main_menu(query, user=user, is_edit=True)
    else:
        await query.answer("❌ You haven't joined the channel yet! Click 'Join Channel' first.", show_alert=True)
        return CHOOSING_ACTION

# -----------------------------------------
# 3. VERIFICATION & ADMISSION YEAR CHECK SYSTEM
# -----------------------------------------

async def check_astu_student(full_name: str):
    logger.info(f"Verification started for name: {full_name}")
    search_name = re.sub(r'\s+', ' ', full_name.strip()).lower()
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = await browser.new_context(viewport={'width': 1280, 'height': 800})
            page = await context.new_page()
            
            response = await page.goto("https://portal.astu.edu.et", timeout=30000)
            if not response or response.status >= 400:
                await browser.close()
                return "ERROR", None
            
            await page.wait_for_load_state("domcontentloaded")
            hamburger = page.locator(".navbar-toggler, .navbar-toggle, [aria-label='Toggle navigation']")
            if await hamburger.count() > 0 and await hamburger.first.is_visible():
                await hamburger.first.click()
                await page.wait_for_timeout(1000)
                
            verification_menu = page.get_by_text("Verification", exact=True).first
            if await verification_menu.count() == 0: verification_menu = page.locator("text=Verification").first
            await verification_menu.wait_for(state="visible", timeout=10000)
            await verification_menu.click()
            await page.wait_for_timeout(1000)
            
            student_verification = page.get_by_text("Student Verification", exact=True).first
            if await student_verification.count() == 0: student_verification = page.locator("text=Student Verification").first
            await student_verification.wait_for(state="visible", timeout=10000)
            await student_verification.click()
            
            await page.wait_for_load_state("networkidle", timeout=15000)
            search_input = page.locator("input[type='text'], input[type='search'], input[name*='search']").first
            await search_input.wait_for(state="visible", timeout=10000)
            await search_input.fill(full_name.strip())
            
            search_button = page.locator("button[type='submit'], input[type='submit'], button:has-text('Search')").first
            if await search_button.count() > 0 and await search_button.first.is_visible():
                await search_button.first.click()
            else:
                await search_input.press("Enter")
                
            try: await page.wait_for_load_state("networkidle", timeout=10000)
            except: pass
            await page.wait_for_timeout(3000)
            
            await page.evaluate("document.querySelectorAll('input').forEach(el => el.remove());")
            raw_body_text = await page.locator("body").inner_text()
            raw_text_single_spaced = re.sub(r'\s+', ' ', raw_body_text)
            body_text_clean = raw_text_single_spaced.lower()
            
            await browser.close()
            
            not_found_indicators = ["no matching", "not found", "no records", "no student", "showing 0 to 0 of 0 entries", "empty"]
            if any(indicator in body_text_clean for indicator in not_found_indicators) or search_name not in body_text_clean:
                return "NOT_FOUND", None

            detected_school = None
            if "applied natural science" in body_text_clean or "soasn" in body_text_clean: detected_school = "School of Applied Natural Science (#SoASN)"
            elif "civil engineering and architecture" in body_text_clean or "socea" in body_text_clean: detected_school = "School of Civil Engineering and Architecture (#SoCEA)"
            elif "mechanical, chemical" in body_text_clean or "somcme" in body_text_clean: detected_school = "School of Mechanical, Chemical and Material Engineering (#SoMCME)"
            elif "electrical engineering" in body_text_clean or "soeec" in body_text_clean: detected_school = "School of Electrical Engineering and Computing (#SoEEC)"

            if "2025/2026" in raw_text_single_spaced:
                return "VERIFIED_ELIGIBLE", detected_school
                
            matches = re.findall(r'(\d{4}/\d{4})', raw_text_single_spaced)
            found_year_str = ", ".join(list(set(matches))) if matches else "an older or different batch"
            return "WRONG_YEAR", found_year_str

    except Exception as e:
        logger.error(f"ASTU Verification Error: {e}")
        return "ERROR", None

async def prompt_verify_name(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="cancel")]]
    text = (
        "🎓 *ASTU Student Verification*\n\n"
        "Before continuing, we need to verify that you are an unplaced ASTU student.\n"
        "Please enter your *full name* exactly as it appears in your ASTU student record."
    )
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return VERIFY_NAME

async def handle_verify_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name_input = update.message.text.strip()
    wait_msg = await update.message.reply_text("✨ Verifying that you’re an ASTU student…Please wait.")
    
    status, detail = await check_astu_student(name_input)
    
    if status == "ERROR":
        keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="action_register")], [InlineKeyboardButton("Cancel", callback_data="cancel")]]
        await wait_msg.edit_text("⚠️ *Service Unavailable*\nPlease try again later.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return CHOOSING_ACTION
    elif status == "NOT_FOUND":
        keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="action_register")], [InlineKeyboardButton("Cancel", callback_data="cancel")]]
        await wait_msg.edit_text("❌ *Verification Failed*\nWe could not find your record.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return CHOOSING_ACTION
    elif status == "WRONG_YEAR":
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="back_menu")]]
        await wait_msg.edit_text(f"⛔ *Ineligible Batch*\nFound admission year: *{detail}*.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return CHOOSING_ACTION
    elif status == "VERIFIED_ELIGIBLE":
        user_id = update.effective_user.id
        detected_school = detail
        
        conn = sqlite3.connect("astu_placement.db")
        c = conn.cursor()
        c.execute("""
            UPDATE users SET verified_name = ?, is_verified = 1, verified_at = CURRENT_TIMESTAMP, verified_school = ?
            WHERE user_id = ?
        """, (name_input, detected_school, user_id))
        conn.commit()
        conn.close()
        
        if detected_school and detected_school in SCHOOL_DEPARTMENTS:
            context.user_data["school"] = detected_school
            await wait_msg.edit_text(f"✅ *Verified*\n🏫 Detected School: {detected_school}\nProceeding to GPA entry...", parse_mode="Markdown")
            return await prompt_gpa_current(wait_msg, context)
        else:
            await wait_msg.edit_text("✅ *Verified*\nPlease continue with your choices.", parse_mode="Markdown")
            return await prompt_school(wait_msg, context)

# -----------------------------------------
# 4. MENU & NAVIGATION HELPERS
# -----------------------------------------

async def show_main_menu(update_or_msg, user, is_edit=False):
    fn = user.first_name.replace('<', '&lt;').replace('>', '&gt;')
    uid = user.id
    profile_link = f"<a href='tg://user?id={uid}'>{fn}</a>"
    
    registered = is_user_registered(uid)
    
    if not registered:
        status_str = "⚠️ Not Registered (Please Register First)"
        text = (
            f"<blockquote>👋 Welcome, {profile_link}!\n🆔 <b>Your ID:</b> <code>{uid}</code>\n📌 <b>Status:</b> {status_str}</blockquote>\n\n"
            "🎓 <b>ASTU Anonymous Department Placement Collector</b>\n\nYou must register to use this bot."
        )
        keyboard = [[InlineKeyboardButton("✅ Register", callback_data="do_register")]]
    else:
        verified_str = " (Verified Fresh Student)" if is_user_verified(uid) else ""
        status_str = f"✅ Registered{verified_str}"
        text = (
            f"<blockquote>👋 Welcome, {profile_link}!\n🆔 <b>Your ID:</b> <code>{uid}</code>\n📌 <b>Status:</b> {status_str}</blockquote>\n\n"
            "🎓 <b>ASTU Anonymous Department Placement Collector</b>\n\n🔒 <b>Privacy Protected:</b> Your submitted placement stats are completely anonymized."
        )
        fill_update_btn = "Update Info" if has_user_submitted(uid) else "Fill Info"
        keyboard = [
            [InlineKeyboardButton(fill_update_btn, callback_data="action_register")],
            [InlineKeyboardButton("View My Data", callback_data="action_mydata")],
            [InlineKeyboardButton("View Stats", callback_data="action_view")],
            [InlineKeyboardButton("Contact Admin", callback_data="action_contact")]
        ]
        
    markup = InlineKeyboardMarkup(keyboard)

    if is_edit:
        if hasattr(update_or_msg, 'edit_message_text'):
            await update_or_msg.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        elif hasattr(update_or_msg, 'edit_text'):
            await update_or_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update_or_msg.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return CHOOSING_ACTION

async def do_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = query.from_user
    
    if not await check_access_and_respond(query, context, user.id): return CHOOSING_ACTION
    
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user.id,))
    is_new = False
    
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)", 
                       (user.id, user.username, user.first_name))
        conn.commit()
        is_new = True
        if ADMIN_ID != 0:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🔔 *New User Registered!*\nName: {user.first_name}\nUsername: @{user.username or 'None'}\nID: `{user.id}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    conn.close()

    # TRIGGER AUTO-BACKUP ONLY FOR NEW REGISTRATIONS
    if is_new:
        await send_db_backup(context, caption=f"📦 Auto-Backup: New Registration ({user.id})")

    await query.answer("✅ Successfully Registered!")
    return await show_main_menu(query, user=user, is_edit=True)

async def action_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Request Update", callback_data="contact_req_update")],
        [InlineKeyboardButton("Report Problem", callback_data="contact_rep_prob")],
        [InlineKeyboardButton("Feedback", callback_data="contact_feedback")],
        [InlineKeyboardButton("Back to Main Menu", callback_data="back_menu")]
    ]
    await query.edit_message_text(
        "<b>Contact Admin</b>\n\nHow can we help you? Please select the type of message:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return CHOOSING_ACTION

async def handle_contact_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mapping = {"contact_req_update": "Request Update", "contact_rep_prob": "Report Problem", "contact_feedback": "Feedback"}
    context.user_data['contact_category'] = mapping[query.data]
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="cancel")]]
    
    await query.edit_message_text(
        f"📩 <b>{mapping[query.data]}</b>\n\nPlease type your message below.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_ADMIN_MSG

async def receive_admin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    msg = update.message.text
    category = context.user_data.get('contact_category', 'Message')
    
    admin_text = (
        f"📩 <b>New Message: {category}</b>\n\n"
        f"<b>From:</b> {user.first_name} (@{user.username or 'No Username'})\n"
        f"<b>ID:</b> <code>{user.id}</code>\n\n"
        f"<b>Message:</b>\n{msg}"
    )
    if ADMIN_ID:
        try: await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
        except Exception: pass
            
    await update.message.reply_text("✅ Your message has been sent successfully!")
    return await show_main_menu(update.message, user=user, is_edit=False)

# -----------------------------------------
# 5. PROMPT GENERATORS
# -----------------------------------------

async def prompt_school(query, context):
    keyboard = []
    for abbrev, full_name in SCHOOL_MAP.items():
        keyboard.append([InlineKeyboardButton(full_name, callback_data=f"sch_{abbrev}")])
    keyboard.append([InlineKeyboardButton("Back", callback_data="back_menu"), InlineKeyboardButton("Cancel", callback_data="cancel")])
    text = "*Step 1: Select your School (Fallback):*"
    if hasattr(query, 'edit_message_text'):
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await query.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SCHOOL

async def prompt_gpa_current(msg_obj, context):
    keyboard = [[InlineKeyboardButton("Back", callback_data="back_school"), InlineKeyboardButton("Cancel", callback_data="cancel")]]
    text = f"🏫 *School:* {context.user_data.get('school')}\n\n*Step 2: Enter your CURRENT semester GPA*\n(1.50 - 4.00):"
    if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GPA_CURRENT

async def prompt_gpa_next(msg_obj, context):
    keyboard = [[InlineKeyboardButton("Back", callback_data="back_gpacurr"), InlineKeyboardButton("Cancel", callback_data="cancel")]]
    text = "*Step 3: Enter your EXPECTED cumulative GPA after next semester*\n(1.50 - 4.00):"
    if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GPA_NEXT

async def prompt_gender(msg_obj, context):
    keyboard = [
        [InlineKeyboardButton("Male", callback_data="gen_Male")],
        [InlineKeyboardButton("Female", callback_data="gen_Female")],
        [InlineKeyboardButton("Back", callback_data="back_gpanext"), InlineKeyboardButton("Cancel", callback_data="cancel")]
    ]
    text = "*Step 4: Select your Gender:*"
    if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GENDER

async def prompt_dept1(query, context):
    depts = SCHOOL_DEPARTMENTS[context.user_data["school"]]
    keyboard = [[InlineKeyboardButton(d, callback_data=f"d1_{d}")] for d in depts]
    keyboard.append([InlineKeyboardButton("Back", callback_data="back_gender"), InlineKeyboardButton("Cancel", callback_data="cancel")])
    await query.edit_message_text("*Step 5: Select your 1st Choice Department:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return DEPT_1

async def prompt_dept2(query, context):
    school = context.user_data["school"]
    dept_1 = context.user_data["dept_first"]
    remaining_depts = [d for d in SCHOOL_DEPARTMENTS[school] if d != dept_1]
    keyboard = [[InlineKeyboardButton(d, callback_data=f"d2_{d}")] for d in remaining_depts]
    keyboard.append([InlineKeyboardButton("Back", callback_data="back_dept1"), InlineKeyboardButton("Cancel", callback_data="cancel")])
    await query.edit_message_text(f"🥇 *1st Choice:* {dept_1}\n\n*Step 6: Select your 2nd Choice Department:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return DEPT_2

async def prompt_confirm(query, context):
    summary = (
        "📋 *Please Confirm Your Information*\n\n"
        f"• *School:* {context.user_data['school']}\n"
        f"• *Current GPA:* {context.user_data['gpa_current']:.2f}\n"
        f"• *Expected GPA:* {context.user_data['gpa_next']:.2f}\n"
        f"• *Gender:* {context.user_data['gender']}\n"
        f"• *1st Choice:* {context.user_data['dept_first']}\n"
        f"• *2nd Choice:* {context.user_data['dept_second']}\n\n"
        "Is this correct?"
    )
    keyboard = [
        [InlineKeyboardButton("Confirm & Save", callback_data="cfm_save")],
        [InlineKeyboardButton("Back", callback_data="back_dept2"), InlineKeyboardButton("Cancel", callback_data="cancel")]
    ]
    await query.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CONFIRM

# -----------------------------------------
# 6. BOT WORKFLOW & LOGIC
# -----------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    msg_obj = update.message if update.message else update.callback_query.message
    
    if not await check_access_and_respond(msg_obj, context, user.id): return CHOOSING_ACTION
    
    if update.message:
        msg = await update.message.reply_text("⏳ Initializing bot...\n█ ░ ░ ░ ░ ░ ░ ░ ░ ░ 10%")
        for bar in ["█ █ ░ ░ ░ ░ ░ ░ ░ ░ 20%", "█ █ █ █ ░ ░ ░ ░ ░ ░ 40%", "█ █ █ █ █ █ ░ ░ ░ ░ 60%", "█ █ █ █ █ █ █ █ ░ ░ 80%", "█ █ █ █ █ █ █ █ █ █ 100%"]:
            await asyncio.sleep(0.2)
            await msg.edit_text(f"⏳ Initializing bot...\n{bar}")
        return await show_main_menu(msg, user=user, is_edit=True)
    elif update.callback_query:
        await update.callback_query.answer()
        return await show_main_menu(update.callback_query, user=user, is_edit=True)

async def action_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    
    if not await check_access_and_respond(query, context, user_id): return CHOOSING_ACTION
    await query.answer()

    conn = sqlite3.connect("astu_placement.db")
    c = conn.cursor()
    c.execute("SELECT is_verified, verified_school FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    
    is_verified = res and res[0] == 1
    verified_school = res[1] if res else None

    if not is_verified: return await prompt_verify_name(query, context)
    if verified_school and verified_school in SCHOOL_DEPARTMENTS:
        context.user_data["school"] = verified_school
        return await prompt_gpa_current(query, context)
    return await prompt_school(query, context)

async def action_mydata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    
    if not await check_access_and_respond(query, context, user_id): return CHOOSING_ACTION
    if not is_user_registered(user_id):
        await query.answer("⚠️ You must register first before using the bot!", show_alert=True)
        return CHOOSING_ACTION
    
    await query.answer()
    user_hash = get_user_hash(user_id)
    
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT school, gpa_current, gpa_next, gender, dept_first, dept_second, timestamp FROM submissions WHERE hash_id = ?", (user_hash,))
    res = cursor.fetchone()
    conn.close()
    
    keyboard = [[InlineKeyboardButton("Back to Main Menu", callback_data="back_menu")]]
    
    if not res:
        text = "ℹ️ *No Submission Found*\n\nYou have not submitted any placement data yet."
    else:
        text = (
            "📋 *Your Submitted Data*\n\n"
            f"• *School:* {res[0]}\n"
            f"• *Current GPA:* {res[1]:.2f}\n"
            f"• *Expected GPA:* {res[2]:.2f}\n"
            f"• *Gender:* {res[3]}\n"
            f"• *1st Choice:* {res[4]}\n"
            f"• *2nd Choice:* {res[5]}\n"
            f"• *Last Updated:* {res[6]}\n\n"
        )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHOOSING_ACTION

async def show_batch_menu(msg_obj):
    keyboard = [
        [InlineKeyboardButton("2018 Batch", callback_data="batch_2018")],
        [InlineKeyboardButton("2019 Batch", callback_data="batch_2019")],
        [InlineKeyboardButton("2020 Batch", callback_data="batch_2020")],
        [InlineKeyboardButton("Back to Main Menu", callback_data="back_menu")]
    ]
    text = "🎓 *Select Batch Year:*\n\nPlease select which batch placement data you would like to view:"
    if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def action_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await check_access_and_respond(query, context, query.from_user.id): return CHOOSING_ACTION
    if not is_user_registered(query.from_user.id):
        await query.answer("⚠️ You must register first!", show_alert=True)
        return CHOOSING_ACTION
    
    await query.answer()
    await show_batch_menu(query)
    return CHOOSING_ACTION

async def handle_batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_user_registered(query.from_user.id): return CHOOSING_ACTION

    batch = query.data.replace("batch_", "")
    if batch in ["2019", "2020"]:
        await query.answer(f"⚠️ Batch {batch} placement has not started yet!", show_alert=True)
        return CHOOSING_ACTION
    
    await query.answer()
    await show_view_stats(query)
    return CHOOSING_ACTION

async def school_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_user_verified(query.from_user.id): return await prompt_verify_name(query, context)
        
    context.user_data["school"] = SCHOOL_MAP.get(query.data.replace("sch_", ""), "Unknown School")
    return await prompt_gpa_current(query, context)

async def gpa_current_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        gpa = float(update.message.text.strip())
        if 1.50 <= gpa <= 4.00:
            context.user_data["gpa_current"] = round(gpa, 2)
            return await prompt_gpa_next(update.message, context)
        else:
            await update.message.reply_text("⚠️ Invalid GPA. (1.50 - 4.00):")
            return GPA_CURRENT
    except ValueError:
        await update.message.reply_text("⚠️ Invalid format. (1.50 - 4.00):")
        return GPA_CURRENT

async def gpa_next_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        gpa = float(update.message.text.strip())
        if 1.50 <= gpa <= 4.00:
            context.user_data["gpa_next"] = round(gpa, 2)
            return await prompt_gender(update.message, context)
        else:
            await update.message.reply_text("⚠️ Invalid GPA. (1.50 - 4.00):")
            return GPA_NEXT
    except ValueError:
        await update.message.reply_text("⚠️ Invalid format. (1.50 - 4.00):")
        return GPA_NEXT

async def gender_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["gender"] = query.data.replace("gen_", "")
    return await prompt_dept1(query, context)

async def dept_1_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["dept_first"] = query.data.replace("d1_", "")
    return await prompt_dept2(query, context)

async def dept_2_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["dept_second"] = query.data.replace("d2_", "")
    return await prompt_confirm(query, context)

async def confirm_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_hash = get_user_hash(user_id)

    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO submissions (hash_id, school, gpa_current, gpa_next, gender, dept_first, dept_second)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash_id) DO UPDATE SET
            school=excluded.school, gpa_current=excluded.gpa_current,
            gpa_next=excluded.gpa_next, gender=excluded.gender,
            dept_first=excluded.dept_first, dept_second=excluded.dept_second,
            timestamp=CURRENT_TIMESTAMP
    """, (user_hash, context.user_data["school"], context.user_data["gpa_current"],
          context.user_data["gpa_next"], context.user_data["gender"], 
          context.user_data["dept_first"], context.user_data["dept_second"]))
    conn.commit()
    conn.close()

    # TRIGGER AUTO-BACKUP FOR SUBMISSIONS
    await send_db_backup(context, caption=f"📦 Auto-Backup: New Submission by Hash {user_hash[:8]}")

    keyboard = [[InlineKeyboardButton("Back to Main Menu", callback_data="back_menu")]]
    await query.edit_message_text("✅ *Data saved successfully!*\n\nYour details have been updated.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHOOSING_ACTION

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = query.from_user
    await query.answer()
    data = query.data

    if data == "back_menu": return await show_main_menu(query, user=user, is_edit=True)
    elif data == "back_school": 
        conn = sqlite3.connect("astu_placement.db")
        c = conn.cursor()
        c.execute("SELECT verified_school FROM users WHERE user_id = ?", (user.id,))
        res = c.fetchone()
        conn.close()
        if res and res[0] in SCHOOL_DEPARTMENTS: return await show_main_menu(query, user=user, is_edit=True)
        else: return await prompt_school(query, context)
    elif data == "back_gpacurr": return await prompt_gpa_current(query, context)
    elif data == "back_gpanext": return await prompt_gpa_next(query, context)
    elif data == "back_gender": return await prompt_gender(query, context)
    elif data == "back_dept1": return await prompt_dept1(query, context)
    elif data == "back_dept2": return await prompt_dept2(query, context)

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = query.from_user
    await query.answer("Process cancelled.")
    return await show_main_menu(query, user=user, is_edit=True)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await update.message.reply_text("❌ Process cancelled.")
    return await show_main_menu(update.message, user=user, is_edit=False)

async def prompt_use_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⚠️ Please click one of the buttons on the message above.")

# -----------------------------------------
# 7. DATA VIEWING
# -----------------------------------------

async def show_view_stats(msg_obj):
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM submissions")
    total_count = cursor.fetchone()[0]

    if total_count == 0:
        text = "No student data has been recorded yet for 2018 Batch."
        keyboard = [[InlineKeyboardButton("Back", callback_data="action_view"), InlineKeyboardButton("Main Menu", callback_data="back_menu")]]
        if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        conn.close()
        return

    cursor.execute("SELECT DISTINCT dept_first FROM submissions ORDER BY dept_first")
    departments = cursor.fetchall()
    conn.close()

    keyboard = [[InlineKeyboardButton(d[0], callback_data=f"v_{d[0][:30]}")] for d in departments]
    keyboard.append([InlineKeyboardButton("Back", callback_data="action_view"), InlineKeyboardButton("Main Menu", callback_data="back_menu")])

    text = (f"📊 *ASTU Placement Statistics (2018 Batch)*\nTotal Anonymized Submissions: {total_count}\n\nSelect a department:")
    if hasattr(msg_obj, 'edit_message_text'): await msg_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def view_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    if not await check_access_and_respond(update.message, context, user_id): return CHOOSING_ACTION
    if not is_user_registered(user_id):
        await update.message.reply_text("⚠️ *Registration Required*", parse_mode="Markdown")
        return CHOOSING_ACTION
    await show_batch_menu(update.message)
    return CHOOSING_ACTION

async def handle_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_user_registered(query.from_user.id): return CHOOSING_ACTION
    await query.answer()
    dept_name = query.data.replace("v_", "")
    keyboard = [
        [InlineKeyboardButton("👨 Male Statistics", callback_data=f"vg_M_{dept_name[:30]}")],
        [InlineKeyboardButton("👩 Female Statistics", callback_data=f"vg_F_{dept_name[:30]}")],
        [InlineKeyboardButton("🔙 Back", callback_data="batch_2018")]
    ]
    await query.edit_message_text(f"📊 *Department:* {dept_name}\n\nSelect gender:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHOOSING_ACTION

async def handle_view_gender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_user_registered(query.from_user.id): return CHOOSING_ACTION
        
    await query.answer()
    parts = query.data.split("_", 2)
    gender_str = "Male" if parts[1] == "M" else "Female"
    dept_prefix = parts[2]
    
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT dept_first FROM submissions WHERE dept_first LIKE ? LIMIT 1", (f"{dept_prefix}%",))
    dept_result = cursor.fetchone()
    actual_dept = dept_result[0] if dept_result else dept_prefix
        
    cursor.execute("""
        SELECT COUNT(*), AVG(gpa_next), MAX(gpa_next), MIN(gpa_next)
        FROM submissions WHERE dept_first LIKE ? AND gender = ?
    """, (f"{dept_prefix}%", gender_str))
    count, avg_gpa, max_gpa, min_gpa = cursor.fetchone()

    if count == 0:
        await query.edit_message_text(f"⚠️ No data found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"v_{dept_prefix}")]]), parse_mode="Markdown")
        conn.close()
        return CHOOSING_ACTION

    cursor.execute("""
        SELECT gpa_current, gpa_next, dept_second FROM submissions WHERE dept_first LIKE ? AND gender = ? ORDER BY gpa_next DESC
    """, (f"{dept_prefix}%", gender_str))
    students = cursor.fetchall()
    conn.close()

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data=f"v_{actual_dept[:30]}")]]
    msg = f"📊 *Department: {actual_dept}*\n🚻 *Gender:* {gender_str}\n• *Total:* {count}\n• *High/Low/Avg:* {max_gpa:.2f} / {min_gpa:.2f} / {avg_gpa:.2f}\n\n"

    if len(students) <= 10:
        for i, (curr_gpa, next_gpa, dept_2) in enumerate(students, 1):
            msg += f"{i}. *{next_gpa:.2f}* (Curr: {curr_gpa:.2f}) | 2nd: {dept_2}\n"
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        msg += "📄 *List exceeds 10 students. Sending txt file...*"
        full_txt = f"=== {actual_dept} ({gender_str.upper()}) ===\nRank | Exp GPA | Curr GPA | 2nd Choice\n" + "-" * 40 + "\n"
        for i, (curr_gpa, next_gpa, dept_2) in enumerate(students, 1):
            full_txt += f"{i}. {next_gpa:.2f} | (Curr: {curr_gpa:.2f}) | 2nd: {dept_2}\n"
            
        bio = io.BytesIO(full_txt.encode('utf-8'))
        bio.name = f"{actual_dept.replace(' ', '_')}_{gender_str}_TopList.txt"
        
        await query.message.delete()
        await context.bot.send_document(chat_id=query.message.chat_id, document=bio, caption=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHOOSING_ACTION

# -----------------------------------------
# 8. ADMIN PANEL AND TOOLS
# -----------------------------------------

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id != ADMIN_ID: return CHOOSING_ACTION
    
    text = (
        "🛠️ *Admin Control Panel*\n\n"
        "Here are your available command tools:\n"
        "• `/users` - List all registered users\n"
        "• `/ban <user_id>` - Ban a user from the bot\n"
        "• `/unban <user_id>` - Unban a user\n"
        "• `/broadcast <message>` - Message everyone\n"
        "• `/dashboard` - View Bot Performance\n"
        "• `/togglebot` - Turn bot ON/OFF\n"
        "• `/backup` - Manual database backup\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return CHOOSING_ACTION

async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers a manual database backup to the admin."""
    if update.message.from_user.id != ADMIN_ID: return
    await update.message.reply_text("⏳ Generating manual backup...")
    await send_db_backup(context, caption="📦 Manual Database Backup")

async def admin_restore_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restores the DB when a valid .db file is sent directly by the Admin."""
    if update.message.from_user.id != ADMIN_ID: return
    
    doc = update.message.document
    if doc and doc.file_name.endswith('.db'):
        try:
            file = await context.bot.get_file(doc.file_id)
            await file.download_to_drive("astu_placement.db")
            await update.message.reply_text("✅ Database successfully restored.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to restore database: {e}")

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id != ADMIN_ID: return CHOOSING_ACTION
    conn = sqlite3.connect("astu_placement.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, first_name, is_banned FROM users")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No users registered yet.")
        return CHOOSING_ACTION

    msg = "👥 *Registered Users:*\n\n"
    for uid, uname, fname, banned in rows:
        uname_str = f"@{uname}" if uname else "No Username"
        ban_str = " ⛔(Banned)" if banned == 1 else ""
        msg += f"• {fname} ({uname_str}) - ID: `{uid}`{ban_str}\n"
    
    if len(msg) > 4000: msg = msg[:4000] + "\n... truncated"
    await update.message.reply_text(msg, parse_mode="Markdown")
    return CHOOSING_ACTION

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(context.args[0])
        conn = sqlite3.connect("astu_placement.db")
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
        if c.rowcount > 0: await update.message.reply_text(f"✅ User `{target_id}` banned.", parse_mode="Markdown")
        else: await update.message.reply_text("⚠️ User not found.")
        conn.commit()
        conn.close()
    except:
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode="Markdown")

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(context.args[0])
        conn = sqlite3.connect("astu_placement.db")
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
        if c.rowcount > 0: await update.message.reply_text(f"✅ User `{target_id}` unbanned.", parse_mode="Markdown")
        else: await update.message.reply_text("⚠️ User not found.")
        conn.commit()
        conn.close()
    except:
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode="Markdown")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    msg_content = " ".join(context.args)
    if not msg_content:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return
        
    conn = sqlite3.connect("astu_placement.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = c.fetchall()
    conn.close()
    
    success = 0
    await update.message.reply_text(f"⏳ Broadcasting to {len(users)} users...")
    
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 *Announcement:*\n\n{msg_content}", parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.05)
        except: pass
            
    await update.message.reply_text(f"✅ Delivered to {success}/{len(users)} users.")

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    
    conn = sqlite3.connect("astu_placement.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM submissions")
    subs_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_count = c.fetchone()[0]
    conn.close()
    
    status = "🟢 ACTIVE" if BOT_ACTIVE else "🔴 INACTIVE"
    text = f"📈 *Dashboard*\n\n• *Status:* {status}\n• *Users:* {users_count}\n• *Submissions:* {subs_count}\n• *Banned:* {banned_count}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def admin_togglebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if update.message.from_user.id != ADMIN_ID: return
    BOT_ACTIVE = not BOT_ACTIVE
    await update.message.reply_text(f"Bot global status changed to: {'🟢 ON' if BOT_ACTIVE else '🔴 OFF'}")

# -----------------------------------------
# 9. MAIN APPLICATION RUNNER
# -----------------------------------------

def main():
    init_db()

    try: asyncio.get_event_loop()
    except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

    BOT_TOKEN = "8671762395:AAGmNjfP8_wER5lbyvzpcrBEbsruURVvyaw"
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(CMD_START, start),
            CommandHandler("start", start)
        ],
        states={
            CHOOSING_ACTION: [
                CallbackQueryHandler(do_register, pattern="^do_register$"),
                CallbackQueryHandler(action_register, pattern="^action_register$"),
                CallbackQueryHandler(action_mydata, pattern="^action_mydata$"),
                CallbackQueryHandler(action_view, pattern="^action_view$"),
                CallbackQueryHandler(action_contact, pattern="^action_contact$"),
                CallbackQueryHandler(handle_contact_choice, pattern="^contact_"),
                CallbackQueryHandler(handle_batch_callback, pattern="^batch_"),
                CallbackQueryHandler(handle_view_callback, pattern="^v_"),
                CallbackQueryHandler(handle_view_gender_callback, pattern="^vg_"),
                CallbackQueryHandler(back_callback, pattern="^back_menu$"),
                CallbackQueryHandler(verify_sub_callback, pattern="^verify_sub$"),
            ],
            VERIFY_NAME: [
                MessageHandler(CUSTOM_TEXT, handle_verify_name_input),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$")
            ],
            AWAITING_ADMIN_MSG: [
                MessageHandler(CUSTOM_TEXT, receive_admin_msg),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$")
            ],
            SCHOOL: [
                CallbackQueryHandler(school_choice, pattern="^sch_"),
                CallbackQueryHandler(back_callback, pattern="^back_menu$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
                MessageHandler(CUSTOM_TEXT, prompt_use_inline)
            ],
            GPA_CURRENT: [
                MessageHandler(CUSTOM_TEXT, gpa_current_choice),
                CallbackQueryHandler(back_callback, pattern="^back_school$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$")
            ],
            GPA_NEXT: [
                MessageHandler(CUSTOM_TEXT, gpa_next_choice),
                CallbackQueryHandler(back_callback, pattern="^back_gpacurr$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$")
            ],
            GENDER: [
                CallbackQueryHandler(gender_choice, pattern="^gen_"),
                CallbackQueryHandler(back_callback, pattern="^back_gpanext$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
                MessageHandler(CUSTOM_TEXT, prompt_use_inline)
            ],
            DEPT_1: [
                CallbackQueryHandler(dept_1_choice, pattern="^d1_"),
                CallbackQueryHandler(back_callback, pattern="^back_gender$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
                MessageHandler(CUSTOM_TEXT, prompt_use_inline)
            ],
            DEPT_2: [
                CallbackQueryHandler(dept_2_choice, pattern="^d2_"),
                CallbackQueryHandler(back_callback, pattern="^back_dept1$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
                MessageHandler(CUSTOM_TEXT, prompt_use_inline)
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_choice, pattern="^cfm_save$"),
                CallbackQueryHandler(back_callback, pattern="^back_dept2$"),
                CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
                MessageHandler(CUSTOM_TEXT, prompt_use_inline)
            ],
        },
        fallbacks=[
            MessageHandler(CMD_START, start),
            MessageHandler(CMD_CANCEL, cancel_cmd),
            MessageHandler(CMD_VIEW, view_cmd),
            MessageHandler(CMD_USERS, admin_users),
            MessageHandler(CMD_ADMIN, admin_menu),
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("view", view_cmd),
            CommandHandler("users", admin_users),
            CommandHandler("admin", admin_menu)
        ],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("view", view_cmd))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("backup", admin_backup))  # New Manual Backup
    app.add_handler(CommandHandler("ban", admin_ban))
    app.add_handler(CommandHandler("unban", admin_unban))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("togglebot", admin_togglebot))
    
    app.add_handler(MessageHandler(CMD_VIEW, view_cmd))
    app.add_handler(MessageHandler(CMD_USERS, admin_users))
    app.add_handler(MessageHandler(CMD_ADMIN, admin_menu))
    
    # New Drag-and-Drop Restore Feature
    app.add_handler(MessageHandler(filters.Document.ALL, admin_restore_db))

    print("✅ ASTU Placement Bot is now running...")
    print("Press Ctrl+C to stop.")
    keep_alive()
    app.run_polling()

if __name__ == "__main__":
    main()