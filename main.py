"""
Tanishuv boti v2 (aiogram 3.x)
Anonim suhbat, VIP (pul / Stars / kanal taklifi / sovg'a), taklif mukofotlari,
suhbat ichida 1vs1 o'yinlar, AI suhbat, admin bilan aloqa, to'liq admin panel.
requirements.txt:  aiogram
"""
import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatMemberUpdated, KeyboardButton,
                           LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# =================== SOZLAMALAR ===================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8554402317"))      # BOSH ADMIN
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")         # tekin variant
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
# ===================================================

BOT_USERNAME = ""
PLANS = {"1": (30, "1 oylik"), "2": (60, "2 oylik"), "4": (120, "4 oylik"), "12": (365, "1 yillik")}

# ---------------------------------------------------------------- DB
db = sqlite3.connect("dating.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, gender TEXT, age INTEGER, city TEXT,
  photo TEXT, username TEXT, vip_until INTEGER DEFAULT 0,
  banned INTEGER DEFAULT 0, plan_chosen INTEGER DEFAULT 0, created INTEGER DEFAULT 0,
  ref_rewards INTEGER DEFAULT 0, chan_rewards INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS queue(user_id INTEGER PRIMARY KEY, gender TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS pairs(a INTEGER, b INTEGER);
CREATE TABLE IF NOT EXISTS watch(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(user_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS reports(reporter INTEGER, reported INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT, card TEXT);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount INTEGER,
  method TEXT, status TEXT DEFAULT 'new', receipt TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS referrals(invited_id INTEGER PRIMARY KEY, inviter_id INTEGER);
CREATE TABLE IF NOT EXISTS chan_links(user_id INTEGER PRIMARY KEY, link TEXT);
CREATE TABLE IF NOT EXISTS chan_joins(joiner_id INTEGER PRIMARY KEY, inviter_id INTEGER, left_ INTEGER DEFAULT 0);
""")
DEFAULTS = {
    "price_1": "50000", "price_2": "90000", "price_4": "180000", "price_12": "480000",
    "stars_1": "200", "stars_2": "360", "stars_4": "720", "stars_12": "1920",
    "ref_count": "20", "ref_days": "30", "chan_count": "100", "chan_days": "150",
    "gift_min": "250", "gift_days": "30", "photo_secs": "5", "ai_limit": "0",
    "channel": "", "intro": "",
}
for _k, _v in DEFAULTS.items():
    db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (_k, _v))
db.commit()
for _stmt in ("ALTER TABLE users ADD COLUMN created INTEGER DEFAULT 0",
              "ALTER TABLE users ADD COLUMN ref_rewards INTEGER DEFAULT 0",
              "ALTER TABLE users ADD COLUMN chan_rewards INTEGER DEFAULT 0"):
    try:
        db.execute(_stmt)
        db.commit()
    except sqlite3.OperationalError:
        pass


def q(sql, args=(), one=False, commit=False):
    cur = db.execute(sql, args)
    if commit:
        db.commit()
        return cur.lastrowid
    return cur.fetchone() if one else cur.fetchall()


def setting(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS.get(k, "")


def setint(k):
    d = re.sub(r"\D", "", setting(k))
    return int(d) if d else 0


def esc(s):
    return html.escape(str(s))


def money(n):
    return f"{int(n):,}".replace(",", " ")


def fdate(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def is_main(uid):
    return uid == ADMIN_ID


def is_admin(uid):
    return uid == ADMIN_ID or q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True) is not None


def admin_ids():
    ids = [ADMIN_ID]
    ids += [r["id"] for r in q("SELECT id FROM admins")]
    return ids


def is_vip(uid):
    u = get_user(uid)
    return bool(u and u["vip_until"] > time.time())


def grant_vip(uid, days):
    u = get_user(uid)
    new = int(max(time.time(), u["vip_until"]) + days * 86400)
    q("UPDATE users SET vip_until=? WHERE id=?", (new, uid), commit=True)
    return new


def partner(uid):
    r = q("SELECT * FROM pairs WHERE a=? OR b=?", (uid, uid), one=True)
    if not r:
        return None
    return r["b"] if r["a"] == uid else r["a"]


def today():
    return datetime.utcnow().strftime("%Y-%m-%d")


async def notify_admins(bot, text, kb=None, photo=None):
    for aid in admin_ids():
        try:
            if photo:
                await bot.send_photo(aid, photo, caption=text, reply_markup=kb)
            else:
                await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logging.warning("Adminga yuborib bo'lmadi %s: %s", aid, e)


# ---------------------------------------------------------------- Taqiqlangan kontaktlar
PHONE_RE = re.compile(r"(?:\d[\s\-\.\(\)]*){9,}")
HANDLE_RE = re.compile(r"@\w{3,}")
LINK_RE = re.compile(r"(https?://|www\.|t\.me|telegram\.me|wa\.me|instagram\.com|\.com\b|\.uz\b)", re.I)
APP_RE = re.compile(
    r"(telegram|telegramm|tg\b|insta|instagram|whatsapp|vatsap|watsap|snapchat|snap\b|tiktok|"
    r"facebook|viber|imo\b|телеграм|инстаграм|инста|ватсап|вацап|тикток)", re.I)


def has_contact(text):
    return bool(PHONE_RE.search(text) or HANDLE_RE.search(text)
                or LINK_RE.search(text) or APP_RE.search(text))


# ---------------------------------------------------------------- Filtrlar / holatlar
class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and is_admin(u.id))


ADM = IsAdmin()
MAIN = F.from_user.id == ADMIN_ID


class Reg(StatesGroup):
    name = State(); gender = State(); age = State(); city = State(); photo = State()


class Pay(StatesGroup):
    receipt = State()


class AI(StatesGroup):
    chat = State()


class Contact(StatesGroup):
    msg = State()


class Adm(StatesGroup):
    setting = State(); broadcast = State(); ban = State(); unban = State()
    find = State(); watch = State(); grant = State(); msg = State(); reply = State()
    addadmin = State(); addcard = State()


router = Router()
pending_ref = {}


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and u.id != ADMIN_ID:
            row = get_user(u.id)
            if row and row["banned"]:
                return
        return await handler(event, data)


# ---------------------------------------------------------------- Klaviaturalar
def main_menu(uid):
    b = ReplyKeyboardBuilder()
    for t in ("💬 Suhbatdosh topish", "🤖 AI suhbat", "💎 VIP obuna", "🤝 Do'st taklif qilish",
              "👤 Profilim", "📞 Admin bilan bog'lanish", "ℹ️ Yordam"):
        b.button(text=t)
    if is_admin(uid):
        b.button(text="🛠 Admin panel")
    b.adjust(2, 2, 2, 2)
    return b.as_markup(resize_keyboard=True)


def chat_kb():
    b = ReplyKeyboardBuilder()
    for t in ("⏭ Keyingisi", "⛔ Tugatish", "🖼 Rasmini ko'rish", "🔍 Kim bilan?", "🎮 O'yin", "⚠️ Shikoyat"):
        b.button(text=t)
    b.adjust(2, 2, 2)
    return b.as_markup(resize_keyboard=True)


def cancel_kb(text="❌ Bekor qilish"):
    b = ReplyKeyboardBuilder()
    b.button(text=text)
    return b.as_markup(resize_keyboard=True)


def inline(rows, width=2):
    b = InlineKeyboardBuilder()
    for text, data in rows:
        b.button(text=text, callback_data=data)
    b.adjust(width)
    return b.as_markup()


def kb_rows(rows):
    """rows = [[(text, data), ...], ...]"""
    b = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            b.button(text=text, callback_data=data)
    b.adjust(*[len(r) for r in rows])
    return b.as_markup()


RULES = ("📜 <b>Qoidalar</b>\n\n"
         "• Bot faqat <b>18 yosh va undan katta</b> foydalanuvchilar uchun.\n"
         "• Haqorat, tahdid va noqonuniy xatti-harakatlar taqiqlanadi.\n"
         "• Xavfsizlik maqsadida suhbatlar admin tomonidan kuzatilishi mumkin.\n"
         "• Tekin obunada telefon raqam, nik va havolalar yuborish taqiqlanadi.\n\n"
         "Davom etib, shu qoidalarga rozilik bildirasiz.")

INTRO = ("👋 <b>Botga xush kelibsiz!</b>\n\n"
         "Bu bot orqali yangi odamlar bilan <b>anonim</b> suhbatlashishingiz mumkin.\n\n"
         "<b>Qanday ishlaydi:</b>\n"
         "1️⃣ Qisqa ro'yxatdan o'tasiz (ism, jins, yosh, shahar, rasm).\n"
         "2️⃣ «💬 Suhbatdosh topish» ni bosasiz, bot qarama-qarshi jinsdan suhbatdosh topadi.\n"
         "3️⃣ Suhbat bot orqali o'tadi, bir-biringizning Telegram manzilingizni ko'rmaysiz.\n"
         "4️⃣ Suhbat paytida «🎮 O'yin» orqali suhbatdosh bilan 1vs1 o'ynashingiz mumkin.\n"
         "5️⃣ «⏭ Keyingisi» bilan boshqasini topasiz, «⛔ Tugatish» bilan tugatasiz.\n"
         "6️⃣ «🤖 AI suhbat» da sun'iy intellekt bilan psixolog, do'st yoki dugona sifatida gaplashasiz.\n\n"
         "🆓 <b>Tekin tarif:</b> matn, stiker va ovozli xabar yuborasiz. Telefon raqam, nik va havola "
         "yuborish taqiqlangan. Suhbatdosh rasmi {secs} soniya ko'rinadi.\n"
         "💎 <b>VIP:</b> cheklov yo'q, suhbatdosh kimligi va Telegram manzilini ko'rasiz, rasm doim ochiq.\n"
         "🤝 Do'stlaringizni taklif qilib VIP'ni <b>tekin</b> oling («🤝 Do'st taklif qilish»).\n\n"
         "⚠️ Xavfsizlik uchun suhbatlar admin tomonidan kuzatilishi mumkin.")


def intro_text():
    custom = setting("intro").strip()
    if custom:
        return esc(custom)
    return INTRO.replace("{secs}", setting("photo_secs"))


# ---------------------------------------------------------------- Obuna / reja
async def check_sub(bot, uid):
    ch = setting("channel").strip()
    if not ch or is_admin(uid):
        return True
    try:
        m = await bot.get_chat_member(ch, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("Obuna tekshirib bo'lmadi: %s", e)
        return True


async def gate(m, bot):
    uid = m.chat.id
    if not get_user(uid):
        await m.answer("Avval /start bosib ro'yxatdan o'ting.")
        return False
    if not await check_sub(bot, uid):
        ch = setting("channel").strip()
        kb = InlineKeyboardBuilder()
        if ch.startswith("@"):
            kb.button(text="📢 Obuna bo'lish", url="https://t.me/" + ch[1:])
        kb.button(text="✅ Tekshirish", callback_data="sub:check")
        kb.adjust(1)
        await m.answer("Botdan foydalanish uchun avval kanal/guruhga obuna bo'ling 👇",
                       reply_markup=kb.as_markup())
        return False
    if not get_user(uid)["plan_chosen"]:
        await m.answer("Tarifni tanlang:", reply_markup=inline(
            [("🆓 Tekin", "plan:free"), ("💎 VIP", "plan:vip")]))
        return False
    return True


@router.callback_query(F.data == "sub:check")
async def sub_check(c: CallbackQuery, bot: Bot):
    if await check_sub(bot, c.from_user.id):
        await c.message.delete()
        if await gate(c.message, bot):
            await c.message.answer("Menyu:", reply_markup=main_menu(c.from_user.id))
    else:
        await c.answer("Hali obuna bo'lmagansiz!", show_alert=True)


@router.callback_query(F.data.startswith("plan:"))
async def plan_choose(c: CallbackQuery):
    q("UPDATE users SET plan_chosen=1 WHERE id=?", (c.from_user.id,), commit=True)
    await c.message.delete()
    await c.message.answer("Menyu:", reply_markup=main_menu(c.from_user.id))
    if c.data == "plan:vip":
        await c.message.answer(vip_menu_text(), reply_markup=vip_menu_kb())


# ---------------------------------------------------------------- START / RO'YXAT
@router.message(F.text == "❌ Bekor qilish")
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id))


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id
    arg = command.args or ""
    if arg.startswith("ref_") and arg[4:].isdigit() and not get_user(uid):
        pending_ref[uid] = int(arg[4:])
    if not get_user(uid):
        await m.answer(intro_text(), reply_markup=ReplyKeyboardRemove())
        await m.answer(RULES)
        await m.answer("✏️ Ismingizni yozing:")
        await state.set_state(Reg.name)
        return
    if partner(uid):
        return await m.answer("Siz hozir suhbatdasiz.", reply_markup=chat_kb())
    if await gate(m, bot):
        await m.answer("Asosiy menyu:", reply_markup=main_menu(uid))


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:30])
    await m.answer("Jinsingizni tanlang:", reply_markup=inline([("👨 Erkak", "sex:m"), ("👩 Ayol", "sex:f")]))
    await state.set_state(Reg.gender)


@router.callback_query(Reg.gender, F.data.startswith("sex:"))
async def reg_gender(c: CallbackQuery, state: FSMContext):
    await state.update_data(gender=c.data[4:])
    await c.message.edit_text("Jins tanlandi ✅")
    await c.message.answer("Yoshingiz (raqam bilan):")
    await state.set_state(Reg.age)


@router.message(Reg.age, F.text)
async def reg_age(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Yoshni raqam bilan yozing.")
    age = int(m.text)
    if age < 18:
        await state.clear()
        return await m.answer("Kechirasiz, bot faqat 18 yosh va undan kattalar uchun.")
    if age > 80:
        return await m.answer("Yoshni to'g'ri kiriting.")
    await state.update_data(age=age)
    await m.answer("Shahringiz:")
    await state.set_state(Reg.city)


@router.message(Reg.city, F.text)
async def reg_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip()[:40])
    await m.answer("Profil rasmingizni yuboring 📷:")
    await state.set_state(Reg.photo)


@router.message(Reg.photo, F.photo)
async def reg_photo(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    fid = m.photo[-1].file_id
    q("INSERT OR REPLACE INTO users(id,name,gender,age,city,photo,username,created) VALUES(?,?,?,?,?,?,?,?)",
      (m.from_user.id, d["name"], d["gender"], d["age"], d["city"], fid,
       m.from_user.username or "", int(time.time())), commit=True)
    await state.clear()
    g = "Erkak" if d["gender"] == "m" else "Ayol"
    await notify_admins(bot, (f"🆕 <b>Yangi foydalanuvchi</b>\n👤 {esc(d['name'])}, {g}, {d['age']}\n"
                              f"🏙 {esc(d['city'])}\n🆔 <code>{m.from_user.id}</code>\n"
                              f"🔗 @{esc(m.from_user.username or '—')}"), photo=fid)
    await m.answer("✅ Ro'yxatdan o'tdingiz!")
    await process_referral(bot, m.from_user.id)
    if await gate(m, bot):
        await m.answer("Asosiy menyu:", reply_markup=main_menu(m.from_user.id))


@router.message(F.text == "ℹ️ Yordam")
async def help_cmd(m: Message):
    await m.answer(intro_text())


@router.message(F.text == "👤 Profilim")
async def profile(m: Message, bot: Bot):
    if not await gate(m, bot):
        return
    u = get_user(m.from_user.id)
    vip = "💎 VIP" if is_vip(u["id"]) else "🆓 Tekin"
    until = f" ({fdate(u['vip_until'])} gacha)" if is_vip(u["id"]) else ""
    await m.answer_photo(u["photo"], caption=(
        f"👤 {esc(u['name'])}, {u['age']} yosh\n🏙 {esc(u['city'])}\nTarif: {vip}{until}"))


# ---------------------------------------------------------------- VIP MENYU
def vip_menu_text():
    t = ("💎 <b>VIP obuna</b>\n\n"
         "✅ Suhbatdosh kimligi va Telegram manzilini ko'rish\n"
         "✅ Telefon raqam, nik va havolalar yuborish\n"
         "✅ Rasm, video va fayllar yuborish\n"
         "✅ Suhbatdosh rasmini cheklovsiz ko'rish\n\n<b>Narxlar:</b>\n")
    for k, (days, label) in PLANS.items():
        t += f"• {label}: <b>{money(setint('price_' + k))} so'm</b>\n"
    return t + "\nTo'lov usulini tanlang 👇"


def vip_menu_kb():
    return inline([
        ("💳 Pul o'tkazmasi", "vip:cash"),
        ("⭐ Telegram Stars", "vip:stars"),
        ("📢 Kanalga taklif qilib (tekin)", "vip:chan"),
        ("🎁 Adminga sovg'a qilib", "vip:gift"),
        ("🤝 Botga taklif qilib (tekin)", "vip:ref"),
    ], 1)


@router.message(F.text == "💎 VIP obuna")
async def vip_menu(m: Message, bot: Bot):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    u = get_user(m.from_user.id)
    head = f"💎 Sizda VIP {fdate(u['vip_until'])} gacha faol.\n\n" if is_vip(u["id"]) else ""
    await m.answer(head + vip_menu_text(), reply_markup=vip_menu_kb())


@router.callback_query(F.data == "vip:info")
async def vip_info_cb(c: CallbackQuery):
    await c.answer()
    await c.message.answer(vip_menu_text(), reply_markup=vip_menu_kb())


# ---- 1-usul: pul o'tkazmasi
@router.callback_query(F.data == "vip:cash")
async def vip_cash(c: CallbackQuery):
    await c.answer()
    rows = [(f"{label} — {money(setint('price_' + k))} so'm", f"cash:{k}") for k, (d, label) in PLANS.items()]
    await c.message.answer("Qaysi muddatga VIP olasiz?", reply_markup=inline(rows, 1))


@router.callback_query(F.data.startswith("cash:"))
async def cash_plan(c: CallbackQuery, state: FSMContext):
    k = c.data[5:]
    if k not in PLANS:
        return await c.answer()
    await c.answer()
    cards = q("SELECT card FROM cards")
    card_txt = "\n".join(f"<code>{esc(r['card'])}</code>" for r in cards) or "Karta kiritilmagan, admin bilan bog'laning."
    await state.set_state(Pay.receipt)
    await state.update_data(plan=k)
    await c.message.answer(
        f"💳 <b>{PLANS[k][1]} VIP</b> — <b>{money(setint('price_' + k))} so'm</b>\n\n"
        f"Shu kartaga o'tkazing:\n{card_txt}\n\n"
        "To'lovdan keyin <b>chek rasmini</b> yuboring 📸", reply_markup=cancel_kb())


@router.message(Pay.receipt, F.photo)
async def pay_receipt(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    k = d["plan"]
    amount = setint("price_" + k)
    pid = q("INSERT INTO payments(user_id,plan,amount,method,receipt,ts) VALUES(?,?,?,?,?,?)",
            (m.from_user.id, k, amount, "cash", m.photo[-1].file_id, int(time.time())), commit=True)
    u = get_user(m.from_user.id)
    await notify_admins(
        bot, (f"🧾 <b>VIP to'lov #{pid}</b>\n👤 {esc(u['name'])} | <code>{u['id']}</code>\n"
              f"📦 {PLANS[k][1]} — {money(amount)} so'm"),
        kb=inline([("✅ Tasdiqlash", f"pay:ok:{pid}"), ("❌ Rad etish", f"pay:no:{pid}")]),
        photo=m.photo[-1].file_id)
    await m.answer("⏳ Chek adminga yuborildi. Tekshirilgach VIP faollashadi.",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(ADM, F.data.startswith("pay:"))
async def admin_pay(c: CallbackQuery, bot: Bot):
    _, act, pid = c.data.split(":")
    p = q("SELECT * FROM payments WHERE id=?", (pid,), one=True)
    if not p or p["status"] != "new":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    if act == "ok":
        q("UPDATE payments SET status='ok' WHERE id=?", (pid,), commit=True)
        until = grant_vip(p["user_id"], PLANS[p["plan"]][0])
        await bot.send_message(p["user_id"], f"✅ To'lov tasdiqlandi! 💎 VIP {fdate(until)} gacha faollashdi.")
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n✅ Tasdiqlandi")
    else:
        q("UPDATE payments SET status='no' WHERE id=?", (pid,), commit=True)
        await bot.send_message(p["user_id"], "❌ To'lov tasdiqlanmadi. Admin bilan bog'laning.")
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n❌ Rad etildi")


# ---- 2-usul: Telegram Stars
@router.callback_query(F.data == "vip:stars")
async def vip_stars(c: CallbackQuery):
    await c.answer()
    rows = [(f"{label} — {setint('stars_' + k)} ⭐", f"stars:{k}") for k, (d, label) in PLANS.items()]
    await c.message.answer("⭐ Telegram Stars bilan to'lash. Muddatni tanlang:", reply_markup=inline(rows, 1))


@router.callback_query(F.data.startswith("stars:"))
async def stars_plan(c: CallbackQuery, bot: Bot):
    k = c.data[6:]
    if k not in PLANS:
        return await c.answer()
    await c.answer()
    stars = setint("stars_" + k)
    await bot.send_invoice(
        chat_id=c.from_user.id, title=f"VIP — {PLANS[k][1]}",
        description=f"{PLANS[k][1]} VIP obuna", payload=f"vip:{k}", currency="XTR",
        prices=[LabeledPrice(label=f"VIP {PLANS[k][1]}", amount=stars)])


@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def paid_ok(m: Message, bot: Bot):
    sp = m.successful_payment
    uid = m.from_user.id
    payload = sp.invoice_payload
    u = get_user(uid)
    name = u["name"] if u else str(uid)
    if payload.startswith("vip:"):
        k = payload[4:]
        until = grant_vip(uid, PLANS[k][0])
        q("INSERT INTO payments(user_id,plan,amount,method,status,ts) VALUES(?,?,?,?,?,?)",
          (uid, k, sp.total_amount, "stars", "ok", int(time.time())), commit=True)
        await m.answer(f"✅ To'lov qabul qilindi! 💎 VIP {fdate(until)} gacha faol.")
        await notify_admins(bot, f"⭐ {esc(name)} (<code>{uid}</code>) {PLANS[k][1]} VIP sotib oldi ({sp.total_amount}⭐)")
    elif payload.startswith("gift:"):
        gid = payload[5:]
        try:
            await bot.send_gift(user_id=ADMIN_ID, gift_id=gid, text=f"🎁 {name} dan sovg'a"[:120])
        except Exception as e:
            logging.error("Sovg'a yuborilmadi: %s", e)
            try:
                await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=sp.telegram_payment_charge_id)
                await m.answer("😔 Sovg'ani yuborib bo'lmadi, Stars qaytarildi. Boshqa usulni sinab ko'ring.")
            except Exception as e2:
                logging.error("Refund xato: %s", e2)
                await m.answer("😔 Sovg'ani yuborib bo'lmadi. Admin bilan bog'laning.")
            return
        days = setint("gift_days")
        until = grant_vip(uid, days)
        q("INSERT INTO payments(user_id,plan,amount,method,status,ts) VALUES(?,?,?,?,?,?)",
          (uid, "gift", sp.total_amount, "gift", "ok", int(time.time())), commit=True)
        await m.answer(f"🎁 Sovg'angiz adminga yuborildi, rahmat! 💎 VIP {fdate(until)} gacha faol.")
        await notify_admins(bot, f"🎁 {esc(name)} (<code>{uid}</code>) sizga sovg'a yubordi ({sp.total_amount}⭐)")


# ---- 3-usul: sovg'a
async def gift_options(bot):
    res = await bot.get_available_gifts()
    mins = setint("gift_min")
    gifts = sorted([g for g in res.gifts if g.star_count >= mins], key=lambda g: g.star_count)
    return gifts[:5]


@router.callback_query(F.data == "vip:gift")
async def vip_gift(c: CallbackQuery, bot: Bot):
    await c.answer()
    try:
        gifts = await gift_options(bot)
    except Exception as e:
        logging.error("Sovg'alar ro'yxati xato: %s", e)
        gifts = []
    if not gifts:
        return await c.message.answer("🎁 Hozircha sovg'a usuli mavjud emas. Boshqa usulni tanlang.")
    rows = []
    for g in gifts:
        emoji = getattr(getattr(g, "sticker", None), "emoji", None) or "🎁"
        rows.append((f"{emoji} {g.star_count} ⭐", f"gift:{g.id}"))
    await c.message.answer(
        "🎁 Admin uchun sovg'a tanlang (taxminan $5 va undan yuqori Telegram sovg'alari).\n"
        f"Sovg'a adminga yuboriladi va sizga 💎 VIP ({setint('gift_days')} kun) beriladi:",
        reply_markup=inline(rows, 1))


@router.callback_query(F.data.startswith("gift:"))
async def gift_pick(c: CallbackQuery, bot: Bot):
    gid = c.data[5:]
    await c.answer()
    try:
        gifts = await gift_options(bot)
    except Exception:
        gifts = []
    g = next((x for x in gifts if str(x.id) == gid), None)
    if not g:
        return await c.message.answer("Bu sovg'a hozir mavjud emas. Ro'yxatni qayta oching.")
    await bot.send_invoice(
        chat_id=c.from_user.id, title="🎁 Adminga sovg'a",
        description=f"Sovg'a adminga yuboriladi, sizga {setint('gift_days')} kun VIP beriladi",
        payload=f"gift:{gid}", currency="XTR",
        prices=[LabeledPrice(label="Sovg'a", amount=g.star_count)])


# ---------------------------------------------------------------- TAKLIF MUKOFOTLARI
async def process_referral(bot, uid):
    inv = pending_ref.pop(uid, None)
    if not inv or inv == uid or not get_user(inv):
        return
    if q("SELECT 1 FROM referrals WHERE invited_id=?", (uid,), one=True):
        return
    q("INSERT INTO referrals(invited_id,inviter_id) VALUES(?,?)", (uid, inv), commit=True)
    n = q("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (inv,), one=True)[0]
    need = max(1, setint("ref_count"))
    due = n // need - get_user(inv)["ref_rewards"]
    if due > 0:
        until = grant_vip(inv, setint("ref_days") * due)
        q("UPDATE users SET ref_rewards=ref_rewards+? WHERE id=?", (due, inv), commit=True)
        try:
            await bot.send_message(inv, f"🎉 {need} ta do'st taklif qildingiz! 💎 VIP {fdate(until)} gacha berildi.")
        except Exception:
            pass
    else:
        try:
            await bot.send_message(inv, f"🤝 Yangi do'st qo'shildi! Taklif qilinganlar: {n}")
        except Exception:
            pass


async def ref_info(m, uid):
    n = q("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (uid,), one=True)[0]
    need = max(1, setint("ref_count"))
    nxt = (n // need + 1) * need
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    await m.answer(
        f"🤝 <b>Do'st taklif qiling — VIP tekin!</b>\n\n"
        f"Har {need} ta taklif qilingan do'st uchun <b>{setint('ref_days')} kun VIP</b> beriladi.\n\n"
        f"Sizning havolangiz:\n{link}\n\n"
        f"👥 Taklif qilinganlar: <b>{n}</b>\n🎯 Keyingi mukofot: {nxt} tadan keyin")


@router.message(F.text == "🤝 Do'st taklif qilish")
async def ref_menu(m: Message):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    await ref_info(m, m.from_user.id)


@router.callback_query(F.data == "vip:ref")
async def ref_cb(c: CallbackQuery):
    await c.answer()
    await ref_info(c.message, c.from_user.id)


async def get_chan_link(bot, uid):
    r = q("SELECT link FROM chan_links WHERE user_id=?", (uid,), one=True)
    if r:
        return r["link"]
    ch = setting("channel").strip()
    if not ch:
        return None
    link = await bot.create_chat_invite_link(chat_id=ch, name=f"u{uid}")
    q("INSERT OR REPLACE INTO chan_links VALUES(?,?)", (uid, link.invite_link), commit=True)
    return link.invite_link


@router.callback_query(F.data == "vip:chan")
async def chan_info(c: CallbackQuery, bot: Bot):
    await c.answer()
    uid = c.from_user.id
    if not setting("channel").strip():
        return await c.message.answer("Kanal hali sozlanmagan. Keyinroq urinib ko'ring.")
    try:
        link = await get_chan_link(bot, uid)
    except Exception as e:
        logging.error("Kanal havolasi xato: %s", e)
        return await c.message.answer("Havola yaratib bo'lmadi. Admin bilan bog'laning.")
    n = q("SELECT COUNT(*) FROM chan_joins WHERE inviter_id=? AND left_=0", (uid,), one=True)[0]
    need = max(1, setint("chan_count"))
    days = setint("chan_days")
    await c.message.answer(
        f"📢 <b>Kanalga {need} kishi taklif qiling — {days} kun VIP tekin!</b>\n\n"
        f"Sizning shaxsiy havolangiz:\n{link}\n\n"
        f"👥 Qo'shilganlar: <b>{n}</b> / {need}\n"
        "Faqat shu havola orqali kanalga yangi qo'shilganlar hisoblanadi.",
        reply_markup=inline([("🔄 Yangilash", "vip:chan")], 1))


@router.chat_member()
async def on_chat_member(ev: ChatMemberUpdated, bot: Bot):
    ch = setting("channel").strip().lower()
    if not ch:
        return
    uname = ("@" + ev.chat.username.lower()) if ev.chat.username else ""
    if ch != uname and ch != str(ev.chat.id):
        return
    old, new = ev.old_chat_member.status, ev.new_chat_member.status
    joiner = ev.new_chat_member.user.id
    if new == "member" and old in ("left", "kicked") and ev.invite_link:
        row = q("SELECT user_id FROM chan_links WHERE link=?", (ev.invite_link.invite_link,), one=True)
        if not row or row["user_id"] == joiner:
            return
        if q("SELECT 1 FROM chan_joins WHERE joiner_id=?", (joiner,), one=True):
            q("UPDATE chan_joins SET left_=0 WHERE joiner_id=?", (joiner,), commit=True)
            return
        inv = row["user_id"]
        q("INSERT INTO chan_joins(joiner_id,inviter_id) VALUES(?,?)", (joiner, inv), commit=True)
        n = q("SELECT COUNT(*) FROM chan_joins WHERE inviter_id=? AND left_=0", (inv,), one=True)[0]
        need = max(1, setint("chan_count"))
        u = get_user(inv)
        if u:
            due = n // need - u["chan_rewards"]
            if due > 0:
                until = grant_vip(inv, setint("chan_days") * due)
                q("UPDATE users SET chan_rewards=chan_rewards+? WHERE id=?", (due, inv), commit=True)
                try:
                    await bot.send_message(inv, f"🎉 Kanalga {need} kishi taklif qildingiz! "
                                                f"💎 VIP {fdate(until)} gacha berildi.")
                except Exception:
                    pass
    elif new in ("left", "kicked") and old == "member":
        q("UPDATE chan_joins SET left_=1 WHERE joiner_id=?", (joiner,), commit=True)


# ---------------------------------------------------------------- ADMIN BILAN BOG'LANISH
@router.message(F.text == "📞 Admin bilan bog'lanish")
async def contact_start(m: Message, state: FSMContext):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    await state.set_state(Contact.msg)
    await m.answer("Adminga xabaringizni yozing (matn, rasm yoki ovozli xabar):", reply_markup=cancel_kb())


@router.message(Contact.msg)
async def contact_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = get_user(m.from_user.id)
    for aid in admin_ids():
        try:
            await bot.send_message(
                aid, f"📩 <b>Xabar</b>: {esc(u['name'])}\n🆔 <code>{u['id']}</code>",
                reply_markup=inline([("↩️ Javob berish", f"rp:{u['id']}")] +
                                    ([("🔎 Anketa", f"u:{u['id']}")] if aid == ADMIN_ID else []), 2))
            await bot.copy_message(aid, m.chat.id, m.message_id)
        except Exception as e:
            logging.warning("Aloqa xabari yuborilmadi: %s", e)
    await m.answer("✅ Xabaringiz adminga yuborildi. Javobni shu yerda olasiz.",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(ADM, F.data.startswith("rp:"))
async def reply_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.reply)
    await state.update_data(uid=int(c.data[3:]))
    await c.message.answer("Javobingizni yozing:")


@router.message(ADM, Adm.reply, F.text)
async def reply_send(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    try:
        await bot.send_message(d["uid"], "📩 <b>Admin javobi:</b>\n\n" + esc(m.text))
        await m.answer("✅ Yuborildi.")
    except Exception:
        await m.answer("Yuborib bo'lmadi.")


# ---------------------------------------------------------------- AI
PERSONAS = {
    "psy": ("Sen mehribon va e'tiborli psixolog-suhbatdoshsan. Foydalanuvchini diqqat bilan tingla, "
            "his-tuyg'ularini tushun, ochiq savollar ber, maslahatni yumshoq ber. Tashxis qo'yma va "
            "professional psixolog o'rnini bosmasligingni kerak bo'lsa ayt. Agar foydalanuvchi o'ziga "
            "zarar yetkazish yoki yashashni xohlamaslik haqida yozsa, uni jiddiy qabul qil, qo'llab-quvvatla, "
            "ishongan odamiga yoki mutaxassisga/favqulodda xizmatga murojaat qilishga yumshoq unda."),
    "friend": ("Sen foydalanuvchining samimiy do'sti (yigit) rolidasan: oddiy, iliq, ba'zan hazil bilan, "
               "qo'llab-quvvatlovchi uslubda gaplash."),
    "girl": ("Sen foydalanuvchining samimiy dugonasi (qiz) rolidasan: iliq, mehribon, tushunadigan "
             "uslubda gaplash, sirlarni tinglashga tayyor bo'l."),
}
COMMON_RULES = (" Asosan o'zbek tilida (lotin) yoz, foydalanuvchi boshqa tilda yozsa o'sha tilda javob ber. "
                "Javoblar qisqa (2-6 gap) va samimiy bo'lsin. Foydalanuvchi so'rasa, sen sun'iy intellekt "
                "ekaningni yashirma.")


async def ask_gemini(persona, history):
    contents = [{"role": "model" if h["role"] == "assistant" else "user",
                 "parts": [{"text": h["content"]}]} for h in history]
    payload = {"systemInstruction": {"parts": [{"text": PERSONAS[persona] + COMMON_RULES}]},
               "contents": contents, "generationConfig": {"maxOutputTokens": 700}}
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        logging.error("Gemini xato: %s", data)
        return None


async def ask_claude(persona, history):
    payload = {"model": AI_MODEL, "max_tokens": 700,
               "system": PERSONAS[persona] + COMMON_RULES, "messages": history}
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    if not text:
        logging.error("AI xato: %s", data)
    return text or None


def ai_enabled():
    return bool(GEMINI_API_KEY or ANTHROPIC_API_KEY)


async def ask_ai(persona, history):
    try:
        if GEMINI_API_KEY:
            return await ask_gemini(persona, history) or None
        if ANTHROPIC_API_KEY:
            return await ask_claude(persona, history)
    except Exception as e:
        logging.error("AI xato: %s", e)
    return None


@router.message(F.text == "🤖 AI suhbat")
async def ai_menu(m: Message, bot: Bot):
    if not await gate(m, bot):
        return
    if partner(m.from_user.id):
        return await m.answer("Avval suhbatni tugating.")
    if not ai_enabled():
        return await m.answer("AI bo'limi hozircha o'chiq.")
    await m.answer("Kim bilan suhbatlashmoqchisiz?", reply_markup=inline(
        [("🧠 Psixolog", "ai:psy"), ("🤝 Do'st", "ai:friend"), ("👭 Dugona", "ai:girl")], 3))


@router.callback_query(F.data.startswith("ai:"))
async def ai_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AI.chat)
    await state.update_data(persona=c.data[3:], history=[])
    await c.message.delete()
    await c.message.answer("Eshitaman, yozing 💬\n(Chiqish uchun «🔙 Chiqish» tugmasini bosing)",
                           reply_markup=cancel_kb("🔙 Chiqish"))


@router.message(AI.chat, F.text)
async def ai_chat(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    if m.text == "🔙 Chiqish":
        await state.clear()
        return await m.answer("AI suhbat tugadi.", reply_markup=main_menu(uid))
    limit = setint("ai_limit")
    if limit > 0 and not is_vip(uid):
        used = q("SELECT n FROM ai_usage WHERE user_id=? AND day=?", (uid, today()), one=True)
        if used and used["n"] >= limit:
            return await m.answer(f"Bugungi tekin limit ({limit} ta xabar) tugadi. "
                                  "Cheklovsiz foydalanish uchun 💎 VIP oling.",
                                  reply_markup=inline([("💎 VIP olish", "vip:info")], 1))
    d = await state.get_data()
    history = d["history"] + [{"role": "user", "content": m.text}]
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    reply = await ask_ai(d["persona"], history[-20:])
    if not reply:
        return await m.answer("Hozir javob bera olmayapman, birozdan keyin urinib ko'ring.")
    q("INSERT INTO ai_usage VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET n=n+1",
      (uid, today()), commit=True)
    history.append({"role": "assistant", "content": reply})
    await state.update_data(history=history[-20:])
    await m.answer(esc(reply))


# ---------------------------------------------------------------- SUHBATDOSH TOPISH
async def end_chat(uid, bot, notify=True):
    await end_game_for(uid, bot)
    p = partner(uid)
    q("DELETE FROM pairs WHERE a=? OR b=?", (uid, uid), commit=True)
    if p and notify:
        try:
            await bot.send_message(p, "⛔ Suhbatdosh suhbatni tugatdi.", reply_markup=main_menu(p))
        except Exception:
            pass
    return p


async def notify_pair(bot, a, b):
    ua, ub = get_user(a), get_user(b)
    for to, other in ((a, ub), (b, ua)):
        g = "👨 Erkak" if other["gender"] == "m" else "👩 Ayol"
        note = "" if is_vip(to) else "\n\nℹ️ Tekin tarifda telefon, nik va havolalar yuborish taqiqlangan."
        try:
            await bot.send_message(to, f"✅ Suhbatdosh topildi!\n{g}, {other['age']} yosh\n\n"
                                       f"Yozishingiz yoki «🎮 O'yin» ni bosishingiz mumkin 👇{note}",
                                   reply_markup=chat_kb())
        except Exception:
            pass


async def find_match(m, bot):
    uid = m.chat.id
    me = get_user(uid)
    want = "f" if me["gender"] == "m" else "m"
    row = q("SELECT user_id FROM queue WHERE gender=? AND user_id!=? ORDER BY ts LIMIT 1", (want, uid), one=True)
    if row:
        other = row["user_id"]
        q("DELETE FROM queue WHERE user_id IN (?,?)", (uid, other), commit=True)
        q("INSERT INTO pairs(a,b) VALUES(?,?)", (uid, other), commit=True)
        await notify_pair(bot, uid, other)
    else:
        q("INSERT OR REPLACE INTO queue VALUES(?,?,?)", (uid, me["gender"], int(time.time())), commit=True)
        await m.answer("🔎 Suhbatdosh qidirilmoqda... Topilganda xabar beraman.",
                       reply_markup=cancel_kb("❌ Qidiruvni bekor qilish"))


@router.message(F.text == "💬 Suhbatdosh topish")
async def search(m: Message, bot: Bot):
    if not await gate(m, bot):
        return
    if partner(m.from_user.id):
        return await m.answer("Siz allaqachon suhbatdasiz.", reply_markup=chat_kb())
    await find_match(m, bot)


@router.message(F.text == "❌ Qidiruvni bekor qilish")
async def cancel_search(m: Message):
    q("DELETE FROM queue WHERE user_id=?", (m.from_user.id,), commit=True)
    await m.answer("Qidiruv bekor qilindi.", reply_markup=main_menu(m.from_user.id))


@router.message(F.text == "⛔ Tugatish")
async def stop_chat(m: Message, bot: Bot):
    if not partner(m.from_user.id):
        return await m.answer("Siz suhbatda emassiz.", reply_markup=main_menu(m.from_user.id))
    await end_chat(m.from_user.id, bot)
    await m.answer("Suhbat tugatildi.", reply_markup=main_menu(m.from_user.id))


@router.message(F.text == "⏭ Keyingisi")
async def next_chat(m: Message, bot: Bot):
    if partner(m.from_user.id):
        await end_chat(m.from_user.id, bot)
    if await gate(m, bot):
        await find_match(m, bot)


@router.message(F.text == "🔍 Kim bilan?")
async def who(m: Message):
    p = partner(m.from_user.id)
    if not p:
        return await m.answer("Siz suhbatda emassiz.")
    if not is_vip(m.from_user.id):
        return await m.answer("🔒 Suhbatdosh kimligini va Telegram manzilini faqat VIP foydalanuvchilar ko'ra oladi.",
                              reply_markup=inline([("💎 VIP olish", "vip:info")], 1))
    u = get_user(p)
    link = f"@{u['username']}" if u["username"] else f'<a href="tg://user?id={p}">Profilga o\'tish</a>'
    await m.answer_photo(u["photo"], caption=(
        f"👤 {esc(u['name'])}, {u['age']} yosh\n🏙 {esc(u['city'])}\n🔗 {link}"))


bg_tasks = set()


async def delete_later(bot, chat_id, msg_id, secs):
    await asyncio.sleep(secs)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


@router.message(F.text == "🖼 Rasmini ko'rish")
async def view_photo(m: Message, bot: Bot):
    p = partner(m.from_user.id)
    if not p:
        return await m.answer("Siz suhbatda emassiz.")
    u = get_user(p)
    if is_vip(m.from_user.id):
        return await m.answer_photo(u["photo"], caption="🖼 Suhbatdosh rasmi", protect_content=True)
    secs = max(1, setint("photo_secs"))
    msg = await m.answer_photo(
        u["photo"], protect_content=True,
        caption=f"⏳ Bu rasm {secs} soniyadan keyin o'chadi.\n💎 VIP'da rasm doim ochiq turadi.")
    t = asyncio.create_task(delete_later(bot, m.chat.id, msg.message_id, secs))
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


@router.message(F.text == "⚠️ Shikoyat")
async def report(m: Message, bot: Bot):
    p = partner(m.from_user.id)
    if not p:
        return await m.answer("Siz suhbatda emassiz.")
    q("INSERT OR IGNORE INTO watch VALUES(?)", (p,), commit=True)
    q("INSERT INTO reports VALUES(?,?,?)", (m.from_user.id, p, int(time.time())), commit=True)
    await bot.send_message(ADMIN_ID, f"⚠️ <b>Shikoyat</b>\nShikoyatchi: <code>{m.from_user.id}</code>\n"
                                     f"Shikoyat qilingan: <code>{p}</code> (kuzatuvga olindi)",
                           reply_markup=inline([("🚫 Bloklash", f"ban:{p}"), ("🔎 Anketa", f"u:{p}")]))
    await m.answer("Shikoyat adminga yuborildi. Rahmat.")


# ---------------------------------------------------------------- O'YINLAR (1vs1)
GAMES = {"rps": "✊ Tosh-qaychi-qog'oz", "ttt": "❌⭕ X-O", "c4": "🔴🟡 4 ta qator",
         "dice": "🎲 Kub tashlash", "quiz": "🧠 Viktorina"}
games = {}
user_game = {}
game_seq = [0]

QUESTIONS = [
    ("O'zbekiston poytaxti qaysi shahar?", ["Samarqand", "Toshkent", "Buxoro"], 1),
    ("Eng katta okean qaysi?", ["Atlantika", "Hind", "Tinch"], 2),
    ("Yer Quyosh atrofida necha kunda aylanadi?", ["365", "300", "400"], 0),
    ("Suv necha gradusda qaynaydi?", ["50", "100", "150"], 1),
    ("Haftada necha kun bor?", ["5", "6", "7"], 2),
    ("Dunyodagi eng baland tog' qaysi?", ["Everest", "Elbrus", "Alp"], 0),
    ("Eng katta sayyora qaysi?", ["Mars", "Yupiter", "Venera"], 1),
    ("12 × 12 necha bo'ladi?", ["124", "144", "154"], 1),
    ("Qaysi hayvon «cho'l kemasi» deyiladi?", ["Ot", "Tuya", "Eshak"], 1),
    ("Yerning tabiiy yo'ldoshi nima?", ["Oy", "Mars", "Quyosh"], 0),
    ("Bir yilda necha oy bor?", ["10", "11", "12"], 2),
    ("Quyosh qaysi tomondan chiqadi?", ["G'arb", "Sharq", "Shimol"], 1),
]


def opp(g, uid):
    return g["players"][1] if g["players"][0] == uid else g["players"][0]


def finish(g, result):
    g["done"] = True
    g["result"] = result      # yutuvchi ID, 0 = durang, -1 = tugatildi


def final_text(g, uid):
    r = g["result"]
    if r == 0:
        return "🤝 <b>Durang!</b>"
    if r == -1:
        return "🚪 O'yin tugatildi."
    return "🏆 <b>Siz yutdingiz!</b>" if r == uid else "😔 <b>Raqib yutdi.</b>"


def exit_btn(g):
    return [("🚪 O'yinni tugatish", f"g:{g['id']}:x:0")]


# ---- Tosh-qaychi-qog'oz
RPS_E = {"R": "✊", "P": "✋", "S": "✌️"}
RPS_BEATS = {"R": "S", "S": "P", "P": "R"}


def rps_init(g):
    p = g["players"]
    g.update(moves={p[0]: None, p[1]: None}, score={p[0]: 0, p[1]: 0}, last=None)


def rps_move(g, uid, arg):
    if arg not in RPS_E or g["moves"][uid] is not None:
        return
    g["moves"][uid] = arg
    a, b = g["players"]
    if g["moves"][a] and g["moves"][b]:
        ma, mb = g["moves"][a], g["moves"][b]
        if ma == mb:
            win = 0
        else:
            win = a if RPS_BEATS[ma] == mb else b
        if win:
            g["score"][win] += 1
        g["last"] = (dict(g["moves"]), win)
        g["moves"] = {a: None, b: None}
        if win and g["score"][win] >= 2:
            finish(g, win)


def rps_render(g, uid):
    o = opp(g, uid)
    t = f"Hisob: Siz <b>{g['score'][uid]}</b> : <b>{g['score'][o]}</b> Raqib (2 tagacha)\n"
    if g["last"]:
        mv, win = g["last"]
        res = "Durang" if win == 0 else ("Siz yutdingiz" if win == uid else "Raqib yutdi")
        t += f"Oxirgi raund: {RPS_E[mv[uid]]} vs {RPS_E[mv[o]]} — {res}\n"
    if g["done"]:
        return t + "\n" + final_text(g, uid), []
    if g["moves"][uid]:
        return t + "\n⏳ Raqib tanlovini kutyapsiz...", [exit_btn(g)]
    row = [(RPS_E[k], f"g:{g['id']}:r:{k}") for k in ("R", "P", "S")]
    return t + "\nTanlang:", [row, exit_btn(g)]


# ---- X-O
TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def ttt_init(g):
    g.update(board=[""] * 9, turn=0)


def ttt_move(g, uid, arg):
    i = int(arg)
    if g["players"][g["turn"]] != uid or g["board"][i]:
        return
    mark = "X" if g["turn"] == 0 else "O"
    g["board"][i] = mark
    for a, b, c in TTT_LINES:
        if g["board"][a] == g["board"][b] == g["board"][c] == mark:
            return finish(g, uid)
    if all(g["board"]):
        return finish(g, 0)
    g["turn"] = 1 - g["turn"]


def ttt_render(g, uid):
    me_mark = "❌" if g["players"][0] == uid else "⭕"
    sym = {"X": "❌", "O": "⭕", "": "▫️"}
    rows = []
    for r in range(3):
        rows.append([(sym[g["board"][r * 3 + c]], f"g:{g['id']}:t:{r * 3 + c}") for c in range(3)])
    if g["done"]:
        return f"Siz: {me_mark}\n\n" + final_text(g, uid), []
    turn = "Sizning navbatingiz ✅" if g["players"][g["turn"]] == uid else "Raqib navbati ⏳"
    return f"Siz: {me_mark}\n{turn}", rows + [exit_btn(g)]


# ---- 4 ta qator
def c4_init(g):
    g.update(board=[[0] * 7 for _ in range(6)], turn=0)


def c4_win(board, mark):
    for r in range(6):
        for c in range(7):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                cells = [(r + dr * k, c + dc * k) for k in range(4)]
                if all(0 <= x < 6 and 0 <= y < 7 and board[x][y] == mark for x, y in cells):
                    return True
    return False


def c4_move(g, uid, arg):
    col = int(arg)
    if g["players"][g["turn"]] != uid:
        return
    for r in range(5, -1, -1):
        if g["board"][r][col] == 0:
            mark = g["turn"] + 1
            g["board"][r][col] = mark
            if c4_win(g["board"], mark):
                return finish(g, uid)
            if all(g["board"][0][c] for c in range(7)):
                return finish(g, 0)
            g["turn"] = 1 - g["turn"]
            return


def c4_render(g, uid):
    sym = {0: "⚪", 1: "🔴", 2: "🟡"}
    me = "🔴" if g["players"][0] == uid else "🟡"
    board = "\n".join("".join(sym[x] for x in row) for row in g["board"])
    board += "\n1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    if g["done"]:
        return f"Siz: {me}\n\n{board}\n\n" + final_text(g, uid), []
    turn = "Sizning navbatingiz ✅" if g["players"][g["turn"]] == uid else "Raqib navbati ⏳"
    row = [(str(c + 1), f"g:{g['id']}:c:{c}") for c in range(7)]
    return f"Siz: {me}\n\n{board}\n\n{turn}", [row, exit_btn(g)]


# ---- Kub tashlash
def dice_init(g):
    p = g["players"]
    g.update(rolls={p[0]: None, p[1]: None}, last=None)


async def dice_roll(bot, g, uid):
    if g["rolls"][uid] is not None:
        return
    msg = await bot.send_dice(uid, emoji="🎲")
    g["rolls"][uid] = msg.dice.value
    a, b = g["players"]
    if g["rolls"][a] is not None and g["rolls"][b] is not None:
        await asyncio.sleep(3.5)
        ra, rb = g["rolls"][a], g["rolls"][b]
        g["last"] = {a: ra, b: rb}
        if ra == rb:
            g["rolls"] = {a: None, b: None}
        else:
            finish(g, a if ra > rb else b)


def dice_render(g, uid):
    o = opp(g, uid)
    t = ""
    if g["last"]:
        t = f"Natija: Siz <b>{g['last'][uid]}</b> — Raqib <b>{g['last'][o]}</b>\n"
    if g["done"]:
        return t + "\n" + final_text(g, uid), []
    if g["last"]:
        t += "Durang! Qayta tashlang.\n"
    if g["rolls"][uid] is not None:
        return t + "\n⏳ Raqib tashlashini kutyapsiz...", [exit_btn(g)]
    return t + "\nKubni tashlang:", [[("🎲 Tashlash", f"g:{g['id']}:d:0")], exit_btn(g)]


# ---- Viktorina
def quiz_init(g):
    p = g["players"]
    g.update(qs=random.sample(QUESTIONS, 5), qi=0, ans={p[0]: None, p[1]: None},
             score={p[0]: 0, p[1]: 0}, last=None)


def quiz_move(g, uid, arg):
    if g["ans"][uid] is not None:
        return
    g["ans"][uid] = int(arg)
    a, b = g["players"]
    if g["ans"][a] is not None and g["ans"][b] is not None:
        qtext, opts, correct = g["qs"][g["qi"]]
        for p in (a, b):
            if g["ans"][p] == correct:
                g["score"][p] += 1
        g["last"] = (dict(g["ans"]), opts[correct])
        g["ans"] = {a: None, b: None}
        g["qi"] += 1
        if g["qi"] >= 5:
            sa, sb = g["score"][a], g["score"][b]
            finish(g, 0 if sa == sb else (a if sa > sb else b))


def quiz_render(g, uid):
    o = opp(g, uid)
    t = ""
    if g["last"]:
        ans, right = g["last"]
        t = f"Oldingi savol javobi: <b>{esc(right)}</b>\n"
    t += f"Hisob: Siz <b>{g['score'][uid]}</b> : <b>{g['score'][o]}</b> Raqib\n\n"
    if g["done"]:
        return t + final_text(g, uid), []
    qtext, opts, correct = g["qs"][g["qi"]]
    t += f"Savol {g['qi'] + 1}/5\n<b>{esc(qtext)}</b>"
    if g["ans"][uid] is not None:
        return t + "\n\n⏳ Javob qabul qilindi, raqibni kuting...", [exit_btn(g)]
    rows = [[(opt, f"g:{g['id']}:z:{i}")] for i, opt in enumerate(opts)]
    return t, rows + [exit_btn(g)]


INIT = {"rps": rps_init, "ttt": ttt_init, "c4": c4_init, "dice": dice_init, "quiz": quiz_init}
RENDER = {"rps": rps_render, "ttt": ttt_render, "c4": c4_render, "dice": dice_render, "quiz": quiz_render}
MOVE = {"r": ("rps", rps_move), "t": ("ttt", ttt_move), "c": ("c4", c4_move), "z": ("quiz", quiz_move)}


async def refresh(bot, g):
    for uid in g["players"]:
        text, rows = RENDER[g["type"]](g, uid)
        text = GAMES[g["type"]] + "\n\n" + text
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=g["msgs"][uid],
                                        reply_markup=kb_rows(rows) if rows else None)
        except Exception as e:
            logging.debug("O'yin xabari yangilanmadi: %s", e)


def cleanup(g):
    for uid in g["players"]:
        user_game.pop(uid, None)
    games.pop(g["id"], None)


async def end_game_for(uid, bot):
    gid = user_game.get(uid)
    g = games.get(gid) if gid else None
    if g:
        finish(g, -1)
        await refresh(bot, g)
        cleanup(g)


@router.message(F.text == "🎮 O'yin")
async def game_menu(m: Message):
    p = partner(m.from_user.id)
    if not p:
        return await m.answer("O'yin faqat suhbat paytida mavjud.")
    if m.from_user.id in user_game or p in user_game:
        return await m.answer("Hozir o'yin davom etmoqda.")
    await m.answer("🎮 Suhbatdosh bilan qaysi o'yinni o'ynaysiz?",
                   reply_markup=inline([(name, f"gi:{k}") for k, name in GAMES.items()], 1))


@router.callback_query(F.data.startswith("gi:"))
async def game_invite(c: CallbackQuery, bot: Bot):
    kind = c.data[3:]
    uid = c.from_user.id
    p = partner(uid)
    if kind not in GAMES or not p:
        return await c.answer("Suhbat tugagan.", show_alert=True)
    if uid in user_game or p in user_game:
        return await c.answer("Hozir o'yin davom etmoqda.", show_alert=True)
    await c.answer()
    await c.message.edit_text(f"⏳ «{GAMES[kind]}» taklifi yuborildi. Suhbatdosh javobini kuting...")
    await bot.send_message(p, f"🎮 Suhbatdosh sizni <b>{GAMES[kind]}</b> o'yiniga taklif qildi!",
                           reply_markup=inline([("✅ Qabul qilish", f"ga:{kind}:{uid}"),
                                                ("❌ Rad etish", f"gd:{uid}")]))


@router.callback_query(F.data.startswith("gd:"))
async def game_decline(c: CallbackQuery, bot: Bot):
    inviter = int(c.data[3:])
    await c.answer()
    await c.message.edit_text("❌ Taklifni rad etdingiz.")
    try:
        await bot.send_message(inviter, "😔 Suhbatdosh o'yin taklifini rad etdi.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("ga:"))
async def game_accept(c: CallbackQuery, bot: Bot):
    _, kind, inviter = c.data.split(":")
    inviter = int(inviter)
    uid = c.from_user.id
    if kind not in GAMES or partner(uid) != inviter:
        return await c.answer("Taklif eskirgan.", show_alert=True)
    if uid in user_game or inviter in user_game:
        return await c.answer("Hozir o'yin davom etmoqda.", show_alert=True)
    await c.answer()
    game_seq[0] += 1
    g = {"id": game_seq[0], "type": kind, "players": [inviter, uid], "msgs": {}, "done": False, "result": None}
    INIT[kind](g)
    games[g["id"]] = g
    user_game[inviter] = user_game[uid] = g["id"]
    await c.message.delete()
    for p in g["players"]:
        text, rows = RENDER[kind](g, p)
        msg = await bot.send_message(p, GAMES[kind] + "\n\n" + text, reply_markup=kb_rows(rows) if rows else None)
        g["msgs"][p] = msg.message_id


@router.callback_query(F.data.startswith("g:"))
async def game_cb(c: CallbackQuery, bot: Bot):
    parts = c.data.split(":")
    if len(parts) != 4:
        return await c.answer()
    _, gid, act, arg = parts
    g = games.get(int(gid))
    uid = c.from_user.id
    if not g or g["done"] or uid not in g["players"]:
        return await c.answer("O'yin tugagan.", show_alert=True)
    await c.answer()
    if act == "x":
        finish(g, -1)
    elif act == "d":
        await dice_roll(bot, g, uid)
    elif act in MOVE and MOVE[act][0] == g["type"]:
        MOVE[act][1](g, uid, arg)
    await refresh(bot, g)
    if g["done"]:
        cleanup(g)


# ---------------------------------------------------------------- ADMIN PANEL
SET_META = {
    "price_1": ("1 oylik VIP narxi (so'm)", True), "price_2": ("2 oylik VIP narxi (so'm)", True),
    "price_4": ("4 oylik VIP narxi (so'm)", True), "price_12": ("1 yillik VIP narxi (so'm)", True),
    "stars_1": ("1 oylik — Stars narxi", True), "stars_2": ("2 oylik — Stars narxi", True),
    "stars_4": ("4 oylik — Stars narxi", True), "stars_12": ("1 yillik — Stars narxi", True),
    "ref_count": ("Botga nechta do'st taklif qilsa VIP", True),
    "ref_days": ("Shu uchun necha kun VIP", True),
    "chan_count": ("Kanalga nechta kishi taklif qilsa VIP", True),
    "chan_days": ("Shu uchun necha kun VIP", True),
    "gift_min": ("Sovg'aning minimal narxi (Stars; $5 ≈ 250)", True),
    "gift_days": ("Sovg'a uchun necha kun VIP", True),
    "photo_secs": ("Tekin: rasm necha soniya ko'rinadi", True),
    "ai_limit": ("Tekin AI limit (kuniga, 0 = cheksiz)", True),
    "channel": ("Majburiy kanal/guruh (@username, o'chirish: -)", False),
    "intro": ("Kirish matni (standart: -)", False),
}
SET_GROUPS = {
    "price": ("💵 Narxlar (so'm)", ["price_1", "price_2", "price_4", "price_12"]),
    "stars": ("⭐ Stars narxlari", ["stars_1", "stars_2", "stars_4", "stars_12"]),
    "rew": ("🎁 Mukofotlar", ["ref_count", "ref_days", "chan_count", "chan_days", "gift_min", "gift_days"]),
    "other": ("⚙️ Boshqa", ["photo_secs", "ai_limit", "channel", "intro"]),
}


def admin_kb(uid):
    if is_main(uid):
        return inline([
            ("📊 Statistika", "adm:stats"), ("🔎 Foydalanuvchi qidirish", "adm:find"),
            ("👥 Foydalanuvchilar", "adm:users"), ("💬 Faol suhbatlar", "adm:chats"),
            ("👁 Kuzatuv ro'yxati", "adm:watchlist"), ("➕ Kuzatuvga qo'shish", "adm:watch"),
            ("💎 VIP berish", "adm:grant"), ("🧾 Kutilayotgan to'lovlar", "adm:pays"),
            ("💳 Kartalar", "adm:cards"), ("👮 Adminlar", "adm:admins"),
            ("⚙️ Sozlamalar", "adm:settings"), ("⚠️ Shikoyatlar", "adm:reports"),
            ("✉️ Shaxsiy xabar", "adm:msg"), ("📨 Hammaga xabar", "adm:bc"),
            ("🚫 Bloklash", "adm:ban"), ("♻️ Blokdan chiqarish", "adm:unban"),
            ("🚫 Bloklanganlar", "adm:banned"),
        ])
    return inline([("📊 Statistika", "adm:stats"), ("🧾 Kutilayotgan to'lovlar", "adm:pays")], 1)


@router.message(ADM, F.text == "🛠 Admin panel")
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb(m.from_user.id))


def n1(sql, args=()):
    return q(sql, args, one=True)[0]


@router.callback_query(ADM, F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    await c.answer()
    now = int(time.time())
    t = "📊 <b>Statistika</b>\n"
    t += "\n👥 Foydalanuvchilar: " + str(n1("SELECT COUNT(*) FROM users"))
    t += "\n👨 Erkak: " + str(n1("SELECT COUNT(*) FROM users WHERE gender='m'"))
    t += "  👩 Ayol: " + str(n1("SELECT COUNT(*) FROM users WHERE gender='f'"))
    t += "\n🆕 Bugun yangi: " + str(n1("SELECT COUNT(*) FROM users WHERE created>?", (now - 86400,)))
    t += "\n💎 Faol VIP: " + str(n1("SELECT COUNT(*) FROM users WHERE vip_until>?", (now,)))
    t += "\n💬 Faol suhbatlar: " + str(n1("SELECT COUNT(*) FROM pairs"))
    t += "\n🔎 Navbatda: " + str(n1("SELECT COUNT(*) FROM queue"))
    t += "\n🚫 Bloklangan: " + str(n1("SELECT COUNT(*) FROM users WHERE banned=1"))
    t += "\n⚠️ Shikoyatlar: " + str(n1("SELECT COUNT(*) FROM reports"))
    t += "\n🤝 Bot takliflari: " + str(n1("SELECT COUNT(*) FROM referrals"))
    t += "\n📢 Kanal takliflari: " + str(n1("SELECT COUNT(*) FROM chan_joins WHERE left_=0"))
    cash = q("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE method='cash' AND status='ok'", one=True)
    stars = q("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE method='stars' AND status='ok'", one=True)
    gifts = n1("SELECT COUNT(*) FROM payments WHERE method='gift' AND status='ok'")
    t += f"\n\n💳 O'tkazma: {cash[0]} ta, {money(cash[1])} so'm"
    t += f"\n⭐ Stars: {stars[0]} ta, {stars[1]} ⭐"
    t += f"\n🎁 Sovg'alar: {gifts} ta"
    await c.message.answer(t)


@router.callback_query(ADM, F.data == "adm:pays")
async def adm_pays(c: CallbackQuery):
    await c.answer()
    rows = q("SELECT * FROM payments WHERE status='new' AND method='cash' ORDER BY id DESC LIMIT 15")
    if not rows:
        return await c.message.answer("Kutilayotgan to'lovlar yo'q.")
    for p in rows:
        u = get_user(p["user_id"])
        await c.message.answer_photo(
            p["receipt"], caption=(f"🧾 <b>#{p['id']}</b> {esc(u['name'])} | <code>{u['id']}</code>\n"
                                   f"📦 {PLANS[p['plan']][1]} — {money(p['amount'])} so'm"),
            reply_markup=inline([("✅ Tasdiqlash", f"pay:ok:{p['id']}"), ("❌ Rad etish", f"pay:no:{p['id']}")]))


# ---- foydalanuvchi anketasi
async def send_user_card(m, uid):
    u = get_user(uid)
    if not u:
        return await m.answer("Foydalanuvchi topilmadi.")
    g = "Erkak" if u["gender"] == "m" else "Ayol"
    vip = f"{fdate(u['vip_until'])} gacha" if u["vip_until"] > time.time() else "yo'q"
    role = "👮 Admin" if is_admin(uid) else "Foydalanuvchi"
    inv = n1("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (uid,))
    watched = q("SELECT 1 FROM watch WHERE user_id=?", (uid,), one=True) is not None
    chat = partner(uid)
    cap = (f"👤 <b>{esc(u['name'])}</b> ({role})\n{g}, {u['age']} yosh\n🏙 {esc(u['city'])}\n"
           f"🆔 <code>{uid}</code>\n🔗 @{esc(u['username'] or '—')}\n💎 VIP: {vip}\n"
           f"🤝 Taklif qilganlari: {inv}\n"
           f"💬 Hozir suhbatda: {'ha (' + str(chat) + ')' if chat else 'yo`q'}\n"
           f"👁 Kuzatuvda: {'ha' if watched else 'yo`q'}\n🚫 Bloklangan: {'ha' if u['banned'] else 'yo`q'}")
    kb = inline([
        ("🙈 Kuzatuvni to'xtatish" if watched else "👁 Chatini kuzatish", f"uw:{uid}" if watched else f"wa:{uid}"),
        ("💎 VIP berish", f"gv:{uid}"), ("❌ VIP olish", f"rv:{uid}"),
        ("✉️ Xabar yozish", f"rp:{uid}"), ("⛔ Suhbatni uzish", f"kick:{uid}"),
        ("♻️ Blokdan chiqarish" if u["banned"] else "🚫 Bloklash", f"unban:{uid}" if u["banned"] else f"ban:{uid}"),
    ])
    await m.answer_photo(u["photo"], caption=cap.replace("yo`q", "yo'q"), reply_markup=kb)


def users_buttons(rows):
    return inline([(f"{r['name']} | {r['age']} | {r['city']}", f"u:{r['id']}") for r in rows], 1)


def search_users(t):
    t = t.strip()
    if t.isdigit():
        r = get_user(int(t))
        return [r] if r else []
    if t.startswith("@"):
        return q("SELECT * FROM users WHERE username=? COLLATE NOCASE", (t[1:],))
    like = "%" + t + "%"
    return q("SELECT * FROM users WHERE name LIKE ? OR username LIKE ? OR city LIKE ? LIMIT 10", (like, like, like))


@router.callback_query(MAIN, F.data.startswith("u:"))
async def user_card_cb(c: CallbackQuery):
    await c.answer()
    await send_user_card(c.message, int(c.data[2:]))


@router.message(MAIN, Adm.find, F.text)
async def adm_find(m: Message, state: FSMContext):
    await state.clear()
    rows = search_users(m.text)
    if not rows:
        return await m.answer("Hech narsa topilmadi.")
    if len(rows) == 1:
        return await send_user_card(m, rows[0]["id"])
    await m.answer("Topilganlar, birini tanlang:", reply_markup=users_buttons(rows))


@router.callback_query(MAIN, F.data.startswith("wa:"))
async def watch_add_cb(c: CallbackQuery):
    q("INSERT OR IGNORE INTO watch VALUES(?)", (int(c.data[3:]),), commit=True)
    await c.answer("Kuzatuv boshlandi 👁 Endi uning xabarlari sizga yuboriladi.", show_alert=True)


@router.callback_query(MAIN, F.data.startswith("uw:"))
async def watch_del_cb(c: CallbackQuery):
    q("DELETE FROM watch WHERE user_id=?", (int(c.data[3:]),), commit=True)
    await c.answer("Kuzatuv to'xtatildi", show_alert=True)


@router.callback_query(MAIN, F.data.startswith("gv:"))
async def give_vip_cb(c: CallbackQuery, bot: Bot):
    uid = int(c.data[3:])
    until = grant_vip(uid, 30)
    await c.answer("💎 30 kun VIP berildi", show_alert=True)
    try:
        await bot.send_message(uid, f"💎 Sizga VIP berildi! {fdate(until)} gacha faol.")
    except Exception:
        pass


@router.callback_query(MAIN, F.data.startswith("rv:"))
async def revoke_vip_cb(c: CallbackQuery):
    q("UPDATE users SET vip_until=0 WHERE id=?", (int(c.data[3:]),), commit=True)
    await c.answer("VIP olib tashlandi", show_alert=True)


@router.callback_query(MAIN, F.data.startswith("kick:"))
async def kick_cb(c: CallbackQuery, bot: Bot):
    uid = int(c.data[5:])
    p = await end_chat(uid, bot)
    try:
        await bot.send_message(uid, "⛔ Suhbat admin tomonidan tugatildi.", reply_markup=main_menu(uid))
    except Exception:
        pass
    await c.answer("Suhbat uzildi" if p else "Suhbat topilmadi", show_alert=True)


async def do_ban(uid, bot):
    q("UPDATE users SET banned=1 WHERE id=?", (uid,), commit=True)
    q("DELETE FROM queue WHERE user_id=?", (uid,), commit=True)
    await end_chat(uid, bot)


@router.callback_query(MAIN, F.data.startswith("ban:"))
async def ban_cb(c: CallbackQuery, bot: Bot):
    await do_ban(int(c.data[4:]), bot)
    await c.answer("Bloklandi", show_alert=True)


@router.callback_query(MAIN, F.data.startswith("unban:"))
async def unban_cb(c: CallbackQuery):
    q("UPDATE users SET banned=0 WHERE id=?", (int(c.data[6:]),), commit=True)
    await c.answer("Blokdan chiqarildi", show_alert=True)


# ---- sozlamalar
@router.callback_query(MAIN, F.data == "adm:settings")
async def adm_settings(c: CallbackQuery):
    await c.answer()
    await c.message.answer("⚙️ Qaysi bo'limni o'zgartirasiz?", reply_markup=inline(
        [(name, f"sg:{k}") for k, (name, keys) in SET_GROUPS.items()], 2))


@router.callback_query(MAIN, F.data.startswith("sg:"))
async def adm_setgroup(c: CallbackQuery):
    await c.answer()
    name, keys = SET_GROUPS[c.data[3:]]
    t = f"<b>{name}</b>\n\n"
    for k in keys:
        v = setting(k) or "—"
        if k == "intro":
            v = "(maxsus matn)" if setting(k) else "(standart)"
        t += f"• {SET_META[k][0]}: <b>{esc(v)}</b>\n"
    await c.message.answer(t, reply_markup=inline([(SET_META[k][0][:40], f"set:{k}") for k in keys], 1))


@router.callback_query(MAIN, F.data.startswith("set:"))
async def set_start(c: CallbackQuery, state: FSMContext):
    key = c.data[4:]
    await c.answer()
    await state.set_state(Adm.setting)
    await state.update_data(key=key)
    await c.message.answer(f"{SET_META[key][0]}\nHozirgi: <b>{esc(setting(key) or '—')}</b>\n\nYangi qiymatni yozing:")


@router.message(MAIN, Adm.setting, F.text)
async def set_save(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d["key"]
    val = m.text.strip()
    if SET_META[key][1]:
        if not val.isdigit():
            return await m.answer("Faqat raqam yozing.")
    elif val == "-":
        val = ""
    q("UPDATE settings SET v=? WHERE k=?", (val, key), commit=True)
    await state.clear()
    await m.answer("✅ Saqlandi.")


# ---- kartalar
@router.callback_query(MAIN, F.data == "adm:cards")
async def adm_cards(c: CallbackQuery):
    await c.answer()
    rows = q("SELECT * FROM cards")
    for r in rows:
        await c.message.answer(f"💳 <code>{esc(r['card'])}</code>",
                               reply_markup=inline([("🗑 O'chirish", f"dc:{r['id']}")], 1))
    if not rows:
        await c.message.answer("Karta yo'q.")
    await c.message.answer("Foydalanuvchilarga barcha kartalar ko'rsatiladi.",
                           reply_markup=inline([("➕ Karta qo'shish", "addcard")], 1))


@router.callback_query(MAIN, F.data == "addcard")
async def addcard_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.addcard)
    await c.message.answer("Karta raqami va egasi ismini yozing\n(masalan: 8600 1234 5678 9012 Ali V.):")


@router.message(MAIN, Adm.addcard, F.text)
async def addcard_save(m: Message, state: FSMContext):
    await state.clear()
    q("INSERT INTO cards(card) VALUES(?)", (m.text.strip()[:80],), commit=True)
    await m.answer("✅ Karta qo'shildi.")


@router.callback_query(MAIN, F.data.startswith("dc:"))
async def delcard(c: CallbackQuery):
    q("DELETE FROM cards WHERE id=?", (int(c.data[3:]),), commit=True)
    await c.answer("O'chirildi", show_alert=True)
    await c.message.delete()


# ---- adminlar
@router.callback_query(MAIN, F.data == "adm:admins")
async def adm_admins(c: CallbackQuery):
    await c.answer()
    rows = q("SELECT * FROM admins")
    for r in rows:
        u = get_user(r["id"])
        name = u["name"] if u else "Noma'lum"
        await c.message.answer(f"👮 {esc(name)} (<code>{r['id']}</code>)",
                               reply_markup=inline([("🗑 Olib tashlash", f"da:{r['id']}")], 1))
    if not rows:
        await c.message.answer("Qo'shimcha adminlar yo'q.")
    await c.message.answer("Yangi admin qo'shish:", reply_markup=inline([("➕ Admin qo'shish", "addadmin")], 1))


@router.callback_query(MAIN, F.data == "addadmin")
async def addadmin_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.addadmin)
    await c.message.answer("Yangi admin ID yoki @username ini yozing.\n"
                           "(U avval botga /start bosib ro'yxatdan o'tgan bo'lishi kerak)")


@router.message(MAIN, Adm.addadmin, F.text)
async def addadmin_save(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    rows = search_users(m.text)
    if len(rows) != 1:
        return await m.answer("Aniq bitta foydalanuvchi topilmadi. ID bilan urinib ko'ring.")
    u = rows[0]
    if is_admin(u["id"]):
        return await m.answer("Bu foydalanuvchi allaqachon admin.")
    q("INSERT OR IGNORE INTO admins VALUES(?)", (u["id"],), commit=True)
    await m.answer(f"✅ {esc(u['name'])} admin qilindi.")
    try:
        await bot.send_message(u["id"], "👮 Siz admin etib tayinlandingiz! /start bosing, menyuda «🛠 Admin panel» chiqadi. "
                                        "Sizga to'lov cheklari va foydalanuvchi xabarlari keladi.")
    except Exception:
        pass


@router.callback_query(MAIN, F.data.startswith("da:"))
async def deladmin(c: CallbackQuery, bot: Bot):
    uid = int(c.data[3:])
    q("DELETE FROM admins WHERE id=?", (uid,), commit=True)
    await c.answer("Admin olib tashlandi", show_alert=True)
    await c.message.delete()
    try:
        await bot.send_message(uid, "Siz endi admin emassiz.", reply_markup=main_menu(uid))
    except Exception:
        pass


# ---- ro'yxatlar va boshqa amallar
@router.callback_query(MAIN, F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    act = c.data[4:]
    await c.answer()
    if act == "find":
        await state.set_state(Adm.find)
        await c.message.answer("Qidirish uchun ism, shahar, @username yoki ID yozing:")
    elif act == "users":
        rows = q("SELECT * FROM users ORDER BY rowid DESC LIMIT 20")
        if not rows:
            return await c.message.answer("Foydalanuvchilar yo'q.")
        await c.message.answer("👥 Oxirgi 20 ta. Anketasini ko'rish uchun tanlang:", reply_markup=users_buttons(rows))
    elif act == "chats":
        rows = q("SELECT * FROM pairs LIMIT 20")
        if not rows:
            return await c.message.answer("Faol suhbatlar yo'q.")
        for r in rows:
            ua, ub = get_user(r["a"]), get_user(r["b"])
            await c.message.answer(
                f"💬 {esc(ua['name'])} ({r['a']}) ↔ {esc(ub['name'])} ({r['b']})",
                reply_markup=inline([("👁 Kuzatish", f"wp:{r['a']}:{r['b']}"), ("⛔ Uzish", f"kick:{r['a']}")]))
    elif act == "watchlist":
        rows = q("SELECT user_id FROM watch")
        if not rows:
            return await c.message.answer("Kuzatuv ro'yxati bo'sh.")
        await c.message.answer("👁 Kuzatuvdagilar (anketa uchun tanlang):", reply_markup=inline(
            [(f"{get_user(r['user_id'])['name'] if get_user(r['user_id']) else r['user_id']}", f"u:{r['user_id']}")
             for r in rows], 1))
        await c.message.answer("Hammasini to'xtatish:", reply_markup=inline([("🧹 Tozalash", "wclear")], 1))
    elif act == "watch":
        await state.set_state(Adm.watch)
        await c.message.answer("Kuzatiladigan foydalanuvchi ID sini yozing:")
    elif act == "grant":
        await state.set_state(Adm.grant)
        await c.message.answer("Format: <code>ID KUN</code> (masalan: 123456789 30)")
    elif act == "msg":
        await state.set_state(Adm.msg)
        await c.message.answer("Format: <code>ID matn</code> (masalan: 123456789 Salom!)")
    elif act == "reports":
        rows = q("SELECT * FROM reports ORDER BY rowid DESC LIMIT 10")
        if not rows:
            return await c.message.answer("Shikoyatlar yo'q.")
        for r in rows:
            await c.message.answer(
                f"⚠️ {datetime.fromtimestamp(r['ts']).strftime('%d.%m %H:%M')}\n"
                f"Shikoyatchi: <code>{r['reporter']}</code>\nShikoyat qilingan: <code>{r['reported']}</code>",
                reply_markup=inline([("🔎 Anketa", f"u:{r['reported']}"), ("🚫 Bloklash", f"ban:{r['reported']}")]))
    elif act == "banned":
        rows = q("SELECT * FROM users WHERE banned=1 LIMIT 30")
        if not rows:
            return await c.message.answer("Bloklanganlar yo'q.")
        for r in rows:
            await c.message.answer(f"🚫 {esc(r['name'])} (<code>{r['id']}</code>)",
                                   reply_markup=inline([("♻️ Blokdan chiqarish", f"unban:{r['id']}")], 1))
    elif act == "bc":
        await state.set_state(Adm.broadcast)
        await c.message.answer("Hammaga yuboriladigan xabarni yozing:")
    elif act == "ban":
        await state.set_state(Adm.ban)
        await c.message.answer("Bloklanadigan ID ni yozing:")
    elif act == "unban":
        await state.set_state(Adm.unban)
        await c.message.answer("Blokdan chiqariladigan ID ni yozing:")


@router.callback_query(MAIN, F.data.startswith("wp:"))
async def watch_pair(c: CallbackQuery):
    _, a, b = c.data.split(":")
    q("INSERT OR IGNORE INTO watch VALUES(?)", (int(a),), commit=True)
    q("INSERT OR IGNORE INTO watch VALUES(?)", (int(b),), commit=True)
    await c.answer("Kuzatuv boshlandi 👁", show_alert=True)


@router.callback_query(MAIN, F.data == "wclear")
async def watch_clear(c: CallbackQuery):
    q("DELETE FROM watch", commit=True)
    await c.answer("Tozalandi", show_alert=True)


@router.message(MAIN, Adm.watch, F.text)
async def adm_watch(m: Message, state: FSMContext):
    await state.clear()
    if m.text.strip().isdigit():
        q("INSERT OR IGNORE INTO watch VALUES(?)", (int(m.text),), commit=True)
        await m.answer("👁 Kuzatuvga qo'shildi.")


@router.message(MAIN, Adm.grant, F.text)
async def adm_grant(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    parts = m.text.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and get_user(int(parts[0])):
        uid, days = int(parts[0]), int(parts[1])
        until = grant_vip(uid, days)
        await m.answer(f"💎 VIP berildi ({fdate(until)} gacha).")
        try:
            await bot.send_message(uid, f"💎 Sizga {days} kunlik VIP berildi!")
        except Exception:
            pass
    else:
        await m.answer("Format noto'g'ri yoki foydalanuvchi topilmadi.")


@router.message(MAIN, Adm.msg, F.text)
async def adm_msg(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    parts = m.text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].isdigit():
        try:
            await bot.send_message(int(parts[0]), esc(parts[1]))
            await m.answer("✉️ Yuborildi.")
        except Exception:
            await m.answer("Yuborib bo'lmadi.")
    else:
        await m.answer("Format noto'g'ri.")


@router.message(MAIN, Adm.broadcast, F.text)
async def adm_bc(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ok = 0
    for r in q("SELECT id FROM users WHERE banned=0"):
        try:
            await bot.send_message(r["id"], m.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"📨 Yuborildi: {ok}")


@router.message(MAIN, Adm.ban, F.text)
async def adm_ban(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if m.text.strip().isdigit():
        await do_ban(int(m.text), bot)
        await m.answer("🚫 Bloklandi.")


@router.message(MAIN, Adm.unban, F.text)
async def adm_unban(m: Message, state: FSMContext):
    await state.clear()
    if m.text.strip().isdigit():
        q("UPDATE users SET banned=0 WHERE id=?", (int(m.text),), commit=True)
        await m.answer("♻️ Blokdan chiqarildi.")


# ---------------------------------------------------------------- Suhbatni uzatish (eng oxirida)
@router.message(StateFilter(None))
async def relay(m: Message, bot: Bot):
    uid = m.from_user.id
    p = partner(uid)
    if not p:
        if q("SELECT 1 FROM queue WHERE user_id=?", (uid,), one=True):
            return await m.answer("🔎 Hali qidirilmoqda...")
        return await m.answer("Menyudan tanlang 👇", reply_markup=main_menu(uid))
    if not is_vip(uid):
        if m.content_type not in ("text", "sticker", "voice"):
            return await m.answer("🔒 Tekin tarifda faqat matn, stiker va ovozli xabar yuborish mumkin. "
                                  "Rasm/video uchun 💎 VIP kerak.")
        if m.text and has_contact(m.text):
            return await m.answer("🚫 Tekin tarifda telefon raqam, nik, havola va ilova nomlarini "
                                  "yuborish taqiqlangan. 💎 VIP oling.",
                                  reply_markup=inline([("💎 VIP olish", "vip:info")], 1))
    try:
        await bot.copy_message(p, m.chat.id, m.message_id)
    except Exception:
        await end_chat(uid, bot, notify=False)
        return await m.answer("Suhbatdosh botni tark etdi. Suhbat tugatildi.", reply_markup=main_menu(uid))
    if q("SELECT 1 FROM watch WHERE user_id IN (?,?)", (uid, p), one=True):
        ua, ub = get_user(uid), get_user(p)
        try:
            await bot.send_message(ADMIN_ID, f"👁 {esc(ua['name'])} ({uid}) → {esc(ub['name'])} ({p})")
            await bot.copy_message(ADMIN_ID, m.chat.id, m.message_id)
        except Exception:
            pass


@router.message(~StateFilter(None))
async def wrong_input(m: Message):
    await m.answer("Iltimos, so'ralgan ma'lumotni yuboring yoki «❌ Bekor qilish» ni bosing.")


# ---------------------------------------------------------------- RUN
async def health(request):
    return web.Response(text="Bot ishlayapti")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
    await site.start()


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    await start_web()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(BanMiddleware())
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
