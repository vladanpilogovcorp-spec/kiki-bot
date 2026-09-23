"""
Бот «Кики» — координация выносов, ДР-поздравлений, кальянов и шоу-программы.

РОЛИ (назначаются через /start, одна главная роль на человека):
- waiter          — официант: подаёт заявки, отмечает свою готовность
- hookah          — кальянщик: получает заявки с кальяном, отмечает готовность
- hookah_chief    — начальник кальянщиков: то же, что кальянщик, плюс видит
                    всех кальянщиков и может отметить готовность за любого
- mc              — MC: объявляет готовые заявки, видит теги под ДР/треком
- admin           — админ: всё вышеперечисленное + пауза, /show, статистика,
                    назначение ролей другим людям

Меню — обычная (не инлайн) клавиатура снизу экрана, подстраивается под роль,
набирать команды вручную не нужно.

СТОЛЫ:
- VIP-группа (высший приоритет): VIP1, VIP2, VIP3, VIP4, 333, 555, 777
- Обычные: 11-25, 31-33, 41-43, 51, 52, wc1, wc2

ЗАЯВКА:
Стол → Вынос/ДР → (если ДР: текст / текст+алкоголь / текст+алкоголь+трек) →
алкоголь (если нужен) → текст поздравления → трек да/нет (Натали) →
фото или текст к заявке → кальян да/нет (если да — меню кальянов) →
отметка «чек 200+» → подтверждение → отправка.

Если в заявке есть кальян, она отдельно прилетает кальянщикам. Заявка
становится по-настоящему «готовой» только когда официант (и кальянщик, если
он есть в заявке) оба нажали «Готов».

Приоритет считается автоматически: стол из VIP-группы, отметка «чек 200+»
и ДР поднимают заявку выше в очереди.

В 06:00 бот сам присылает статистику за прошедшие сутки: сколько было
выносов/ДР/кальянов, на каком алкоголе, и кто из сотрудников за сколько
в среднем отмечал готовность.

Хранение — в памяти процесса (тест). Для продакшена нужна БД, см. README.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# КОНФИГ
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
MC_USERNAME = os.environ.get("MC_USERNAME", "MC_username")
MUSIC_USERNAME = os.environ.get("MUSIC_USERNAME", "Natali_username")
DJ_USERNAME = os.environ.get("DJ_USERNAME", "dj_username")
DANCER_USERNAMES = [u.strip() for u in os.environ.get("DANCER_USERNAMES", "dancer1,dancer2").split(",") if u.strip()]
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))

VIP_TABLES = ["VIP1", "VIP2", "VIP3", "VIP4", "333", "555", "777"]
REGULAR_TABLES = [
    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
    "21", "22", "23", "24", "25", "31", "32", "33", "41", "42", "43",
    "51", "52", "wc1", "wc2",
]
ALL_TABLES = VIP_TABLES + REGULAR_TABLES

ALCOHOL_OPTIONS = ["Азуль", "Дон Периньон", "Дон Хулио", "Белуга 6л", "Кристалл"]
HOOKAH_MENU = ["Классический", "Двойное яблоко", "Мохито", "Мультифрукт"]  # поправь под реальное меню

SHOW_SEGMENTS = ["Интро", "Трек / номер", "Поздравление ДР", "Другое"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kiki-bot")

# ---------------------------------------------------------------------------
# РОЛИ (глобально, не привязаны к конкретному чату)
# ---------------------------------------------------------------------------

ROLE_LABELS = {
    "waiter": "Официант",
    "hookah": "Кальянщик",
    "hookah_chief": "Начальник кальянщиков",
    "mc": "MC",
    "admin": "Админ",
}

user_roles: dict[int, str] = {}
# по умолчанию первый, кто напишет /start и выберет "Админ", становится админом;
# дальше админ может назначать роли другим через ADMIN_IDS или командой /setrole
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}


def get_role(user_id: int) -> Optional[str]:
    if user_id in ADMIN_IDS:
        return "admin"
    return user_roles.get(user_id)


def is_admin(user_id: int) -> bool:
    return get_role(user_id) == "admin"


# ---------------------------------------------------------------------------
# СОСТОЯНИЕ ЗАЯВОК
# ---------------------------------------------------------------------------


@dataclass
class Order:
    id: int
    table: str
    is_vip_table: bool
    order_type: str  # "vynos" | "bday"
    bday_variant: Optional[str] = None  # "text" | "alcohol" | "alcohol_track"
    alcohol: Optional[str] = None
    congrats_text: Optional[str] = None
    track: bool = False
    note_text: Optional[str] = None
    note_photo_id: Optional[str] = None
    hookah: bool = False
    hookah_item: Optional[str] = None
    big_check: bool = False

    status: str = "collecting"  # collecting -> ready -> announced -> done
    ready_waiter: bool = False
    ready_hookah: bool = False

    waiter_name: str = ""
    hookah_name: str = ""

    created_at: datetime = field(default_factory=lambda: datetime.now(TIMEZONE))
    ready_at: Optional[datetime] = None
    announced_at: Optional[datetime] = None
    done_at: Optional[datetime] = None

    def needs_hookah_confirm(self) -> bool:
        return self.hookah

    def is_fully_ready(self) -> bool:
        if self.needs_hookah_confirm():
            return self.ready_waiter and self.ready_hookah
        return self.ready_waiter

    def priority_score(self) -> tuple:
        # Меньше = выше приоритет
        vip_rank = 0 if self.is_vip_table else 1
        check_rank = 0 if self.big_check else 1
        bday_rank = 0 if self.order_type == "bday" else 1
        ref_time = self.ready_at or self.created_at
        return (vip_rank, check_rank, bday_rank, ref_time)


class ChatState:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.next_id = 1
        self.paused = False
        self.current_show: Optional[str] = None
        self.view_message_ids: dict[str, int] = {}
        self.done_archive: list[Order] = []  # для статистики за день

    def add_order(self, **kwargs) -> Order:
        order = Order(id=self.next_id, **kwargs)
        self.orders[order.id] = order
        self.next_id += 1
        return order

    def active_orders(self) -> list[Order]:
        return sorted(
            [o for o in self.orders.values() if o.status != "done"],
            key=lambda o: o.priority_score(),
        )

    def by_status(self, status: str) -> list[Order]:
        return sorted([o for o in self.orders.values() if o.status == status], key=lambda o: o.priority_score())


chat_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in chat_states:
        chat_states[chat_id] = ChatState()
    return chat_states[chat_id]


router = Router()
bot_instance: Optional[Bot] = None  # используется фоновой задачей статистики

# ---------------------------------------------------------------------------
# КЛАВИАТУРА ПОД РОЛЬ
# ---------------------------------------------------------------------------


def role_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    role = get_role(user_id)
    rows: list[list[str]] = []

    if role in ("waiter", "admin"):
        rows.append(["📋 Новая заявка"])
        rows.append(["👤 Мои заявки"])
    if role in ("hookah", "hookah_chief", "admin"):
        rows.append(["💨 Заявки на кальян"])
    if role in ("hookah_chief", "admin"):
        rows.append(["👑 Контроль кальянщиков"])
    if role in ("mc", "admin"):
        rows.append(["🎤 Экран MC"])
    if role == "admin":
        rows.append(["🧑‍💼 Менеджер", "🎬 Начать номер"])
        rows.append(["📊 Статистика", "👥 Роли сотрудников"])

    if not rows:
        rows = [["ℹ️ Кто я?"]]

    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)


def role_pick_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"pickrole:{key}")] for key, label in ROLE_LABELS.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    role = get_role(message.from_user.id)
    if role:
        await message.answer(f"Ты вошёл как: {ROLE_LABELS[role]}", reply_markup=role_keyboard(message.from_user.id))
    else:
        await message.answer("Кто ты?", reply_markup=role_pick_keyboard())


@router.callback_query(F.data.startswith("pickrole:"))
async def on_pick_role(callback: CallbackQuery) -> None:
    role = callback.data.split(":", 1)[1]
    user_roles[callback.from_user.id] = role
    await callback.message.edit_text(f"Готово, ты — {ROLE_LABELS[role]}")
    await callback.bot.send_message(
        callback.message.chat.id, "Меню обновлено 👇", reply_markup=role_keyboard(callback.from_user.id)
    )
    await callback.answer()


@router.message(F.text == "ℹ️ Кто я?")
async def cmd_whoami_btn(message: Message) -> None:
    await message.reply("Роль ещё не назначена. Напиши /start.")


# ---------------------------------------------------------------------------
# FSM МАСТЕРА ЗАЯВКИ
# ---------------------------------------------------------------------------


class NewOrder(StatesGroup):
    choosing_table = State()
    choosing_type = State()
    choosing_bday_variant = State()
    choosing_alcohol = State()
    entering_congrats = State()
    asking_track = State()
    entering_note = State()
    asking_hookah = State()
    choosing_hookah_item = State()
    asking_big_check = State()
    confirming = State()


def tables_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for t in ALL_TABLES:
        prefix = "⭐" if t in VIP_TABLES else ""
        row.append(InlineKeyboardButton(text=f"{prefix}{t}", callback_data=f"table:{t}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no"),
    ]])


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🍾 Вынос", callback_data="type:vynos"),
        InlineKeyboardButton(text="🎂 ДР", callback_data="type:bday"),
    ]])


def bday_variant_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Только поздравление", callback_data="bdayvar:text")],
        [InlineKeyboardButton(text="Поздравление + алкоголь", callback_data="bdayvar:alcohol")],
        [InlineKeyboardButton(text="Поздравление + алкоголь + трек", callback_data="bdayvar:alcohol_track")],
    ])


def alcohol_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=a, callback_data=f"alcohol:{a}")] for a in ALCOHOL_OPTIONS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hookah_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=h, callback_data=f"hookahitem:{h}")] for h in HOOKAH_MENU]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно, отправить", callback_data="confirm:send")],
        [InlineKeyboardButton(text="✏️ Начать заново", callback_data="confirm:restart")],
    ])


@router.message(F.text == "📋 Новая заявка")
async def start_new_order(message: Message, state: FSMContext) -> None:
    if get_role(message.from_user.id) not in ("waiter", "admin"):
        await message.reply("Заявки подают официанты.")
        return
    await state.clear()
    await state.set_state(NewOrder.choosing_table)
    await message.answer("Выбери стол:", reply_markup=tables_keyboard())


@router.callback_query(NewOrder.choosing_table, F.data.startswith("table:"))
async def pick_table(callback: CallbackQuery, state: FSMContext) -> None:
    table = callback.data.split(":", 1)[1]
    await state.update_data(table=table, is_vip_table=table in VIP_TABLES)
    await state.set_state(NewOrder.choosing_type)
    await callback.message.edit_text(f"Стол {table}. Вынос или ДР?", reply_markup=type_keyboard())
    await callback.answer()


@router.callback_query(NewOrder.choosing_type, F.data.startswith("type:"))
async def pick_type(callback: CallbackQuery, state: FSMContext) -> None:
    order_type = callback.data.split(":", 1)[1]
    await state.update_data(order_type=order_type)
    if order_type == "bday":
        await state.set_state(NewOrder.choosing_bday_variant)
        await callback.message.edit_text("Какой вариант поздравления?", reply_markup=bday_variant_keyboard())
    else:
        await state.set_state(NewOrder.choosing_alcohol)
        await callback.message.edit_text(
            "Это на алкоголе?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, выбрать бутылку", callback_data="wantalcohol:yes")],
                [InlineKeyboardButton(text="Нет", callback_data="wantalcohol:no")],
            ]),
        )
    await callback.answer()


@router.callback_query(NewOrder.choosing_bday_variant, F.data.startswith("bdayvar:"))
async def pick_bday_variant(callback: CallbackQuery, state: FSMContext) -> None:
    variant = callback.data.split(":", 1)[1]
    await state.update_data(bday_variant=variant, track=(variant == "alcohol_track"))
    if variant == "text":
        await state.set_state(NewOrder.entering_congrats)
        await callback.message.edit_text("Текст поздравления — напиши в чат.")
    else:
        await state.set_state(NewOrder.choosing_alcohol)
        await callback.message.edit_text("Выбери бутылку:", reply_markup=alcohol_keyboard())
    await callback.answer()


@router.callback_query(NewOrder.choosing_alcohol, F.data.startswith("wantalcohol:"))
async def pick_wants_alcohol(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.split(":", 1)[1] == "no":
        await state.update_data(alcohol=None)
        await state.set_state(NewOrder.asking_track)
        await callback.message.edit_text("Нужен трек? (тегнем Натали)", reply_markup=yes_no_keyboard("track"))
    else:
        await callback.message.edit_text("Выбери бутылку:", reply_markup=alcohol_keyboard())
    await callback.answer()


@router.callback_query(NewOrder.choosing_alcohol, F.data.startswith("alcohol:"))
async def pick_alcohol(callback: CallbackQuery, state: FSMContext) -> None:
    alcohol = callback.data.split(":", 1)[1]
    await state.update_data(alcohol=alcohol)
    data = await state.get_data()
    if data.get("order_type") == "bday":
        await state.set_state(NewOrder.entering_congrats)
        await callback.message.edit_text("Текст поздравления — напиши в чат.")
    else:
        await state.set_state(NewOrder.asking_track)
        await callback.message.edit_text("Нужен трек? (тегнем Натали)", reply_markup=yes_no_keyboard("track"))
    await callback.answer()


@router.message(NewOrder.entering_congrats)
async def enter_congrats(message: Message, state: FSMContext) -> None:
    await state.update_data(congrats_text=message.text.strip())
    data = await state.get_data()
    await try_delete(message)
    if data.get("bday_variant") == "alcohol_track":
        # трек уже подразумевается этим вариантом ДР
        await state.update_data(track=True)
        await ask_note(message, state)
    else:
        await state.set_state(NewOrder.asking_track)
        await message.answer("Нужен трек? (тегнем Натали)", reply_markup=yes_no_keyboard("track"))


@router.callback_query(NewOrder.asking_track, F.data.startswith("track:"))
async def pick_track(callback: CallbackQuery, state: FSMContext) -> None:
    track = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(track=track)
    await ask_note(callback.message, state, edit=True)
    await callback.answer()


async def ask_note(message: Message, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(NewOrder.entering_note)
    text = "Пришли фото к заявке или просто напиши текст-комментарий."
    if edit:
        await message.edit_text(text)
    else:
        await message.answer(text)


@router.message(NewOrder.entering_note, F.photo)
async def enter_note_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(note_photo_id=message.photo[-1].file_id, note_text=message.caption)
    await try_delete(message)
    await ask_hookah(message, state)


@router.message(NewOrder.entering_note)
async def enter_note_text(message: Message, state: FSMContext) -> None:
    await state.update_data(note_text=message.text.strip())
    await try_delete(message)
    await ask_hookah(message, state)


async def ask_hookah(message: Message, state: FSMContext) -> None:
    await state.set_state(NewOrder.asking_hookah)
    await message.answer("Добавка кальяном?", reply_markup=yes_no_keyboard("hookah"))


@router.callback_query(NewOrder.asking_hookah, F.data.startswith("hookah:"))
async def pick_hookah(callback: CallbackQuery, state: FSMContext) -> None:
    wants_hookah = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(hookah=wants_hookah)
    if wants_hookah:
        await state.set_state(NewOrder.choosing_hookah_item)
        await callback.message.edit_text("Выбери позицию:", reply_markup=hookah_menu_keyboard())
    else:
        await ask_big_check(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(NewOrder.choosing_hookah_item, F.data.startswith("hookahitem:"))
async def pick_hookah_item(callback: CallbackQuery, state: FSMContext) -> None:
    item = callback.data.split(":", 1)[1]
    await state.update_data(hookah_item=item)
    await ask_big_check(callback.message, state, edit=True)
    await callback.answer()


async def ask_big_check(message: Message, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(NewOrder.asking_big_check)
    text = "Чек гостя 200+?"
    kb = yes_no_keyboard("bigcheck")
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(NewOrder.asking_big_check, F.data.startswith("bigcheck:"))
async def pick_big_check(callback: CallbackQuery, state: FSMContext) -> None:
    big_check = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(big_check=big_check)
    await state.set_state(NewOrder.confirming)
    data = await state.get_data()
    summary = build_summary(data)
    await callback.message.edit_text(summary, reply_markup=confirm_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


def build_summary(data: dict) -> str:
    lines = ["<b>Проверь заявку:</b>", ""]
    lines.append(f"Стол: {data.get('table')}")
    lines.append(f"Тип: {'ДР' if data.get('order_type') == 'bday' else 'Вынос'}")
    if data.get("alcohol"):
        lines.append(f"Алкоголь: {data['alcohol']}")
    if data.get("congrats_text"):
        lines.append(f"Поздравление: {data['congrats_text']}")
    lines.append(f"Трек: {'да' if data.get('track') else 'нет'}")
    if data.get("note_text"):
        lines.append(f"Комментарий: {data['note_text']}")
    if data.get("note_photo_id"):
        lines.append("Фото: приложено")
    lines.append(f"Кальян: {data.get('hookah_item') if data.get('hookah') else 'нет'}")
    lines.append(f"Чек 200+: {'да' if data.get('big_check') else 'нет'}")
    return "\n".join(lines)


@router.callback_query(NewOrder.confirming, F.data == "confirm:restart")
async def restart_order(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewOrder.choosing_table)
    await callback.message.edit_text("Начинаем заново. Выбери стол:", reply_markup=tables_keyboard())
    await callback.answer()


@router.callback_query(NewOrder.confirming, F.data == "confirm:send")
async def send_order(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)

    order = chat_state.add_order(
        table=data["table"],
        is_vip_table=data.get("is_vip_table", False),
        order_type=data["order_type"],
        bday_variant=data.get("bday_variant"),
        alcohol=data.get("alcohol"),
        congrats_text=data.get("congrats_text"),
        track=data.get("track", False),
        note_text=data.get("note_text"),
        note_photo_id=data.get("note_photo_id"),
        hookah=data.get("hookah", False),
        hookah_item=data.get("hookah_item"),
        big_check=data.get("big_check", False),
        waiter_name=callback.from_user.full_name,
    )

    await state.clear()
    await callback.message.edit_text(f"✅ Заявка по столу {order.table} отправлена.")
    await refresh_all_views(callback.bot, chat_id)
    await callback.answer("Отправлено")


async def try_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------------------
# ЭКРАНЫ (обновляемые сообщения)
# ---------------------------------------------------------------------------


def order_summary_line(o: Order) -> str:
    kind = "🎂 ДР" if o.order_type == "bday" else "🍾 Вынос"
    mark = "⭐" if o.is_vip_table else "•"
    tags = []
    if o.big_check:
        tags.append("чек 200+")
    if o.hookah:
        tags.append(f"кальян: {o.hookah_item or '?'}")
    if o.track:
        tags.append(f"трек, cc @{MC_USERNAME} @{MUSIC_USERNAME}")
    tag_str = f" ({', '.join(tags)})" if tags else ""
    status_label = {
        "collecting": "🟡 Собирается",
        "ready": "🟢 Готово",
        "announced": "🟣 Объявлено",
    }.get(o.status, o.status)
    return f"{mark} Стол {o.table} — {kind}{tag_str} — {status_label}"


def render_waiter(chat_state: ChatState, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    orders = [o for o in chat_state.active_orders()]
    lines = ["<b>👤 Мои заявки</b>", ""]
    rows = []
    if not orders:
        lines.append("Нет активных заявок.")
    for o in orders:
        lines.append(order_summary_line(o))
        if not o.ready_waiter:
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_w:{o.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_hookah(chat_state: ChatState, chief: bool) -> tuple[str, InlineKeyboardMarkup]:
    orders = [o for o in chat_state.active_orders() if o.hookah]
    title = "👑 Контроль кальянщиков" if chief else "💨 Заявки на кальян"
    lines = [f"<b>{title}</b>", ""]
    rows = []
    if not orders:
        lines.append("Нет заявок с кальяном.")
    for o in orders:
        status = "✅ готов" if o.ready_hookah else "⏳ ждём"
        lines.append(f"Стол {o.table} — {o.hookah_item} — {status}")
        if not o.ready_hookah:
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_h:{o.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_mc(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    ready = chat_state.by_status("ready")
    waiting = [o for o in chat_state.active_orders() if o.status == "collecting"]
    lines = ["<b>🎤 MC</b>", ""]
    if chat_state.paused:
        lines.append("⏸ <b>Пауза — идёт номер</b>")
    lines.append("<b>Готовы к объявлению:</b>")
    rows = []
    if not ready:
        lines.append("Пока пусто.")
    for o in ready:
        lines.append(order_summary_line(o))
        if o.congrats_text:
            lines.append(f"    💬 «{o.congrats_text}»")
        text = "⏸ На паузе" if chat_state.paused else f"📣 Объявить — Стол {o.table}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"announce:{o.id}")])
    lines.append("")
    lines.append("<b>Ожидают готовности:</b>")
    if not waiting:
        lines.append("Пусто.")
    for o in waiting:
        lines.append(order_summary_line(o))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_manager(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    orders = chat_state.active_orders()
    lines = ["<b>🧑‍💼 Менеджер</b>", ""]
    rows = []
    if chat_state.current_show:
        lines.append(f"🎬 Идёт номер: <b>{chat_state.current_show}</b> (через «Начать номер»)")
    else:
        pause_text = "▶ Возобновить" if chat_state.paused else "⏸ Пауза"
        rows.append([InlineKeyboardButton(text=pause_text, callback_data="toggle_pause")])
        lines.append("⏸ Пауза активна" if chat_state.paused else "Очередь активна")
    lines.append("")
    lines.append("<b>Очередь:</b>")
    if not orders:
        lines.append("Пусто.")
    for o in orders:
        lines.append(order_summary_line(o))
        if o.status == "announced":
            rows.append([InlineKeyboardButton(text=f"🏁 Выполнено — Стол {o.table}", callback_data=f"done:{o.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


VIEW_RENDERERS = {
    "waiter": lambda cs, uid: render_waiter(cs, uid),
    "hookah": lambda cs, uid: render_hookah(cs, False),
    "hookah_chief": lambda cs, uid: render_hookah(cs, True),
    "mc": lambda cs, uid: render_mc(cs),
    "manager": lambda cs, uid: render_manager(cs),
}


async def show_view(bot: Bot, chat_id: int, view: str, user_id: int) -> None:
    chat_state = get_state(chat_id)
    text, kb = VIEW_RENDERERS[view](chat_state, user_id)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    chat_state.view_message_ids[f"{view}:{user_id}"] = msg.message_id


async def refresh_all_views(bot: Bot, chat_id: int) -> None:
    chat_state = get_state(chat_id)
    for key, message_id in list(chat_state.view_message_ids.items()):
        view, uid_str = key.split(":")
        uid = int(uid_str)
        text, kb = VIEW_RENDERERS[view](chat_state, uid)
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                continue
            chat_state.view_message_ids.pop(key, None)


@router.message(F.text == "👤 Мои заявки")
async def btn_waiter(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "waiter", message.from_user.id)


@router.message(F.text == "💨 Заявки на кальян")
async def btn_hookah(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "hookah", message.from_user.id)


@router.message(F.text == "👑 Контроль кальянщиков")
async def btn_hookah_chief(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "hookah_chief", message.from_user.id)


@router.message(F.text == "🎤 Экран MC")
async def btn_mc(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "mc", message.from_user.id)


@router.message(F.text == "🧑‍💼 Менеджер")
async def btn_manager(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await show_view(message.bot, message.chat.id, "manager", message.from_user.id)


# ---------------------------------------------------------------------------
# ДЕЙСТВИЯ ПО ЗАЯВКАМ
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("ready_w:"))
async def on_ready_waiter(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_waiter = True
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готов")


@router.callback_query(F.data.startswith("ready_h:"))
async def on_ready_hookah(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_hookah = True
    order.hookah_name = callback.from_user.full_name
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готов")


@router.callback_query(F.data.startswith("announce:"))
async def on_announce(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    if chat_state.paused:
        await callback.answer("Пауза — сейчас идёт номер", show_alert=True)
        return
    order.status = "announced"
    order.announced_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Объявлено")


@router.callback_query(F.data.startswith("done:"))
async def on_done(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.status = "done"
    order.done_at = datetime.now(TIMEZONE)
    chat_state.done_archive.append(order)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готово")


@router.callback_query(F.data == "toggle_pause")
async def on_toggle_pause(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    chat_state = get_state(callback.message.chat.id)
    chat_state.paused = not chat_state.paused
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Пауза" if chat_state.paused else "Возобновлено")


# ---------------------------------------------------------------------------
# ШОУ-НОМЕРА (админ)
# ---------------------------------------------------------------------------


def show_pick_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=s, callback_data=f"show_pick:{s}")] for s in SHOW_SEGMENTS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🎬 Начать номер")
async def btn_show(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    chat_state = get_state(message.chat.id)
    if chat_state.current_show:
        await message.reply(f"Сейчас уже идёт «{chat_state.current_show}».")
        return
    await message.answer("Какой номер начинается?", reply_markup=show_pick_keyboard())


@router.callback_query(F.data.startswith("show_pick:"))
async def on_show_pick(callback: CallbackQuery) -> None:
    segment = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)
    chat_state.current_show = segment
    chat_state.paused = True
    tags = " ".join([f"@{MC_USERNAME}", f"@{DJ_USERNAME}"] + [f"@{u}" for u in DANCER_USERNAMES])
    await callback.message.edit_text(
        f"🎬 <b>Идёт номер: {segment}</b>\nОчередь на паузе.\n\n{tags}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ Номер закончился", callback_data="show_end")]]),
        parse_mode=ParseMode.HTML,
    )
    try:
        await callback.bot.pin_chat_message(chat_id, callback.message.message_id)
    except TelegramBadRequest:
        pass
    await refresh_all_views(callback.bot, chat_id)
    await callback.answer("Объявлено")


@router.callback_query(F.data == "show_end")
async def on_show_end(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)
    finished = chat_state.current_show or "номер"
    chat_state.current_show = None
    chat_state.paused = False
    await callback.message.edit_text(f"✅ «{finished}» завершён. Очередь возобновлена.")
    await refresh_all_views(callback.bot, chat_id)
    await callback.answer("Возобновлено")


# ---------------------------------------------------------------------------
# СТАТИСТИКА
# ---------------------------------------------------------------------------


def build_stats_text(chat_state: ChatState) -> str:
    orders = chat_state.done_archive
    if not orders:
        return "📊 За прошедшие сутки заявок не было."

    total = len(orders)
    vynos = sum(1 for o in orders if o.order_type == "vynos")
    bday = sum(1 for o in orders if o.order_type == "bday")
    hookah = sum(1 for o in orders if o.hookah)

    alcohol_counts: dict[str, int] = {}
    for o in orders:
        if o.alcohol:
            alcohol_counts[o.alcohol] = alcohol_counts.get(o.alcohol, 0) + 1

    hookah_counts: dict[str, int] = {}
    for o in orders:
        if o.hookah_item:
            hookah_counts[o.hookah_item] = hookah_counts.get(o.hookah_item, 0) + 1

    # среднее время реакции официантов (от создания до готовности)
    waiter_times: dict[str, list[float]] = {}
    for o in orders:
        if o.waiter_name and o.ready_at:
            secs = (o.ready_at - o.created_at).total_seconds()
            waiter_times.setdefault(o.waiter_name, []).append(secs)

    lines = ["<b>📊 Статистика за сутки</b>", ""]
    lines.append(f"Всего заявок: {total} (выносы: {vynos}, ДР: {bday}, с кальяном: {hookah})")

    if alcohol_counts:
        lines.append("")
        lines.append("<b>По алкоголю:</b>")
        for name, count in sorted(alcohol_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    if hookah_counts:
        lines.append("")
        lines.append("<b>По кальянам:</b>")
        for name, count in sorted(hookah_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    if waiter_times:
        lines.append("")
        lines.append("<b>Скорость по официантам (в среднем):</b>")
        for name, times in sorted(waiter_times.items(), key=lambda x: sum(x[1]) / len(x[1])):
            avg = sum(times) / len(times)
            lines.append(f"  {name}: {avg:.0f} сек, заявок: {len(times)}")

    return "\n".join(lines)


@router.message(F.text == "📊 Статистика")
async def btn_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    chat_state = get_state(message.chat.id)
    await message.answer(build_stats_text(chat_state), parse_mode=ParseMode.HTML)


async def daily_stats_job(bot: Bot) -> None:
    """Раз в сутки в 06:00 по TIMEZONE шлёт статистику и чистит архив дня."""
    while True:
        now = datetime.now(TIMEZONE)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        for chat_id, chat_state in list(chat_states.items()):
            try:
                await bot.send_message(chat_id, build_stats_text(chat_state), parse_mode=ParseMode.HTML)
            except TelegramBadRequest:
                pass
            chat_state.done_archive = []


# ---------------------------------------------------------------------------
# РОЛИ СОТРУДНИКОВ (админ назначает)
# ---------------------------------------------------------------------------


@router.message(F.text == "👥 Роли сотрудников")
async def btn_roles(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Чтобы назначить роль сотруднику: попроси его написать боту /start, "
        "он выберет роль сам через кнопки. Если нужно назначить роль другому "
        "человеку напрямую, пришли мне его user_id и роль в формате:\n"
        "<code>/setrole USER_ID роль</code>\n"
        "Роли: waiter, hookah, hookah_chief, mc, admin",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("setrole"))
async def cmd_setrole(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or parts[2] not in ROLE_LABELS:
        await message.reply("Формат: /setrole USER_ID роль (waiter/hookah/hookah_chief/mc/admin)")
        return
    target_id = int(parts[1])
    user_roles[target_id] = parts[2]
    await message.reply(f"Готово: {target_id} → {ROLE_LABELS[parts[2]]}")


# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА
# ---------------------------------------------------------------------------


async def main() -> None:
    global bot_instance
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("Укажи токен бота: переменная окружения BOT_TOKEN.")

    bot = Bot(token=BOT_TOKEN)
    bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(daily_stats_job(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
