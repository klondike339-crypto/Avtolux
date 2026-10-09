import os, json, sqlite3, asyncio, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.getenv("ADMIN_ID", "0"))          # числовой Telegram ID владельца
WEBAPP = os.getenv("WEBAPP_URL", "").strip()
if not WEBAPP.startswith("https://"): WEBAPP = ""             # https-адрес мини-приложения (необязательно)
PORT = int(os.getenv("PORT", "8080"))
MID = json.loads(os.getenv("MASTER_IDS", "{}"))  # {"Роман": 123456789} — уведомления мастерам
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

def now(): return datetime.now(TZ).replace(tzinfo=None)
def today(): return now().date()

W = "Цены «от»: легковой / кроссовер / джип / микроавтобус"
T = "R12-15 / R16 / R17 (паркетн. от R15) / R19 (джипы от R15) / премиум. В непогоду на улице +50%"
S5 = "250 / 300 / 350 / 400 / 500"
CATS = {
 "wash": {"t": "🚿 Мойка", "dur": 40, "hours": [0, 24], "leg": W,
  "masters": ["Роман", "Борис", "Виталий", "Олег"], "items": [
   ["Техническая мойка", "300 / 350 / 400 / 800"], ["Экспресс (пена, ковры)", "450 / 550 / 650 / 1500"],
   ["Двухфазная мойка", "1000 / 1200 / 1300 / 2000"], ["Трёхфазная мойка", "1800 / 2000 / 2500 / 3000"],
   ["Комплексная мойка", "1900 / 2300 / 2700 / 4500"], ["Мойка двигателя", "1500 / 1800 / 2100 / 2500"],
   ["Уборка салона пылесосом", "400 / 500 / 600 / 800"], ["Безконтактная сушка", "800 / 1000 / 1200 / 1500"],
   ["Озонирование салона", "1000 / 1000 / 1000 / 1500"], ["Антидождь", "800 / 1000 / 1200 / 1200"],
   ["Химчистка кузова", "3000 / 4000 / 5000 / 5000"], ["Химчистка салона", "15000 / 15000 / 15000 / 15000"]]},
 "tire": {"t": "🛞 Шиномонтаж", "dur": 60, "hours": [0, 24], "leg": T, "masters": ["Роман", "Дмитрий"], "items": [
   ["Снятие/установка колеса на авто", S5], ["Снятие/установка покрышки", S5], ["Балансировка колеса", S5],
   ["Полный монтаж 1 колеса «под ключ»", "750 / 900 / 1050 / 1200 / 1500"],
   ["Полный монтаж авто «под ключ»", "3000 / 3600 / 4200 / 4800 / 6000"],
   ["Монтаж 1 колеса без снятия с авто", "500 / 600 / 700 / 700 / 1000"],
   ["Монтаж авто без снятия", "2000 / 2400 / 2800 / 3600 / 4000"],
   ["Ремонт покрышки (жгут / заплата)", "300 / от 350"], ["Вулканизация, 1 место", "от 1800"],
   ["Правка диска (сталь / литой)", "от 200 / от 400"]]},
 "service": {"t": "🔧 Автосервис", "dur": 90, "hours": [9, 21], "leg": "Стоимость определяет мастер",
  "masters": ["Павел"], "items": [["Диагностика и ремонт", "по договорённости"]]},
}
STORE = ("🛞 <b>Сезонное хранение шин</b> (6 мес., за комплект)\n"
         "R12–14 — 3600 ₽\nR15–16 — 4200 ₽\nR17–22 — 4800 ₽")

db = sqlite3.connect("autolux.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""create table if not exists b(id integer primary key, uid int, name text, cat text, svc text,
 master text, d text, t text, st text default 'ok', rem int default 0, ask int default 0);
create table if not exists r(id integer primary key, uid int, name text, stars int, txt text, ts text);""")

def times(cat):
    a, b = CATS[cat]["hours"]
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(a * 60, b * 60, 30)]

def busy(cat, master, d):
    n, out = -(-CATS[cat]["dur"] // 30), set()
    for x in db.execute("select t from b where cat=? and master=? and d=? and st='ok'", (cat, master, d)):
        h, m = map(int, x["t"].split(":"))
        for k in range(n):
            i = h * 60 + m + 30 * k
            out.add(f"{i // 60 % 24:02d}:{i % 60:02d}")
    return out

def free(cat, master, d):
    T_, bs, n = times(cat), busy(cat, master, d), -(-CATS[cat]["dur"] // 30)
    cur = now().strftime("%Y-%m-%d %H:%M")
    return [t for i, t in enumerate(T_) if i + n <= len(T_) and not bs & set(T_[i:i + n]) and f"{d} {t}" > cur]

def masters_of(cat, master): return CATS[cat]["masters"] if master == "any" else [master]
def slots(cat, master, d): return sorted({t for m in masters_of(cat, master) for t in free(cat, m, d)})

def add(uid, name, cat, svc, master, d, t):
    m = next((x for x in masters_of(cat, master) if t in free(cat, x, d)), None)
    if not m: return None
    cur = db.execute("insert into b(uid,name,cat,svc,master,d,t) values(?,?,?,?,?,?,?)", (uid, name, cat, svc, m, d, t))
    db.commit()
    return m, cur.lastrowid

async def tell(bot, uid, txt, **k):
    try: await bot.send_message(uid, txt, **k)
    except Exception: logging.warning("не удалось отправить %s", uid)

def ik(rows): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=a, callback_data=b) for a, b in row] for row in rows])
def stars(): return ik([[(f"{i}⭐", f"r:{i}") for i in range(1, 6)]])
def dm(d): return f"{d[8:]}.{d[5:7]}"

def menu():
    rows = [[KeyboardButton(text="📝 Записаться"), KeyboardButton(text="💰 Прайс")],
            [KeyboardButton(text="🛞 Хранение шин"), KeyboardButton(text="⭐ Отзыв")],
            [KeyboardButton(text="📍 Контакты"), KeyboardButton(text="📋 Мои записи")]]
    if WEBAPP: rows.insert(0, [KeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=WEBAPP))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

async def notify(bot, u, cat, svc, res, d, t):
    m, i = res
    txt = f"{CATS[cat]['t']} — {svc}\nМастер: {m}\n{dm(d)} в {t}"
    if ADMIN: await tell(bot, ADMIN, f"🆕 Запись #{i}\n{txt}\nКлиент: <a href='tg://user?id={u.id}'>{u.full_name}</a>")
    if MID.get(m): await tell(bot, MID[m], f"🆕 Новая запись к вам\n{txt}")
    await tell(bot, u.id, f"✅ Вы записаны!\n{txt}\n📍 Фадеев ручей 9/1, тел. 20-51-51")

r = Router()

class St(StatesGroup): q = State()
class Rv(StatesGroup): txt = State()

@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Добро пожаловать в <b>Автолюкс</b>! 🚗\nМойка, шиномонтаж, автосервис и хранение шин. Выберите действие в меню.", reply_markup=menu())

@r.message(Command("myid"))
async def myid(m: Message): await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")

@r.message(F.text == "📝 Записаться")
async def book(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Что вас интересует?", reply_markup=ik([[(v["t"], "c:" + k)] for k, v in CATS.items()]))

@r.callback_query(F.data.startswith("c:"))
async def cat(c: CallbackQuery, state: FSMContext):
    k = c.data[2:]
    await state.update_data(cat=k)
    await c.message.edit_text("Выберите услугу:", reply_markup=ik([[(n, f"s:{i}")] for i, (n, _) in enumerate(CATS[k]["items"])]))

@r.callback_query(F.data.startswith("s:"))
async def svc(c: CallbackQuery, state: FSMContext):
    k = (await state.get_data())["cat"]
    await state.update_data(svc=CATS[k]["items"][int(c.data[2:])][0])
    await c.message.edit_text("Выберите мастера:", reply_markup=ik([[(x, "m:" + x)] for x in CATS[k]["masters"]] + [[("Любой свободный", "m:any")]]))

@r.callback_query(F.data.startswith("m:"))
async def mas(c: CallbackQuery, state: FSMContext):
    await state.update_data(master=c.data[2:])
    t = today()
    await c.message.edit_text("Выберите дату:", reply_markup=ik(
        [[((t + timedelta(i)).strftime("%d.%m"), "d:" + (t + timedelta(i)).isoformat()) for i in range(j, j + 4)] for j in (0, 4)]))

@r.callback_query(F.data.startswith("d:"))
async def day(c: CallbackQuery, state: FSMContext):
    d, dt = await state.get_data(), c.data[2:]
    await state.update_data(d=dt)
    sl = slots(d["cat"], d["master"], dt)
    if not sl: return await c.answer("На эту дату нет свободного времени", show_alert=True)
    await c.message.edit_text(f"Время на {dm(dt)}:", reply_markup=ik([[(t, "t:" + t) for t in sl[i:i + 4]] for i in range(0, len(sl), 4)]))

@r.callback_query(F.data.startswith("t:"))
async def tm(c: CallbackQuery, state: FSMContext):
    d, t = await state.get_data(), c.data[2:]
    await state.clear()
    res = add(c.from_user.id, c.from_user.full_name, d["cat"], d["svc"], d["master"], d["d"], t)
    if not res: return await c.message.edit_text("Это время уже занято. Начните запись заново.")
    await c.message.edit_text("Готово ✅")
    await notify(c.bot, c.from_user, d["cat"], d["svc"], res, d["d"], t)

@r.message(F.web_app_data)
async def wa(m: Message):
    try:
        p = json.loads(m.web_app_data.data)
        cat, svc_, ma, d, t = p["cat"], p["svc"], p["master"], p["d"], p["t"]
        datetime.fromisoformat(d)
        assert cat in CATS and svc_ in [x[0] for x in CATS[cat]["items"]] and (ma == "any" or ma in CATS[cat]["masters"])
        res = add(m.from_user.id, m.from_user.full_name, cat, svc_, ma, d, t)
    except Exception:
        return await m.answer("Не удалось оформить запись, попробуйте ещё раз.")
    if not res: return await m.answer("Это время уже занято, выберите другое.")
    await notify(m.bot, m.from_user, cat, svc_, res, d, t)

@r.message(F.text == "💰 Прайс")
async def price(m: Message):
    await m.answer("Выберите раздел:", reply_markup=ik([[(v["t"], "p:" + k)] for k, v in CATS.items()] + [[("🛞 Хранение шин", "p:store")]]))

@r.callback_query(F.data.startswith("p:"))
async def pr(c: CallbackQuery):
    k = c.data[2:]
    if k == "store": txt = STORE
    else:
        v = CATS[k]
        txt = f"<b>{v['t']}</b>\n<i>{v['leg']}</i>\n\n" + "\n".join(f"• {n} — {p}" for n, p in v["items"])
    await c.message.answer(txt)
    await c.answer()

@r.message(F.text == "🛞 Хранение шин")
async def st(m: Message, state: FSMContext):
    await state.set_state(St.q)
    await m.answer(STORE + "\n\nНапишите одним сообщением марку авто, размер шин и удобную дату — передам администратору.")

@r.message(F.text == "⭐ Отзыв")
async def rv(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Оцените наш сервис:", reply_markup=stars())

@r.message(F.text == "📍 Контакты")
async def ct(m: Message):
    await m.answer("📍 Фадеев ручей 9/1\n📞 20-51-51\n🚿 Мойка и 🛞 шиномонтаж — круглосуточно\n🔧 Автосервис — 09:00–21:00")

@r.message(F.text == "📋 Мои записи")
async def mine(m: Message):
    rows = db.execute("select * from b where uid=? and st='ok' and d>=? order by d,t", (m.from_user.id, today().isoformat())).fetchall()
    if not rows: return await m.answer("Активных записей нет.")
    for x in rows:
        await m.answer(f"{CATS[x['cat']]['t']} — {x['svc']}\n{x['master']}, {dm(x['d'])} в {x['t']}", reply_markup=ik([[("❌ Отменить", f"x:{x['id']}")]]))

@r.callback_query(F.data.startswith("x:"))
async def cx(c: CallbackQuery):
    i = int(c.data[2:])
    x = db.execute("select * from b where id=?", (i,)).fetchone()
    if not x or c.from_user.id not in (x["uid"], ADMIN): return await c.answer("Нет доступа", show_alert=True)
    db.execute("update b set st='cancel' where id=?", (i,))
    db.commit()
    await c.message.edit_text(f"Запись #{i} отменена.")
    await tell(c.bot, ADMIN if c.from_user.id != ADMIN else x["uid"], f"❌ Запись #{i} ({dm(x['d'])} {x['t']}) отменена.")

@r.callback_query(F.data.startswith("r:"))
async def rs(c: CallbackQuery, state: FSMContext):
    await state.set_state(Rv.txt)
    await state.update_data(stars=int(c.data[2:]))
    await c.message.edit_text("Спасибо! Напишите комментарий или отправьте /skip")

@r.message(Rv.txt)
async def rt(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    txt = "" if m.text == "/skip" else (m.text or "")[:500]
    db.execute("insert into r(uid,name,stars,txt,ts) values(?,?,?,?,?)", (m.from_user.id, m.from_user.full_name, d["stars"], txt, now().isoformat()))
    db.commit()
    await tell(m.bot, ADMIN, f"⭐ Новый отзыв {d['stars']}/5 от {m.from_user.full_name}\n{txt}")
    await m.answer("Благодарим за отзыв! 🙏")

@r.message(St.q)
async def st2(m: Message, state: FSMContext):
    await state.clear()
    await tell(m.bot, ADMIN, f"🛞 Заявка на хранение от <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a>:\n{m.text or '—'}")
    await m.answer("Заявка принята, с вами свяжутся ✅")

# ---- админ-панель: доступна только по числовому ID владельца ----
@r.message(Command("admin"), F.from_user.id == ADMIN)
async def adm(m: Message):
    await m.answer("🔐 Админ-панель", reply_markup=ik([[("Сегодня", "a:0"), ("Завтра", "a:1")], [("Отзывы", "a:rv")]]))

@r.callback_query(F.data.startswith("a:"), F.from_user.id == ADMIN)
async def adc(c: CallbackQuery):
    k = c.data[2:]
    if k == "rv":
        for x in db.execute("select * from r order by id desc limit 10"):
            await c.message.answer(f"{x['stars']}/5 — {x['name']}\n{x['txt']}")
    else:
        d = (today() + timedelta(int(k))).isoformat()
        rows = db.execute("select * from b where d=? and st='ok' order by t", (d,)).fetchall()
        await c.message.answer(f"Записей на {dm(d)}: {len(rows)}")
        for x in rows:
            await c.message.answer(f"#{x['id']} {x['t']} {CATS[x['cat']]['t']} {x['svc']}\n{x['master']} · <a href='tg://user?id={x['uid']}'>{x['name']}</a>",
                                   reply_markup=ik([[("❌ Отменить", f"x:{x['id']}")]]))
    await c.answer()

# ---- напоминания за час и просьба об отзыве через час после визита ----
async def loop(bot):
    while True:
        await asyncio.sleep(60)
        try:
            n = now()
            for x in db.execute("select * from b where st='ok'").fetchall():
                at = datetime.fromisoformat(f"{x['d']}T{x['t']}")
                if not x["rem"] and timedelta(0) < at - n <= timedelta(hours=1):
                    await tell(bot, x["uid"], f"⏰ Напоминание: сегодня в {x['t']} — {x['svc']}, мастер {x['master']}.\n📍 Фадеев ручей 9/1")
                    db.execute("update b set rem=1 where id=?", (x["id"],))
                if not x["ask"] and n - at >= timedelta(hours=1):
                    await tell(bot, x["uid"], "Как вам визит в Автолюкс? Оцените, пожалуйста:", reply_markup=stars())
                    db.execute("update b set ask=1 where id=?", (x["id"],))
            db.commit()
        except Exception:
            logging.exception("loop")

# ---- сервер мини-приложения ----
async def page(_): return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp.html"))
async def cfg(_): return web.json_response(CATS)
async def sl(req):
    q = req.query
    k = q.get("cat")
    if k not in CATS or q.get("master", "any") not in CATS[k]["masters"] + ["any"]: return web.json_response([])
    return web.json_response(slots(k, q.get("master", "any"), q.get("d", "")))

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/api/config", cfg), web.get("/api/slots", sl)])
    run = web.AppRunner(app)
    await run.setup()
    await web.TCPSite(run, "0.0.0.0", PORT).start()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(r)
    asyncio.create_task(loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
